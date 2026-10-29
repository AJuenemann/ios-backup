"""Erkennung angeschlossener iPhones (libimobiledevice) und Einhängen per ifuse."""

import os
import errno
import shutil
import subprocess
import threading
from dataclasses import dataclass, replace

from ..config import runtime_dir

POLL_SECONDS = 2.0

# Zustände eines Geräts
CONNECTING = "connecting"
UNTRUSTED = "untrusted"   # „Diesem Computer vertrauen?“ noch nicht bestätigt
LOCKED = "locked"         # Code-Sperre aktiv / DCIM nicht lesbar
READY = "ready"
ERROR = "error"


@dataclass
class Device:
    udid: str
    name: str = "iPhone"
    model: str = ""
    ios: str = ""
    state: str = CONNECTING
    mountpoint: str | None = None
    message: str = ""
    fake: bool = False

    @property
    def subtitle(self):
        parts = [p for p in (self.model, f"iOS {self.ios}" if self.ios else "") if p]
        return " · ".join(parts)


def _run(args, timeout=15):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def tools_available():
    missing = [t for t in ("idevice_id", "idevicepair", "ideviceinfo", "ifuse") if not shutil.which(t)]
    return missing


def list_udids():
    code, out = _run(["idevice_id", "-l"], timeout=10)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip() and " " not in line.strip()]


def pair_state(udid):
    code, out = _run(["idevicepair", "-u", udid, "validate"])
    if code == 0 and "SUCCESS" in out.upper():
        return READY, ""
    code, out = _run(["idevicepair", "-u", udid, "pair"])
    low = out.lower()
    if code == 0 and "success" in low:
        return READY, ""
    if "passcode" in low or "locked" in low:
        return LOCKED, out
    if "trust" in low or "dialog" in low or "not paired" in low:
        return UNTRUSTED, out
    return UNTRUSTED, out


def device_info(udid):
    info = {}
    for key in ("DeviceName", "ProductType", "ProductVersion"):
        code, out = _run(["ideviceinfo", "-u", udid, "-k", key])
        if code == 0 and out:
            info[key] = out.splitlines()[0].strip()
    return info


def lock_state(udid):
    # PasswordProtected meldet nur, ob ein Code eingerichtet ist – nicht, ob gerade gesperrt ist.
    # Der tatsächliche Lesezugriff wird über den Mount-/DCIM-Check in _advance() entschieden.
    return READY, ""


def _model_name(product_type):
    if not product_type:
        return ""
    for prefix, label in (("iPhone", "iPhone"), ("iPad", "iPad"), ("iPod", "iPod touch")):
        if product_type.startswith(prefix):
            return label
    return product_type


def _fusermount():
    return shutil.which("fusermount3") or shutil.which("fusermount") or "fusermount"


def is_mounted(path):
    try:
        # ismount() selbst verschluckt Fehler eines abgerissenen FUSE-Mounts.
        os.stat(path)
        return os.path.ismount(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        if e.errno in (errno.EIO, errno.ENOTCONN):
            return True
        raise


def unmount(path):
    if path and is_mounted(path):
        code, _ = _run([_fusermount(), "-u", path])
        if code != 0:
            _run([_fusermount(), "-uz", path])
    try:
        os.rmdir(path)
    except (OSError, TypeError):
        pass


def mount(udid):
    mp = str(runtime_dir() / udid)
    if is_mounted(mp):
        try:
            os.listdir(mp)
            return mp, ""
        except OSError:
            unmount(mp)
    os.makedirs(mp, exist_ok=True)
    code, out = _run(["ifuse", mp, "-u", udid], timeout=30)
    if code != 0 or not is_mounted(mp):
        try:
            os.rmdir(mp)
        except OSError:
            pass
        return None, out or "ifuse fehlgeschlagen"
    return mp, ""


class DeviceMonitor:
    """Fragt regelmäßig angeschlossene Geräte ab und meldet Änderungen.

    `on_change(devices)` wird im Hintergrund-Thread aufgerufen – der Aufrufer muss selbst
    in den GTK-Hauptthread wechseln (GLib.idle_add).
    """

    def __init__(self, on_change, fake_dirs=()):
        self.on_change = on_change
        self.devices = {}
        self.ejected = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        for i, d in enumerate(fake_dirs):
            udid = f"fake-{i}"
            self.devices[udid] = Device(
                udid, name=os.path.basename(os.path.abspath(d)) or "Testgerät",
                model="Simuliertes iPhone", state=READY, mountpoint=os.path.abspath(d), fake=True,
            )

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="device-monitor", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        for dev in self.devices.values():
            if not dev.fake:
                unmount(dev.mountpoint)

    def refresh(self):
        self._wake.set()

    def eject(self, udid):
        dev = self.devices.get(udid)
        if not dev:
            return
        if not dev.fake:
            self.ejected.add(udid)
            unmount(dev.mountpoint)
        del self.devices[udid]
        self._emit()

    def _emit(self):
        self.on_change({udid: replace(dev) for udid, dev in self.devices.items()})

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as e:  # pragma: no cover - Robustheit
                print("Gerätemonitor:", e)
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def _poll(self):
        if self._stop.is_set():
            return
        present = set(list_udids())
        changed = False
        # Ausgeworfene Geräte erst nach Abziehen wieder anzeigen
        self.ejected &= present
        for udid in list(self.devices):
            if not self.devices[udid].fake and udid not in present:
                unmount(self.devices[udid].mountpoint)
                del self.devices[udid]
                changed = True
        for udid in present - self.ejected:
            dev = self.devices.get(udid)
            if dev is None:
                dev = self.devices[udid] = Device(udid)
                changed = True
                self._emit()
            try:
                changed |= self._advance(dev)
            except OSError as e:
                dev.state, dev.message = ERROR, str(e)
                changed = True
        if changed:
            self._emit()

    def _advance(self, dev):
        before = (dev.state, dev.name, dev.message)
        if dev.state != READY:
            state, msg = pair_state(dev.udid)
            if state != READY:
                dev.state, dev.message = state, msg
                return before != (dev.state, dev.name, dev.message)
        state, msg = lock_state(dev.udid)
        if state != READY:
            dev.state, dev.message = state, msg
            return before != (dev.state, dev.name, dev.message)
        if dev.state == READY and dev.mountpoint and os.path.isdir(os.path.join(dev.mountpoint, "DCIM")):
            return False
        if state == READY:
            info = device_info(dev.udid)
            dev.name = info.get("DeviceName") or dev.name
            dev.model = _model_name(info.get("ProductType"))
            dev.ios = info.get("ProductVersion", "")
            mp, err = mount(dev.udid)
            if not mp:
                locked = any(word in err.lower() for word in ("passwordprotected", "password protected",
                                                              "passcode", "locked"))
                dev.state, dev.message = (LOCKED if locked else ERROR), err
                return before != (dev.state, dev.name, dev.message)
            dev.mountpoint = mp
            if os.path.isdir(os.path.join(dev.mountpoint, "DCIM")):
                dev.state, dev.message = READY, ""
            else:
                dev.state, dev.message = LOCKED, ""
        else:
            dev.state, dev.message = state, msg
        return before != (dev.state, dev.name, dev.message)
