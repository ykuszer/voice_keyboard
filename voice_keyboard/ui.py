"""The widget: a small card on the screen, and the work behind it.

It is a layer surface with keyboard mode NONE, and that is the single most
important line in this file. A normal window would take focus the moment it
appeared, and the synthesised keystrokes would then be typed into this widget
instead of into the text field the user is looking at. A layer surface with no
keyboard interactivity still receives pointer events, so the controls work
while focus stays exactly where it was.

That same choice is why the card is dragged rather than moved by the
compositor: a layer surface has no title bar and hyprland will not move it, so
the position is this file's business. The surface therefore covers the whole
usable screen and the card is placed inside it, with the input region clipped
to the card so everything around it still belongs to the windows underneath.
That is what makes dragging work: pointer events are measured against the
surface, and this surface never moves.

Inference happens on one worker thread, not a pool, because the order the
utterances were spoken in is the order they have to be typed in -- and because
whisper-server serialises requests behind a single model anyway.
"""

from __future__ import annotations

import json
import logging
import math
import os
import pathlib
import queue
import signal
import threading
import time

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")

from gi.repository import Gdk, GLib, Gtk, GtkLayerShell, Pango  # noqa: E402

from . import inject  # noqa: E402
from .asr import AsrError, Transcriber, WhisperServer, is_rtl  # noqa: E402
from .audio import Recorder, sources  # noqa: E402

log = logging.getLogger(__name__)

NAMESPACE = "voice-keyboard"

# Order matters only in that auto comes first; the four are the languages
# this is for.
LANGUAGES = [
    ("", "Auto"),
    ("he", "עברית"),
    ("en", "English"),
    ("pt", "Português"),
    ("es", "Español"),
]

# The meter is drawn on a decibel scale. A quiet microphone sits around -45
# dBFS, which on a linear bar is a sliver indistinguishable from silence --
# the display then says "dead" about a microphone that is merely quiet, which
# is exactly the wrong thing to tell someone whose dictation is not working.
METER_FLOOR_DB = -60.0


def _state_path() -> pathlib.Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return pathlib.Path(base) / "voice-keyboard" / "state.json"


class VoiceKeyboard:
    def __init__(self, opts) -> None:
        self.opts = opts
        self.target = opts.output
        self.device = opts.device
        self.translate = opts.translate
        self.bidi = opts.bidi

        self.language: str | None = opts.language if opts.language != "auto" else None
        self.detect_language = opts.language == "auto"

        self.server = WhisperServer(
            binary=opts.whisper_server,
            model=opts.model,
            vad_model=opts.vad_model,
            host=opts.host,
            port=opts.port,
            threads=opts.threads,
            log_path=opts.server_log,
        )
        self.transcriber = Transcriber(self.server, timeout=opts.request_timeout)
        self.recorder: Recorder | None = None

        self._work: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run_worker, name="asr", daemon=True)
        self._pending = 0
        self._last_used = time.monotonic()
        self._peak_db = METER_FLOOR_DB

        self._state = self._load_state()
        self._build()
        self._worker.start()
        GLib.timeout_add(80, self._tick)

        # Hiding rather than quitting keeps the model loaded, so bringing the
        # widget back is instant instead of another minute of loading.
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._on_signal_toggle)

    # --- persisted position -------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(_state_path().read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        path = _state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._state))
        except OSError as exc:
            log.debug("could not save state: %s", exc)

    # --- the card ----------------------------------------------------------

    def _build(self) -> None:
        self.window = Gtk.Window(title="voice keyboard")
        GtkLayerShell.init_for_window(self.window)
        GtkLayerShell.set_namespace(self.window, NAMESPACE)
        GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.OVERLAY)
        # Never take the keyboard: the keystrokes we synthesise must land in
        # whatever window the user is actually typing into.
        GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.NONE)

        # Anchored to all four edges, so the surface is the whole usable
        # screen and moving the card is moving a widget inside it. The
        # obvious alternative -- a card-sized surface moved by its
        # layer-shell margins -- cannot be dragged correctly: pointer
        # positions arrive relative to the surface, so moving the surface
        # moves the frame the next event is measured in. Events already in
        # flight then describe a position that no longer exists, and
        # applying them compounds. Measured on hyprland, a 320px pointer
        # drag threw the card 1220px across the screen that way.
        #
        # No exclusive zone is set, which keeps the surface inside the
        # bars' reserved space -- the same frame the margins were measured
        # in, so a position saved by the older build still means the place
        # it meant then.
        for edge in (
            GtkLayerShell.Edge.LEFT,
            GtkLayerShell.Edge.TOP,
            GtkLayerShell.Edge.RIGHT,
            GtkLayerShell.Edge.BOTTOM,
        ):
            GtkLayerShell.set_anchor(self.window, edge, True)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("vk-card")
        card.set_size_request(self.opts.width, -1)
        card.add(self._build_header())
        card.add(self._build_panes())
        card.add(self._build_footer())

        # An event box, because a Gtk.Box has no window of its own to collect
        # pointer events on.
        self.frame = Gtk.EventBox()
        self.frame.add(card)
        self.frame.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.frame.connect("button-press-event", self._on_drag_begin)
        self.frame.connect("button-release-event", self._on_drag_end)
        self.frame.connect("motion-notify-event", self._on_drag_motion)
        self._grab: tuple[float, float] | None = None

        # Gtk.Fixed, because the point of the screen-sized surface is to put
        # the card at a position of our choosing within it.
        self.layout = Gtk.Fixed()
        self.layout.put(self.frame, 0, 0)
        self.window.add(self.layout)

        # The card's height follows the text in it, and the surface's size is
        # whatever the compositor says it is -- which it says after the
        # window has already been allocated once at the card's own size.
        # Neither the position nor the input region can be settled at
        # startup, so both are redone whenever either size changes.
        self._sizes: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._placement_pending = False
        saved = self._saved_position()
        self._positioned = saved is not None
        self.card_x, self.card_y = saved or (0.0, 0.0)
        self._at = (0, 0)
        self.frame.connect("size-allocate", self._on_size_allocate)
        self.window.connect("size-allocate", self._on_size_allocate)

        # Until the first allocation the card's size is unknown, so the
        # region that should be clipped to it is not known either. An empty
        # region for those few milliseconds lets clicks through, which is the
        # harmless way to be wrong: the other way swallows every click on the
        # desktop.
        self.window.connect(
            "realize", lambda w: w.get_window().input_shape_combine_region(cairo.Region(), 0, 0)
        )

        self._load_css()
        self.window.show_all()

    def _saved_position(self) -> tuple[float, float] | None:
        """Where the card was left, or nothing if it has never been moved."""
        saved = self._state.get("position")
        if isinstance(saved, list) and len(saved) == 2:
            return float(saved[0]), float(saved[1])
        return None

    def _default_position(self, area, card) -> tuple[float, float]:
        """Centred on the chosen edge, at the requested margin."""
        x = max(0, (area[0] - card[0]) // 2)
        y = self.opts.margin if self.opts.position == "top" else area[1] - card[1] - self.opts.margin
        return x, max(0, y)

    def _on_size_allocate(self, _widget, _allocation) -> None:
        window = self.window.get_allocation()
        card = self.frame.get_allocation()
        sizes = ((window.width, window.height), (card.width, card.height))
        # Placing the card allocates it again, so without this the handler
        # would call itself for as long as GTK let it.
        if sizes == self._sizes:
            return
        self._sizes = sizes
        if not self._positioned:
            # A card nobody has placed belongs where this was asked to put
            # it, and goes on belonging there as the screen changes under it.
            # This is also what gets the first placement right: GTK allocates
            # the window at the card's own size before the compositor says
            # how big the surface really is, and a default worked out in that
            # moment would put the card in the corner.
            self.card_x, self.card_y = self._default_position(*sizes)
        # Gtk.Fixed.move() queues a resize. During size-allocate GTK can
        # discard that request, leaving the frame at its previous position.
        if not self._placement_pending:
            self._placement_pending = True
            GLib.idle_add(self._place_after_allocation)

    def _place_after_allocation(self) -> bool:
        self._placement_pending = False
        self._place_card()
        return GLib.SOURCE_REMOVE

    def _place_card(self) -> None:
        """Put the card where it belongs, and the input region on top of it."""
        (area_w, area_h), (card_w, card_h) = self._sizes
        # Clamped to the screen when it is drawn, not when it is chosen. The
        # wanted position outlives a screen it does not currently fit on --
        # and the first allocation is one of those, since the surface is
        # still the size of the card at that point.
        self._at = (
            int(min(max(0.0, self.card_x), max(0, area_w - card_w))),
            int(min(max(0.0, self.card_y), max(0, area_h - card_h))),
        )
        self.layout.move(self.frame, *self._at)

        # The surface is the size of the screen, so without clipping the
        # input region to the card it would eat every click on the desktop.
        # That is the one failure mode of a surface this big, and the reason
        # the region is set here rather than once: it has to follow the card.
        surface = self.window.get_window()
        if surface is not None:
            rect = cairo.RectangleInt(self._at[0], self._at[1], card_w, card_h)
            surface.input_shape_combine_region(cairo.Region(rect), 0, 0)

    # --- dragging ----------------------------------------------------------

    def _on_drag_begin(self, _widget, event) -> bool:
        if event.button != 1:
            return False
        # Surface coordinates, not widget ones. The widget moves with the
        # card and would measure the drag against its own moving self; the
        # surface stays where it is. What is kept is where in the card the
        # pointer took hold of it, which is what has to stay true while it
        # moves.
        self._grab = (event.x_root - self._at[0], event.y_root - self._at[1])
        self._positioned = True
        return False

    def _on_drag_end(self, _widget, event) -> bool:
        if event.button != 1 or self._grab is None:
            return False
        # GTK may coalesce motion events before the release.
        self._drag_to(event)
        self._grab = None
        # Where it ended up rather than where the pointer went: a drag off
        # the edge of the screen should not be remembered as a position off
        # the edge of the screen.
        self.card_x, self.card_y = self._at
        self._state["position"] = list(self._at)
        self._save_state()
        return False

    def _on_drag_motion(self, _widget, event) -> bool:
        """Put the card where the pointer is, every event, no pacing.

        Each event carries the pointer's position in the surface rather than
        a delta, so there is nothing to accumulate and nothing to go stale:
        acting on an event that has been overtaken costs one redundant move,
        not a wrong position. Each move costs about 0.08ms.
        """
        if self._grab is None:
            return False
        if not event.state & Gdk.ModifierType.BUTTON1_MASK:
            self._grab = None
            return False
        self._drag_to(event)
        return True

    def _drag_to(self, event) -> None:
        self.card_x = event.x_root - self._grab[0]
        self.card_y = event.y_root - self._grab[1]
        self._place_card()

    # --- rows --------------------------------------------------------------

    def _build_header(self) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        handle = Gtk.EventBox()
        handle.set_above_child(True)
        handle.set_tooltip_text("Drag to move")
        handle.connect(
            "realize",
            lambda widget: widget.get_window().set_cursor(
                Gdk.Cursor.new_from_name(widget.get_display(), "move")
            ),
        )
        grip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        grip.add(Gtk.Label(label="⠿"))
        handle.add(grip)
        row.pack_start(handle, True, True, 0)

        self.dot = Gtk.Label(label="●")
        self.dot.get_style_context().add_class("vk-dot")
        grip.pack_start(self.dot, False, False, 0)

        self.status = Gtk.Label(label="Idle", xalign=0.0)
        self.status.get_style_context().add_class("vk-status")
        grip.pack_start(self.status, False, False, 0)

        # Hiding, not quitting: the model stays loaded and the same keybind
        # brings the card back instantly.
        close = Gtk.Button(label="×")
        close.get_style_context().add_class("vk-close")
        close.set_tooltip_text("Hide. The keybind brings it back.")
        close.connect("clicked", lambda _b: self.hide())
        row.pack_end(close, False, False, 0)

        # Naming the language is the cheapest speed control there is: it
        # skips the two extra passes auto-detection costs, which is most of
        # why the first utterance of a session is slow.
        self.badge = Gtk.ComboBoxText()
        self.badge.get_style_context().add_class("vk-badge")
        self.badge.set_tooltip_text(
            "Language. Auto costs two extra passes on the first utterance;\n"
            "naming it is faster and more accurate."
        )
        for code, caption in LANGUAGES:
            self.badge.append(code, caption)
        self._setting_language = True
        self.badge.set_active_id(self.language or "")
        self._setting_language = False
        self.badge.connect("changed", self._on_language)
        row.pack_end(self.badge, False, False, 0)

        self.meter = Gtk.LevelBar()
        self.meter.set_min_value(0.0)
        self.meter.set_max_value(1.0)
        self.meter.set_size_request(130, 8)
        self.meter.set_valign(Gtk.Align.CENTER)
        self.meter.get_style_context().add_class("vk-meter")
        self.meter.set_tooltip_text("Microphone level")
        row.pack_end(self.meter, False, False, 0)

        return row

    def _pane(self, css_class: str, placeholder: str) -> Gtk.Label:
        label = Gtk.Label(label=placeholder, xalign=0.0)
        label.get_style_context().add_class(css_class)
        label.set_single_line_mode(True)
        label.set_ellipsize(Pango.EllipsizeMode.START)
        label.set_max_width_chars(1)
        return label

    def _build_panes(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.native_label = self._pane("vk-native", "…")
        self.english_label = self._pane("vk-english", "")
        box.add(self.native_label)
        box.add(self.english_label)
        return box

    def _build_footer(self) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.toggle = Gtk.Button(label="Start")
        self.toggle.get_style_context().add_class("vk-start")
        self.toggle.connect("clicked", self._on_toggle)
        row.pack_start(self.toggle, False, False, 0)

        self.mic = Gtk.ComboBoxText()
        self.mic.get_style_context().add_class("vk-mic")
        self.mic.set_tooltip_text("Microphone")
        self.mic.connect("changed", self._on_mic_changed)
        self._reload_sources()
        row.pack_start(self.mic, False, False, 0)

        # The English pane is a second pass over the same audio. Off, an
        # utterance costs half as much, and the next one starts sooner.
        self.translate_button = Gtk.ToggleButton(label="+EN")
        self.translate_button.get_style_context().add_class("vk-translate")
        self.translate_button.set_tooltip_text(
            "Also translate to English.\nA second pass: roughly twice the work per utterance."
        )
        self.translate_button.set_active(self.translate)
        self.translate_button.connect("toggled", self._on_translate)
        row.pack_start(self.translate_button, False, False, 0)

        # Visual order is a property of the window being typed into, not of
        # the language, so it belongs next to the controls rather than buried
        # in a config file.
        self.bidi_button = Gtk.ToggleButton(label="⇄")
        self.bidi_button.get_style_context().add_class("vk-translate")
        self.bidi_button.set_tooltip_text(
            "Visual order, for terminals.\n"
            "Terminals have no bidi and draw Hebrew mirrored; this "
            "pre-reorders it.\nLeave off for editors, browsers and chat."
        )
        self.bidi_button.set_active(self.bidi == "visual")
        self.bidi_button.connect("toggled", self._on_bidi)
        row.pack_start(self.bidi_button, False, False, 0)

        row.pack_end(self._build_target_chooser(), False, False, 0)
        return row

    def _build_target_chooser(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.get_style_context().add_class("vk-target")
        self._target_buttons: dict[str, Gtk.RadioButton] = {}
        first = None
        for key, caption in (("native", "Native"), ("english", "English")):
            button = Gtk.RadioButton.new_with_label_from_widget(first, caption)
            if first is None:
                first = button
            button.set_mode(False)
            button.get_style_context().add_class("vk-target-button")
            button.set_active(key == self.target)
            button.connect("toggled", self._on_target, key)
            self._target_buttons[key] = button
            box.add(button)
        return box

    def _load_css(self) -> None:
        # The surface is the size of the screen, so a painted window
        # background would be a sheet of theme colour over the desktop. This
        # is not left to the stylesheet: --css is optional, and the one rule
        # the layout depends on cannot be.
        base = Gtk.CssProvider()
        base.load_from_data(b"window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), base, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        if not self.opts.css:
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(self.opts.css)
        except GLib.Error as exc:
            log.warning("could not load css %s: %s", self.opts.css, exc)
            return
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

    # --- microphones -------------------------------------------------------

    def _reload_sources(self) -> None:
        """Fill the menu, preferring whatever is already selected."""
        self._sources = sources()
        self._loading_sources = True
        self.mic.remove_all()
        self.mic.append("", "Default microphone")
        for source in self._sources:
            self.mic.append(source.name, source.label())

        active = self.device or ""
        if active and all(s.name != active for s in self._sources):
            # Selected device has gone away -- a bluetooth headset that
            # disconnected, say. Say so rather than silently recording nothing.
            self.mic.append(active, f"{active} (unavailable)")
        self.mic.set_active_id(active)
        self._loading_sources = False

    def _on_mic_changed(self, combo) -> None:
        if getattr(self, "_loading_sources", False):
            return
        chosen = combo.get_active_id()
        self.device = chosen or None
        log.info("microphone set to %s", self.device or "system default")
        if self.recorder is not None and self.recorder.running:
            # Restart capture so the change takes effect immediately rather
            # than at the next press of start.
            self._stop_capture()
            self._start_capture()

    # --- state -------------------------------------------------------------

    def _set_status(self, state: str, message: str | None = None) -> bool:
        captions = {
            "idle": "Idle",
            "hidden": "Hidden",
            "loading": "Loading model…",
            "listening": "Listening",
            "thinking": "Transcribing…",
            "error": message or "Error",
        }
        self.status.set_text(captions.get(state, state))
        context = self.dot.get_style_context()
        for known in ("idle", "loading", "listening", "thinking", "error"):
            context.remove_class(f"vk-dot-{known}")
        context.add_class(f"vk-dot-{state}")
        if state == "error" and message:
            self.status.set_tooltip_text(message)
            log.error("%s", message)
        return False

    def _tick(self) -> bool:
        if self.recorder is not None and self.recorder.running:
            self.meter.set_value(self._meter_value(self.recorder.level))
            self._set_status("thinking" if self._pending else "listening")
        else:
            self.meter.set_value(0.0)

        failure = self.server.failed()
        if failure:
            self._set_status("error", failure)

        self._maybe_release_model()
        return True

    def _meter_value(self, level: float) -> float:
        """Map an RMS level onto a decibel bar with a little peak hold."""
        db = METER_FLOOR_DB if level <= 1e-6 else max(METER_FLOOR_DB, 20.0 * math.log10(level))
        # Fall slowly, rise at once: a bar that tracks RMS exactly flickers
        # too fast to read.
        self._peak_db = db if db > self._peak_db else self._peak_db - 1.5
        return min(1.0, max(0.0, (self._peak_db - METER_FLOOR_DB) / -METER_FLOOR_DB))

    def _maybe_release_model(self) -> None:
        if self.opts.idle_timeout <= 0 or not self.server.ours:
            return
        if (self.recorder is not None and self.recorder.running) or self._pending:
            self._last_used = time.monotonic()
            return
        if time.monotonic() - self._last_used > self.opts.idle_timeout:
            self.server.stop()

    # --- buttons -----------------------------------------------------------

    def _on_toggle(self, _button) -> None:
        if self.recorder is not None and self.recorder.running:
            self.stop()
        else:
            self.start()

    def _on_target(self, button, key: str) -> None:
        if not button.get_active():
            return
        self.target = key
        if key == "english" and not self.translate:
            self.translate_button.set_active(True)

    def _on_language(self, combo) -> None:
        if getattr(self, "_setting_language", False):
            return
        code = combo.get_active_id() or ""
        self.language = code or None
        self.detect_language = not code
        log.info("language set to %s", self.language or "auto-detect")

    def _on_bidi(self, button) -> None:
        self.bidi = "visual" if button.get_active() else "logical"
        log.info("typing in %s order", self.bidi)

    def _on_translate(self, button) -> None:
        self.translate = button.get_active()
        # Typing the English pane while refusing to produce it would quietly
        # type nothing, so the two controls are kept honest about each other.
        if not self.translate and self.target == "english":
            self._target_buttons["native"].set_active(True)

    def _on_signal_toggle(self) -> bool:
        if self.window.is_visible():
            self.hide()
        else:
            self.show()
        return GLib.SOURCE_CONTINUE

    def show(self) -> None:
        # The list is rebuilt on the way in, so a headset plugged in while the
        # card was hidden is simply there.
        self._reload_sources()
        self.window.show_all()
        # Hiding tears the wl_surface down, so the clipped input region goes
        # with it and has to be put back on the new one.
        if self._sizes is not None:
            self._place_card()
        self._set_status("idle")

    def hide(self) -> None:
        self._grab = None
        self.stop()
        self.window.hide()

    # --- capture -----------------------------------------------------------

    def _start_capture(self) -> None:
        self.recorder = Recorder(
            on_utterance=self._queue_utterance,
            on_error=lambda msg: GLib.idle_add(self._set_status, "error", msg),
            device=self.device,
            gain=self.opts.gain,
            aggressiveness=self.opts.vad_aggressiveness,
            silence_ms=self.opts.silence_ms,
            start_ms=self.opts.start_ms,
            max_ms=self.opts.max_seconds * 1000,
        )
        self.recorder.start()

    def _stop_capture(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()

    def start(self) -> None:
        self._last_used = time.monotonic()
        self._peak_db = METER_FLOOR_DB
        self.toggle.set_label("Stop")
        self.toggle.get_style_context().add_class("vk-stop")
        try:
            self.server.start()
        except AsrError as exc:
            self._set_status("error", str(exc))
            return
        self._set_status("listening" if self.server.listening() else "loading")
        self._start_capture()

    def stop(self) -> None:
        self._stop_capture()
        self.toggle.set_label("Start")
        self.toggle.get_style_context().remove_class("vk-stop")
        self._set_status("thinking" if self._pending else "idle")

    def shutdown(self) -> None:
        self._stop_capture()
        self.server.stop()

    # --- the worker --------------------------------------------------------

    def _queue_utterance(self, utterance) -> None:
        self._pending += 1
        self._work.put(utterance)

    def _run_worker(self) -> None:
        while True:
            utterance = self._work.get()
            try:
                self._handle(utterance)
            except AsrError as exc:
                GLib.idle_add(self._set_status, "error", str(exc))
            except Exception:  # noqa: BLE001 -- a worker that dies is a dead widget
                log.exception("while transcribing")
                GLib.idle_add(self._set_status, "error", "transcription failed")
            finally:
                self._pending -= 1
                self._work.task_done()

    def _await_server(self) -> None:
        deadline = time.monotonic() + self.opts.load_timeout
        if not self.server.listening():
            GLib.idle_add(self._set_status, "loading")
        while not self.server.listening():
            failure = self.server.failed()
            if failure:
                raise AsrError(failure)
            if time.monotonic() > deadline:
                raise AsrError("whisper-server did not come up")
            time.sleep(0.25)

    def _handle(self, utterance) -> None:
        self._last_used = time.monotonic()
        self._await_server()
        wav = utterance.wav()

        if self.detect_language and self.language is None:
            reply = self.transcriber.detect(wav)
            if reply.language:
                self.language = reply.language
                GLib.idle_add(self._refresh_badge)
            native = reply
        else:
            native = self.transcriber.transcribe(wav, self.language)

        GLib.idle_add(self._show, "native", native.text)
        if self.target == "native":
            self._inject(native.text)

        # Already English, or the English pane was turned off: either way the
        # second pass over the same audio is pure cost.
        if self.language == "en" or not self.translate:
            GLib.idle_add(self._show, "english", native.text if self.language == "en" else "")
            return

        english = self.transcriber.translate(wav, self.language)
        GLib.idle_add(self._show, "english", english.text)
        if self.target == "english":
            self._inject(english.text)

    def _inject(self, text: str) -> None:
        if not text:
            return
        try:
            inject.inject(
                text + " ",
                method=self.opts.inject_method,
                restore_layout=self.opts.restore_layout,
                bidi=self.bidi,
            )
        except (OSError, RuntimeError) as exc:
            GLib.idle_add(self._set_status, "error", str(exc))

    # --- display -----------------------------------------------------------

    def _refresh_badge(self) -> bool:
        """Show what auto-detect settled on, without re-triggering the handler."""
        self._setting_language = True
        self.badge.set_active_id(self.language or "")
        self._setting_language = False
        return False

    def _show(self, which: str, text: str) -> bool:
        label = self.native_label if which == "native" else self.english_label
        label.set_text(text)
        if which == "native":
            rtl = is_rtl(text)
            label.set_xalign(1.0 if rtl else 0.0)
            label.set_direction(Gtk.TextDirection.RTL if rtl else Gtk.TextDirection.LTR)
        return False


def run(opts) -> int:
    widget = VoiceKeyboard(opts)

    # SIGTERM's default action skips the finally below, which is how a
    # widget that is killed leaves its server behind. Quit the loop instead
    # so shutdown actually runs.
    def _quit(*_):
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _quit)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _quit)

    if opts.start:
        widget.start()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        widget.shutdown()
    return 0
