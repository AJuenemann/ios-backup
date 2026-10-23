"""Mediathek an einen anderen Ort umziehen."""

import os
import shutil

from .db import DB_DIR
from .importer import ImportCancelled, copy_file


class RelocateError(Exception):
    pass


def check_target(src_root, dst_root):
    src = os.path.realpath(src_root)
    dst = os.path.realpath(dst_root)
    if dst == src or dst.startswith(src.rstrip("/") + "/"):
        raise RelocateError("Das Ziel darf nicht innerhalb der aktuellen Mediathek liegen.")
    if src.startswith(dst.rstrip("/") + "/"):
        raise RelocateError("Das Ziel darf kein übergeordneter Ordner der Mediathek sein.")
    if os.path.exists(dst) and os.listdir(dst):
        raise RelocateError("Der Zielordner muss leer sein.")


def _walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            yield path, os.path.relpath(path, root)


def relocate(library_root, new_root, delete_source=False, progress=None, cancel=None):
    """Kopiert die komplette Mediathek (inkl. Index) nach `new_root`, prüft und löscht optional das Original.

    Gleiches Laufwerk + Ziel existiert noch nicht → schnelles Umbenennen.
    progress(done_bytes, total_bytes, name). Gibt den neuen Pfad zurück.
    """
    import threading

    cancel = cancel or threading.Event()
    check_target(library_root, new_root)
    src_dev = os.stat(library_root).st_dev
    parent = os.path.dirname(os.path.abspath(new_root)) or "/"
    if delete_source and not os.path.exists(new_root) and os.stat(parent).st_dev == src_dev:
        os.rename(library_root, new_root)
        return new_root

    files = [(p, r, os.path.getsize(p)) for p, r in _walk(library_root)]
    total = sum(s for _, _, s in files)
    os.makedirs(new_root, exist_ok=True)
    if shutil.disk_usage(new_root).free < total:
        raise RelocateError(f"Nicht genug Speicherplatz am Ziel ({total / 1e9:.1f} GB benötigt).")
    done = 0

    def on_bytes(n):
        nonlocal done
        done += n
        if progress:
            progress(done, total, current)

    current = ""
    try:
        for path, rel, _size in files:
            current = rel
            dst = os.path.join(new_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            copy_file(path, dst, cancel, on_bytes, os.stat(path).st_mtime)
        for path, rel, size in files:
            if os.path.getsize(os.path.join(new_root, rel)) != size:
                raise RelocateError(f"Überprüfung fehlgeschlagen: {rel}")
        if not os.path.exists(os.path.join(new_root, DB_DIR)):
            raise RelocateError("Index wurde nicht mitkopiert.")
    except BaseException:
        shutil.rmtree(new_root, ignore_errors=True)
        raise
    if delete_source:
        shutil.rmtree(library_root, ignore_errors=True)
    return new_root
