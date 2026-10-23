"""Persistente Einstellungen (JSON unter ~/.config/ios-photos/)."""

import json
import os
import threading
from pathlib import Path

APP_ID = "io.github.ajuenemann.IosPhotos"
APP_ICON = f"{APP_ID}-rosette"
APP_NAME = "iPhone Fotos"
DEFAULT_LIBRARY = "/run/media/dude/DATA/iPhone_Backup"


def _xdg(var, fallback):
    return Path(os.environ.get(var) or Path.home() / fallback)


def config_dir():
    return _xdg("XDG_CONFIG_HOME", ".config") / "ios-photos"


def cache_dir():
    return _xdg("XDG_CACHE_HOME", ".cache") / "ios-photos"


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/ios-photos-{os.getuid()}"
    return Path(base) / "ios-photos"


def autostart_file():
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP_ID}.Monitor.desktop"


DEFAULTS = {
    "library_path": DEFAULT_LIBRARY,
    "thumb_size": 150,
    "media_filter": "all",
    "hide_imported": False,
    "last_album": None,
    "devices": {},
}


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_dir() / "config.json"
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
            tmp.replace(self.path)

    def device(self, udid):
        return self.data.setdefault("devices", {}).setdefault(
            udid, {"name": None, "open_on_connect": True}
        )

    def set_device(self, udid, **values):
        self.device(udid).update(values)
        self.save()
