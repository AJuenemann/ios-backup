"""Lädt Vorschaubilder asynchron (zuletzt angefragte zuerst) mit LRU-Speicher-Cache."""

import os
import threading
import time
from collections import OrderedDict

from gi.repository import Gdk, GLib

from ..media.thumbnails import PREVIEW_SIZE, THUMB_SIZE, get_thumbnail

MEM_ITEMS = 400


class ThumbLoader:
    def __init__(self, cache_root, size=THUMB_SIZE, workers=3, mem_items=MEM_ITEMS):
        self.cache_root = str(cache_root)
        self.size = size
        self.mem_items = mem_items
        self._mem = OrderedDict()
        self._callbacks = {}
        self._stack = []
        self._failed = {}
        self._cond = threading.Condition()
        self._stopped = False
        for i in range(workers):
            threading.Thread(target=self._work, name=f"thumbs-{i}", daemon=True).start()

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def peek(self, f):
        tex = self._mem.get(f.path)
        if tex is not None:
            self._mem.move_to_end(f.path)
        return tex

    def request(self, f, callback):
        """Gibt die Textur sofort zurück, falls bekannt; sonst wird `callback(texture|None)` später gerufen."""
        tex = self.peek(f)
        if tex is not None:
            return tex
        if time.monotonic() - self._failed.get(f.path, float("-inf")) < 5:
            GLib.idle_add(callback, None)
            return None
        with self._cond:
            pending = f.path in self._callbacks
            cbs = self._callbacks.setdefault(f.path, [])
            cbs.append(callback)
            if not pending:
                self._stack.append(f)
            else:
                # nach oben holen, damit sichtbare Kacheln zuerst geladen werden
                try:
                    self._stack.remove(f)
                except ValueError:
                    pass
                else:
                    self._stack.append(f)
            self._cond.notify()
        return None

    def cancel(self, f, callback):
        with self._cond:
            cbs = self._callbacks.get(f.path)
            if cbs and callback in cbs:
                cbs.remove(callback)

    def _work(self):
        while True:
            with self._cond:
                while not self._stack and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                f = self._stack.pop()
                if not self._callbacks.get(f.path):
                    self._callbacks.pop(f.path, None)
                    continue
            tex = None
            try:
                path = get_thumbnail(self.cache_root, f.path, self.size, f.mtime, f.size)
                tex = Gdk.Texture.new_from_filename(path)
            except Exception as e:  # noqa: BLE001
                print(f"Vorschau fehlgeschlagen für {os.path.basename(f.path)}: {e}")
            GLib.idle_add(self._deliver, f, tex)

    def _deliver(self, f, tex):
        with self._cond:
            cbs = self._callbacks.pop(f.path, [])
        if tex is None:
            self._failed[f.path] = time.monotonic()
        else:
            self._failed.pop(f.path, None)
            self._mem[f.path] = tex
            self._mem.move_to_end(f.path)
            while len(self._mem) > self.mem_items:
                self._mem.popitem(last=False)
        for cb in cbs:
            cb(tex)
        return False


def load_preview(cache_root, f, callback):
    """Lädt eine große Vorschau (PREVIEW_SIZE) im Hintergrund."""

    def work():
        tex = None
        try:
            path = get_thumbnail(str(cache_root), f.path, PREVIEW_SIZE, f.mtime, f.size)
            tex = Gdk.Texture.new_from_filename(path)
        except Exception as e:  # noqa: BLE001
            print(f"Große Vorschau fehlgeschlagen: {e}")
        GLib.idle_add(callback, f, tex)

    threading.Thread(target=work, daemon=True).start()
