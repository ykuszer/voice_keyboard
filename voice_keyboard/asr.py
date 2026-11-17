"""The whisper.cpp side: one resident server, and the requests made of it.

The server exists because the model does not fit in a per-utterance process.
Loading large-v3 takes seconds and 3GB; whisper-server loads it once and
answers ``/inference`` over HTTP for as long as it is up, with ``language`` and
``translate`` chosen per request. So a single resident model can produce both
panes.

Three things measured on this hardware shaped what follows, and all three are
worth knowing before changing any of it:

* ``response_format=verbose_json`` is the only way to learn which language
  whisper picked -- and it costs an extra pass (+2.2s on small, +5.8s on
  large-v3) because it also computes word timestamps. So it is used once per
  session to settle the language, never per utterance.
* ``language=auto`` costs an extra detection pass of its own (+5.9s on
  large-v3). Forcing a known language is free *and* more accurate, which is
  the other half of why the language is settled once and then reused.
* ``detect_language=true`` is a red herring: it means "detect and exit", and
  returns an empty transcript.
* ``audio_ctx`` is ignored by this build -- a 3 second utterance costs the
  same encoder pass as a 30 second one. There is no shortcut for short audio,
  which is why the choice of model is the whole latency budget.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)

# whisper reports languages by name; every other part of this wants the code,
# including whisper itself when the language is being forced.
LANGUAGE_CODES = {
    "english": "en",
    "hebrew": "he",
    "portuguese": "pt",
    "spanish": "es",
}

# Scripts are enough to know which way a line of text runs, which is all the
# widget actually needs -- and unlike the language, it is free to work out.
RTL_RANGES = (
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0xFB1D, 0xFB4F),  # Hebrew presentation forms
)


def is_rtl(text: str) -> bool:
    """True if the first strongly-directional character runs right to left."""
    for ch in text:
        point = ord(ch)
        for low, high in RTL_RANGES:
            if low <= point <= high:
                return True
        if ch.isalpha():
            return False
    return False


def language_code(name: str | None) -> str | None:
    """'hebrew' -> 'he'. Passes through anything already a code."""
    if not name:
        return None
    name = name.strip().lower()
    if name in {"auto", "unknown"}:
        return None
    if len(name) == 2:
        return name
    return LANGUAGE_CODES.get(name, name[:2])


def collapse_repeats(
    text: str,
    *,
    min_repeats: int = 3,
    single_word_repeats: int = 6,
    max_period: int = 12,
) -> str:
    """Trim whisper's repetition loops down to one copy.

    Handed audio it cannot make sense of -- too long, too noisy, or not
    speech at all -- whisper falls into repeating the same clause until it
    runs out of context. temperature_inc is meant to break out of that and
    usually does; when it does not, the result is a paragraph of the same
    sentence, and this gets typed into the user's document.

    A repeated *phrase* is a loop: real speech does not say "I want to test
    this" three times running. A repeated single *word* is usually not --
    "no no no", "very very very" are ordinary emphasis -- so a lone word has
    to repeat far more times before it is treated as a fault. Only trailing
    repetition is collapsed, which is where the loops occur.
    """
    words = text.split()
    if len(words) < 2:
        return text
    for period in range(1, max_period + 1):
        needed = single_word_repeats if period == 1 else min_repeats
        if period * needed > len(words):
            continue
        block = words[-period:]
        at = len(words) - period
        count = 1
        while at - period >= 0 and words[at - period : at] == block:
            count += 1
            at -= period
        if count >= needed:
            return " ".join(words[: at + period])
    return text


class AsrError(Exception):
    """Anything that went wrong between the audio and the text."""


@dataclass(frozen=True)
class Reply:
    text: str
    language: str | None = None


class WhisperServer:
    """A resident whisper-server: ours if we started it, shared if not.

    Attaching to a server that is already listening means a systemd unit, or
    another copy of the widget, or a hand-started server for debugging, is all
    equally usable -- and that we never leave two 3GB models loaded.
    """

    def __init__(
        self,
        *,
        binary: str,
        model: str,
        vad_model: str | None = None,
        host: str = "127.0.0.1",
        port: int = 18080,
        threads: int = 0,
        log_path: str | None = None,
    ) -> None:
        self.binary = binary
        self.model = model
        self.vad_model = vad_model
        self.host = host
        self.port = port
        self.threads = threads
        self.log_path = log_path

        self._proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ours(self) -> bool:
        return self._proc is not None

    def listening(self) -> bool:
        """Whether anything is accepting connections on the port.

        whisper-server binds only after the weights are in, so an open port is
        a sound readiness signal. It never logs a 'listening' line, so do not
        be tempted to scrape the log for one.
        """
        try:
            with socket.create_connection((self.host, self.port), timeout=0.25):
                return True
        except OSError:
            return False

    def command(self) -> list[str]:
        cmd = [
            self.binary,
            "--model",
            self.model,
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        if self.threads:
            cmd += ["--threads", str(self.threads)]
        if self.vad_model:
            # Trims each chunk to the speech in it, and keeps whisper from
            # inventing a sentence when handed a cut that turned out to be
            # mostly room tone.
            cmd += ["--vad", "--vad-model", self.vad_model]
        return cmd

    @staticmethod
    def _die_with_parent() -> None:
        """Ask the kernel to kill this child when its parent dies.

        Without it, a widget killed outright -- SIGKILL, a crash, a test
        harness -- leaves a server behind holding gigabytes of weights on the
        GPU. The next widget starts its own, and two or three models then
        contend for the same integrated GPU: measured here at 3.4s a request
        alone against 9.4s with three servers up, which reads as "it got
        slow" long after the cause is out of sight.
        """
        try:
            import ctypes
            import signal as _signal

            PR_SET_PDEATHSIG = 1
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(
                PR_SET_PDEATHSIG, _signal.SIGTERM, 0, 0, 0
            )
        except Exception:  # noqa: BLE001 -- best effort; a leak is survivable
            pass

    def start(self) -> None:
        """Spawn a server unless one is already there. Returns immediately."""
        if self.listening() or self.ours:
            return
        cmd = self.command()
        log.info("starting whisper-server: %s", " ".join(cmd))
        sink = subprocess.DEVNULL
        if self.log_path:
            sink = open(self.log_path, "wb")  # noqa: SIM115 -- lives as long as the child
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=sink,
                stderr=sink,
                stdin=subprocess.DEVNULL,
                preexec_fn=self._die_with_parent,  # noqa: PLW1509 -- that is the point
            )
        except OSError as exc:
            raise AsrError(f"could not start whisper-server: {exc}") from exc

    def failed(self) -> str | None:
        """A reason, if our child has died; None while it is alive or absent."""
        if self._proc is None or self._proc.poll() is None:
            return None
        code = self._proc.returncode
        self._proc = None
        hint = f" (see {self.log_path})" if self.log_path else ""
        return f"whisper-server exited with status {code}{hint}"

    def stop(self) -> None:
        """Stop the server, but only if it is ours to stop."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        log.info("stopping whisper-server")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class Transcriber:
    """Turns an utterance into text, with the session's language settled once."""

    def __init__(self, server: WhisperServer, *, timeout: float = 180.0) -> None:
        self._server = server
        self._timeout = timeout

    def _post(self, wav: bytes, fields: dict[str, str]) -> dict:
        boundary = uuid.uuid4().hex
        sep = f"--{boundary}".encode()
        parts: list[bytes] = []
        for name, value in fields.items():
            parts += [
                sep,
                f'Content-Disposition: form-data; name="{name}"'.encode(),
                b"",
                str(value).encode(),
            ]
        parts += [
            sep,
            b'Content-Disposition: form-data; name="file"; filename="utterance.wav"',
            b"Content-Type: audio/wav",
            b"",
            wav,
            f"--{boundary}--".encode(),
            b"",
        ]
        body = b"\r\n".join(parts)

        request = urllib.request.Request(
            f"{self._server.url}/inference",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = response.read()
        except urllib.error.URLError as exc:
            raise AsrError(f"whisper-server unreachable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise AsrError("whisper-server timed out") from exc

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AsrError(f"whisper-server returned {payload[:200]!r}") from exc

    def _common(self) -> dict[str, str]:
        return {
            # Cheap format. verbose_json would also report the language, and
            # costs an extra pass for word timestamps nobody here reads.
            "response_format": "json",
            "no_timestamps": "true",
            # Start greedy: dictation wants the same words twice for the same
            # audio. But temperature_inc must stay non-zero -- it is what lets
            # whisper retry a segment it has started looping on, and setting
            # it to 0 makes repetition loops permanent.
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "vad": "true",
        }

    def detect(self, wav: bytes) -> Reply:
        """Transcribe *and* report which language it turned out to be.

        The expensive call -- two extra passes, for the auto-detect and for
        the verbose format that is the only thing that will name the result --
        so it is made once a session and its transcript is kept rather than
        thrown away and asked for again.
        """
        fields = self._common() | {
            "response_format": "verbose_json",
            "language": "auto",
            "translate": "false",
        }
        reply = self._post(wav, fields)
        return Reply(
            text=collapse_repeats((reply.get("text") or "").strip()),
            language=language_code(reply.get("language")),
        )

    def transcribe(self, wav: bytes, language: str | None) -> Reply:
        """The words as spoken. ``language`` None means let whisper decide."""
        fields = self._common() | {
            "language": language or "auto",
            "translate": "false",
        }
        reply = self._post(wav, fields)
        return Reply(
            text=collapse_repeats((reply.get("text") or "").strip()),
            language=language,
        )

    def translate(self, wav: bytes, language: str | None) -> Reply:
        """The same utterance in English. whisper only ever translates to it."""
        fields = self._common() | {
            "language": language or "auto",
            "translate": "true",
        }
        reply = self._post(wav, fields)
        return Reply(
            text=collapse_repeats((reply.get("text") or "").strip()),
            language="en",
        )
