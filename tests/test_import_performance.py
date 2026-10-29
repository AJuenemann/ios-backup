import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from ios_photos.library.db import Library, db_path
from ios_photos.library import importer
from ios_photos.media.scanner import scan_tree


@pytest.mark.parametrize("fat", [False, True])
def test_import_uses_wal_only_for_local_fat_and_keeps_each_commit(tmp_path, monkeypatch, fat):
    source = tmp_path / "source"
    source.mkdir()
    for i in range(4):
        (source / f"IMG_{i:04}.JPG").write_bytes(bytes([i]) * 1024)
    monkeypatch.setattr(importer, "_is_fat", lambda root: fat)
    lib = Library(tmp_path / "library")
    try:
        statements = []
        lib.db.set_trace_callback(statements.append)
        result = importer.run_import(lib, scan_tree(str(source)))
        lib.db.set_trace_callback(None)
        assert result.imported == 4 and not result.fatal and not result.failed
        assert lib.db.execute("PRAGMA journal_mode").fetchone()[0] == ("wal" if fat else "delete")
        assert lib.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        # Importanfang, jedes einzelne Objekt und Importabschluss bleiben gesichert.
        assert statements.count("COMMIT") == 6
        for asset in lib.query():
            for f in asset.files:
                assert Path(f.path).read_bytes() == (source / f.name).read_bytes()
        again = importer.run_import(lib, scan_tree(str(source)))
        assert again.imported == 0 and again.skipped == 4
    finally:
        lib.close()


def test_wal_setup_failure_stops_before_copying(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.JPG").write_bytes(b"photo")
    monkeypatch.setattr(importer, "_is_fat", lambda root: True)
    lib = Library(tmp_path / "library")
    try:
        def fail():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(lib, "enable_wal", fail)
        result = importer.run_import(lib, scan_tree(str(source)))
        assert result.fatal and "database is locked" in result.fatal
        assert result.imported == 0 and not lib.imports()
        assert not list(lib.root.rglob("*.JPG"))
    finally:
        lib.close()


def test_wal_preserves_committed_import_after_process_crash_and_relocates(tmp_path, monkeypatch):
    from ios_photos.library import db
    from ios_photos.library.relocate import relocate

    source, root = tmp_path / "source", tmp_path / "library"
    source.mkdir()
    for name in ("IMG_0001.JPG", "IMG_0001.MOV", "IMG_0001.AAE", "IMG_0002.JPG"):
        (source / name).write_bytes((name.encode() + b"\0") * 100)
    script = """
import os, sys
from ios_photos.library.db import Library
from ios_photos.library import importer
from ios_photos.media.scanner import scan_tree
lib = Library(sys.argv[2])
importer._is_fat = lambda root: True
result = importer.run_import(lib, scan_tree(sys.argv[1]))
assert result.imported == 2 and not result.failed and not result.fatal
lib.db.execute("INSERT INTO albums (name) VALUES ('uncommitted')")
os._exit(23)
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    process = subprocess.run(
        [sys.executable, "-c", script, str(source), str(root)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 23, process.stderr
    assert Path(str(db_path(root)) + "-wal").exists()
    lib = Library(root)
    try:
        assert lib.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert lib.count() == 2 and not lib.albums()
        assert lib.imports()[0]["count"] == 2
        for asset in lib.query():
            for f in asset.files:
                assert hashlib.sha256(Path(f.path).read_bytes()).digest() == hashlib.sha256(
                    (source / f.name).read_bytes()
                ).digest()
        again = importer.run_import(lib, scan_tree(str(source)))
        assert again.imported == 0 and again.skipped == 2
    finally:
        lib.close()
    target = tmp_path / "moved"
    relocate(str(root), str(target))
    monkeypatch.setattr(db, "_is_fat", lambda path: False)
    moved = Library(target)
    try:
        assert moved.count() == 2
        assert moved.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert moved.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert all(Path(f.path).exists() for a in moved.query() for f in a.files)
    finally:
        moved.close()


def test_crash_after_single_object_commit_resumes_without_duplicate(tmp_path):
    source, root = tmp_path / "source", tmp_path / "library"
    source.mkdir()
    for i in range(3):
        (source / f"IMG_{i:04}.JPG").write_bytes(bytes([i]) * 4096)
    script = """
import os, sys
from ios_photos.library.db import Library
from ios_photos.library import importer
from ios_photos.media.scanner import scan_tree
lib = Library(sys.argv[2])
importer._is_fat = lambda root: True
commit = lib.commit
def crash_after_commit():
    commit()
    os._exit(24)
lib.commit = crash_after_commit
importer.run_import(lib, scan_tree(sys.argv[1]))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(source), str(root)],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
        capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 24, process.stderr
    lib = Library(root)
    try:
        assert lib.count() == 1
        result = importer.run_import(lib, scan_tree(str(source)))
        assert result.imported == 2 and result.skipped == 1 and not result.failed
        assert lib.count() == 3
        assert len(list(root.rglob("*.JPG"))) == 3
        assert lib.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        lib.close()
