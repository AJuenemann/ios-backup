"""Zwischenspeicher für Metadaten von iPhone-Dateien, damit erneutes Anschließen schnell geht."""

import sqlite3
import threading
from datetime import datetime


class MetaCache:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta (name TEXT, size INTEGER, mtime INTEGER,"
            " date TEXT, duration REAL, width INTEGER, height INTEGER,"
            " PRIMARY KEY (name, size, mtime))"
        )
        self.db.commit()

    @staticmethod
    def _key(f):
        return (f.name.upper(), f.size, int(f.mtime))

    def get(self, f):
        with self._lock:
            row = self.db.execute(
                "SELECT date, duration, width, height FROM meta WHERE name=? AND size=? AND mtime=?",
                self._key(f),
            ).fetchone()
        if not row:
            return None
        return {
            "date": datetime.fromisoformat(row[0]) if row[0] else None,
            "duration": row[1],
            "width": row[2],
            "height": row[3],
        }

    def put_many(self, items):
        rows = [
            (*self._key(f), m["date"].isoformat() if m.get("date") else None,
             m.get("duration"), m.get("width"), m.get("height"))
            for f, m in items
        ]
        with self._lock:
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?,?,?)", rows)
            self.db.commit()
