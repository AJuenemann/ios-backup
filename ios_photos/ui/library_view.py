"""Mediathek-Ansicht: Jahre / Monate / Tage / Alle Fotos, Medientypen, Alben und Importe."""

from gi.repository import Adw, Gio, GLib, Gtk

from .dialogs import ProgressDialog, alert, ask_text, confirm
from .photogrid import PhotoGrid, Section
from .util import (fmt_count, fmt_datetime, fmt_day, fmt_int, group_by_day, MONTHS,
                   idle, run_in_thread)

MODES = [("years", "Jahre"), ("months", "Monate"), ("days", "Tage"), ("all", "Alle Fotos")]

SOURCES = {
    "library": ("Mediathek", {}),
    "videos": ("Videos", {"kind": "video"}),
    "live": ("Live Photos", {"kind": "live"}),
    "raw": ("RAW", {"raw": True}),
    "screenshots": ("Bildschirmfotos", {"screenshot": True}),
    "hidden": ("Ausgeblendet", {"hidden": True}),
    "imports": ("Importe", {}),
}


class Card(Gtk.FlowBoxChild):
    """Große Kachel für Jahres-/Monatsübersicht."""

    def __init__(self, loader, title, subtitle, asset, payload):
        super().__init__()
        self.payload = payload
        self.loader = loader
        self.asset = asset
        overlay = Gtk.Overlay()
        overlay.add_css_class("year-card")
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        # Feste Fläche bestimmt die Kartengröße; das Bild füllt sie (sonst wären Hochkantbilder zu hoch).
        overlay.set_child(Gtk.Box(width_request=300, height_request=200))
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.picture.add_css_class("card-picture")
        overlay.add_overlay(self.picture)
        shade = Gtk.Box()
        shade.add_css_class("card-shade")
        overlay.add_overlay(shade)
        caption = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START,
                          valign=Gtk.Align.START)
        caption.add_css_class("card-caption")
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("card-title")
        s = Gtk.Label(label=subtitle, xalign=0)
        s.add_css_class("card-subtitle")
        caption.append(t)
        caption.append(s)
        overlay.add_overlay(caption)
        self.set_child(overlay)
        f = asset.display
        tex = loader.request(f, self._on_tex)
        if tex is not None:
            self.picture.set_paintable(tex)

    def _on_tex(self, tex):
        if tex is not None:
            self.picture.set_paintable(tex)
        return False


class LibraryView(Adw.NavigationPage):
    def __init__(self, win, source_id):
        self.win = win
        self.source_id = source_id
        self.album = None
        if source_id.startswith("album:"):
            self.album_id = int(source_id.split(":", 1)[1])
            title, self.query = "Album", {"album_id": self.album_id}
        else:
            self.album_id = None
            title, self.query = SOURCES.get(source_id, SOURCES["library"])
        super().__init__(title=title, tag=source_id)
        self.assets = []
        self.mode = "days" if source_id != "library" else win.config.get("library_mode") or "days"
        self._loaded_once = False
        self._focus_import = None
        self._trashing = False

        self.toolbar = Adw.ToolbarView()
        self.set_child(self.toolbar)
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=title)
        if source_id == "library":
            self.toggle = Adw.ToggleGroup()
            for key, label in MODES:
                self.toggle.add(Adw.Toggle(name=key, label=label))
            self.toggle.set_active_name(self.mode)
            self.toggle.connect("notify::active-name", self._on_mode)
            header.set_title_widget(self.toggle)
        else:
            header.set_title_widget(self.window_title)

        self.menu = Gio.Menu()
        if self.album_id is not None:
            self.menu.append("Album umbenennen …", "lib.rename-album")
            self.menu.append("Album löschen …", "lib.delete-album")
        self.menu.append("Alle auswählen", "lib.select-all")
        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=self.menu,
                                  tooltip_text="Weitere Optionen")
        header.pack_end(menu_btn)
        header.pack_end(Gtk.Button(label="Alle auswählen", action_name="lib.select-all",
                                   tooltip_text="Alle angezeigten Objekte auswählen (Strg+A)"))
        header.pack_end(win.make_zoom_scale())
        self.toolbar.add_top_bar(header)

        self.banner = Adw.Banner(revealed=False)
        self._banner_cb = None
        self.banner.connect("button-clicked", lambda *_: self._banner_cb and self._banner_cb())
        self.toolbar.add_top_bar(self.banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.grid = PhotoGrid(win.loader)
        self.grid.set_zoom(win.zoom)
        self.grid.zoom_changed = win.set_zoom
        self.grid.on_activate = lambda a: win.open_preview(self.grid.order, a)
        self.grid.on_context = self._on_context
        self.grid.on_selection_changed = self._on_selection
        self.stack.add_named(self.grid, "grid")

        self.flow = Gtk.FlowBox(homogeneous=True, min_children_per_line=1, max_children_per_line=5,
                                selection_mode=Gtk.SelectionMode.NONE, row_spacing=14,
                                column_spacing=14, margin_top=18, margin_bottom=18,
                                margin_start=18, margin_end=18, valign=Gtk.Align.START)
        self.flow.connect("child-activated", self._on_card)
        self.flow_scroll = Gtk.ScrolledWindow(child=self.flow, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self.flow_scroll, "cards")

        self.empty = Adw.StatusPage(icon_name="image-x-generic-symbolic")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(Adw.Spinner(), "loading")
        self.toolbar.set_content(self.stack)

        selection = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8,
                            margin_start=18, margin_end=18)
        self.selection_label = Gtk.Label(xalign=0, hexpand=True)
        selection.append(self.selection_label)
        selection.append(Gtk.Button(label="In den Papierkorb …", action_name="lib.trash"))
        self.selection_bar = Gtk.Revealer(child=selection)
        self.toolbar.add_bottom_bar(self.selection_bar)

        group = Gio.SimpleActionGroup()
        for name, cb in (
            ("preview", lambda *_: self._preview()),
            ("open-external", lambda *_: self._open_external()),
            ("show-in-files", lambda *_: self._show_in_files()),
            ("unhide", lambda *_: self._unhide()),
            ("trash", lambda *_: self._trash_selected()),
            ("add-to-new-album", lambda *_: self._add_to_new_album()),
            ("remove-from-album", lambda *_: self._remove_from_album()),
            ("rename-album", lambda *_: self._rename_album()),
            ("delete-album", lambda *_: self._delete_album()),
            ("select-all", lambda *_: self.select_all()),
        ):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            group.add_action(a)
        add = Gio.SimpleAction.new("add-to-album", GLib.VariantType.new("x"))
        add.connect("activate", lambda _a, v: self._add_to_album(v.get_int64()))
        group.add_action(add)
        self.actions = group
        self.insert_action_group("lib", group)
        self._on_selection([])
        self.reload()

    # ------------------------------------------------------------- Daten
    def reload(self, scroll="bottom"):
        lib = self.win.library
        if lib is None:
            self._show_empty("Mediathek nicht verfügbar",
                             f"Der Ordner {self.win.library_path} ist nicht erreichbar.")
            return
        if self.album_id is not None:
            albums = {a["id"]: a for a in lib.albums()}
            self.album = albums.get(self.album_id)
            name = self.album["name"] if self.album else "Album"
            self.set_title(name)
            self.window_title.set_title(name)
        if not self._loaded_once:
            self.stack.set_visible_child_name("loading")
        query = dict(self.query)

        def work():
            return lib.query(**query), (lib.imports() if self.source_id == "imports" else None)

        def done(res):
            self.assets, self.import_list = res
            self._loaded_once = True
            self.rebuild(scroll=scroll if self._focus_import is None else None)
            if self._focus_import is not None:
                key = f"import-{self._focus_import}"
                self._focus_import = None
                GLib.idle_add(lambda: self.grid.scroll_to_section(key) and False)

        run_in_thread(work, done)

    def focus_import(self, import_id):
        self._focus_import = import_id
        if self._loaded_once:
            self.grid.scroll_to_section(f"import-{import_id}")
            self._focus_import = None

    def _show_empty(self, title, desc):
        self.empty.set_title(title)
        self.empty.set_description(desc)
        self.stack.set_visible_child_name("empty")

    def rebuild(self, scroll=None):
        n = len(self.assets)
        self.window_title.set_subtitle(fmt_count(n))
        if not n:
            self.grid.set_sections([])
            if self.source_id == "library":
                self._show_empty("Noch keine Fotos",
                                 "Schließe dein iPhone per USB an, um Fotos und Videos zu importieren.")
            elif self.album_id is not None:
                self._show_empty("Album ist leer",
                                 "Wähle beim Import dieses Album aus oder füge Fotos über das "
                                 "Kontextmenü der Mediathek hinzu.")
            else:
                self._show_empty("Keine Objekte", "Hier gibt es noch nichts zu sehen.")
            return
        if self.source_id == "imports":
            self._build_imports(scroll)
        elif self.mode in ("years", "months"):
            self._build_cards()
        elif self.mode == "all":
            self.grid.set_sections([Section("", "", self.assets, key="all")], scroll=scroll)
            self.stack.set_visible_child_name("grid")
        else:
            sections = [Section(fmt_day(d), fmt_count(len(items)), items, key=f"day-{d}")
                        for d, items in group_by_day(self.assets)]
            self.grid.set_sections(sections, scroll=scroll)
            self.stack.set_visible_child_name("grid")

    def _build_imports(self, scroll):
        by_imp = {}
        for a in self.assets:
            if a.extra.get("import_id") is not None:
                by_imp.setdefault(a.extra["import_id"], []).append(a)
        sections = []
        for imp in self.import_list or []:
            items = by_imp.get(imp["id"], [])
            if items:
                sub = " · ".join(p for p in (imp.get("device") or "", fmt_count(len(items))) if p)
                sections.append(Section(fmt_datetime(imp["started_at"]), sub, items, level=2,
                                        key=f"import-{imp['id']}"))
        if not sections:
            self._show_empty("Noch keine Importe", "Importierte Fotos erscheinen hier, gruppiert nach Import.")
            return
        self.window_title.set_subtitle(fmt_count(len(sections), "Import", "Importe"))
        self.grid.set_sections(sections, scroll="top" if scroll else None)
        self.stack.set_visible_child_name("grid")

    def _build_cards(self):
        child = self.flow.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        groups = {}
        for a in self.assets:
            d = a.sort_date
            key = d.year if self.mode == "years" else (d.year, d.month)
            groups.setdefault(key, []).append(a)
        for key in sorted(groups):
            items = groups[key]
            pick = items[len(items) // 2]
            if self.mode == "years":
                card = Card(self.win.loader, str(key), fmt_count(len(items)), pick, key)
            else:
                card = Card(self.win.loader, MONTHS[key[1] - 1], f"{key[0]} · {fmt_count(len(items))}",
                            pick, key)
            self.flow.append(card)
        self.stack.set_visible_child_name("cards")
        GLib.idle_add(self._cards_to_bottom)

    def _cards_to_bottom(self):
        adj = self.flow_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _on_card(self, _flow, card):
        if self.mode == "years":
            year = card.payload
            self._set_mode("months")
            GLib.idle_add(self._scroll_cards_to, year)
        else:
            year, month = card.payload
            first = next((a for a in self.assets
                          if a.sort_date.year == year and a.sort_date.month == month), None)
            self._set_mode("days", scroll=None)
            if first:
                self.grid.scroll_to_section(f"day-{first.sort_date.date()}")

    def _scroll_cards_to(self, year):
        child = self.flow.get_first_child()
        while child:
            if isinstance(child.payload, tuple) and child.payload[0] == year:
                ok, rect = child.compute_bounds(self.flow)
                if ok:
                    self.flow_scroll.get_vadjustment().set_value(rect.get_y())
                break
            child = child.get_next_sibling()
        return False

    def _set_mode(self, mode, scroll="bottom"):
        self.mode = mode
        if hasattr(self, "toggle"):
            self.toggle.handler_block_by_func(self._on_mode)
            self.toggle.set_active_name(mode)
            self.toggle.handler_unblock_by_func(self._on_mode)
        self.win.config.set("library_mode", mode)
        self.rebuild(scroll=scroll)

    def _on_mode(self, toggle, _pspec):
        self._set_mode(toggle.get_active_name())

    def set_zoom(self, size):
        self.grid.set_zoom(size)

    def set_banner(self, title=None, button=None, callback=None):
        if title:
            self.banner.set_title(title)
            self.banner.set_button_label(button)
            self._banner_cb = callback
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    # ------------------------------------------------------------ Aktionen
    def select_all(self):
        if not self.assets:
            return
        if self.mode in ("years", "months"):
            self._set_mode("all")
        self.grid.select_all()

    def _on_selection(self, selected):
        n = len(selected)
        self.selection_label.set_label(f"{fmt_count(n)} ausgewählt")
        self.selection_bar.set_reveal_child(bool(n))
        self.actions.lookup_action("trash").set_enabled(
            bool(n) and not self._trashing and not self.win.is_busy())

    def _on_context(self, asset, x, y):
        menu = Gio.Menu()
        first = Gio.Menu()
        first.append("Vorschau", "lib.preview")
        first.append("Mit Standardprogramm öffnen", "lib.open-external")
        first.append("Im Dateimanager zeigen", "lib.show-in-files")
        first.append("In den Papierkorb …", "lib.trash")
        if self.source_id == "hidden":
            first.append("Einblenden", "lib.unhide")
        menu.append_section(None, first)
        if self.source_id == "hidden":
            pop = Gtk.PopoverMenu(menu_model=menu, has_arrow=False)
            pop.set_parent(self.grid)
            pop.set_pointing_to(self.win.rect(x, y))
            pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
            pop.popup()
            return
        albums = Gio.Menu()
        lib = self.win.library
        for al in lib.albums() if lib else []:
            if al["id"] != self.album_id:
                item = Gio.MenuItem.new(al["name"], None)
                item.set_action_and_target_value("lib.add-to-album", GLib.Variant.new_int64(al["id"]))
                albums.append_item(item)
        albums.append("Neues Album …", "lib.add-to-new-album")
        n = len(self.grid.selected)
        menu.append_submenu(f"{fmt_int(n)} zu Album hinzufügen" if n > 1 else "Zu Album hinzufügen",
                            albums)
        if self.album_id is not None:
            sec = Gio.Menu()
            sec.append("Aus Album entfernen", "lib.remove-from-album")
            menu.append_section(None, sec)
        pop = Gtk.PopoverMenu(menu_model=menu, has_arrow=False)
        pop.set_parent(self.grid)
        pop.set_pointing_to(self.win.rect(x, y))
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _ids(self):
        return [a.extra["id"] for a in self.grid.selected_assets()]

    def _preview(self):
        cur = self.grid.current()
        if cur:
            self.win.open_preview(self.grid.order, cur)

    def _open_external(self):
        cur = self.grid.current()
        if cur:
            Gtk.FileLauncher.new(Gio.File.new_for_path(cur.display.path)).launch(self.win, None, None)

    def _show_in_files(self):
        cur = self.grid.current()
        if cur:
            Gtk.FileLauncher.new(Gio.File.new_for_path(cur.primary.path)).open_containing_folder(
                self.win, None, None)

    def _unhide(self):
        ids = self._ids()
        if ids:
            self.win.library.set_hidden(ids, False)
            self.win.toast(f"{fmt_count(len(ids))} eingeblendet")
            self.win.library_changed()
            self.win.select_sidebar("library")

    def _trash_selected(self):
        ids = self._ids()
        lib = self.win.library
        if not ids or lib is None or self._trashing:
            return
        if self.win.is_busy():
            self.win.toast("Bitte warte, bis der laufende Import abgeschlossen ist")
            return

        def trash(_checked):
            if self.win.library is not lib or self.win.is_busy():
                self.win.toast("Die Mediathek hat sich geändert oder ein Import läuft. Bitte erneut versuchen.")
                return
            self._trashing = True
            self._on_selection(self.grid.selected)
            progress = ProgressDialog(
                "In den Papierkorb verschieben",
                text=f"{fmt_count(len(ids))} werden vorbereitet …",
            )
            progress.cancel_button.set_tooltip_text(
                "Stoppt nach dem aktuellen Objekt. Bereits verschobene Dateien bleiben im Papierkorb.")
            self.trash_cancel = progress.cancel_event
            progress.present(self.win)

            def update(done, total, name):
                if progress.cancel_event.is_set():
                    progress.update(
                        text="Wird nach dem aktuellen Objekt gestoppt …",
                        detail=f"{fmt_int(done)} von {fmt_int(total)} Dateien bearbeitet",
                    )
                else:
                    progress.update(
                        fraction=done / total if total else 1,
                        text=f"{fmt_int(done)} von {fmt_int(total)} Dateien bearbeitet",
                        detail=f"{name}\nDatei wird verschoben; Index wird in Gruppen gespeichert. "
                               "Bei langsamen Laufwerken kann dies dauern.",
                    )

            def finish():
                self._trashing = False
                progress.finish()
                self.grid.clear_selection()
                self.win.library_changed()
                for view in self.win.import_views.values():
                    view.reload()

            def done(result):
                completed, errors = result
                finish()
                if completed:
                    self.win.toast(f"{fmt_count(completed)} in den Papierkorb verschoben")
                if progress.cancel_event.is_set():
                    self.win.toast("Vorgang gestoppt – bereits verschobene Dateien bleiben im Papierkorb")
                if errors:
                    details = "\n".join(f"• {name}: {message}" for name, message in errors[:10])
                    if len(errors) > 10:
                        details += f"\n… und {len(errors) - 10} weitere Fehler"
                    alert(self.win, "Nicht alle Dateien verschoben",
                          "Einige Dateien konnten nicht in den Papierkorb verschoben werden. "
                          "Bereits verschobene Dateien liegen im System-Papierkorb; "
                          "übrige Dateien bleiben in der Mediathek.\n\n" + details)

            def failed(error):
                finish()
                alert(self.win, "Verschieben fehlgeschlagen", str(error))

            run_in_thread(
                lambda: lib.trash_assets(
                    ids, progress=lambda *args: idle(update, *args), cancel=progress.cancel_event),
                done, failed,
            )

        dialog = confirm(
            self.win, f"{fmt_count(len(ids))} in den Papierkorb verschieben?",
            "Die ausgewählten Objekte werden aus der lokalen Mediathek und allen Alben entfernt. "
            "Alle zugehörigen Dateien, auch Live-Photo-Videos und Begleitdateien, werden in den "
            "System-Papierkorb verschoben.\n\n"
            "Mehrere Dateien werden dort in einem gemeinsamen Ordner mit ihrer bisherigen "
            "Unterordnerstruktur abgelegt.\n\n"
            "Die Originale auf dem iPhone bleiben unverändert. Nicht mehr lokal vorhandene "
            "Objekte können beim nächsten Import erneut importiert werden.",
            "In den Papierkorb", trash, destructive=True,
        )
        dialog.set_default_response("cancel")

    def _add_to_album(self, album_id):
        ids = self._ids()
        if ids:
            self.win.library.add_to_album(album_id, ids)
            name = next((a["name"] for a in self.win.library.albums() if a["id"] == album_id), "")
            self.win.toast(f"{fmt_count(len(ids))} zu „{name}“ hinzugefügt")
            self.win.library_changed(reload_views=False)

    def _add_to_new_album(self):
        ids = self._ids()

        def create(name):
            album_id = self.win.library.create_album(name)
            self.win.library.add_to_album(album_id, ids)
            self.win.toast(f"Album „{name}“ mit {fmt_count(len(ids))} erstellt")
            self.win.library_changed(reload_views=False)

        ask_text(self.win, "Neues Album", "Gib einen Namen für das neue Album ein.", "Erstellen", create)

    def _remove_from_album(self):
        ids = self._ids()
        if ids and self.album_id is not None:
            self.win.library.remove_from_album(self.album_id, ids)
            self.win.toast(f"{fmt_count(len(ids))} aus dem Album entfernt (bleiben in der Mediathek)")
            self.win.library_changed()

    def _rename_album(self):
        if not self.album:
            return

        def rename(name):
            self.win.library.rename_album(self.album_id, name)
            self.win.library_changed()

        ask_text(self.win, "Album umbenennen", "", "Umbenennen", rename, initial=self.album["name"])

    def _delete_album(self):
        if not self.album:
            return

        def delete(_checked):
            self.win.library.delete_album(self.album_id)
            self.win.library_changed()
            self.win.select_sidebar("library")

        confirm(self.win, f"Album „{self.album['name']}“ löschen?",
                "Die Fotos bleiben in der Mediathek erhalten – nur das Album wird entfernt.",
                "Löschen", delete, destructive=True)
