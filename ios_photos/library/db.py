"""Mediathek: SQLite-Index über die importierten Originaldateien (relative Pfade → umziehbar)."""

import os
import sqlite3
import threading
import tempfile
from datetime import datetime
from pathlib import Path

from ..media.scanner import Asset, SourceFile, name_key

DB_DIR = ".ios-photos"
DB_NAME = "library.db"
SCHEMA_VERSION = 3
TRASH_DB_BATCH = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    raw INTEGER DEFAULT 0,
    screenshot INTEGER DEFAULT 0,
    edited INTEGER DEFAULT 0,
    taken_at TEXT,
    duration REAL,
    width INTEGER,
    height INTEGER,
    imported_at TEXT,
    import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    legacy INTEGER DEFAULT 0,
    source_udid TEXT,
    hidden INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS assets_taken ON assets(taken_at);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    rel TEXT NOT NULL UNIQUE,
    name_key TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL,
    ftype TEXT NOT NULL,
    variant TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS files_fp ON files(name_key, size);
CREATE INDEX IF NOT EXISTS files_asset ON files(asset_id);
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS album_assets (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    PRIMARY KEY (album_id, asset_id)
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    device_name TEXT,
    udid TEXT,
    album_id INTEGER,
    count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db_path(root):
    return Path(root) / DB_DIR / DB_NAME


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


class Library:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()
        (self.root / DB_DIR).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path(self.root)), check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and not _is_fat(self.root):
            mode = self.db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode != "delete":
                self.db.close()
                raise sqlite3.OperationalError("WAL konnte nach dem Mediathek-Umzug nicht deaktiviert werden")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        self.db.commit()
        self._fp = None

    def _migrate(self):
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(assets)")}
        if "hidden" not in cols:
            self.db.execute("ALTER TABLE assets ADD COLUMN hidden INTEGER DEFAULT 0")

    # ---------------------------------------------------------------- allgemein
    def close(self):
        with self._lock:
            self.db.close()

    def abspath(self, rel):
        return str(self.root / rel)

    def is_empty(self):
        with self._lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM assets a WHERE EXISTS ("
                "SELECT 1 FROM files f WHERE f.asset_id=a.id AND f.ftype!='sidecar')"
            ).fetchone()[0] == 0

    def count(self):
        with self._lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM assets a WHERE EXISTS ("
                "SELECT 1 FROM files f WHERE f.asset_id=a.id AND f.ftype!='sidecar')"
            ).fetchone()[0]

    def get_meta(self, key, default=None):
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
            self.db.commit()

    # ------------------------------------------------------------- Duplikate
    def fingerprints(self):
        """{(NAME, größe): (asset_id, rel)} – im Speicher gehalten für schnelle Statusprüfung."""
        with self._lock:
            if self._fp is None:
                self._fp = {
                    (n, s): (a, r)
                    for n, s, a, r in self.db.execute("SELECT name_key, size, asset_id, rel FROM files")
                }
            return self._fp

    def known_rels(self):
        with self._lock:
            return {r for (r,) in self.db.execute("SELECT rel FROM files")}

    def asset_date(self, asset_id):
        with self._lock:
            row = self.db.execute("SELECT taken_at FROM assets WHERE id=?", (asset_id,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def mark_status(self, assets):
        """Setzt asset.status ('new'/'imported') anhand der Fingerprints der Mediendateien."""
        fps = self.fingerprints()
        for a in assets:
            hits = [fps.get(f.fingerprint) for f in a.media_files]
            if all(hits):
                a.status = "imported"
                a.extra["id"] = hits[0][0]
                if a.date is None:
                    a.date = self.asset_date(hits[0][0])
            else:
                a.status = "new"
                known = [h for h in hits if h]
                if known:
                    a.extra["partial_of"] = known[0]
        return assets

    # ----------------------------------------------------------- Schreiben
    def add_asset(self, asset, rels, import_id=None, legacy=False, source_udid=None, commit=True):
        """`rels`: {SourceFile: rel_pfad_in_mediathek}. Gibt die Asset-ID zurück."""
        with self._lock:
            existing = asset.extra.get("partial_of")
            asset_id = None
            if existing:
                asset_id = existing[0]
                row = self.db.execute("SELECT source_udid FROM assets WHERE id=?", (asset_id,)).fetchone()
                existing_udid = row[0] if row else None
                if source_udid and existing_udid != source_udid:
                    asset_id = None
            if asset_id is None:
                cur = self.db.execute(
                    "INSERT INTO assets (kind, raw, screenshot, edited, taken_at, duration, width, height,"
                    " imported_at, import_id, legacy, source_udid, hidden)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        asset.kind, int(asset.raw), int(asset.screenshot), int(asset.edited),
                        asset.sort_date.isoformat(), asset.duration, asset.width, asset.height,
                        datetime.now().isoformat(timespec="seconds"), import_id, int(legacy), source_udid,
                        int(bool(asset.extra.get("hidden"))),
                    ),
                )
                asset_id = cur.lastrowid
            for f, rel in rels.items():
                try:
                    mtime = os.stat(self.abspath(rel)).st_mtime
                except OSError:
                    mtime = f.mtime
                self.db.execute(
                    "INSERT OR REPLACE INTO files (asset_id, rel, name_key, size, mtime, ftype, variant)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (asset_id, rel, name_key(f.name), f.size, mtime, f.ftype, f.variant),
                )
                if self._fp is not None:
                    self._fp[(name_key(f.name), f.size)] = (asset_id, rel)
            if commit:
                self.db.commit()
            return asset_id

    def commit(self):
        with self._lock:
            self.db.commit()

    def enable_wal(self):
        """Lokaler Index: einzelne dauerhafte Commits ohne Journal-Neuanlage."""
        with self._lock:
            self.db.execute("PRAGMA synchronous=FULL")
            mode = self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise sqlite3.OperationalError(
                    f"WAL für die lokale Mediathek nicht verfügbar (Modus: {mode})"
                )

    def move_file(self, file_rel, new_rel, commit=True):
        with self._lock:
            self.db.execute("UPDATE files SET rel=? WHERE rel=?", (new_rel, file_rel))
            if commit:
                self.db.commit()
            self._fp = None

    def remove_missing(self):
        """Entfernt Einträge, deren Dateien nicht mehr existieren. Gibt die Anzahl zurück."""
        with self._lock:
            gone = [fid for fid, rel in self.db.execute("SELECT id, rel FROM files")
                    if not os.path.exists(self.abspath(rel))]
            self.db.executemany("DELETE FROM files WHERE id=?", [(g,) for g in gone])
            self.db.execute("DELETE FROM assets WHERE id NOT IN (SELECT asset_id FROM files)")
            self.db.commit()
            self._fp = None
            return len(gone)

    def _trash_folder(self):
        """Erstellt einen Sammelordner direkt im System-Papierkorb desselben Laufwerks."""
        from gi.repository import Gio

        folder = Path(tempfile.mkdtemp(prefix="iPhone Fotos - ", dir=self.root.resolve()))
        try:
            if not Gio.File.new_for_path(str(folder)).trash(None):
                raise OSError("Der Sammelordner konnte nicht in den Papierkorb verschoben werden")
        finally:
            if folder.exists():
                folder.rmdir()
        entries = Gio.File.new_for_uri("trash:///").enumerate_children(
            "standard::target-uri,trash::orig-path", Gio.FileQueryInfoFlags.NONE, None,
        )
        try:
            for entry in entries:
                if entry.get_attribute_byte_string("trash::orig-path") != str(folder):
                    continue
                uri = entry.get_attribute_string("standard::target-uri")
                target = Gio.File.new_for_uri(uri).get_path() if uri else None
                if target:
                    path = Path(target)
                    if (not path.is_symlink() and path.is_dir()
                            and path.stat().st_dev == self.root.stat().st_dev
                            and not path.resolve().is_relative_to(self.root.resolve())):
                        return path
                break
        finally:
            entries.close(None)
        raise OSError("Der Sammelordner im System-Papierkorb ist nicht lokal erreichbar. "
                      "Es wurden keine Originaldateien verschoben.")

    def trash_assets(self, asset_ids, progress=None, cancel=None):
        """Verschiebt lokale Dateien in den System-Papierkorb; meldet Teilerfolge."""
        from gi.repository import Gio, GLib

        completed, errors = 0, []
        with self._lock:
            jobs = [
                (asset_id, self.db.execute(
                    "SELECT id, rel FROM files WHERE asset_id=?", (asset_id,)
                ).fetchall())
                for asset_id in dict.fromkeys(asset_ids)
            ]
        total = sum(len(rows) for _, rows in jobs)
        done = 0
        root = self.root.resolve()
        moved = []
        destination = None

        def save_moved():
            if not moved:
                return
            with self._lock:
                try:
                    self.db.execute("BEGIN")
                    self.db.executemany("DELETE FROM files WHERE id=?", [(fid,) for fid, _ in moved])
                    self.db.executemany(
                        "DELETE FROM assets WHERE id=? AND NOT EXISTS"
                        " (SELECT 1 FROM files WHERE asset_id=?)",
                        [(asset_id, asset_id) for asset_id in {aid for _, aid in moved}],
                    )
                    self.db.commit()
                except sqlite3.Error:
                    self.db.rollback()
                    raise
                self._fp = None
                moved.clear()

        for asset_id, rows in jobs:
            if not rows:
                continue
            failed = False
            asset_moved = False
            for file_id, rel in rows:
                if cancel is not None and cancel.is_set() and not asset_moved:
                    save_moved()
                    return completed, errors
                if progress:
                    progress(done, total, rel)
                path = self.root / rel
                try:
                    resolved = path.resolve()
                    if (Path(rel).is_absolute() or resolved == root
                            or not resolved.is_relative_to(root)
                            or path.is_symlink() or path.is_dir()):
                        raise OSError("Kein gültiger lokaler Dateipfad innerhalb der Mediathek")
                    if total > 1:
                        if destination is None:
                            try:
                                destination = self._trash_folder()
                            except (GLib.Error, OSError) as e:
                                save_moved()
                                return completed, errors + [("Papierkorb", str(e))]
                        target = destination / resolved.relative_to(root)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        path.rename(target)
                    elif not Gio.File.new_for_path(str(path)).trash(None):
                        raise OSError("Die Datei konnte nicht in den Papierkorb verschoben werden")
                except (GLib.Error, OSError) as e:
                    errors.append((rel, str(e)))
                    failed = True
                else:
                    moved.append((file_id, asset_id))
                    asset_moved = True
                    if len(moved) >= TRASH_DB_BATCH:
                        save_moved()
                done += 1
                if progress:
                    progress(done, total, rel)
            if cancel is not None and cancel.is_set():
                save_moved()
                return completed, errors
            if not failed:
                completed += 1
        save_moved()
        return completed, errors

    # ------------------------------------------------------------- Importe
    def start_import(self, udid, device_name, album_id=None):
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO imports (started_at, device_name, udid, album_id) VALUES (?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), device_name, udid, album_id),
            )
            self.db.commit()
            return cur.lastrowid

    def finish_import(self, import_id, count):
        with self._lock:
            if count:
                self.db.execute(
                    "UPDATE imports SET finished_at=?, count=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), count, import_id),
                )
            else:
                self.db.execute("DELETE FROM imports WHERE id=?", (import_id,))
            self.db.commit()

    def imports(self):
        with self._lock:
            rows = self.db.execute(
                "SELECT i.id, i.started_at, i.device_name, COUNT(a.id) FROM imports i"
                " JOIN assets a ON a.import_id = i.id GROUP BY i.id ORDER BY i.started_at DESC"
            ).fetchall()
        return [
            {"id": r[0], "started_at": datetime.fromisoformat(r[1]), "device": r[2], "count": r[3]}
            for r in rows
        ]

    # -------------------------------------------------------------- Alben
    def albums(self):
        with self._lock:
            rows = self.db.execute(
                "SELECT al.id, al.name, COUNT(a.id) FROM albums al"
                " LEFT JOIN album_assets aa ON aa.album_id = al.id"
                " LEFT JOIN assets a ON a.id = aa.asset_id AND a.hidden = 0"
                " GROUP BY al.id ORDER BY al.name"
            ).fetchall()
        return [{"id": r[0], "name": r[1], "count": r[2]} for r in rows]

    def create_album(self, name):
        name = name.strip()
        with self._lock:
            row = self.db.execute("SELECT id FROM albums WHERE name=?", (name,)).fetchone()
            if row:
                return row[0]
            cur = self.db.execute(
                "INSERT INTO albums (name, created_at) VALUES (?, ?)",
                (name, datetime.now().isoformat(timespec="seconds")),
            )
            self.db.commit()
            return cur.lastrowid

    def rename_album(self, album_id, name):
        with self._lock:
            self.db.execute("UPDATE albums SET name=? WHERE id=?", (name.strip(), album_id))
            self.db.commit()

    def delete_album(self, album_id):
        with self._lock:
            self.db.execute("DELETE FROM albums WHERE id=?", (album_id,))
            self.db.commit()

    def add_to_album(self, album_id, asset_ids, commit=True):
        with self._lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO album_assets VALUES (?, ?)", [(album_id, a) for a in asset_ids]
            )
            if commit:
                self.db.commit()

    def remove_from_album(self, album_id, asset_ids):
        with self._lock:
            self.db.executemany(
                "DELETE FROM album_assets WHERE album_id=? AND asset_id=?",
                [(album_id, a) for a in asset_ids],
            )
            self.db.commit()

    def set_hidden(self, asset_ids, hidden):
        if not asset_ids:
            return
        with self._lock:
            self.db.executemany(
                "UPDATE assets SET hidden=? WHERE id=?",
                [(int(bool(hidden)), a) for a in asset_ids],
            )
            self.db.commit()

    # ------------------------------------------------------------- Abfragen
    def query(self, kind=None, raw=None, screenshot=None, album_id=None, import_id=None,
              hidden=None):
        """Liefert Asset-Objekte (Status 'imported') aufsteigend nach Aufnahmedatum."""
        where, args = ["EXISTS (SELECT 1 FROM files mf WHERE mf.asset_id = a.id AND mf.ftype != 'sidecar')"], []
        if hidden == "all":
            pass
        elif hidden is None:
            where.append("a.hidden = 0")
        elif hidden:
            where.append("a.hidden = 1")
        else:
            where.append("a.hidden = 0")
        if kind:
            where.append("a.kind = ?")
            args.append(kind)
        if raw:
            where.append("a.raw = 1")
        if screenshot:
            where.append("a.screenshot = 1")
        if album_id is not None:
            where.append("a.id IN (SELECT asset_id FROM album_assets WHERE album_id = ?)")
            args.append(album_id)
        if import_id is not None:
            where.append("a.import_id = ?")
            args.append(import_id)
        sql = (
            "SELECT a.id, a.kind, a.raw, a.screenshot, a.edited, a.taken_at, a.duration, a.width,"
            " a.height, a.import_id, f.rel, f.size, f.mtime, f.ftype, f.variant"
            " FROM assets a JOIN files f ON f.asset_id = a.id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY a.taken_at, a.id, f.rel"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        assets, by_id = [], {}
        for (aid, kind_, raw_, shot, edited, taken, dur, w, h, imp, rel, size, mtime, ftype, variant) in rows:
            a = by_id.get(aid)
            if a is None:
                a = Asset(
                    key=f"lib:{aid}", files=[], kind=kind_, raw=bool(raw_), screenshot=bool(shot),
                    edited=bool(edited), date=datetime.fromisoformat(taken) if taken else None,
                    duration=dur, width=w, height=h, status="imported",
                    extra={"id": aid, "import_id": imp},
                )
                by_id[aid] = a
                assets.append(a)
            a.files.append(SourceFile(
                self.abspath(rel), rel, os.path.basename(rel), size, mtime or 0, ftype, variant or ""
            ))
        return assets

    def counts(self):
        with self._lock:
            q = self.db.execute
            return {
                "all": q("SELECT COUNT(*) FROM assets a WHERE hidden=0 AND EXISTS ("
                         "SELECT 1 FROM files f WHERE f.asset_id=a.id AND f.ftype!='sidecar')").fetchone()[0],
                "video": q("SELECT COUNT(*) FROM assets a WHERE hidden=0 AND kind='video'"
                           " AND EXISTS (SELECT 1 FROM files f WHERE f.asset_id=a.id AND f.ftype!='sidecar')")
                .fetchone()[0],
                "live": q("SELECT COUNT(*) FROM assets a WHERE hidden=0 AND kind='live'"
                          " AND EXISTS (SELECT 1 FROM files f WHERE f.asset_id=a.id AND f.ftype!='sidecar')")
                .fetchone()[0],
                "raw": q("SELECT COUNT(*) FROM assets a WHERE hidden=0 AND raw=1"
                         " AND EXISTS (SELECT 1 FROM files f WHERE f.asset_id=a.id AND f.ftype!='sidecar')")
                .fetchone()[0],
                "screenshot": q("SELECT COUNT(*) FROM assets a WHERE hidden=0"
                                " AND screenshot=1 AND EXISTS (SELECT 1 FROM files f WHERE f.asset_id=a.id"
                                " AND f.ftype!='sidecar')").fetchone()[0],
                "hidden": q("SELECT COUNT(*) FROM assets a WHERE hidden=1 AND EXISTS (SELECT 1 FROM files f"
                            " WHERE f.asset_id=a.id AND f.ftype!='sidecar')").fetchone()[0],
            }

    def all_rels(self):
        with self._lock:
            return [r for (r,) in self.db.execute("SELECT rel FROM files")]
