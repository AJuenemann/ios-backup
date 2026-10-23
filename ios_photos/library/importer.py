"""Kopiert Objekte vom iPhone sicher in die Mediathek (JJJJ/MM/TT/Originalname)."""

import errno
import os
import shutil
import threading
import time
from dataclasses import dataclass, field

CHUNK = 4 * 1024 * 1024
FAT_LIMIT = 4 * 1024 ** 3 - 1


class ImportCancelled(Exception):
    pass


@dataclass
class ImportResult:
    imported: int = 0
    skipped: int = 0
    failed: list = field(default_factory=list)   # [(name, fehlermeldung)]
    cancelled: bool = False
    import_id: int | None = None
    asset_ids: list = field(default_factory=list)
    fatal: str | None = None


def date_dir(asset):
    d = asset.sort_date
    return f"{d:%Y}/{d:%m}/{d:%d}"


class _DirIndex:
    """Merkt sich (case-insensitive, wie FAT32) vorhandene Namen pro Zielordner."""

    def __init__(self, root):
        self.root = root
        self._dirs = {}

    def names(self, rel_dir):
        if rel_dir not in self._dirs:
            try:
                self._dirs[rel_dir] = {n.upper() for n in os.listdir(os.path.join(self.root, rel_dir))}
            except FileNotFoundError:
                self._dirs[rel_dir] = set()
        return self._dirs[rel_dir]

    def add(self, rel_dir, name):
        self.names(rel_dir).add(name.upper())


def _with_suffix(name, n):
    stem, ext = os.path.splitext(name)
    return f"{stem}_{n}{ext}" if n else name


def plan_names(files, rel_dir, index):
    """Wählt für alle Dateien eines Objekts denselben Namenszusatz, damit Paare zusammenbleiben."""
    n = 0
    taken = index.names(rel_dir)
    while any(_with_suffix(f.name, n).upper() in taken for f in files):
        n += 1
    return {f: _with_suffix(f.name, n) for f in files}


def copy_file(src, dst, cancel, on_bytes, mtime=None):
    part = dst + ".part"
    try:
        with open(src, "rb") as fin, open(part, "wb") as fout:
            while True:
                if cancel.is_set():
                    raise ImportCancelled()
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                on_bytes(len(chunk))
            fout.flush()
            os.fsync(fout.fileno())
        if os.path.getsize(part) != os.path.getsize(src):
            raise OSError(errno.EIO, "Größe stimmt nach dem Kopieren nicht überein")
        os.replace(part, dst)
        if mtime:
            try:
                os.utime(dst, (mtime, mtime))
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise


def run_import(library, assets, *, album_id=None, udid=None, device_name=None,
               cancel=None, progress=None):
    """Importiert `assets` (bereits mit Status markiert). Läuft im Worker-Thread.

    progress(done_items, total_items, done_bytes, total_bytes, current_name)
    """
    cancel = cancel or threading.Event()
    result = ImportResult()
    library.mark_status(assets)
    todo = [a for a in assets if a.status == "new"]
    result.skipped = len(assets) - len(todo)
    fps = library.fingerprints()
    jobs = []
    for a in todo:
        if a.extra.get("partial_of"):
            jobs.append((a, [f for f in a.files if f.fingerprint not in fps]))
        else:
            jobs.append((a, list(a.files)))
    total_bytes = sum(f.size for _, files in jobs for f in files)
    total_items = len(jobs)

    root = str(library.root)
    try:
        free = shutil.disk_usage(root).free
    except OSError as e:
        result.fatal = f"Mediathek nicht erreichbar: {e}"
        return result
    if total_bytes > free:
        result.fatal = (
            f"Nicht genug Speicherplatz: benötigt {total_bytes / 1e9:.1f} GB, "
            f"frei {free / 1e9:.1f} GB."
        )
        return result

    result.import_id = library.start_import(udid, device_name, album_id)
    index = _DirIndex(root)
    done_bytes = 0
    last_emit = 0.0

    def emit(i, name, force=False):
        nonlocal last_emit
        now = time.monotonic()
        if progress and (force or now - last_emit > 0.1):
            last_emit = now
            progress(i, total_items, done_bytes, total_bytes, name)

    def on_bytes(n):
        nonlocal done_bytes
        done_bytes += n
        emit(current, current_name)

    current, current_name = 0, ""
    consecutive_errors = 0
    try:
        for current, (asset, files) in enumerate(jobs):
            current_name = asset.primary.name
            emit(current, current_name, force=True)
            partial = asset.extra.get("partial_of")
            rel_dir = os.path.dirname(partial[1]) if partial else date_dir(asset)
            os.makedirs(os.path.join(root, rel_dir), exist_ok=True)
            names = plan_names(files, rel_dir, index)
            written = []
            try:
                for f in files:
                    if f.size > FAT_LIMIT and _is_fat(root):
                        raise OSError(errno.EFBIG, "Datei größer als 4 GB – auf FAT32 nicht speicherbar")
                    dst = os.path.join(root, rel_dir, names[f])
                    copy_file(f.path, dst, cancel, on_bytes, f.mtime)
                    index.add(rel_dir, names[f])
                    written.append(f)
            except ImportCancelled:
                _cleanup(root, rel_dir, names, written)
                raise
            except OSError as e:
                _cleanup(root, rel_dir, names, written)
                done_bytes += sum(f.size for f in files if f not in written)
                result.failed.append((asset.primary.name, e.strerror or str(e)))
                consecutive_errors += 1
                if e.errno in (errno.ENOTCONN, errno.ENODEV, errno.ENOSPC, errno.EROFS) \
                        or consecutive_errors >= 5:
                    result.fatal = _fatal_text(e)
                    break
                continue
            consecutive_errors = 0
            rels = {f: f"{rel_dir}/{names[f]}" for f in files}
            asset_id = library.add_asset(asset, rels, import_id=result.import_id, source_udid=udid,
                                         commit=False)
            if album_id is not None:
                library.add_to_album(album_id, [asset_id], commit=False)
            library.commit()
            result.asset_ids.append(asset_id)
            result.imported += 1
    except ImportCancelled:
        result.cancelled = True
    finally:
        library.commit()
        library.finish_import(result.import_id, result.imported)
        if not result.imported:
            result.import_id = None
        emit(result.imported, "", force=True)
    return result


def _cleanup(root, rel_dir, names, written):
    for f in written:
        try:
            os.unlink(os.path.join(root, rel_dir, names[f]))
        except OSError:
            pass


def _fatal_text(e):
    if e.errno == errno.ENOSPC:
        return "Das Ziellaufwerk ist voll."
    if e.errno == errno.EROFS:
        return "Das Ziellaufwerk ist schreibgeschützt."
    return "Die Verbindung zum iPhone wurde unterbrochen."


def _is_fat(path):
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            mounts = [line.split()[:3] for line in fh]
    except OSError:
        return False
    path = os.path.realpath(path)
    best = ("", "")
    for _dev, mnt, fstype in mounts:
        mnt = mnt.replace("\\040", " ")
        if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best[0]):
            best = (mnt, fstype)
    return best[1] in ("vfat", "msdos")
