"""Import-Ansicht für ein angeschlossenes iPhone – nach dem Vorbild von Apple Fotos."""

import threading

from gi.repository import Adw, Gio, GLib, Gtk

from ..device.monitor import CONNECTING, ERROR, LOCKED, READY, UNTRUSTED
from ..library.importer import run_import
from ..media.metadata import apply_metadata
from ..media.scanner import scan_dcim
from .dialogs import alert, ask_text
from .photogrid import PhotoGrid, Section
from .util import fmt_count, fmt_day, fmt_int, fmt_size, group_by_day, idle, run_in_thread

FILTERS = [("all", "Alle Objekte"), ("photo", "Fotos"), ("video", "Videos"), ("live", "Live Photos")]


def _match(asset, flt):
    if flt == "photo":
        return asset.kind in ("photo", "live")
    if flt in ("video", "live"):
        return asset.kind == flt
    return True


class ImportView(Adw.NavigationPage):
    def __init__(self, win, device):
        super().__init__(title=device.name, tag=f"device:{device.udid}")
        self.win = win
        self.device = device
        self.assets = None
        self.loading = False
        self.importing = False
        self.cancel_import = None
        self.cancel_load = threading.Event()
        self._album_ids = []

        self.toolbar = Adw.ToolbarView()
        self.set_child(self.toolbar)

        # ---- Kopfzeile
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=device.name, subtitle=device.subtitle)
        header.set_title_widget(self.window_title)

        menu = Gio.Menu()
        menu.append("Bereits importierte ausblenden", "imp.hide-imported")
        menu.append("Neu einlesen", "imp.reload")
        menu.append("iPhone auswerfen", "imp.eject")
        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                                  tooltip_text="Weitere Optionen")
        header.pack_end(menu_btn)

        self.filter_dd = Gtk.DropDown.new_from_strings([label for _, label in FILTERS])
        self.filter_dd.set_tooltip_text("Anzeigen")
        keys = [k for k, _ in FILTERS]
        cur = self.win.config.get("media_filter")
        self.filter_dd.set_selected(keys.index(cur) if cur in keys else 0)
        self.filter_dd.connect("notify::selected", self._on_filter)
        header.pack_end(self.filter_dd)

        header.pack_end(self.win.make_zoom_scale())
        self.toolbar.add_top_bar(header)

        # ---- Import-Leiste (wie bei Apple direkt unter der Titelleiste)
        bar = Gtk.Box(spacing=12)
        bar.add_css_class("import-bar")
        self.open_check = Gtk.CheckButton(label="Beim Anschließen öffnen")
        self.open_check.set_tooltip_text(
            "Diese App automatisch öffnen, wenn dieses iPhone angeschlossen wird")
        self.open_check.set_active(self.win.config.device(device.udid).get("open_on_connect", True)
                                   and self.win.app.autostart_enabled())
        self.open_check.connect("toggled", self._on_open_toggled)
        bar.append(self.open_check)

        bar.append(Gtk.Box(hexpand=True))
        bar.append(Gtk.Label(label="Album:"))
        self.album_dd = Gtk.DropDown(model=Gtk.StringList())
        self.album_dd.set_tooltip_text("Album, in das importiert wird")
        self._album_handler = self.album_dd.connect("notify::selected", self._on_album_selected)
        bar.append(self.album_dd)
        bar.append(Gtk.Box(hexpand=True))

        self.import_sel_btn = Gtk.Button(label="Auswahl importieren", sensitive=False)
        self.import_sel_btn.connect("clicked", lambda *_: self.import_selected())
        bar.append(self.import_sel_btn)
        self.import_all_btn = Gtk.Button(label="Alle neuen Objekte importieren", sensitive=False)
        self.import_all_btn.add_css_class("suggested-action")
        self.import_all_btn.connect("clicked", lambda *_: self.import_all())
        bar.append(self.import_all_btn)
        self.import_bar = bar
        self.toolbar.add_top_bar(bar)

        # ---- Inhalt
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.grid = PhotoGrid(win.loader, dim_imported=True, show_edit_badge=False)
        self.grid.set_zoom(self.win.zoom)
        self.grid.zoom_changed = self.win.set_zoom
        self.grid.on_selection_changed = self._on_selection
        self.grid.on_activate = lambda a: self.win.open_preview(self.grid.order, a)
        self.grid.on_context = self._on_context
        self.stack.add_named(self.grid, "grid")

        self.loading_page = Adw.StatusPage(title="Fotos und Videos werden gelesen …")
        self.loading_page.set_paintable(None)
        spinner = Adw.Spinner(width_request=48, height_request=48)
        self.loading_page.set_child(spinner)
        self.stack.add_named(self.loading_page, "loading")

        self.state_page = Adw.StatusPage()
        self.state_button = Gtk.Button(halign=Gtk.Align.CENTER)
        self.state_button.add_css_class("pill")
        self.state_button.add_css_class("suggested-action")
        self.state_button.connect("clicked", self._on_state_button)
        self.state_page.set_child(self.state_button)
        self.stack.add_named(self.state_page, "state")
        self.toolbar.set_content(self.stack)

        # ---- Fortschritt
        prog = Gtk.Box(spacing=12)
        prog.add_css_class("import-progress")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.prog_label = Gtk.Label(xalign=0, ellipsize=3)
        self.prog_bar = Gtk.ProgressBar()
        col.append(self.prog_label)
        col.append(self.prog_bar)
        prog.append(col)
        self.stop_btn = Gtk.Button(label="Importieren stoppen", valign=Gtk.Align.CENTER)
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.connect("clicked", self._on_stop)
        prog.append(self.stop_btn)
        self.progress_revealer = Gtk.Revealer(child=prog, reveal_child=False)
        self.toolbar.add_bottom_bar(self.progress_revealer)

        # ---- Aktionen
        group = Gio.SimpleActionGroup()
        hide = Gio.SimpleAction.new_stateful(
            "hide-imported", None, GLib.Variant.new_boolean(bool(self.win.config.get("hide_imported"))))
        hide.connect("change-state", self._on_hide_imported)
        group.add_action(hide)
        for name, cb in (("reload", lambda *_: self.reload()),
                         ("eject", lambda *_: self.win.eject(self.device.udid)),
                         ("import-selected", lambda *_: self.import_selected()),
                         ("preview", lambda *_: self._preview_current())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            group.add_action(a)
        self.actions = group
        self.insert_action_group("imp", group)

        self._state = None
        self.refresh_albums()
        self.set_device(device)

    # ---------------------------------------------------------------- Zustand
    def set_device(self, device):
        self.device = device
        self.set_title(device.name)
        self.window_title.set_title(device.name)
        if device.state == READY:
            if self.assets is None and not self.loading:
                self.load()
            elif self.assets is not None:
                self._show_content()
        else:
            self.assets = None if device.state != READY else self.assets
            self._show_state(device.state, device.message)
        self._update_buttons()

    def _show_state(self, state, message=""):
        p = self.state_page
        self.state_button.set_visible(True)
        if state == CONNECTING:
            p.set_icon_name("phone-symbolic")
            p.set_title("Verbindung wird hergestellt …")
            p.set_description("Einen Moment bitte.")
            self.state_button.set_visible(False)
        elif state == UNTRUSTED:
            p.set_icon_name("phone-symbolic")
            p.set_title("Diesem Computer vertrauen?")
            p.set_description(
                "Entsperre dein iPhone und tippe in der Meldung „Diesem Computer vertrauen?“ "
                "auf <b>Vertrauen</b>. Gib anschließend deinen Gerätecode ein.")
            self.state_button.set_label("Erneut versuchen")
        elif state == LOCKED:
            p.set_icon_name("system-lock-screen-symbolic")
            p.set_title("iPhone entsperren")
            p.set_description("Entsperre dein iPhone, damit auf Fotos und Videos zugegriffen werden kann.")
            self.state_button.set_label("Erneut versuchen")
        elif state == ERROR:
            p.set_icon_name("dialog-warning-symbolic")
            p.set_title("Verbindung fehlgeschlagen")
            p.set_description(GLib.markup_escape_text(message or "Unbekannter Fehler.")
                              + "\n\nPrüfe das Kabel, entsperre das iPhone und versuche es erneut.")
            self.state_button.set_label("Erneut versuchen")
        elif state == "empty":
            p.set_icon_name("image-x-generic-symbolic")
            p.set_title("Keine Fotos oder Videos")
            p.set_description("Auf diesem iPhone wurden keine Fotos oder Videos gefunden.")
            self.state_button.set_label("Neu einlesen")
        elif state == "no-library":
            p.set_icon_name("drive-harddisk-symbolic")
            p.set_title("Mediathek nicht verfügbar")
            p.set_description(
                f"Der Mediathek-Ordner <b>{GLib.markup_escape_text(str(self.win.library_path))}</b> "
                "ist nicht erreichbar. Ist das Laufwerk eingehängt?")
            self.state_button.set_label("Einstellungen öffnen")
        self._state = state
        self.stack.set_visible_child_name("state")

    def _on_state_button(self, _btn):
        if self._state == "empty":
            self.reload()
        elif self._state == "no-library":
            self.win.open_preferences()
        else:
            self.win.app.monitor.refresh()

    # ------------------------------------------------------------- Laden
    def reload(self):
        if self.importing:
            return
        self.assets = None
        if self.device.state == READY:
            self.load()

    def load(self):
        if self.win.library is None:
            self._show_state("no-library")
            return
        self.loading = True
        self.cancel_load.clear()
        self.loading_page.set_title("Fotos und Videos werden gelesen …")
        self.loading_page.set_description("")
        self.stack.set_visible_child_name("loading")
        self._update_buttons()
        mountpoint = self.device.mountpoint
        lib = self.win.library
        cache = self.win.metacache
        cancel = self.cancel_load

        def progress_scan(n):
            idle(self.loading_page.set_description, f"{fmt_int(n)} Dateien gefunden")

        def progress_meta(done, total):
            idle(self.loading_page.set_description,
                 f"Aufnahmedaten werden gelesen: {fmt_int(done)} von {fmt_int(total)}")

        def work():
            assets = scan_dcim(mountpoint, progress=progress_scan)
            if assets is None:
                return None
            lib.mark_status(assets)
            need = [a for a in assets if a.date is None]
            apply_metadata(need, progress=progress_meta, cancel=cancel, cache=cache)
            return assets

        run_in_thread(work, self._loaded, self._load_failed)

    def _loaded(self, assets):
        self.loading = False
        if assets is None:
            self._show_state(LOCKED)
            return
        self.assets = assets
        self._show_content(scroll="top")

    def _load_failed(self, err):
        self.loading = False
        self._show_state(ERROR, str(err))

    # ---------------------------------------------------------- Darstellung
    def _show_content(self, scroll=None):
        if not self.assets:
            self._show_state("empty")
            self._update_buttons()
            return
        self.rebuild(scroll=scroll)
        self.stack.set_visible_child_name("grid")
        GLib.idle_add(self.grid.grab_focus)

    def rebuild(self, scroll=None):
        if self.assets is None:
            return
        flt = [k for k, _ in FILTERS][self.filter_dd.get_selected()]
        hide = bool(self.win.config.get("hide_imported"))
        shown = [a for a in self.assets if _match(a, flt)]
        new = [a for a in shown if a.status == "new"]
        old = [a for a in shown if a.status == "imported"]
        sections = []
        if new:
            sections.append(Section("Neue Objekte", f"{fmt_count(len(new))} · {fmt_size(sum(a.size for a in new))}",
                                    [], level=1, select_items=new, key="new"))
            for day, items in group_by_day(new, descending=True):
                sections.append(Section(fmt_day(day), fmt_count(len(items)), items, key=f"new-{day}"))
        else:
            sections.append(Section("Keine neuen Objekte",
                                    "Alle Fotos und Videos auf diesem iPhone wurden bereits importiert.",
                                    [], level=1, selectable_header=False, key="new"))
        if old and not hide:
            sections.append(Section("Bereits importiert", fmt_count(len(old)), [], level=1,
                                    select_items=old, key="old"))
            for day, items in group_by_day(old, descending=True):
                sections.append(Section(fmt_day(day), fmt_count(len(items)), items, key=f"old-{day}"))
        self.grid.set_sections(sections, scroll=scroll)
        total_new = sum(1 for a in self.assets if a.status == "new")
        self.window_title.set_subtitle(
            f"{fmt_count(total_new, 'neues Objekt', 'neue Objekte')} · {fmt_count(len(self.assets))} insgesamt")
        self._update_buttons()

    def _on_filter(self, dd, _pspec):
        self.win.config.set("media_filter", [k for k, _ in FILTERS][dd.get_selected()])
        self.rebuild(scroll="top")

    def _on_hide_imported(self, action, value):
        action.set_state(value)
        self.win.config.set("hide_imported", value.get_boolean())
        self.rebuild()

    def _on_selection(self, selected):
        self._update_buttons()

    def _update_buttons(self):
        ready = self.assets is not None and not self.importing and not self.loading \
            and self.win.library is not None
        sel_new = [a for a in self.grid.selected_assets() if a.status == "new"] if ready else []
        all_new = [a for a in (self.assets or []) if a.status == "new"]
        self.import_sel_btn.set_sensitive(bool(sel_new))
        self.import_sel_btn.set_label(
            f"{fmt_int(len(sel_new))} importieren" if sel_new else "Auswahl importieren")
        self.import_all_btn.set_sensitive(ready and bool(all_new))
        self.album_dd.set_sensitive(not self.importing)
        self.actions.lookup_action("import-selected").set_enabled(bool(sel_new))

    def set_zoom(self, size):
        self.grid.set_zoom(size)

    def _preview_current(self):
        cur = self.grid.current()
        if cur:
            self.win.open_preview(self.grid.order, cur)

    def _on_context(self, asset, x, y):
        menu = Gio.Menu()
        menu.append("Vorschau", "imp.preview")
        menu.append("Auswahl importieren", "imp.import-selected")
        pop = Gtk.PopoverMenu(menu_model=menu, has_arrow=False)
        pop.set_parent(self.grid)
        rect = self.win.rect(x, y)
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    # -------------------------------------------------------------- Alben
    def refresh_albums(self):
        lib = self.win.library
        albums = lib.albums() if lib else []
        model = Gtk.StringList()
        model.append("Ohne")
        self._album_ids = [None]
        for al in albums:
            model.append(al["name"])
            self._album_ids.append(al["id"])
        model.append("Neues Album …")
        self.album_dd.handler_block(self._album_handler)
        self.album_dd.set_model(model)
        last = self.win.config.get("last_album")
        self.album_dd.set_selected(self._album_ids.index(last) if last in self._album_ids else 0)
        self.album_dd.handler_unblock(self._album_handler)
        self._prev_album = self.album_dd.get_selected()

    def current_album_id(self):
        i = self.album_dd.get_selected()
        return self._album_ids[i] if i < len(self._album_ids) else None

    def _on_album_selected(self, dd, _pspec):
        i = dd.get_selected()
        if i == len(self._album_ids):  # „Neues Album …“
            dd.handler_block(self._album_handler)
            dd.set_selected(self._prev_album)
            dd.handler_unblock(self._album_handler)
            ask_text(self.win, "Neues Album", "Gib einen Namen für das neue Album ein.", "Erstellen",
                     self._create_album)
            return
        self._prev_album = i
        self.win.config.set("last_album", self.current_album_id())

    def _create_album(self, name):
        album_id = self.win.library.create_album(name)
        self.win.config.set("last_album", album_id)
        self.win.library_changed()

    # ------------------------------------------------------------- Import
    def _on_open_toggled(self, check):
        active = check.get_active()
        self.win.config.set_device(self.device.udid, open_on_connect=active, name=self.device.name)
        if active and not self.win.app.autostart_enabled():
            self.win.app.set_autostart(True)
            self.win.toast("Die App wartet ab der nächsten Anmeldung im Hintergrund auf dein iPhone.")

    def import_selected(self):
        sel = [a for a in self.grid.selected_assets() if a.status == "new"]
        if sel:
            self.start_import(sel)

    def import_all(self):
        new = [a for a in (self.assets or []) if a.status == "new"]
        if new:
            self.start_import(new)

    def start_import(self, assets):
        if self.importing or self.win.library is None:
            return
        self.importing = True
        self.cancel_import = threading.Event()
        self.stop_btn.set_sensitive(True)
        self.prog_bar.set_fraction(0)
        self.prog_label.set_label(f"Import von {fmt_count(len(assets))} wird vorbereitet …")
        self.progress_revealer.set_reveal_child(True)
        self._update_buttons()
        lib = self.win.library
        album_id = self.current_album_id()
        dev = self.device
        cancel = self.cancel_import

        def progress(done, total, done_b, total_b, name):
            idle(self._on_progress, done, total, done_b, total_b, name)

        def work():
            return run_import(lib, assets, album_id=album_id, udid=None if dev.fake else dev.udid,
                              device_name=dev.name, cancel=cancel, progress=progress)

        run_in_thread(work, self._import_done, self._import_failed)

    def _on_progress(self, done, total, done_b, total_b, name):
        frac = done_b / total_b if total_b else (done / total if total else 1)
        self.prog_bar.set_fraction(frac)
        if name:
            self.prog_label.set_label(
                f"{fmt_int(min(done + 1, total))} von {fmt_int(total)} wird importiert – {name}  "
                f"({fmt_size(done_b)} von {fmt_size(total_b)})")

    def _on_stop(self, btn):
        if self.cancel_import:
            self.cancel_import.set()
            btn.set_sensitive(False)
            self.prog_label.set_label("Import wird gestoppt …")

    def _import_failed(self, err):
        self.importing = False
        self.progress_revealer.set_reveal_child(False)
        self._update_buttons()
        alert(self.win, "Import fehlgeschlagen", str(err))

    def _import_done(self, result):
        self.importing = False
        self.progress_revealer.set_reveal_child(False)
        if self.win.library and self.assets:
            self.win.library.mark_status(self.assets)
        self.grid.clear_selection()
        self.rebuild()
        self.win.library_changed()
        if result.fatal:
            body = result.fatal
            if result.imported:
                body += f"\n\nBis dahin wurden {fmt_count(result.imported)} importiert."
            alert(self.win, "Import abgebrochen", body)
        elif result.failed:
            lines = "\n".join(f"• {n}: {msg}" for n, msg in result.failed[:10])
            more = f"\n… und {len(result.failed) - 10} weitere" if len(result.failed) > 10 else ""
            alert(self.win, f"{fmt_count(len(result.failed))} konnten nicht importiert werden",
                  lines + more)
        if result.imported:
            text = (f"Import gestoppt – {fmt_count(result.imported)} importiert" if result.cancelled
                    else f"{fmt_count(result.imported)} importiert")
            self.win.toast(text, "Anzeigen", lambda: self.win.show_import(result.import_id))
        elif result.cancelled:
            self.win.toast("Import gestoppt")
        self.win.on_import_finished(self)
        self._update_buttons()

    def shutdown(self):
        self.cancel_load.set()
        if self.cancel_import:
            self.cancel_import.set()
