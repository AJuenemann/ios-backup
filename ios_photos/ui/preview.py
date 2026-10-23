"""Großansicht (wie Quick Look): Leertaste/Doppelklick öffnet, Pfeiltasten blättern."""

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .thumbloader import load_preview
from .util import fmt_datetime, fmt_duration, fmt_size

_gst_ok = None


def can_play_video():
    """Prüft, ob GStreamer H.264/HEVC dekodieren kann (z. B. durch gstreamer1.0-libav)."""
    global _gst_ok
    if _gst_ok is None:
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            _gst_ok = any(Gst.ElementFactory.find(n) for n in
                          ("avdec_h265", "avdec_h264", "vah265dec", "vaapih265dec", "nvh265dec"))
        except (ImportError, ValueError):
            _gst_ok = False
    return _gst_ok


class PreviewDialog(Adw.Dialog):
    def __init__(self, win, assets, current):
        super().__init__(title="Vorschau")
        self.win = win
        self.assets = assets
        self.index = next((i for i, a in enumerate(assets) if a.key == current.key), 0)
        w = win.get_width() or 1200
        h = win.get_height() or 800
        self.set_content_width(int(w * 0.88))
        self.set_content_height(int(h * 0.9))

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle()
        header.set_title_widget(self.title)
        self.live_btn = Gtk.ToggleButton(tooltip_text="Live Photo abspielen")
        live_content = Adw.ButtonContent(icon_name="ios-photos-live-symbolic", label="LIVE")
        self.live_btn.set_child(live_content)
        self.live_btn.add_css_class("live-button")
        self.live_btn.connect("toggled", self._on_live)
        header.pack_start(self.live_btn)
        ext = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Mit Standardprogramm öffnen")
        ext.connect("clicked", lambda *_: self._open_external())
        header.pack_end(ext)
        tv.add_top_bar(header)

        self.overlay = Gtk.Overlay()
        self.overlay.add_css_class("preview-stage")
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.overlay.set_child(self.picture)
        self.video = Gtk.Video(autoplay=True, visible=False)
        self.overlay.add_overlay(self.video)
        self.spinner = Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.CENTER)
        self.overlay.add_overlay(self.spinner)
        self.notice = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                              halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=24)
        self.notice.add_css_class("osd")
        self.notice.add_css_class("toolbar")
        lbl = Gtk.Label(label="Für die Wiedergabe fehlt ein Video-Decoder (Paket gstreamer1.0-libav).",
                        wrap=True)
        play_ext = Gtk.Button(label="In Videoplayer öffnen", halign=Gtk.Align.CENTER)
        play_ext.add_css_class("pill")
        play_ext.connect("clicked", lambda *_: self._open_external())
        self.notice.append(lbl)
        self.notice.append(play_ext)
        self.overlay.add_overlay(self.notice)

        self.prev_btn = self._nav_button("go-previous-symbolic", Gtk.Align.START, -1)
        self.next_btn = self._nav_button("go-next-symbolic", Gtk.Align.END, 1)
        tv.set_content(self.overlay)

        self.info = Gtk.Label(ellipsize=3, margin_top=6, margin_bottom=6)
        self.info.add_css_class("dim-label")
        tv.add_bottom_bar(self.info)
        self.set_child(tv)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("closed", lambda *_: self._stop_video())
        self.show_current()

    def _nav_button(self, icon, align, step):
        b = Gtk.Button(icon_name=icon, halign=align, valign=Gtk.Align.CENTER, margin_start=12,
                       margin_end=12)
        b.add_css_class("osd")
        b.add_css_class("circular")
        b.connect("clicked", lambda *_: self.step(step))
        self.overlay.add_overlay(b)
        return b

    @property
    def asset(self):
        return self.assets[self.index]

    def step(self, delta):
        new = self.index + delta
        if 0 <= new < len(self.assets):
            self.index = new
            self.show_current()

    def show_current(self):
        a = self.asset
        self._stop_video()
        self.notice.set_visible(False)
        self.prev_btn.set_visible(self.index > 0)
        self.next_btn.set_visible(self.index < len(self.assets) - 1)
        self.title.set_title(a.primary.name)
        self.title.set_subtitle(fmt_datetime(a.sort_date) if a.sort_date else "")
        parts = [", ".join(f.name for f in a.media_files)]
        if a.width and a.height:
            parts.append(f"{a.width} × {a.height}")
        if a.kind == "video" and a.duration:
            parts.append(fmt_duration(a.duration))
        parts.append(fmt_size(a.size))
        if a.status == "imported" and a.files and not a.key.startswith("lib:"):
            parts.append("bereits importiert")
        self.info.set_label("  ·  ".join(parts))
        self.live_btn.set_visible(a.kind == "live" and can_play_video())
        self.live_btn.set_active(False)

        tex = self.win.loader.peek(a.display)
        self.picture.set_paintable(tex)
        self.spinner.set_visible(True)
        load_preview(self.win.cache_root, a.display, self._on_preview)
        if a.kind == "video":
            if can_play_video():
                self._play(a.display.path, loop=False)
            else:
                self.notice.set_visible(True)

    def _on_preview(self, f, tex):
        if f is not self.asset.display:
            return False
        self.spinner.set_visible(False)
        if tex is not None:
            self.picture.set_paintable(tex)
        return False

    def _play(self, path, loop):
        self.video.set_loop(loop)
        self.video.set_file(Gio.File.new_for_path(path))
        self.video.set_visible(True)

    def _stop_video(self):
        stream = self.video.get_media_stream()
        if stream:
            stream.pause()
        self.video.set_file(None)
        self.video.set_visible(False)

    def _on_live(self, btn):
        lv = self.asset.live_video
        if btn.get_active() and lv:
            btn.add_css_class("active")
            self._play(lv.path, loop=False)
            GLib.timeout_add(int((self.asset.duration or 3) * 1000) + 600, self._live_done, self.index)
        else:
            btn.remove_css_class("active")
            self._stop_video()

    def _live_done(self, index):
        if index == self.index and self.live_btn.get_active():
            self.live_btn.set_active(False)
        return False

    def _open_external(self):
        f = self.asset.display
        Gtk.FileLauncher.new(Gio.File.new_for_path(f.path)).launch(self.win, None, None)

    def _on_key(self, _c, keyval, _code, _state):
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up):
            self.step(-1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down):
            self.step(1)
            return True
        if keyval == Gdk.KEY_space:
            self.close()
            return True
        return False
