"""Mediathek: SQLite-Index über die importierten Originaldateien (relative Pfade → umziehbar)."""

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..media.scanner import Asset, SourceFile, name_key

DB_DIR = ".ios-photos"
DB_NAME = "library.db"
SCHEMA_VERSION = 1

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
    source_udid TEXT
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


class Library:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()
        (self.root / DB_DIR).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path(self.root)), check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        self.db.commit()
        self._fp = None

    # ---------------------------------------------------------------- allgemein
    def close(self):
        with self._lock:
            self.db.close()

    def abspath(self, rel):
        return str(self.root / rel)

    def is_empty(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0

    def count(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

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
            if existing:
                asset_id = existing[0]
            else:
                cur = self.db.execute(
                    "INSERT INTO assets (kind, raw, screenshot, edited, taken_at, duration, width, height,"
                    " imported_at, import_id, legacy, source_udid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        asset.kind, int(asset.raw), int(asset.screenshot), int(asset.edited),
                        asset.sort_date.isoformat(), asset.duration, asset.width, asset.height,
                        datetime.now().isoformat(timespec="seconds"), import_id, int(legacy), source_udid,
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
                "SELECT al.id, al.name, COUNT(aa.asset_id) FROM albums al"
                " LEFT JOIN album_assets aa ON aa.album_id = al.id GROUP BY al.id ORDER BY al.name"
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

    # ------------------------------------------------------------- Abfragen
    def query(self, kind=None, raw=None, screenshot=None, album_id=None, import_id=None):
        """Liefert Asset-Objekte (Status 'imported') aufsteigend nach Aufnahmedatum."""
        where, args = [], []
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
                "all": q("SELECT COUNT(*) FROM assets").fetchone()[0],
                "video": q("SELECT COUNT(*) FROM assets WHERE kind='video'").fetchone()[0],
                "live": q("SELECT COUNT(*) FROM assets WHERE kind='live'").fetchone()[0],
                "raw": q("SELECT COUNT(*) FROM assets WHERE raw=1").fetchone()[0],
                "screenshot": q("SELECT COUNT(*) FROM assets WHERE screenshot=1").fetchone()[0],
            }

    def all_rels(self):
        with self._lock:
            return [r for (r,) in self.db.execute("SELECT rel FROM files")]
