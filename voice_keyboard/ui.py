"""The widget: a small card on the screen, and the work behind it.

It is a layer surface with keyboard mode NONE, and that is the single most
important line in this file. A normal window would take focus the moment it
appeared, and the synthesised keystrokes would then be typed into this widget
instead of into the text field the user is looking at. A layer surface with no
keyboard interactivity still receives pointer events, so the controls work
while focus stays exactly where it was.

That same choice is why the card is dragged rather than moved by the
compositor: a layer surface has no title bar and hyprland will not move it, so
position is a pair of margins this file maintains, and dragging edits them.

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

# Roughly 60Hz. Each move costs about 0.08ms, so what this paces is the
# compositor's willingness to reconfigure the surface, not our own work.
DRAG_INTERVAL_MS = 16


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

        # Anchored to two edges, always, so that position is just a pair of
        # margins. Anchoring to one edge would let the compositor centre the
        # card on the other axis and there would be nothing to drag.
        GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.TOP, True)

        self.margin_x, self.margin_y = self._initial_position()
        self._apply_margins()

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("vk-card")
        card.set_size_request(self.opts.width, -1)
        card.add(self._build_header())
        card.add(self._build_panes())
        card.add(self._build_footer())

        # An event box, because a Gtk.Box has no window of its own to collect
        # pointer events on.
        frame = Gtk.EventBox()
        frame.add(card)
        frame.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        frame.connect("button-press-event", self._on_drag_begin)
        frame.connect("button-release-event", self._on_drag_end)
        frame.connect("motion-notify-event", self._on_drag_motion)
        self._drag_from: tuple[float, float] | None = None
        self._drag_to: tuple[float, float] | None = None
        self._drag_tick: int | None = None

        self.window.add(frame)
        self._load_css()
        self.window.show_all()

    def _monitor_geometry(self) -> Gdk.Rectangle:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_geometry()

    def _initial_position(self) -> tuple[int, int]:
        """Where the card starts: where it was left, or centred on the edge."""
        saved = self._state.get("position")
        if isinstance(saved, list) and len(saved) == 2:
            return int(saved[0]), int(saved[1])

        area = self._monitor_geometry()
        x = max(0, (area.width - self.opts.width) // 2)
        # The height is not known until the card is realised, so approximate;
        # being a few pixels out on first run costs nothing and the position
        # is saved the moment it is dragged.
        y = self.opts.margin if self.opts.position == "top" else area.height - 150 - self.opts.margin
        return x, max(0, y)

    def _apply_margins(self) -> None:
        GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.LEFT, int(self.margin_x))
        GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.TOP, int(self.margin_y))
        # A new margin only reaches the compositor when the surface is
        # committed, and GTK commits when it draws. Nothing about moving the
        # card dirties a widget, so without asking for a draw the margins
        # change in memory and the card sits exactly where it was.
        self.window.queue_draw()

    # --- dragging ----------------------------------------------------------

    def _on_drag_begin(self, _widget, event) -> bool:
        if event.button != 1:
            return False
        self._drag_from = (event.x, event.y)
        # A plain timer, not the frame clock. Updates do have to be paced --
        # every move shifts the coordinate frame the next event is measured
        # in, so acting per event means acting on stale positions. But the
        # frame clock paces them far too slowly: changing a layer-shell
        # margin dirties no widget, so GTK schedules no redraw, so
        # add_tick_callback barely runs. Measured 5 ticks against 11 motion
        # events, where a timer gives 164 over the same couple of seconds.
        if self._drag_tick is None:
            self._drag_tick = GLib.timeout_add(DRAG_INTERVAL_MS, self._on_drag_tick)
        return False

    def _on_drag_end(self, _widget, _event) -> bool:
        if self._drag_from is None:
            return False
        self._drag_from = None
        self._drag_to = None
        if self._drag_tick is not None:
            GLib.source_remove(self._drag_tick)
            self._drag_tick = None
        self._state["position"] = [int(self.margin_x), int(self.margin_y)]
        self._save_state()
        return False

    def _on_drag_tick(self) -> bool:
        """Apply at most one move per frame, from the latest pointer position."""
        if self._drag_from is None:
            return GLib.SOURCE_REMOVE
        if self._drag_to is None:
            return GLib.SOURCE_CONTINUE

        dx, dy = self._drag_to
        self._drag_to = None
        if not dx and not dy:
            return GLib.SOURCE_CONTINUE

        area = self._monitor_geometry()
        allocation = self.window.get_allocation()
        self.margin_x = min(max(0, self.margin_x + dx), max(0, area.width - allocation.width))
        self.margin_y = min(max(0, self.margin_y + dy), max(0, area.height - allocation.height))
        self._apply_margins()
        return GLib.SOURCE_CONTINUE

    def _on_drag_motion(self, _widget, event) -> bool:
        """Record where the pointer is; the frame clock does the moving.

        Wayland hands out no absolute pointer position, so this works in
        deltas within the surface: how far the pointer has drifted from the
        point it grabbed. Replacing rather than accumulating is deliberate --
        until the card actually moves, every event measures the same total
        displacement, so the newest reading is the whole truth and the older
        ones are not increments to add to it.
        """
        if self._drag_from is None:
            return False
        self._drag_to = (event.x - self._drag_from[0], event.y - self._drag_from[1])
        return True

    # --- rows --------------------------------------------------------------

    def _build_header(self) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.dot = Gtk.Label(label="●")
        self.dot.get_style_context().add_class("vk-dot")
        row.pack_start(self.dot, False, False, 0)

        self.status = Gtk.Label(label="Idle", xalign=0.0)
        self.status.get_style_context().add_class("vk-status")
        row.pack_start(self.status, False, False, 0)

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
        self._set_status("idle")

    def hide(self) -> None:
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
