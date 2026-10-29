"""Virtualisiertes Foto-Raster mit Abschnitts-/Tages-Überschriften (wie in Apple Fotos).

Technik: Gtk.ListView, deren Zeilen entweder eine Überschrift oder eine Reihe quadratischer
Kacheln sind. Die Spaltenzahl ergibt sich aus Breite und Zoomstufe.
"""

from dataclasses import dataclass, field

from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gtk

from .util import fmt_duration

GAP = 3
PAD = 16


@dataclass
class Section:
    title: str = ""
    subtitle: str = ""
    items: list = field(default_factory=list)
    level: int = 2               # 1 = großer Bereich („Neue Objekte“), 2 = Tag
    selectable_header: bool = True
    key: str = ""
    select_items: list | None = None   # Elemente für „Auswählen“ (Standard: items)

    @property
    def selection_scope(self):
        return self.select_items if self.select_items is not None else self.items


class RowItem(GObject.Object):
    def __init__(self, kind, section=None, items=None, index=0):
        super().__init__()
        self.kind = kind          # "header" | "cells" | "spacer"
        self.section = section
        self.items = items or []
        self.index = index        # Position des ersten Elements in der Gesamtreihenfolge


class ThumbCell(Gtk.Widget):
    __gtype_name__ = "IosPhotosThumbCell"

    def __init__(self, grid):
        super().__init__()
        self.grid = grid
        self.asset = None
        self._texture_cb = None
        self._size = 150
        self.add_css_class("thumb-cell")
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self.overlay = Gtk.Overlay()
        self.overlay.set_parent(self)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.picture.add_css_class("thumb-picture")
        self.overlay.set_child(self.picture)

        self.placeholder = Gtk.Image(icon_name="image-x-generic-symbolic", pixel_size=32)
        self.placeholder.add_css_class("dim-label")
        self.overlay.add_overlay(self.placeholder)

        self.badge_tl = Gtk.Box(spacing=4, halign=Gtk.Align.START, valign=Gtk.Align.START)
        self.badge_tl.add_css_class("thumb-badges")
        self.live_icon = Gtk.Image(icon_name="ios-photos-live-symbolic")
        self.live_icon.set_tooltip_text("Live Photo")
        self.raw_label = Gtk.Label(label="RAW")
        self.raw_label.add_css_class("thumb-tag")
        self.edit_icon = Gtk.Image(icon_name="document-edit-symbolic")
        self.edit_icon.set_tooltip_text("Bearbeitet")
        for w in (self.live_icon, self.raw_label, self.edit_icon):
            self.badge_tl.append(w)
        self.overlay.add_overlay(self.badge_tl)

        self.video_box = Gtk.Box(spacing=4, halign=Gtk.Align.END, valign=Gtk.Align.END)
        self.video_box.add_css_class("thumb-badges")
        self.video_box.append(Gtk.Image(icon_name="camera-video-symbolic"))
        self.duration = Gtk.Label()
        self.video_box.append(self.duration)
        self.overlay.add_overlay(self.video_box)

        self.check = Gtk.Image(icon_name="object-select-symbolic", halign=Gtk.Align.END,
                               valign=Gtk.Align.END)
        self.check.add_css_class("thumb-check")
        self.overlay.add_overlay(self.check)

        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)

    def do_dispose(self):
        self.unbind()
        if self.overlay is not None:
            self.overlay.unparent()
            self.overlay = None
        Gtk.Widget.do_dispose(self)

    # Feste quadratische Größe – unabhängig von der Bildgröße
    def do_measure(self, orientation, for_size):
        self.overlay.measure(orientation, self._size)
        return self._size, self._size, -1, -1

    def do_size_allocate(self, width, height, baseline):
        self.overlay.allocate(width, height, baseline, None)

    def _set_size(self, size):
        if size != self._size:
            self._size = size
            self.queue_resize()

    def bind(self, asset, size):
        if self.asset is asset:
            self._set_size(size)
            self.update_state()
            return
        self.unbind()
        self.asset = asset
        self._set_size(size)
        self.live_icon.set_visible(asset.kind == "live")
        self.raw_label.set_visible(asset.raw)
        self.edit_icon.set_visible(asset.edited and self.grid.show_edit_badge)
        self.badge_tl.set_visible(asset.kind == "live" or asset.raw
                                  or (asset.edited and self.grid.show_edit_badge))
        self.video_box.set_visible(asset.kind == "video")
        if asset.kind == "video":
            self.duration.set_label(fmt_duration(asset.duration) if asset.duration else "")
        self.update_state()
        f = asset.display
        self.picture.set_paintable(None)
        self.placeholder.set_visible(True)
        def cb(tex):
            if self._texture_cb == (f, cb):
                return self._on_texture(tex)
            return False

        self._texture_cb = (f, cb)
        tex = self.grid.loader.request(f, cb)
        if tex is not None:
            self._texture_cb = None
            self._on_texture(tex)

    def unbind(self):
        if self._texture_cb is not None:
            f, cb = self._texture_cb
            self.grid.loader.cancel(f, cb)
            self._texture_cb = None
        self.asset = None

    def _on_texture(self, tex):
        self._texture_cb = None
        if self.asset is None:
            return False
        if tex is not None:
            self.picture.set_paintable(tex)
            self.placeholder.set_visible(False)
        else:
            self.placeholder.set_from_icon_name(
                "video-x-generic-symbolic" if self.asset.kind == "video" else "image-missing-symbolic")
        return False

    def update_state(self):
        a = self.asset
        if a is None:
            return
        selected = a.key in self.grid.selected
        dimmed = self.grid.dim_imported and a.status == "imported"
        if selected:
            self.add_css_class("selected")
        else:
            self.remove_css_class("selected")
        if dimmed:
            self.add_css_class("imported")
        else:
            self.remove_css_class("imported")
        self.check.set_visible(selected or dimmed)
        if selected:
            self.check.remove_css_class("imported-check")
        elif dimmed:
            self.check.add_css_class("imported-check")

    def _on_pressed(self, gesture, n_press, x, y):
        if self.asset is None:
            return
        button = gesture.get_current_button()
        state = gesture.get_current_event_state()
        self.grid.grab_focus()
        if button == Gdk.BUTTON_SECONDARY:
            if self.asset.key not in self.grid.selected:
                self.grid.select_only(self.asset)
            ok, pt = self.compute_point(self.grid, Graphene.Point().init(x, y))
            self.grid.emit_context(self.asset, pt.x if ok else 0, pt.y if ok else 0)
            return
        if n_press == 2:
            self.grid.select_only(self.asset)
            if self.grid.on_activate:
                self.grid.on_activate(self.asset)
            return
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        self.grid.click(self.asset, ctrl, shift)


class SectionHeader(Gtk.Box):
    def __init__(self, grid):
        super().__init__(spacing=12, margin_start=PAD, margin_end=PAD)
        self.grid = grid
        self.section = None
        self.titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=3)
        self.subtitle = Gtk.Label(xalign=0, ellipsize=3)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.add_css_class("caption")
        self.titles.append(self.title)
        self.titles.append(self.subtitle)
        self.append(self.titles)
        self.button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.button.add_css_class("flat")
        self.button.add_css_class("section-select")
        self.button.connect("clicked", self._on_select)
        self.append(self.button)

    def bind(self, section):
        self.section = section
        self.title.set_label(section.title)
        self.subtitle.set_label(section.subtitle)
        self.subtitle.set_visible(bool(section.subtitle))
        for c in ("section-big", "section-day"):
            self.remove_css_class(c)
        self.add_css_class("section-big" if section.level == 1 else "section-day")
        self.button.set_visible(section.selectable_header and bool(section.selection_scope))
        self.update_button()

    def update_button(self):
        if not self.section:
            return
        scope = self.section.selection_scope
        all_sel = scope and all(a.key in self.grid.selected for a in scope)
        self.button.set_label("Auswahl aufheben" if all_sel else "Auswählen")

    def _on_select(self, _btn):
        scope = self.section.selection_scope
        keys = {a.key for a in scope}
        if keys <= self.grid.selected:
            self.grid.selected -= keys
        else:
            self.grid.selected |= keys
            if scope:
                self.grid.anchor = scope[0].key
        self.grid.selection_updated()


class RowWidget(Gtk.Box):
    def __init__(self, grid):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.grid = grid
        self.header = SectionHeader(grid)
        self.cells_box = Gtk.Box(spacing=GAP, margin_start=PAD, margin_end=PAD,
                                 margin_bottom=GAP)
        self.cells = []
        self.append(self.header)
        self.append(self.cells_box)

    def bind(self, row):
        if row.kind == "header":
            self.header.set_visible(True)
            self.cells_box.set_visible(False)
            self.header.bind(row.section)
            self.header.set_margin_top(28 if row.section.level == 1 else 14)
            self.header.set_margin_bottom(8)
            for c in self.cells:
                c.unbind()
            return
        self.header.set_visible(False)
        self.header.section = None
        self.cells_box.set_visible(True)
        while len(self.cells) < len(row.items):
            c = ThumbCell(self.grid)
            self.cells.append(c)
            self.cells_box.append(c)
        size = self.grid.cell_size
        for i, c in enumerate(self.cells):
            if i < len(row.items):
                c.set_visible(True)
                c.bind(row.items[i], size)
            else:
                c.unbind()
                c.set_visible(False)

    def unbind(self):
        for c in self.cells:
            c.unbind()
        self.header.section = None


class PhotoGrid(Gtk.Widget):
    __gtype_name__ = "IosPhotosPhotoGrid"

    def __init__(self, loader, dim_imported=False, show_edit_badge=True):
        super().__init__(focusable=True, hexpand=True, vexpand=True)
        self.loader = loader
        self.dim_imported = dim_imported
        self.show_edit_badge = show_edit_badge
        self.sections = []
        self.order = []
        self._pos = {}
        self.selected = set()
        self.anchor = None
        self.zoom = 150
        self.cell_size = 150
        self.cols = 1
        self._width = 0
        self._relayout_id = 0
        self._pending_scroll = None
        self._rows_widgets = set()
        self.on_selection_changed = None
        self.on_activate = None
        self.on_context = None

        self.store = Gio.ListStore(item_type=RowItem)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        self.list = Gtk.ListView(model=Gtk.NoSelection(model=self.store), factory=factory)
        self.list.add_css_class("photo-grid")
        self.list.set_can_focus(False)
        self.scrolled = Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scrolled.set_parent(self)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        zoom = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        zoom.connect("scroll", self._on_scroll)
        self.add_controller(zoom)
        self.zoom_changed = None

    # ----------------------------------------------------------- Layout
    def do_dispose(self):
        if self.scrolled is not None:
            self.scrolled.unparent()
            self.scrolled = None
        Gtk.Widget.do_dispose(self)

    def do_measure(self, orientation, for_size):
        m = self.scrolled.measure(orientation, for_size)
        return m[0], m[1], -1, -1

    def do_size_allocate(self, width, height, baseline):
        self.scrolled.allocate(width, height, baseline, None)
        if width != self._width or self._pending_scroll is not None:
            self._width = width
            self._queue_relayout()

    def _queue_relayout(self):
        if not self._relayout_id:
            self._relayout_id = GLib.idle_add(self._relayout)

    def set_zoom(self, size):
        size = int(max(80, min(400, size)))
        if size != self.zoom:
            self.zoom = size
            self._queue_relayout()

    def set_sections(self, sections, keep_selection=True, scroll=None):
        """scroll: None (Position halten), "top", "bottom" oder Abschnitts-Key."""
        self.sections = [s for s in sections if s.items or s.level == 1]
        self.order = [a for s in self.sections for a in s.items]
        self._pos = {a.key: i for i, a in enumerate(self.order)}
        if keep_selection:
            self.selected &= set(self._pos)
        else:
            self.selected = set()
        if self.anchor not in self._pos:
            self.anchor = None
        self._pending_scroll = scroll
        self._queue_relayout()
        self._notify_selection()

    def _relayout(self):
        self._relayout_id = 0
        width = self._width or 800
        avail = max(1, width - 2 * PAD)
        cols = max(1, (avail + GAP) // (self.zoom + GAP))
        self.cols = cols
        self.cell_size = max(40, (avail - (cols - 1) * GAP) // cols)
        adj = self.scrolled.get_vadjustment()
        ratio = adj.get_value() / adj.get_upper() if adj.get_upper() > 0 else 0
        rows = []
        idx = 0
        for s in self.sections:
            if s.title:
                rows.append(RowItem("header", s))
            for i in range(0, len(s.items), cols):
                rows.append(RowItem("cells", s, s.items[i:i + cols], idx + i))
            idx += len(s.items)
        self._row_of_section = {}
        for i, r in enumerate(rows):
            if r.kind == "header" and r.section.key:
                self._row_of_section.setdefault(r.section.key, i)
        self.store.splice(0, self.store.get_n_items(), rows)
        # scroll_to vor der ersten Allokation lässt die ListView leer, bis gescrollt wird
        if not self.get_mapped() or not self._width:
            return False
        target = self._pending_scroll
        self._pending_scroll = None
        if rows:
            if target == "top":
                self.list.scroll_to(0, Gtk.ListScrollFlags.NONE, None)
            elif target == "bottom":
                self.list.scroll_to(len(rows) - 1, Gtk.ListScrollFlags.NONE, None)
            elif target in self._row_of_section:
                self.list.scroll_to(self._row_of_section[target], Gtk.ListScrollFlags.NONE, None)
            else:
                GLib.idle_add(lambda: adj.set_value(ratio * adj.get_upper()) and False)
        return False

    def scroll_to_section(self, key):
        row = getattr(self, "_row_of_section", {}).get(key)
        if row is not None:
            self.list.scroll_to(row, Gtk.ListScrollFlags.NONE, None)
        else:
            self._pending_scroll = key

    def scroll_to_asset(self, asset):
        pos = self._pos.get(asset.key)
        if pos is None:
            return
        for i in range(self.store.get_n_items()):
            r = self.store.get_item(i)
            if r.kind == "cells" and r.index <= pos < r.index + len(r.items):
                self.list.scroll_to(i, Gtk.ListScrollFlags.NONE, None)
                return

    # ----------------------------------------------------------- Factory
    def _setup(self, _f, item):
        w = RowWidget(self)
        item.set_child(w)
        item.set_activatable(False)
        item.set_focusable(False)

    def _bind(self, _f, item):
        w = item.get_child()
        w.bind(item.get_item())
        self._rows_widgets.add(w)

    def _unbind(self, _f, item):
        w = item.get_child()
        w.unbind()
        self._rows_widgets.discard(w)

    # ----------------------------------------------------------- Auswahl
    def click(self, asset, ctrl=False, shift=False):
        key = asset.key
        if shift and self.anchor in self._pos:
            a, b = sorted((self._pos[self.anchor], self._pos[key]))
            rng = {x.key for x in self.order[a:b + 1]}
            self.selected = (self.selected | rng) if ctrl else rng
        else:
            self.selected ^= {key}
            self.anchor = key
        self.selection_updated()

    def select_only(self, asset):
        self.selected = {asset.key}
        self.anchor = asset.key
        self.selection_updated()

    def select_all(self):
        self.selected = set(self._pos)
        self.selection_updated()

    def clear_selection(self):
        self.selected = set()
        self.selection_updated()

    def selected_assets(self):
        return [a for a in self.order if a.key in self.selected]

    def current(self):
        if self.anchor in self._pos:
            return self.order[self._pos[self.anchor]]
        sel = self.selected_assets()
        return sel[0] if sel else None

    def selection_updated(self):
        for w in list(self._rows_widgets):
            for c in w.cells:
                c.update_state()
            w.header.update_button()
        self._notify_selection()

    def refresh_states(self):
        self.selection_updated()

    def _notify_selection(self):
        if self.on_selection_changed:
            self.on_selection_changed(self.selected_assets())

    def emit_context(self, asset, x, y):
        if self.on_context:
            self.on_context(asset, x, y)

    def _on_key(self, _ctrl, keyval, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.select_all()
            return True
        if keyval == Gdk.KEY_Escape and self.selected:
            self.clear_selection()
            return True
        if keyval in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            cur = self.current()
            if cur and self.on_activate:
                self.on_activate(cur)
            return True
        if ctrl and keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self._zoom_by(1.2)
            return True
        if ctrl and keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self._zoom_by(1 / 1.2)
            return True
        delta = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -self.cols,
                 Gdk.KEY_Down: self.cols}.get(keyval)
        if delta and self.order:
            pos = self._pos.get(self.anchor, -1 if delta > 0 else len(self.order))
            new = max(0, min(len(self.order) - 1, pos + delta))
            asset = self.order[new]
            if shift:
                old_anchor = self.anchor
                self.selected.add(asset.key)
                self.anchor = asset.key
                if old_anchor is None:
                    self.anchor = asset.key
                self.selection_updated()
            else:
                self.select_only(asset)
            self.scroll_to_asset(asset)
            return True
        return False

    def _zoom_by(self, factor):
        self.set_zoom(self.zoom * factor)
        if self.zoom_changed:
            self.zoom_changed(self.zoom)

    def _on_scroll(self, ctrl, _dx, dy):
        state = ctrl.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            self._zoom_by(1.1 if dy < 0 else 1 / 1.1)
            return True
        return False
