"""Hauptfenster: Seitenleiste (Mediathek, Medientypen, Alben, Geräte) + Inhaltsbereich."""

import os

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..config import APP_ICON, APP_NAME, cache_dir
from ..device.monitor import CONNECTING, LOCKED, READY, UNTRUSTED
from ..library.db import Library
from ..library.legacy import DATE_DIR_RE
from ..media.metacache import MetaCache
from ..media.scanner import MEDIA_EXT
from . import maintenance
from .import_view import ImportView
from .library_view import LibraryView
from .preferences import PreferencesDialog
from .preview import PreviewDialog
from .thumbloader import ThumbLoader
from .util import fmt_int, run_in_thread

STATE_TEXT = {
    CONNECTING: "Verbinde …",
    UNTRUSTED: "Vertrauen bestätigen",
    LOCKED: "Gesperrt",
    "error": "Fehler",
}


class SidebarRow(Gtk.ListBoxRow):
    def __init__(self, row_id, title, icon, section, count=None, subtitle=None, eject=None):
        super().__init__()
        self.row_id = row_id
        self.section = section
        box = Gtk.Box(spacing=10, margin_top=2, margin_bottom=2)
        box.append(Gtk.Image(icon_name=icon))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        lbl = Gtk.Label(label=title, xalign=0, ellipsize=3)
        labels.append(lbl)
        if subtitle:
            sub = Gtk.Label(label=subtitle, xalign=0, ellipsize=3)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            labels.append(sub)
        box.append(labels)
        if count is not None:
            c = Gtk.Label(label=fmt_int(count))
            c.add_css_class("dim-label")
            c.add_css_class("sidebar-count")
            box.append(c)
        if eject:
            b = Gtk.Button(icon_name="media-eject-symbolic", tooltip_text="Auswerfen",
                           valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.add_css_class("circular")
            b.connect("clicked", lambda *_: eject())
            box.append(b)
        self.set_child(box)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=1280, default_height=820)
        self.app = app
        self.config = app.config
        self.zoom = int(self.config.get("thumb_size") or 150)
        self.cache_root = cache_dir() / "thumbs"
        self.loader = ThumbLoader(self.cache_root)
        self.metacache = MetaCache(cache_dir() / "meta.db")
        self.library = None
        self.library_path = self.config.get("library_path")
        self.devices = {}
        self.import_views = {}
        self.views = {}
        self._zoom_scales = []
        self._current_id = None
        self._auto_selected = set()

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        # ---- Seitenleiste
        side_tv = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        side_header.set_title_widget(Adw.WindowTitle(title=APP_NAME))
        menu = Gio.Menu()
        menu.append("Neues Album …", "win.new-album")
        sec = Gio.Menu()
        sec.append("Einstellungen", "win.preferences")
        sec.append("Tastenkürzel", "win.shortcuts")
        sec.append(f"Über {APP_NAME}", "win.about")
        menu.append_section(None, sec)
        side_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                            tooltip_text="Hauptmenü", primary=True))
        side_tv.add_top_bar(side_header)
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar.set_header_func(self._sidebar_header)
        self.sidebar.connect("row-selected", self._on_row_selected)
        side_tv.set_content(Gtk.ScrolledWindow(child=self.sidebar,
                                               hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.split.set_sidebar(Adw.NavigationPage(title=APP_NAME, child=side_tv))
        self.split.set_content(Adw.NavigationPage(title="", child=Adw.StatusPage()))

        self._install_actions()
        self.connect("close-request", self._on_close)
        self.open_library()
        self.rebuild_sidebar()
        self.select_sidebar("library")

    # ------------------------------------------------------------ Aktionen
    def _install_actions(self):
        for name, cb, accel in (
            ("preferences", lambda *_: self.open_preferences(), "<Control>comma"),
            ("new-album", lambda *_: self._new_album(), "<Control>n"),
            ("about", lambda *_: self._about(), None),
            ("shortcuts", lambda *_: self._shortcuts(), "<Control>question"),
            ("import-all", lambda *_: self._current_import_view() and self._current_import_view().import_all(),
             "<Control>i"),
            ("reload", lambda *_: self._reload_current(), "F5"),
        ):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
            if accel:
                self.app.set_accels_for_action(f"win.{name}", [accel])

    def _current_import_view(self):
        page = self.split.get_content()
        return page if isinstance(page, ImportView) else None

    def _reload_current(self):
        page = self.split.get_content()
        if isinstance(page, ImportView):
            page.reload()
        elif isinstance(page, LibraryView):
            page.reload(scroll=None)

    def _about(self):
        about = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ICON,
            version="1.0.0", developer_name="A. Jünemann",
            comments="Fotos und Videos vom iPhone importieren – nach dem Vorbild von Apple Fotos.",
            license_type=Gtk.License.MIT_X11)
        about.present(self)

    def _shortcuts(self):
        from .dialogs import alert

        alert(
            self,
            "Tastenkürzel",
            "• Alle auswählen: Strg+A\n"
            "• Auswahl aufheben: Esc\n"
            "• Vorschau öffnen/schließen: Leertaste\n"
            "• In Vorschau blättern: ← / →\n"
            "• Vergrößern/Verkleinern: Strg+Mausrad oder Strg+±\n"
            "• Alle neuen Objekte importieren: Strg+I\n"
            "• Neu einlesen: F5\n"
            "• Neues Album: Strg+N\n"
            "• Einstellungen: Strg+,",
        )

    def open_preferences(self):
        PreferencesDialog(self).present(self)

    def toast(self, text, button=None, callback=None):
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=5)
        if button and callback:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: callback())
        self.toasts.add_toast(t)

    def rect(self, x, y):
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        return r

    # ------------------------------------------------------------ Zoom
    def make_zoom_scale(self):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 80, 320, 10)
        scale.set_draw_value(False)
        scale.set_size_request(120, -1)
        scale.set_value(self.zoom)
        scale.set_tooltip_text("Größe der Miniaturen (Strg + Mausrad)")
        scale.connect("value-changed", lambda s: self.set_zoom(s.get_value()))
        self._zoom_scales.append(scale)
        return scale

    def set_zoom(self, size):
        size = int(size)
        if size == self.zoom:
            return
        self.zoom = size
        for s in self._zoom_scales:
            if int(s.get_value()) != size:
                s.set_value(size)
        for v in list(self.views.values()) + list(self.import_views.values()):
            v.set_zoom(size)
        GLib.timeout_add(500, self._save_zoom, size)

    def _save_zoom(self, size):
        if size == self.zoom:
            self.config.set("thumb_size", size)
        return False

    # ----------------------------------------------------------- Mediathek
    def open_library(self):
        self.close_library()
        path = self.library_path
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if not os.path.isdir(parent):
                raise FileNotFoundError(parent)
            self.library = Library(path)
        except Exception as e:  # noqa: BLE001
            print("Mediathek nicht verfügbar:", e)
            self.library = None
        GLib.idle_add(self.update_banner)

    def close_library(self):
        if self.library is not None:
            self.library.close()
            self.library = None

    def set_library_path(self, path):
        self.library_path = path
        self.config.set("library_path", path)
        self.open_library()
        self.library_changed()
        for v in self.import_views.values():
            v.reload()
            v.refresh_albums()

    def is_busy(self):
        return any(v.importing for v in self.import_views.values())

    def library_changed(self, reload_views=True):
        """Nach Import/Album-Änderungen: Seitenleiste und Ansichten aktualisieren."""
        self.rebuild_sidebar()
        for v in self.import_views.values():
            v.refresh_albums()
        if reload_views:
            stale = [k for k in self.views if k != self._current_id]
            for k in stale:
                del self.views[k]
            cur = self.views.get(self._current_id)
            if cur is not None:
                if cur.album_id is not None and self.library and \
                        cur.album_id not in {a["id"] for a in self.library.albums()}:
                    return
                cur.reload(scroll=None)

    def update_banner(self):
        """Hinweis in der Mediathek, falls Altbestand nicht erfasst oder nicht einsortiert ist."""
        lib = self.library
        if lib is None:
            return False

        def work():
            rels = set(lib.all_rels())
            if _has_unindexed(lib.root, rels):
                return "index"
            unsorted = any(not DATE_DIR_RE.match(r) for r in rels)
            return "reorg" if unsorted and lib.get_meta("reorg_dismissed") != "1" else None

        def done(kind):
            view = self.views.get("library")
            if view is None:
                return
            if kind == "index":
                view.set_banner("Im Mediathek-Ordner liegen bereits Fotos. Jetzt erfassen, damit sie nicht "
                                "doppelt importiert werden?", "Erfassen",
                                lambda: maintenance.run_index(self, then=self.update_banner))
            elif kind == "reorg":
                view.set_banner("Einige Fotos liegen noch in alten DCIM-Ordnern (100APPLE …).",
                                "Einsortieren …", lambda: maintenance.run_reorganize(self))
            else:
                view.set_banner(None)

        run_in_thread(work, done)
        return False

    # ----------------------------------------------------------- Seitenleiste
    def rebuild_sidebar(self):
        selected = self._current_id
        self._rebuilding = True
        self.sidebar.remove_all()
        lib = self.library
        counts = lib.counts() if lib else {}
        rows = [SidebarRow("library", "Mediathek", "image-x-generic-symbolic", "Fotos", counts.get("all"))]
        for rid, title, icon, key in (
            ("videos", "Videos", "camera-video-symbolic", "video"),
            ("live", "Live Photos", "ios-photos-live-symbolic", "live"),
            ("raw", "RAW", "camera-photo-symbolic", "raw"),
            ("screenshots", "Bildschirmfotos", "computer-symbolic", "screenshot"),
        ):
            if counts.get(key):
                rows.append(SidebarRow(rid, title, icon, "Medientypen", counts[key]))
        rows.append(SidebarRow("imports", "Importe", "folder-download-symbolic", "Alben"))
        for al in lib.albums() if lib else []:
            rows.append(SidebarRow(f"album:{al['id']}", al["name"], "folder-pictures-symbolic", "Alben",
                                   al["count"]))
        for dev in self.devices.values():
            sub = None if dev.state == READY else STATE_TEXT.get(dev.state)
            rows.append(SidebarRow(f"device:{dev.udid}", dev.name, "phone-symbolic", "Geräte",
                                   subtitle=sub, eject=lambda u=dev.udid: self.eject(u)))
        self._rebuilding = True
        for r in rows:
            self.sidebar.append(r)
        self._rebuilding = False
        if selected:
            self.select_sidebar(selected, activate=False)

    def _sidebar_header(self, row, before):
        if before is None or before.section != row.section:
            if row.section == "Fotos":
                row.set_header(None)
                return
            lbl = Gtk.Label(label=row.section, xalign=0, margin_start=6, margin_top=12, margin_bottom=4)
            lbl.add_css_class("heading")
            lbl.add_css_class("dim-label")
            row.set_header(lbl)
        else:
            row.set_header(None)

    def select_sidebar(self, row_id, activate=True):
        row = self.sidebar.get_first_child()
        while row:
            if isinstance(row, SidebarRow) and row.row_id == row_id:
                if not activate:
                    self._rebuilding = True
                self.sidebar.select_row(row)
                self._rebuilding = False
                return True
            row = row.get_next_sibling()
        return False

    def _on_row_selected(self, _lb, row):
        if row is None or getattr(self, "_rebuilding", False):
            return
        if self._current_id and self._current_id.startswith("device:"):
            cur = self.import_views.get(self._current_id.split(":", 1)[1])
            if cur and cur.importing and row.row_id != self._current_id:
                self.toast("Bitte warte, bis der laufende Import abgeschlossen ist")
                self.select_sidebar(self._current_id, activate=False)
                return
        self.show_view(row.row_id)

    def show_view(self, row_id):
        self._current_id = row_id
        if row_id.startswith("device:"):
            udid = row_id.split(":", 1)[1]
            view = self.import_views.get(udid)
            if view is None and udid in self.devices:
                view = self.import_views[udid] = ImportView(self, self.devices[udid])
        else:
            view = self.views.get(row_id)
            if view is None:
                view = self.views[row_id] = LibraryView(self, row_id)
                if row_id == "library":
                    self.update_banner()
        if view is not None:
            self.split.set_content(view)
            self.split.set_show_content(True)

    def show_import(self, import_id):
        self.select_sidebar("imports")
        view = self.views.get("imports")
        if view:
            view.focus_import(import_id)

    def _new_album(self):
        if self.library is None:
            return
        from .dialogs import ask_text

        def create(name):
            album_id = self.library.create_album(name)
            self.library_changed(reload_views=False)
            self.select_sidebar(f"album:{album_id}")

        ask_text(self, "Neues Album", "Gib einen Namen für das neue Album ein.", "Erstellen", create)

    # ------------------------------------------------------------- Geräte
    def on_devices_changed(self, devices):
        old = self.devices
        self.devices = devices
        for udid in list(self.import_views):
            if udid not in devices:
                view = self.import_views[udid]
                if view.importing:
                    continue
                view.shutdown()
                del self.import_views[udid]
                if self._current_id == f"device:{udid}":
                    self._current_id = "library"
                    self.toast(f"„{old[udid].name if udid in old else 'iPhone'}“ wurde getrennt")
        for udid, dev in devices.items():
            if udid in self.import_views:
                self.import_views[udid].set_device(dev)
            if not dev.fake and dev.state == READY:
                self.config.set_device(udid, name=dev.name)
        self.rebuild_sidebar()
        if self._current_id and not self.select_sidebar(self._current_id, activate=False):
            self.select_sidebar("library")
        # Wie Apple Fotos: neu angeschlossenes Gerät direkt anzeigen
        for udid, dev in devices.items():
            if udid not in old and udid not in self._auto_selected:
                self._auto_selected.add(udid)
                if not self.is_busy():
                    self.select_sidebar(f"device:{udid}")
                break

    def eject(self, udid):
        view = self.import_views.get(udid)
        if view and view.importing:
            self.toast("Bitte warte, bis der Import abgeschlossen ist")
            return
        name = self.devices[udid].name if udid in self.devices else "iPhone"
        if view:
            view.shutdown()
            self.import_views.pop(udid, None)
        if self._current_id == f"device:{udid}":
            self._current_id = "library"
        self.app.monitor.eject(udid)
        self.toast(f"„{name}“ kann jetzt getrennt werden")

    def on_import_finished(self, view):
        if view.device.udid not in self.devices:
            self.on_devices_changed(self.devices)

    # ------------------------------------------------------------- Vorschau
    def open_preview(self, assets, asset):
        if not assets:
            return
        dlg = PreviewDialog(self, assets, asset)
        page = self.split.get_content()

        def closed(_d):
            grid = getattr(page, "grid", None)
            if grid is not None:
                cur = dlg.asset
                grid.select_only(cur)
                grid.scroll_to_asset(cur)
                grid.grab_focus()

        dlg.connect("closed", closed)
        dlg.present(self)

    # ------------------------------------------------------------- Beenden
    def _on_close(self, *_):
        if self.is_busy():
            from .dialogs import confirm

            def stop(_c):
                for v in self.import_views.values():
                    v.shutdown()
                GLib.timeout_add(300, self._really_close)

            confirm(self, "Import läuft noch", "Soll der Import gestoppt und das Fenster geschlossen werden? "
                    "Bereits importierte Objekte bleiben erhalten.", "Stoppen und schließen", stop,
                    destructive=True)
            return True
        self._cleanup()
        return False

    def _really_close(self):
        if self.is_busy():
            return True
        self._cleanup()
        self.destroy()
        return False

    def _cleanup(self):
        for v in self.import_views.values():
            v.shutdown()
        self.loader.stop()
        self.app.window_closed(self)


def _has_unindexed(root, known):
    """True, wenn im Mediathek-Ordner Mediendateien liegen, die nicht im Index sind."""
    root = str(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root)
        for f in filenames:
            if os.path.splitext(f)[1].lower() in MEDIA_EXT:
                rel = f if rel_dir == "." else f"{rel_dir}/{f}"
                if rel not in known:
                    return True
    return False
