import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_file, make_jpeg

from ios_photos.library import legacy
from ios_photos.library.db import Library
from ios_photos.library.importer import run_import
from ios_photos.library.relocate import RelocateError, relocate
from ios_photos.media.metadata import apply_metadata
from ios_photos.media.scanner import Asset, build_assets, scan_dcim, scan_tree, SourceFile
from ios_photos.media.thumbnails import get_thumbnail


def _sf(name, ftype, size=10):
    return SourceFile(f"/x/{name}", f"100APPLE/{name}", name, size, 0, ftype)


def test_grouping():
    files = [
        _sf("IMG_0001.HEIC", "photo"), _sf("IMG_0001.MOV", "video"),
        _sf("IMG_0002.JPG", "photo"), _sf("IMG_0002.AAE", "sidecar"),
        _sf("IMG_E0002.JPG", "photo"), _sf("IMG_O0002.AAE", "sidecar"),
        _sf("IMG_0003.MOV", "video"),
        _sf("DSC00071.JPG", "photo"), _sf("DSC00071.ARW", "raw"),
        _sf("IMG_0004.PNG", "photo"),
        _sf("IMG_0005.AAE", "sidecar"),
    ]
    assets = {a.key: a for a in build_assets(files)}
    assert set(assets) == {f"100APPLE/{k}" for k in
                           ("IMG_0001", "IMG_0002", "IMG_0003", "DSC00071", "IMG_0004")}
    live = assets["100APPLE/IMG_0001"]
    assert live.kind == "live" and live.live_video.name == "IMG_0001.MOV"
    assert live.primary.name == "IMG_0001.HEIC"
    edited = assets["100APPLE/IMG_0002"]
    assert edited.kind == "photo" and edited.edited and len(edited.files) == 4
    assert edited.primary.name == "IMG_0002.JPG" and edited.display.name == "IMG_E0002.JPG"
    assert assets["100APPLE/IMG_0003"].kind == "video"
    assert assets["100APPLE/DSC00071"].raw and assets["100APPLE/DSC00071"].primary.name == "DSC00071.JPG"
    assert assets["100APPLE/IMG_0004"].screenshot


def test_scan_and_metadata(phone):
    assets = scan_dcim(str(phone))
    apply_metadata(assets)
    by = {a.primary.name: a for a in assets}
    assert by["IMG_0001.JPG"].kind == "live"
    assert by["IMG_0001.JPG"].date == datetime(2024, 5, 1, 10, 0, 0)
    assert by["IMG_0003.MOV"].kind == "video"
    assert scan_dcim(str(phone / "nope")) is None


def _import(library, phone, **kw):
    assets = scan_dcim(str(phone))
    apply_metadata(assets)
    return assets, run_import(library, assets, udid="u1", device_name="Test", **kw)


def test_import_and_dedupe(phone, library):
    assets, res = _import(library, phone)
    assert res.imported == 4 and not res.failed and not res.cancelled
    root = library.root
    assert (root / "2024/05/01/IMG_0001.JPG").exists()
    assert (root / "2024/05/01/IMG_0001.MOV").exists()
    assert (root / "2024/05/02/IMG_0002.AAE").exists()
    assert (root / "2023/12/24/IMG_0100.JPG").exists()
    assert not list(root.rglob("*.part"))
    assert library.count() == 4
    assert library.imports()[0]["count"] == 4
    # zweiter Durchlauf: alles bereits importiert
    assets2 = library.mark_status(scan_dcim(str(phone)))
    assert all(a.status == "imported" for a in assets2)
    _, res2 = _import(library, phone)
    assert res2.imported == 0 and res2.skipped == 4 and res2.import_id is None


def test_collision_keeps_pairs_together(phone, library):
    make_file(str(library.root / "2024/05/01/img_0001.jpg"), b"other")
    _, res = _import(library, phone)
    assert res.imported == 4
    assert (library.root / "2024/05/01/IMG_0001_1.JPG").exists()
    assert (library.root / "2024/05/01/IMG_0001_1.MOV").exists()


def test_album_and_partial(phone, library):
    album = library.create_album("Urlaub")
    _, res = _import(library, phone, album_id=album)
    assert library.albums()[0]["count"] == 4
    assert len(library.query(album_id=album)) == 4
    # später auf dem iPhone bearbeitet → IMG_E0002.JPG kommt dazu
    make_jpeg(str(phone / "DCIM/100APPLE/IMG_E0002.JPG"), datetime(2024, 5, 2, 11, 0, 0))
    assets = library.mark_status(scan_dcim(str(phone)))
    new = [a for a in assets if a.status == "new"]
    assert len(new) == 1 and new[0].extra.get("partial_of")
    res = run_import(library, assets)
    assert res.imported == 1
    assert (library.root / "2024/05/02/IMG_E0002.JPG").exists()
    assert library.count() == 4


def test_cancel(phone, library):
    ev = threading.Event()
    ev.set()
    _, res = _import(library, phone, cancel=ev)
    assert res.cancelled and res.imported == 0
    assert library.count() == 0
    assert not [p for p in library.root.rglob("*") if p.is_file() and ".ios-photos" not in str(p)]


def test_copy_waits_for_source_access_and_restarts_partial_file(tmp_path, monkeypatch):
    import builtins
    import errno
    from ios_photos.library import importer

    src, dst = tmp_path / "source", tmp_path / "target"
    src.write_bytes(b"abcdef")
    original_open = builtins.open
    attempts, waiting, progress = [], [], []

    class InterruptedRead:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            if not attempts:
                attempts.append("partial")
                return b"abc"
            raise PermissionError(errno.EACCES, "Device locked")

    def open_source(path, mode):
        if path == str(src) and not attempts:
            return InterruptedRead()
        return original_open(path, mode)

    monkeypatch.setattr(importer, "open", open_source, raising=False)
    cancel = threading.Event()
    monkeypatch.setattr(cancel, "wait", lambda seconds: False)
    importer.copy_file(str(src), str(dst), cancel, progress.append,
                       source_waiting=waiting.append)
    assert waiting == ["source"]
    assert progress == [3, -3, 6]
    assert dst.read_bytes() == b"abcdef"
    assert not (tmp_path / "target.part").exists()


@pytest.mark.parametrize("source_error", [True, False])
def test_copy_wait_can_be_cancelled_but_target_errors_do_not_retry(
        tmp_path, monkeypatch, source_error):
    import builtins
    import errno
    from ios_photos.library import importer

    src, dst = tmp_path / "source", tmp_path / "target"
    src.write_bytes(b"original")
    original_open = builtins.open
    cancel, waiting = threading.Event(), []

    def blocked(path, mode):
        if path == str(src) and source_error:
            raise PermissionError(errno.EACCES, "Device locked")
        if path == str(dst) + ".part" and not source_error:
            raise OSError(errno.ENOSPC, "Destination full")
        return original_open(path, mode)

    def on_wait(name):
        waiting.append(name)
        cancel.set()

    monkeypatch.setattr(importer, "open", blocked, raising=False)
    expected = importer.ImportCancelled if source_error else OSError
    with pytest.raises(expected):
        importer.copy_file(str(src), str(dst), cancel, lambda n: None,
                           source_waiting=on_wait)
    assert waiting == (["source"] if source_error else [])
    assert not dst.exists() and not (tmp_path / "target.part").exists()


def test_legacy_index_and_reorganize(tmp_path):
    root = tmp_path / "backup"
    make_jpeg(str(root / "100APPLE/IMG_0001.JPG"), datetime(2022, 1, 2, 3, 4, 5))
    make_file(str(root / "100APPLE/IMG_0001.AAE"), b"<plist/>")
    make_jpeg(str(root / "101APPLE/IMG_0001.JPG"), datetime(2022, 1, 2, 9, 0, 0), color=(1, 2, 3),
              size=(80, 60))
    make_file(str(root / ".MISC/Info.plist"))
    make_file(str(root / "ispDump.bin"))
    lib = Library(root)
    assert legacy.index_library(lib) == 2
    assert legacy.index_library(lib) == 0
    plan = legacy.plan_reorganize(lib)
    assert plan.assets == 2 and plan.files == 3
    ok, errors = legacy.execute_reorganize(lib, plan)
    assert ok == 3 and not errors
    names = sorted(os.listdir(root / "2022/01/02"))
    assert names == ["IMG_0001.AAE", "IMG_0001.JPG", "IMG_0001_1.JPG"]
    assert not (root / "100APPLE").exists() and (root / ".MISC/Info.plist").exists()
    assert legacy.plan_reorganize(lib).files == 0
    assert lib.remove_missing() == 0
    lib.close()


def test_relocate(phone, library, tmp_path):
    _import(library, phone)
    src = str(library.root)
    library.close()
    target = str(tmp_path / "elsewhere" / "Mediathek")
    try:
        relocate(src, os.path.join(src, "sub"))
    except RelocateError:
        pass
    else:
        raise AssertionError("Ziel innerhalb der Mediathek muss abgelehnt werden")
    relocate(src, target, delete_source=False)
    lib2 = Library(target)
    assert lib2.count() == 4 and lib2.remove_missing() == 0
    assert os.path.exists(os.path.join(target, "2024/05/01/IMG_0001.MOV"))
    lib2.close()


def test_thumbnails(phone, tmp_path):
    from PIL import Image
    import pillow_heif
    heic = tmp_path / "x.heic"
    Image.new("RGB", (400, 300), (10, 20, 30)).save(heic, format="HEIF")
    assert pillow_heif
    for p in (phone / "DCIM/100APPLE/IMG_0001.JPG", phone / "DCIM/100APPLE/IMG_0003.MOV", heic):
        out = get_thumbnail(str(tmp_path / "cache"), str(p), size=100)
        assert Image.open(out).size[0] <= 100


def test_scan_tree_skips_hidden(tmp_path):
    make_jpeg(str(tmp_path / "a/IMG_1.JPG"))
    make_jpeg(str(tmp_path / ".hidden/IMG_2.JPG"))
    assert [a.primary.name for a in scan_tree(str(tmp_path))] == ["IMG_1.JPG"]


def test_hidden_assets_filtered_and_unhide(phone, library):
    assets = scan_dcim(str(phone))
    apply_metadata(assets)
    hidden = next(a for a in assets if a.primary.name == "IMG_0002.JPG")
    hidden.extra["hidden"] = True
    res = run_import(library, assets, udid="u1", device_name="Test")
    assert res.imported == 4
    visible = library.query()
    hidden_assets = library.query(hidden=True)
    assert len(visible) == 3
    assert len(hidden_assets) == 1
    counts = library.counts()
    assert counts["all"] == 3 and counts["hidden"] == 1
    library.set_hidden([hidden_assets[0].extra["id"]], False)
    assert len(library.query()) == 4
    assert library.counts()["hidden"] == 0


def test_library_migrates_hidden_column(tmp_path):
    root = tmp_path / "lib"
    db_dir = root / ".ios-photos"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(str(db_dir / "library.db"))
    con.execute(
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, raw INTEGER DEFAULT 0, "
        "screenshot INTEGER DEFAULT 0, edited INTEGER DEFAULT 0, taken_at TEXT, duration REAL, "
        "width INTEGER, height INTEGER, imported_at TEXT, import_id INTEGER, legacy INTEGER DEFAULT 0, "
        "source_udid TEXT)"
    )
    con.execute(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, rel TEXT NOT NULL UNIQUE, "
        "name_key TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL, ftype TEXT NOT NULL, variant TEXT DEFAULT '')"
    )
    con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, created_at TEXT)")
    con.execute("CREATE TABLE album_assets (album_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, PRIMARY KEY (album_id, asset_id))")
    con.execute("CREATE TABLE imports (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, device_name TEXT, udid TEXT, album_id INTEGER, count INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    con.close()
    lib = Library(root)
    cols = {r[1] for r in lib.db.execute("PRAGMA table_info(assets)")}
    assert "hidden" in cols
    lib.close()


@pytest.fixture
def mock_trash(monkeypatch, library, tmp_path):
    from gi.repository import Gio

    destination = tmp_path / "trash"
    destination.mkdir()
    monkeypatch.setattr(library, "_trash_folder", lambda: destination)

    def install(callback):
        monkeypatch.setattr(Gio.File, "new_for_path",
                            lambda path: SimpleNamespace(trash=lambda cancel: callback(path)))
        monkeypatch.setattr(Path, "rename", lambda path, target: callback(str(path)))

    return install


def test_trash_removes_all_asset_files_and_album_membership(phone, library, mock_trash):
    album = library.create_album("Test")
    _import(library, phone, album_id=album)
    assets = library.query()
    live = next(a for a in assets if a.kind == "live")
    edited = next(a for a in assets if a.edited)
    selected = [live, edited]
    paths = {f.path for a in selected for f in a.files}
    moved = []

    def trash(path):
        assert path in paths
        moved.append(path)
        os.unlink(path)
        return True

    mock_trash(trash)
    library.fingerprints()
    completed, errors = library.trash_assets([a.extra["id"] for a in selected])
    assert completed == 2 and not errors
    assert set(moved) == paths
    assert library.count() == 2
    assert library.albums()[0]["count"] == 2
    assert len(library.fingerprints()) == sum(len(a.files) for a in library.query())
    assert all(os.path.isfile(f.path) for a in library.query() for f in a.files)
    source = library.mark_status(scan_dcim(str(phone)))
    assert all(a.status == "new" for a in source if a.primary.name in {x.primary.name for x in selected})


@pytest.mark.parametrize("partial", [False, True])
def test_trash_failure_keeps_remaining_files_and_index(phone, library, mock_trash, partial):
    from gi.repository import GLib

    _import(library, phone)
    live = next(a for a in library.query() if a.kind == "live")
    rows = library.db.execute("SELECT rel FROM files WHERE asset_id=?",
                              (live.extra["id"],)).fetchall()
    first = library.abspath(rows[0][0])

    def trash(path):
        if partial and path == first:
            os.unlink(path)
            return True
        raise GLib.Error("Papierkorb nicht verfügbar")

    mock_trash(trash)
    completed, errors = library.trash_assets([live.extra["id"]])
    assert completed == 0 and len(errors) == (1 if partial else 2)
    assert library.count() == 4
    remaining = next(a for a in library.query() if a.extra["id"] == live.extra["id"])
    assert len(remaining.files) == (1 if partial else 2)
    assert all(os.path.isfile(f.path) for f in remaining.files)


def test_trash_rejects_external_path(phone, library, tmp_path, monkeypatch):
    from gi.repository import Gio

    _import(library, phone)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"keep")
    asset = library.query()[0]
    library.db.execute("UPDATE files SET rel=? WHERE asset_id=?",
                       (str(outside), asset.extra["id"]))
    library.db.commit()

    def unexpected(path):
        pytest.fail("External file must not reach trash")

    monkeypatch.setattr(Gio.File, "new_for_path", unexpected)
    completed, errors = library.trash_assets([asset.extra["id"]])
    assert completed == 0 and errors
    assert outside.read_bytes() == b"keep"


def test_trash_reports_progress_and_allows_reads_during_file_io(phone, library, mock_trash):
    _import(library, phone)
    assets = library.query()
    total = sum(len(a.files) for a in assets)
    updates = []
    read_finished = threading.Event()
    reader = None

    def trash(path):
        nonlocal reader
        if reader is None:
            reader = threading.Thread(target=lambda: (library.count(), read_finished.set()))
            reader.start()
            assert read_finished.wait(2), "File I/O must not hold the library lock"
        os.unlink(path)
        return True

    mock_trash(trash)
    completed, errors = library.trash_assets(
        [a.extra["id"] for a in assets],
        progress=lambda done, count, name: updates.append((done, count, name)),
    )
    reader.join(timeout=2)
    assert completed == len(assets) and not errors
    assert updates[0][0] == 0 and updates[-1][0] == total
    assert all(count == total and name for _, count, name in updates)
    assert [done for done, _, _ in updates] == sorted(done for done, _, _ in updates)
    assert library.count() == 0


def test_trash_cancel_finishes_current_asset(phone, library, mock_trash):
    _import(library, phone)
    live = next(a for a in library.query() if a.kind == "live")
    cancel = threading.Event()
    moved = []
    updates = []

    def trash(path):
        moved.append(path)
        os.unlink(path)
        cancel.set()
        return True

    mock_trash(trash)
    completed, errors = library.trash_assets(
        [live.extra["id"]], cancel=cancel,
        progress=lambda *args: updates.append(args),
    )
    assert completed == 0 and not errors
    assert len(moved) == 2 and updates[-1][0:2] == (2, 2)
    assert not [a for a in library.query() if a.extra["id"] == live.extra["id"]]


def test_trash_batches_sqlite_commits(phone, library, mock_trash):
    from ios_photos.library.db import TRASH_DB_BATCH

    ids = []
    paths = []
    for i in range(TRASH_DB_BATCH * 2 + 7):
        path = library.root / f"2024/01/01/IMG_{i:04}.JPG"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        f = SourceFile(str(path), path.relative_to(library.root).as_posix(),
                       path.name, 1, 0, "photo")
        ids.append(library.add_asset(
            Asset(str(i), [f]), {f: f.rel}, commit=False,
        ))
        paths.append(path)
    library.commit()
    commits = []
    library.db.set_trace_callback(
        lambda statement: commits.append(statement) if statement == "COMMIT" else None
    )

    def trash(path):
        os.unlink(path)
        return True

    mock_trash(trash)
    completed, errors = library.trash_assets(ids)
    library.db.set_trace_callback(None)
    assert completed == len(ids) and not errors
    assert len(commits) == 3
    assert not any(path.exists() for path in paths)
    assert library.count() == 0


def test_grouped_trash_preserves_files_and_structure(phone, library, tmp_path, monkeypatch):
    _import(library, phone)
    assets = library.query()
    contents = {f.rel: Path(f.path).read_bytes() for a in assets for f in a.files}
    destination = tmp_path / "system-trash" / "batch"
    destination.mkdir(parents=True)
    calls = []

    def folder():
        calls.append(True)
        return destination

    monkeypatch.setattr(library, "_trash_folder", folder)
    completed, errors = library.trash_assets([a.extra["id"] for a in assets])
    assert completed == len(assets) and not errors
    assert calls == [True]
    assert library.count() == 0
    assert {p.relative_to(destination).as_posix(): p.read_bytes()
            for p in destination.rglob("*") if p.is_file()} == contents
    assert all(not (library.root / rel).exists() for rel in contents)


def test_grouped_trash_setup_failure_keeps_all_originals(phone, library, monkeypatch):
    _import(library, phone)
    assets = library.query()
    calls = []

    def unavailable():
        calls.append(True)
        raise OSError("Papierkorb nicht erreichbar")

    monkeypatch.setattr(library, "_trash_folder", unavailable)
    completed, errors = library.trash_assets([a.extra["id"] for a in assets])
    assert completed == 0 and errors == [("Papierkorb", "Papierkorb nicht erreichbar")]
    assert calls == [True]
    assert library.count() == len(assets)
    assert all(Path(f.path).is_file() for a in assets for f in a.files)


def test_trash_cancelled_before_start_does_not_create_folder(phone, library, monkeypatch):
    _import(library, phone)
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(library, "_trash_folder", lambda: pytest.fail("Already cancelled"))
    assert library.trash_assets([a.extra["id"] for a in library.query()],
                                cancel=cancel) == (0, [])


@pytest.mark.parametrize("reachable", [True, False])
def test_trash_folder_uses_local_gio_destination(library, tmp_path, monkeypatch, reachable):
    from gi.repository import Gio

    destination = tmp_path / "system-trash"
    origin = []
    closed = []
    new_for_uri = Gio.File.new_for_uri

    class Entries(list):
        def close(self, cancel):
            closed.append(True)

    def trash(path):
        origin.append(path)
        Path(path).rename(destination)
        return True

    entry = SimpleNamespace(
        get_attribute_byte_string=lambda name: origin[0],
        get_attribute_string=lambda name: destination.as_uri() if reachable else None,
    )
    monkeypatch.setattr(
        Gio.File, "new_for_path",
        lambda path: SimpleNamespace(trash=lambda cancel: trash(path)),
    )
    monkeypatch.setattr(
        Gio.File, "new_for_uri",
        lambda uri: (SimpleNamespace(enumerate_children=lambda *args: Entries([entry]))
                     if uri == "trash:///" else new_for_uri(uri)),
    )
    if reachable:
        assert library._trash_folder() == destination
    else:
        with pytest.raises(OSError, match="nicht lokal erreichbar"):
            library._trash_folder()
    assert closed == [True]
    assert not Path(origin[0]).exists()
    assert destination.is_dir()
