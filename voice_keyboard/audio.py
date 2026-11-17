"""Microphone capture and utterance endpointing.

``pw-record`` is spawned as a child and writes headerless PCM into a pipe:
16 kHz, mono, signed 16-bit little-endian. That rate is not a compromise
between two consumers, it is the one both of them want -- whisper resamples
everything to 16 kHz internally, and webrtcvad accepts only 8/16/32/48 kHz --
so nothing anywhere in this path has to resample.

The endpointer lives here, and is deliberately cheap, because whisper's own VAD
can only segment audio that has already been submitted; it cannot say when the
speaker paused. So a small state machine over 20 ms frames decides where one
utterance ends and the next begins, and whisper's VAD is left the narrower job
of tidying a chunk that has already been cut.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import threading
import warnings
import wave
from collections import deque
from dataclasses import dataclass

import numpy as np

# webrtcvad 2.0.10 imports pkg_resources at module scope, which is deprecated
# and says so on stderr every single start. Nothing here can act on it.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=UserWarning)
    import webrtcvad

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # s16
CHANNELS = 1

# webrtcvad takes 10, 20 or 30 ms frames and nothing else. 20 ms is the middle
# one: long enough that the gate does not chatter, short enough that the level
# meter looks live.
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS // 1000  # 640

BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS


def _frames(ms: int) -> int:
    """Round a duration in milliseconds up to whole frames, minimum one."""
    return max(1, round(ms / FRAME_MS))


@dataclass(frozen=True)
class Utterance:
    """One stretch of speech, cut at the pauses either side of it."""

    pcm: bytes

    @property
    def seconds(self) -> float:
        return len(self.pcm) / BYTES_PER_SECOND

    def wav(self) -> bytes:
        """The same audio with a RIFF header on it, which is what the server
        wants. Built in memory: these are seconds-long, and a temporary file
        would only be something else to fail to clean up."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(self.pcm)
        return buf.getvalue()


@dataclass(frozen=True)
class Source:
    """A microphone PipeWire is willing to give us."""

    name: str  # node.name, which is what --target wants
    description: str  # what to put in front of a person
    is_default: bool = False

    def label(self) -> str:
        return f"{self.description} (default)" if self.is_default else self.description


def sources() -> list[Source]:
    """Every capture device, best-effort.

    Shelling out to pw-dump rather than binding libpipewire: this runs when a
    menu is opened, not per frame, and the alternative is a second native
    dependency for something a JSON parse answers.
    """
    try:
        done = subprocess.run(
            ["pw-dump"], capture_output=True, timeout=5, check=False
        )
        nodes = json.loads(done.stdout or b"[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log.warning("could not list audio sources: %s", exc)
        return []

    default_name = None
    for node in nodes:
        meta = node.get("metadata")
        if not meta:
            continue
        for entry in meta:
            if entry.get("key") == "default.audio.source":
                value = entry.get("value")
                if isinstance(value, dict):
                    default_name = value.get("name")
                elif isinstance(value, str):
                    default_name = value

    found: list[Source] = []
    for node in nodes:
        props = (node.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Source":
            continue
        name = props.get("node.name")
        if not name:
            continue
        found.append(
            Source(
                name=name,
                description=props.get("node.description") or props.get("node.nick") or name,
                is_default=name == default_name,
            )
        )
    found.sort(key=lambda s: (not s.is_default, s.description.lower()))
    return found


def read_exact(stream, count: int) -> bytes:
    """Read exactly ``count`` bytes, or fewer only at end of stream.

    A pipe hands over whatever has arrived, which is not necessarily a whole
    frame: pw-record writes on its own schedule and the first read in
    particular tends to be short. Treating a short read as end-of-stream --
    which is what a bare ``read(n)`` on an unbuffered pipe invites -- stops
    capture a fraction of a second after it starts, and looks from outside
    like a microphone that went dead for no reason.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def amplify(frame: bytes, gain: float) -> bytes:
    """Scale a frame, clipping rather than wrapping.

    Some microphones are simply quiet -- a webcam's built-in capsule a metre
    away is 30dB down on a headset -- and webrtcvad will not open the gate on
    a signal near the noise floor. Boosting here, before both the gate and
    whisper, is the difference between a mic that works and one that appears
    to do nothing at all. It raises the noise with the voice, so it is a last
    resort after raising the capture volume properly.
    """
    if gain == 1.0:
        return frame
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) * gain
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def frame_level(frame: bytes) -> float:
    """RMS of one frame, roughly 0..1, for the level meter.

    Only ever feeds a widget, so there is no need to be careful about what
    reference level this is relative to -- it has to move when someone talks
    and sit still when they do not.
    """
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)) / 32768.0)


class Endpointer:
    """Decides where utterances begin and end, one 20 ms frame at a time.

    Deliberately a pure function of the frames fed to it, with no audio device
    anywhere near it, so that it can be exercised from a file or a test.
    """

    def __init__(
        self,
        *,
        aggressiveness: int = 2,
        start_ms: int = 250,
        silence_ms: int = 700,
        preroll_ms: int = 300,
        max_ms: int = 25_000,
        min_voiced_ms: int = 300,
        tail_ms: int = 200,
    ) -> None:
        self._vad = webrtcvad.Vad(aggressiveness)

        self._start_frames = _frames(start_ms)
        self._end_frames = _frames(silence_ms)
        self._max_frames = _frames(max_ms)
        self._min_voiced_frames = _frames(min_voiced_ms)
        self._tail_frames = _frames(tail_ms)

        # Holds the recent past while idle, so that when speech is confirmed
        # the frames that proved it -- and a little of the breath before them,
        # which is where the first phoneme usually is -- are still available.
        self._ring: deque[bytes] = deque(maxlen=_frames(preroll_ms) + self._start_frames)

        self._reset()

    def _reset(self) -> None:
        self._speaking = False
        self._buf: list[bytes] = []
        self._speech_run = 0
        self._silence_run = 0
        self._voiced = 0
        self._ring.clear()

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, frame: bytes) -> Utterance | None:
        """Absorb one frame. Returns an utterance on the frame that closes one."""
        is_speech = self._vad.is_speech(frame, SAMPLE_RATE)

        if not self._speaking:
            self._ring.append(frame)
            if not is_speech:
                self._speech_run = 0
                return None

            self._speech_run += 1
            if self._speech_run < self._start_frames:
                return None

            # Confirmed. The ring is exactly the preroll plus the run that
            # proved it, so it becomes the head of the utterance wholesale.
            self._speaking = True
            self._buf = list(self._ring)
            self._voiced = self._speech_run
            self._silence_run = 0
            self._ring.clear()
            return None

        self._buf.append(frame)
        if is_speech:
            self._voiced += 1
            self._silence_run = 0
        else:
            self._silence_run += 1

        # A long enough pause is the end of a sentence. The hard cap is there
        # so that someone who never pauses still gets their words typed.
        if self._silence_run >= self._end_frames or len(self._buf) >= self._max_frames:
            return self._close()
        return None

    def flush(self) -> Utterance | None:
        """Close whatever is open, for when the user presses stop mid-sentence."""
        if not self._speaking:
            self._reset()
            return None
        return self._close()

    def _close(self) -> Utterance | None:
        buf, voiced, silence = self._buf, self._voiced, self._silence_run
        self._reset()

        # A cough, a door, a chair. Not worth a decode, and whisper will
        # cheerfully invent a sentence for it if asked.
        if voiced < self._min_voiced_frames:
            log.debug("dropping %d frames, only %d voiced", len(buf), voiced)
            return None

        # Keep a little of the trailing silence and throw the rest: whisper
        # wants a moment of room tone to close on, but a full second of it is
        # just something for the model to hallucinate into.
        drop = max(0, silence - self._tail_frames)
        kept = buf[: len(buf) - drop] if drop else buf
        return Utterance(b"".join(kept))


def frames_from_wav(path: str):
    """Yield 20 ms frames from a 16 kHz mono 16-bit wav file.

    The point of entry for testing: the endpointer and everything downstream
    of it can then be exercised on a fixed file, with no microphone, no room
    and no one having to say anything twice.
    """
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (
            SAMPLE_RATE,
            CHANNELS,
            SAMPLE_WIDTH,
        ):
            raise ValueError(
                f"{path}: need {SAMPLE_RATE} Hz mono 16-bit, got "
                f"{w.getframerate()} Hz {w.getnchannels()}ch {w.getsampwidth() * 8}-bit"
            )
        while True:
            chunk = w.readframes(FRAME_BYTES // SAMPLE_WIDTH)
            if len(chunk) < FRAME_BYTES:
                return
            yield chunk


class Recorder:
    """A microphone, framed and endpointed, with a thread behind it.

    ``on_utterance`` is called from the reader thread, so anything touching a
    GTK widget has to hop to the main loop itself.
    """

    def __init__(
        self,
        *,
        on_utterance,
        device: str | None = None,
        on_error=None,
        gain: float = 1.0,
        **endpointer_kwargs,
    ) -> None:
        self._on_utterance = on_utterance
        self._on_error = on_error
        self._device = device
        self._gain = gain
        self._endpointer_kwargs = endpointer_kwargs

        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

        # Read by the UI on a timer rather than pushed, so that a 50 Hz frame
        # rate does not turn into 50 main-loop callbacks a second.
        self.level = 0.0
        self.speaking = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _command(self) -> list[str]:
        cmd = [
            "pw-record",
            "--rate",
            str(SAMPLE_RATE),
            "--channels",
            str(CHANNELS),
            "--format",
            "s16",
            # Keeps the pipe trickling in small pieces, so the meter moves
            # with the voice instead of in 100 ms steps.
            "--latency",
            f"{FRAME_MS}ms",
            "--raw",
        ]
        if self._device:
            cmd += ["--target", self._device]
        return cmd + ["-"]

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        cmd = self._command()
        log.info("starting capture: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            self._fail(f"could not start pw-record: {exc}")
            return
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
        self.level = 0.0
        self.speaking = False

    def _fail(self, message: str) -> None:
        log.error("%s", message)
        if self._on_error is not None:
            self._on_error(message)

    def _run(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        endpointer = Endpointer(**self._endpointer_kwargs)

        while not self._stopping.is_set():
            frame = read_exact(proc.stdout, FRAME_BYTES)
            # Short read means the pipe closed: either we are stopping, or
            # pw-record died and its stderr is the interesting part.
            if len(frame) < FRAME_BYTES:
                break

            frame = amplify(frame, self._gain)
            self.level = frame_level(frame)
            utterance = endpointer.feed(frame)
            self.speaking = endpointer.speaking
            if utterance is not None:
                self._on_utterance(utterance)

        self.speaking = False

        # A child that was signalled was killed by us, or by whatever is
        # taking the whole session down with it; either way it is not a
        # microphone fault and must not be reported as one.
        signalled = proc.poll() is not None and proc.returncode is not None and proc.returncode < 0
        if self._stopping.is_set() or signalled:
            utterance = endpointer.flush()
            if utterance is not None:
                self._on_utterance(utterance)
            return

        stderr = b""
        if proc.stderr is not None:
            stderr = proc.stderr.read() or b""
        self._fail(
            "microphone capture stopped: " + (stderr.decode(errors="replace").strip() or "no audio")
        )
