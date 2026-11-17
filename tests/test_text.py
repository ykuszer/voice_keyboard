"""Tests for the text handling that runs on every transcript.

These are the pure functions, and they are tested because they are the ones
that can quietly corrupt output: collapse_repeats edits what gets typed, and
is_rtl decides which way a line of Hebrew runs. Everything else in this
project needs a microphone, a GPU or a compositor, and is checked by hand with
--debug-file and --debug-audio.

Deliberately dependency-free -- asr.py imports nothing but the standard
library, so this runs under a bare python3 with no pytest and no webrtcvad.
"""

import sys

from voice_keyboard.asr import collapse_repeats, is_rtl, language_code

COLLAPSE = [
    # (text, expected, why)
    (
        "Hello, I want to test this voice keyboard today.",
        "Hello, I want to test this voice keyboard today.",
        "an ordinary sentence must come through untouched",
    ),
    ("no no no", "no no no", "three repeats of one word is emphasis, not a fault"),
    ("very very very very", "very very very very", "four still is"),
    ("ha ha ha ha ha ha ha ha", "ha", "eight is a loop"),
    (
        "I don't think I can do this. " * 6,
        "I don't think I can do this.",
        "the loop whisper actually produced in testing",
    ),
    ("one two one two one two", "one two", "a two-word phrase repeated is a loop"),
    (
        "keep this but then end end end end end end",
        "keep this but then end",
        "what came before the loop is kept",
    ),
    (
        "שלום שלום שלום שלום",
        "שלום שלום שלום שלום",
        "the single-word rule is not about script",
    ),
    ("שלום עולם שלום עולם שלום עולם", "שלום עולם", "nor is the phrase rule"),
    ("short", "short", "too short to have a period"),
    ("", "", "empty"),
]

RTL = [
    ("שלום, איך אתה", True, "hebrew"),
    ("Hello there", False, "latin"),
    ("", False, "empty is not rtl"),
    ("123 שלום", True, "digits are not directional, so look past them"),
    ("123 hello", False, "...and then find latin"),
    ("!!! ...", False, "punctuation alone is not rtl"),
]

CODES = [
    ("hebrew", "he"),
    ("english", "en"),
    ("spanish", "es"),
    ("portuguese", "pt"),
    ("Hebrew", "he"),
    ("he", "he"),
    ("auto", None),
    ("unknown", None),
    (None, None),
]


def main() -> int:
    failures = 0

    for text, want, why in COLLAPSE:
        got = collapse_repeats(text.strip())
        if got != want:
            failures += 1
            print(f"FAIL collapse_repeats: {why}\n  {text.strip()!r}\n  got    {got!r}\n  wanted {want!r}")

    for text, want, why in RTL:
        got = is_rtl(text)
        if got != want:
            failures += 1
            print(f"FAIL is_rtl: {why}\n  {text!r} -> {got}, wanted {want}")

    for name, want in CODES:
        got = language_code(name)
        if got != want:
            failures += 1
            print(f"FAIL language_code: {name!r} -> {got!r}, wanted {want!r}")

    total = len(COLLAPSE) + len(RTL) + len(CODES)
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
