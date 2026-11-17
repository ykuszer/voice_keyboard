"""Getting the text into whatever window has focus.

``wtype`` is the right tool and ``ydotool`` is the wrong one, which is worth
stating because ydotool is already running on this machine for other reasons.
ydotool synthesises *keycodes*, interpreted through whatever XKB layout is
active, so it can only type what the current layout can reach -- Hebrew comes
out as Latin mojibake. wtype talks the Wayland virtual-keyboard protocol and
brings a keymap of its own, so it types codepoints and does not care what
layout is active or what script the text is in.

Text goes in on stdin rather than as an argument: transcripts are arbitrary
text, and a transcript that happens to begin with a dash would otherwise be
parsed as a flag.
"""

from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger(__name__)

# ydotool's keycodes for the clipboard fallback: left ctrl is 29, v is 47.
_CTRL_V = ["29:1", "47:1", "47:0", "29:0"]


def to_visual_order(text: str) -> str:
    """Reorder a line the way a display would, for things that cannot.

    Unicode stores right-to-left text in *logical* order -- first word spoken
    is first in memory -- and leaves it to the renderer to lay it out
    right-to-left. Editors and browsers do that. Terminals almost universally
    do not: they draw characters left to right in storage order, so correct
    Hebrew arrives looking mirrored.

    Converting to visual order here makes it read correctly in a terminal, at
    the cost of the text no longer being correct Unicode: open the same file
    in an editor that *does* implement bidi and it will be reversed, because
    it will faithfully lay out text that has already been laid out. Hence
    opt-in, per target, rather than a default.
    """
    try:
        from bidi import get_display
    except ImportError:  # pragma: no cover - only if the dep is dropped
        try:
            from bidi.algorithm import get_display  # type: ignore[no-redef]
        except ImportError:
            log.warning("python-bidi is unavailable; typing in logical order")
            return text
    # Line by line: the algorithm is defined on paragraphs, and a transcript
    # is usually one line anyway.
    return "\n".join(get_display(line) for line in text.split("\n"))


def _run(cmd: list[str], *, stdin: bytes | None = None, timeout: float = 10.0):
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _keyboards() -> list[dict]:
    """Hyprland's keyboard table, or an empty list if it will not say."""
    try:
        done = _run(["hyprctl", "-j", "devices"], timeout=3)
        return json.loads(done.stdout or b"{}").get("keyboards") or []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log.debug("could not read hyprland devices: %s", exc)
        return []


def _keymaps() -> dict[str, str]:
    """Every keyboard's active layout, by device name.

    All of them, not just the one hyprland calls "main" -- on a machine
    running ydotoold, "main" is ydotoold's own virtual device rather than the
    keyboard anybody types on, so restoring that one would put the layout back
    on a device nobody is using while the real keyboard stayed switched.
    """
    return {
        board["name"]: board.get("active_keymap", "")
        for board in _keyboards()
        if board.get("name")
    }


def _restore_keymaps(before: dict[str, str], limit: int = 8) -> None:
    """Step any keyboard whose layout moved back to where it was.

    wtype attaches a keymap of its own, which on some hyprland versions can
    leave a keyboard on a different layout than the one the user was on -- so
    the next thing they type by hand comes out in the wrong script. There is
    no "set layout by name", so cycle; the number of configured layouts is
    small and the loop is bounded in case the name never comes back.
    """
    for device, wanted in before.items():
        for _ in range(limit):
            now = _keymaps().get(device)
            if now is None or now == wanted:
                break
            done = _run(["hyprctl", "switchxkblayout", device, "next"], timeout=3)
            if done.returncode != 0:
                log.debug("switchxkblayout %s failed: %s", device, done.stderr)
                break
        else:
            log.warning("gave up restoring layout %r on %s", wanted, device)


def type_text(text: str, *, restore_layout: bool = True) -> None:
    """Type ``text`` as if on a keyboard. Raises OSError if wtype is missing."""
    if not text:
        return

    before = _keymaps() if restore_layout else {}

    done = _run(["wtype", "-"], stdin=text.encode("utf-8"), timeout=30)
    if done.returncode != 0:
        raise RuntimeError(
            "wtype failed: " + (done.stderr.decode(errors="replace").strip() or "unknown error")
        )

    if before:
        _restore_keymaps(before)


def paste_text(text: str) -> None:
    """Fallback: put the text on the clipboard and press ctrl+v.

    For the occasional application that mishandles virtual-keyboard input.
    It clobbers the clipboard and depends on ctrl+v meaning paste in the
    focused window, so it is not the default.
    """
    if not text:
        return
    copied = _run(["wl-copy", "--"], stdin=text.encode("utf-8"))
    if copied.returncode != 0:
        raise RuntimeError(
            "wl-copy failed: " + (copied.stderr.decode(errors="replace").strip() or "unknown")
        )
    done = _run(["ydotool", "key", *_CTRL_V])
    if done.returncode != 0:
        raise RuntimeError(
            "ydotool failed: " + (done.stderr.decode(errors="replace").strip() or "unknown")
        )


def inject(
    text: str,
    *,
    method: str = "wtype",
    restore_layout: bool = True,
    bidi: str = "logical",
) -> None:
    if bidi == "visual":
        text = to_visual_order(text)
    if method == "clipboard":
        paste_text(text)
    else:
        type_text(text, restore_layout=restore_layout)
