"""Einstiegspunkt: Adw.Application mit Gerätemonitor und optionalem Hintergrundmodus."""

import os
import shlex
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .config import APP_ICON, APP_ID, APP_NAME, Config, autostart_file  # noqa: E402
from .device.monitor import DeviceMonitor  # noqa: E402

DATA_DIR = Path(__file__).parent / "data"


def autostart_entry(exec_cmd):
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME} (Hintergrund)\n"
        "Comment=Öffnet iPhone Fotos, sobald ein iPhone angeschlossen wird\n"
        f"Exec={exec_cmd} --monitor\n"
        f"Icon={APP_ICON}\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def launcher_command():
    """Befehl, mit dem die App wieder gestartet werden kann (für die Autostart-Datei)."""
    for cand in (Path.home() / ".local/bin/ios-photos",):
        if cand.exists():
            return shlex.quote(str(cand))
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0.endswith("ios-photos") and os.access(argv0, os.X_OK):
        return shlex.quote(argv0)
    return f"{shlex.quote(sys.executable)} -m ios_photos"


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.config = Config()
        self.monitor = None
        self.window = None
        self.devices = {}
        self._held = False
        self.add_main_option("monitor", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Im Hintergrund auf angeschlossene iPhones warten", None)
        self.add_main_option("fake-device", 0, GLib.OptionFlags.NONE, GLib.OptionArg.FILENAME_ARRAY,
                             "Ordner als simuliertes iPhone anzeigen (zum Testen)", "ORDNER")

    # ---------------------------------------------------------------- Start
    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        Gtk.Window.set_default_icon_name(APP_ICON)
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(DATA_DIR / "icons"))
        from .ui.util import load_css

        load_css()
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self._quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])
        self.set_autostart(self.config.get("background_monitor"))

    def do_command_line(self, cmdline):
        options = cmdline.get_options_dict()
        fv = options.lookup_value("fake-device", None)
        fake = [os.fsdecode(p) for p in fv.get_bytestring_array()] if fv else []
        opts = options.end().unpack()
        if self.monitor is None:
            self.monitor = DeviceMonitor(self._devices_changed_thread, fake_dirs=fake)
            self.devices = dict(self.monitor.devices)
            self.monitor.start()
        if opts.get("monitor") or self.autostart_enabled():
            if not self._held:
                self.hold()
                self._held = True
        if not opts.get("monitor"):
            self.activate()
        return 0

    def do_activate(self):
        if self.window is None:
            from .ui.window import MainWindow

            self.window = MainWindow(self)
            if self.devices:
                self.window.on_devices_changed(self.devices)
        self.window.present()

    def do_shutdown(self):
        if self.monitor is not None:
            self.monitor.stop()
        Adw.Application.do_shutdown(self)

    # ---------------------------------------------------------------- Geräte
    def _devices_changed_thread(self, devices):
        GLib.idle_add(self._devices_changed, devices)

    def _devices_changed(self, devices):
        old = self.devices
        self.devices = devices
        if self.window is not None:
            self.window.on_devices_changed(devices)
        elif self.autostart_enabled():
            for udid, dev in devices.items():
                if udid not in old and not dev.fake:
                    self.config.set_device(udid, name=dev.name)
                    if self.config.device(udid).get("open_on_connect", True):
                        self.activate()
                        break
        return False

    def window_closed(self, win):
        # Im Hintergrundmodus (hold) läuft die App nach dem Schließen weiter.
        if self.window is win:
            self.window = None

    def _quit(self):
        self._quit_requested = True
        if self.window is not None:
            self.window.close()
            if self.window is not None:  # Nachfrage wegen laufendem Import
                return
        self.quit()

    # ------------------------------------------------------------- Autostart
    def autostart_enabled(self):
        return autostart_file().exists()

    def set_autostart(self, enabled):
        path = autostart_file()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(autostart_entry(launcher_command()))
            if not self._held:
                self.hold()
                self._held = True
        else:
            path.unlink(missing_ok=True)
            if self._held:
                self.release()
                self._held = False
        self.config.set("background_monitor", enabled)


def main(argv=None):
    app = Application()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
