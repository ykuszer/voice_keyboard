"""Entry point, argument parsing, and the two headless modes.

``--debug-audio`` and ``--debug-asr`` exist so that the two halves most likely
to be wrong -- is the microphone being cut at sensible places, and is whisper
hearing the right words -- can be checked without a compositor, a layer
surface or a focused window anywhere in the picture.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import sys
import threading
import time


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voice-keyboard", description=__doc__)

    asr = p.add_argument_group("speech recognition")
    asr.add_argument("--whisper-server", default="whisper-server", help="server binary")
    asr.add_argument("--model", help="path to a ggml whisper model")
    asr.add_argument("--vad-model", help="path to a ggml silero VAD model")
    asr.add_argument("--host", default="127.0.0.1")
    asr.add_argument("--port", type=int, default=18080)
    asr.add_argument("--threads", type=int, default=0, help="0 leaves it to whisper")
    asr.add_argument("--server-log", help="where to put whisper-server's output")
    asr.add_argument("--request-timeout", type=float, default=180.0)
    asr.add_argument("--load-timeout", type=float, default=180.0)
    asr.add_argument(
        "--language",
        default="auto",
        help="he, en, pt, es, or auto to settle it from the first utterance",
    )
    asr.add_argument(
        "--idle-timeout",
        type=float,
        default=600.0,
        help="seconds of disuse before the model is released; 0 to keep it",
    )

    audio = p.add_argument_group("audio")
    audio.add_argument("--device", help="pipewire target node; default is the system's")
    audio.add_argument("--vad-aggressiveness", type=int, default=2, choices=range(4))
    audio.add_argument("--silence-ms", type=int, default=700, help="pause that ends an utterance")
    audio.add_argument("--start-ms", type=int, default=250, help="speech that begins one")
    audio.add_argument("--max-seconds", type=int, default=25, help="hard cap on one utterance")
    audio.add_argument(
        "--gain",
        type=float,
        default=1.0,
        help="multiply the input before the gate; for microphones too quiet to trigger it",
    )

    out = p.add_argument_group("output")
    out.add_argument("--output", default="native", choices=("native", "english"))
    out.add_argument("--inject-method", default="wtype", choices=("wtype", "clipboard"))
    out.add_argument(
        "--bidi",
        default="logical",
        choices=("logical", "visual"),
        help="visual reorders right-to-left text for terminals, which have no bidi",
    )
    out.add_argument(
        "--no-translate",
        dest="translate",
        action="store_false",
        help="skip the English pass; halves the work per utterance",
    )
    out.add_argument(
        "--no-restore-layout",
        dest="restore_layout",
        action="store_false",
        help="leave the xkb layout wherever wtype puts it",
    )

    ui = p.add_argument_group("widget")
    ui.add_argument("--css", help="stylesheet for the card")
    ui.add_argument("--position", default="bottom", choices=("bottom", "top"))
    ui.add_argument("--margin", type=int, default=80)
    ui.add_argument("--width", type=int, default=560)
    ui.add_argument("--meter-gain", type=float, default=8.0, help="level meter sensitivity")
    ui.add_argument(
        "--start",
        action="store_true",
        help="begin listening at once instead of waiting for the button",
    )

    dbg = p.add_argument_group("debugging")
    dbg.add_argument("--debug-audio", action="store_true", help="print utterance cuts and exit")
    dbg.add_argument("--debug-asr", action="store_true", help="print transcripts, no widget")
    dbg.add_argument(
        "--debug-file",
        metavar="WAV",
        help="push a 16kHz mono wav through the endpointer and whisper, no microphone",
    )
    dbg.add_argument(
        "--save-audio",
        metavar="WAV",
        help="with --debug-audio, write everything captured to a wav for inspection",
    )
    dbg.add_argument("-v", "--verbose", action="count", default=0)
    return p


def _debug_audio(opts) -> int:
    """Prove the endpointer against a real microphone, and nothing else.

    Prints a decibel reading rather than a bare bar, because the question
    being asked here is almost always "is this microphone too quiet for the
    gate", and that is a number, not a picture.
    """
    import wave

    from .audio import (
        FRAME_BYTES,
        SAMPLE_RATE,
        SAMPLE_WIDTH,
        Recorder,
        amplify,
        frame_level,
        read_exact,
    )

    seen = 0
    captured: list[bytes] = []

    def on_utterance(utterance):
        nonlocal seen
        seen += 1
        print(
            f"\nutterance {seen}: {utterance.seconds:.2f}s, {len(utterance.pcm)} bytes",
            flush=True,
        )

    recorder = Recorder(
        on_utterance=on_utterance,
        on_error=lambda m: print(f"error: {m}", file=sys.stderr, flush=True),
        device=opts.device,
        gain=opts.gain,
        aggressiveness=opts.vad_aggressiveness,
        silence_ms=opts.silence_ms,
        start_ms=opts.start_ms,
        max_ms=opts.max_seconds * 1000,
    )

    # Tapped separately from the recorder so that what gets written is exactly
    # what the gate saw, gain and all.
    if opts.save_audio:
        import subprocess as _sp

        raw = _sp.Popen(
            recorder._command(),  # noqa: SLF001 -- same module, same contract
            stdout=_sp.PIPE,
            stderr=_sp.DEVNULL,
            bufsize=0,
        )
    else:
        raw = None

    print("listening; speak normally, ctrl-c to stop", flush=True)
    if raw is None:
        recorder.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    peak_db = -99.0
    if raw is not None:
        from .audio import Endpointer

        endpointer = Endpointer(
            aggressiveness=opts.vad_aggressiveness,
            silence_ms=opts.silence_ms,
            start_ms=opts.start_ms,
            max_ms=opts.max_seconds * 1000,
        )
        while not stop.is_set():
            frame = read_exact(raw.stdout, FRAME_BYTES)
            if len(frame) < FRAME_BYTES:
                break
            frame = amplify(frame, opts.gain)
            captured.append(frame)
            level = frame_level(frame)
            db = -99.0 if level <= 1e-6 else 20.0 * math.log10(level)
            peak_db = max(peak_db, db)
            speech = endpointer._vad.is_speech(frame, SAMPLE_RATE)  # noqa: SLF001
            cut = endpointer.feed(frame)
            if cut is not None:
                on_utterance(cut)
            if len(captured) % 10 == 0:
                bar = "#" * int(max(0, min(1.0, (db + 60) / 60)) * 30)
                print(
                    f"\r[{bar:<30}] {db:6.1f} dB  peak {peak_db:6.1f}  "
                    f"{'SPEECH' if speech else '      '}  cuts={seen}",
                    end="",
                    flush=True,
                )
        raw.terminate()
    else:
        while not stop.is_set() and recorder.running:
            level = recorder.level
            db = -99.0 if level <= 1e-6 else 20.0 * math.log10(level)
            peak_db = max(peak_db, db)
            bar = "#" * int(max(0, min(1.0, (db + 60) / 60)) * 30)
            print(
                f"\r[{bar:<30}] {db:6.1f} dB  peak {peak_db:6.1f}  "
                f"{'SPEECH' if recorder.speaking else '      '}  cuts={seen}",
                end="",
                flush=True,
            )
            time.sleep(0.1)
        recorder.stop()

    print(f"\n\npeak level {peak_db:.1f} dBFS, {seen} utterance(s) cut")
    if seen == 0:
        print("the gate never opened: try --vad-aggressiveness 0, then --gain 4 or 8")

    if opts.save_audio and captured:
        with wave.open(opts.save_audio, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(b"".join(captured))
        secs = len(captured) * len(captured[0]) / (SAMPLE_RATE * SAMPLE_WIDTH)
        print(f"wrote {opts.save_audio} ({secs:.1f}s)")
    return 0


def _debug_asr(opts) -> int:
    """The whole pipeline except the widget and the typing."""
    from .asr import AsrError, Transcriber, WhisperServer
    from .audio import Recorder

    if not opts.model:
        print("--model is required", file=sys.stderr)
        return 2

    server = WhisperServer(
        binary=opts.whisper_server,
        model=opts.model,
        vad_model=opts.vad_model,
        host=opts.host,
        port=opts.port,
        threads=opts.threads,
        log_path=opts.server_log,
    )
    transcriber = Transcriber(server, timeout=opts.request_timeout)

    server.start()
    print("waiting for whisper-server…", flush=True)
    deadline = time.monotonic() + opts.load_timeout
    while not server.listening():
        failure = server.failed()
        if failure:
            print(failure, file=sys.stderr)
            return 1
        if time.monotonic() > deadline:
            print("whisper-server did not come up", file=sys.stderr)
            return 1
        time.sleep(0.25)
    print("ready; speak", flush=True)

    language = None if opts.language == "auto" else opts.language

    def on_utterance(utterance):
        nonlocal language
        wav = utterance.wav()
        began = time.monotonic()
        try:
            if language is None:
                reply = transcriber.detect(wav)
                language = reply.language
                native = reply
                print(f"  (detected {language})", flush=True)
            else:
                native = transcriber.transcribe(wav, language)
            native_at = time.monotonic() - began
            print(f"native  [{native_at:5.2f}s] {native.text}", flush=True)

            if language != "en":
                english = transcriber.translate(wav, language)
                print(f"english [{time.monotonic() - began:5.2f}s] {english.text}", flush=True)
        except AsrError as exc:
            print(f"error: {exc}", file=sys.stderr, flush=True)

    recorder = Recorder(
        on_utterance=on_utterance,
        on_error=lambda m: print(f"error: {m}", file=sys.stderr, flush=True),
        device=opts.device,
        aggressiveness=opts.vad_aggressiveness,
        silence_ms=opts.silence_ms,
        start_ms=opts.start_ms,
        max_ms=opts.max_seconds * 1000,
        gain=opts.gain,
    )
    recorder.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set() and recorder.running:
        time.sleep(0.2)
    recorder.stop()
    server.stop()
    return 0


def _debug_file(opts) -> int:
    """The whole pipeline on a fixed file: endpointer, then whisper.

    Deterministic, so it can answer "did I break the cutting or the
    recognition" without anyone having to say anything into a microphone.
    """
    from .asr import AsrError, Transcriber, WhisperServer, is_rtl
    from .audio import Endpointer, frames_from_wav

    if not opts.model:
        print("--model is required", file=sys.stderr)
        return 2

    server = WhisperServer(
        binary=opts.whisper_server,
        model=opts.model,
        vad_model=opts.vad_model,
        host=opts.host,
        port=opts.port,
        threads=opts.threads,
        log_path=opts.server_log,
    )
    transcriber = Transcriber(server, timeout=opts.request_timeout)
    server.start()
    deadline = time.monotonic() + opts.load_timeout
    while not server.listening():
        failure = server.failed()
        if failure:
            print(failure, file=sys.stderr)
            return 1
        if time.monotonic() > deadline:
            print("whisper-server did not come up", file=sys.stderr)
            return 1
        time.sleep(0.25)

    endpointer = Endpointer(
        aggressiveness=opts.vad_aggressiveness,
        silence_ms=opts.silence_ms,
        start_ms=opts.start_ms,
        max_ms=opts.max_seconds * 1000,
    )
    utterances = []
    for frame in frames_from_wav(opts.debug_file):
        cut = endpointer.feed(frame)
        if cut is not None:
            utterances.append(cut)
    tail = endpointer.flush()
    if tail is not None:
        utterances.append(tail)

    print(f"{opts.debug_file}: {len(utterances)} utterance(s)")
    language = None if opts.language == "auto" else opts.language
    status = 0
    for n, utterance in enumerate(utterances, 1):
        wav = utterance.wav()
        began = time.monotonic()
        try:
            if language is None:
                reply = transcriber.detect(wav)
                language = reply.language
                native = reply
            else:
                native = transcriber.transcribe(wav, language)
            took = time.monotonic() - began
            arrow = "rtl" if is_rtl(native.text) else "ltr"
            print(f"  {n}: {utterance.seconds:.2f}s lang={language} {arrow} [{took:.2f}s]")
            print(f"     native : {native.text}")
            if language != "en":
                english = transcriber.translate(wav, language)
                print(f"     english: {english.text}  [{time.monotonic() - began:.2f}s total]")
            else:
                print("     english: (same -- already english, second pass skipped)")
        except AsrError as exc:
            print(f"  {n}: error: {exc}", file=sys.stderr)
            status = 1
    server.stop()
    return status


def main(argv: list[str] | None = None) -> int:
    opts = parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(opts.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
    )

    if opts.debug_audio:
        return _debug_audio(opts)
    if opts.debug_asr:
        return _debug_asr(opts)
    if opts.debug_file:
        return _debug_file(opts)

    if not opts.model:
        print("--model is required", file=sys.stderr)
        return 2
    if not os.environ.get("WAYLAND_DISPLAY"):
        print("no WAYLAND_DISPLAY: the widget is a wayland layer surface", file=sys.stderr)
        return 1

    from .ui import run

    # Ctrl-C in a terminal should close the widget, which GTK otherwise
    # swallows along with the rest of the default handler.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return run(opts)


if __name__ == "__main__":
    sys.exit(main())
