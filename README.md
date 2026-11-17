# voice keyboard

Dictation for Hyprland. Click start and talk; each utterance is cut at your
natural pauses, transcribed in whatever of **Hebrew, English, Portuguese or
Spanish** you spoke, translated to English, shown in both, and typed into
whatever window has focus as if it came from a keyboard.

A small card sits at the bottom of the screen with the transcript in the
language you spoke and its English translation underneath. A toggle picks
which of the two gets typed.

```
┌───────────────────────────────────────────────┐
│ ● Listening            ▌▌▌▌▌▌░░░░░      [עב]  │
│ שלום, איך אתה מרגיש היום                      │
│ Hello, how are you feeling today              │
│  [ Stop ]                 Type: Native│English │
└───────────────────────────────────────────────┘
```

## What it will and will not do

whisper's translation task only ever goes **into English**. It cannot render
Spanish or Portuguese speech as Hebrew. So "Hebrew output" means transcription:
speak Hebrew, get Hebrew. The second pane is always English.

## Try it

```sh
nix run .#voice-keyboard-small            # a 466MB model, for a quick look
nix run .#voice-keyboard-medium           # the default tier
```

Two headless modes exist because the parts most likely to be wrong are the
parts hardest to see:

```sh
# is the microphone being cut in sensible places?
nix run .#voice-keyboard -- --debug-audio

# what does whisper actually hear? deterministic, from a file
nix run .#voice-keyboard-medium -- --debug-file sample.wav      # 16kHz mono wav
```

## In a NixOS config

Add the flake and import its home-manager module:

```nix
# flake.nix
voice-keyboard = {
  url = "github:you/voice_keyboard";          # or path:/home/you/code/voice_keyboard
  inputs.nixpkgs.follows = "nixpkgs";
};
```

```nix
# some home module
{ inputs, ... }:
{
  imports = [ inputs.voice-keyboard.homeManagerModules.default ];

  my.home.desktop.voiceKeyboard = {
    enable = true;
    model = "medium";
    language = "auto";
  };
}
```

`$mod+V` opens and closes it. The model is loaded on the first press of start,
never at login, and released again after ten minutes of disuse.

If your config has a palette of its own, hand it over rather than letting the
widget use its built-in Catppuccin defaults:

```nix
my.home.desktop.voiceKeyboard = {
  colors = lib.mkDefault theme.colors;
  fonts.sansSerif = lib.mkDefault theme.fonts.sansSerif;   # needs Hebrew coverage
};
```

Every option is documented in `hm-module.nix`; `model`, `language` and
`silenceMs` are the three worth knowing about.

## Choosing a model

This is the entire latency budget. whisper's encoder always processes a
30-second window, so a three-second utterance costs exactly what a thirty
second one does — and `audio_ctx`, which is meant to shorten that, is ignored
by this build. There is no shortcut for short audio.

Measured on an Iris Xe over Vulkan, warm server, ~4s of speech, per request:

| model | per request | disk | notes |
|---|---|---|---|
| `small` | 1.2s | 466MB | mangled Portuguese in testing |
| `medium-q5_0` | **2.0s** | 515MB | **default**; 5-bit medium, 1.7x faster than plain medium |
| `medium` | 3.4s | 1.5GB | first tier to get Portuguese right |
| `large-v3` | 6.5s | 2.9GB | best quality, does not behave like a keyboard |
| `large-v3-turbo` | ~6.5s | 1.5GB | shares large-v3's encoder, so no faster here — and distilled *without* the translation task, so the English pane degrades badly |

Quantising is close to free speed: `medium-q5_0` moves a third of the weight
through memory and returned an identical transcript to plain `medium` on the
sample it was chosen with, though it is not bit-identical in general -- a
second utterance came back "Are things okay?" where plain medium heard
"Everything's okay."

Two things cost more than the model does, and both are switchable:

- **The English pane is a second pass over the same audio.** `translate =
  false` roughly halves the work per utterance. What gets typed arrives just
  as fast either way; the server is simply free for the next utterance twice
  as soon.
- **Auto-detection costs two extra passes, once per session.** Pinning
  `language` skips it: measured 9.7s for the first utterance against 2.1s
  with the language named.

Nothing is slower than two servers sharing one GPU. A widget that is killed
outright used to leave its server behind holding the weights, and the next one
would start another; three at once measured 9.4s a request against 3.4s alone.
The server is now killed by the kernel when its widget dies, so this cannot
accumulate.

Vulkan measured roughly twice as fast as 16 CPU threads on the encoder and
four times on the decoder, so it is the default despite being an integrated
GPU. `backend = "cpu"` if it misbehaves.

Only one request is on the critical path: the native transcript is typed as
soon as it arrives, and the English pane catches up afterwards. An utterance
already in English skips the second pass entirely.

## How it fits together

```
pw-record --raw -        whisper-server (resident, model loaded once)
      │ 16kHz s16 mono         ▲ POST /inference │ JSON
      ▼                        │                 │
┌─────────────────────────────┴─────────────────▼──────────┐
│ voice-keyboard (one python process)                      │
│  reader thread → webrtcvad endpointer → utterance WAV    │
│  worker thread → native, then english → GLib.idle_add    │
│  GTK main loop → layer-shell card                        │
└────────────────────────────┬─────────────────────────────┘
                             ▼ chosen pane on stdin
                       wtype -   → focused window
```

Four things are load-bearing and easy to undo by accident:

- **The card is a layer surface with keyboard mode `NONE`.** A normal window
  would take focus, and the synthesised keystrokes would be typed into this
  widget instead of the text field you are looking at.
- **`wtype`, not `ydotool`.** ydotool synthesises keycodes interpreted through
  the active XKB layout, so Hebrew comes out as Latin mojibake. wtype brings a
  keymap of its own and types codepoints.
- **The language is settled once per session, not per utterance.** Auto-detect
  costs an extra pass and naming the result costs another, so the first
  utterance is roughly three times the cost of the rest; after that the
  language is forced, which is both free and more accurate. Click the badge to
  detect again.
- **Endpointing is client-side.** whisper's own VAD can only segment audio
  already submitted; it cannot tell you when the speaker paused. webrtcvad
  decides where to cut, and whisper's VAD tidies the chunk afterwards.

## Right-to-left text and terminals

Unicode stores right-to-left text in *logical* order: the first word spoken is
the first in memory, and laying it out right-to-left is the renderer's job.
Editors, browsers and chat clients all do that. **Terminals do not** -- they
draw characters left to right in storage order, so perfectly correct Hebrew
arrives looking mirrored.

That is not a transcription fault and there is nothing to fix in the text. For
dictating into a terminal anyway, `bidi = "visual"` (or the `⇄` button on the
widget) pre-reorders the line so it reads correctly there:

```
logical, correct Unicode:  שלום מה נשמע?
visual, for a terminal:    ?עמשנ המ םולש
```

The catch is that visual order is no longer correct Unicode. Anything that
*does* implement bidi will lay out text that has already been laid out and
show it reversed. So it follows the window being typed into, not the language
being spoken, which is why it is a toggle on the card rather than a setting.
Numbers and embedded Latin keep their own direction either way --
`יש לי 25 שקלים` keeps `25`, not `52`.

## Tests

```sh
nix flake check          # includes the text tests
PYTHONPATH=. python3 tests/test_text.py
```

The tests cover the pure text handling — repetition collapsing and
right-to-left detection — because that is the part that can silently corrupt
what gets typed. Whisper handed audio it cannot parse will loop a clause
dozens of times; `collapse_repeats` trims that, while leaving ordinary
emphasis ("no no no") alone. Everything else needs a microphone, a GPU and a
compositor, and is checked with `--debug-file` and `--debug-audio`.
