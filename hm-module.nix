# The home-manager module: options, the stylesheet, and the hyprland glue.
#
# It declares its own colours and fonts rather than reading a `theme` module
# argument, because this flake has to be usable in a config that has no such
# thing. A config that does have one passes it in -- see the README -- so there
# is still one palette in practice and no hex codes anywhere but the defaults
# below.
self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.my.home.desktop.voiceKeyboard;

  models = pkgs.callPackage "${self}/models.nix" { };

  # Built here rather than taken from the flake's `packages`, because the
  # model and the backend are options and their combinations are not worth
  # enumerating as outputs.
  app = pkgs.callPackage "${self}/package.nix" {
    inherit models;
    model = cfg.model;
    vulkan = cfg.backend == "vulkan";
  };

  # "89b4fa" -> 137, for the two hex digits starting at `at`.
  channel = hex: at: lib.fromHexString (lib.substring at 2 hex);
  css = hex: "#${hex}";
  rgba =
    hex: alpha:
    "rgba(${toString (channel hex 0)}, ${toString (channel hex 2)}, ${toString (channel hex 4)}, ${alpha})";

  inherit (cfg) colors;

  style = pkgs.writeText "voice-keyboard.css" ''
    /* The card floats; the surface under it must not be painted. */
    window {
      background: transparent;
    }

    /* Same frosted slab as the bar and the switcher, so the widget reads as
       part of the same desk rather than a dialog from somewhere else. */
    .vk-card {
      padding: 14px 16px;
      border-radius: 20px;
      background: linear-gradient(160deg, ${rgba colors.base "0.78"} 0%, ${rgba colors.mantle "0.84"} 100%);
      border: 1px solid ${rgba colors.lavender "0.14"};
    }

    .vk-status, .vk-dot, .vk-badge, .vk-target-button, .vk-start {
      font-family: "${cfg.fonts.monospace}", "${cfg.fonts.sansSerif}", sans-serif;
      font-size: ${toString cfg.fontSize}px;
    }

    .vk-status {
      color: ${css colors.subtext0};
    }

    /* The state light. Each state gets a colour because this is the only
       thing on the card that says whether it is listening, thinking, or
       broken. */
    .vk-dot-idle      { color: ${css colors.overlay0}; }
    .vk-dot-loading   { color: ${css colors.yellow}; }
    .vk-dot-listening { color: ${css colors.green}; }
    .vk-dot-thinking  { color: ${css colors.blue}; }
    .vk-dot-error     { color: ${css colors.red}; }

    .vk-badge {
      padding: 1px 10px;
      border-radius: 10px;
      background: ${rgba colors.surface0 "0.85"};
      border: 1px solid ${rgba colors.lavender "0.12"};
      color: ${css colors.lavender};
      box-shadow: none;
    }

    .vk-badge:hover {
      background: ${rgba colors.surface1 "0.9"};
    }

    /* Level meter. Deliberately quiet: it only has to prove the microphone
       is alive, and it must not pull the eye off the text. */
    .vk-meter trough {
      background: ${rgba colors.surface0 "0.9"};
      border-radius: 3px;
      min-height: 6px;
    }

    .vk-meter block.filled {
      background: ${css colors.teal};
      border-radius: 3px;
    }

    /* The two panes. The native one is what was said and is the louder of
       the two; the English one is a gloss. */
    .vk-native {
      color: ${css colors.text};
      font-family: "${cfg.fonts.sansSerif}", sans-serif;
      font-size: ${toString (cfg.fontSize + 4)}px;
    }

    .vk-english {
      color: ${css colors.subtext1};
      font-family: "${cfg.fonts.sansSerif}", sans-serif;
      font-size: ${toString cfg.fontSize}px;
    }

    .vk-start {
      padding: 4px 18px;
      border-radius: 12px;
      background: ${rgba colors.green "0.18"};
      border: 1px solid ${rgba colors.green "0.55"};
      color: ${css colors.text};
      box-shadow: none;
    }

    /* Stop is loud on purpose: a widget that is listening to the room should
       be obvious about it. */
    .vk-start.vk-stop {
      background: ${rgba colors.red "0.20"};
      border-color: ${rgba colors.red "0.65"};
    }

    /* Hide, not quit. Quiet, because it is not the thing to reach for. */
    .vk-close {
      padding: 0 8px;
      border-radius: 10px;
      background: none;
      background-image: none;
      border: 1px solid transparent;
      box-shadow: none;
      color: ${css colors.overlay0};
    }

    .vk-close:hover {
      background: ${rgba colors.red "0.18"};
      color: ${css colors.text};
    }

    /* The microphone menu. Worth having on the card rather than in the
       options: which device is the right one is a question that changes when
       a headset connects, not when the config is rebuilt. */
    .vk-mic {
      font-family: "${cfg.fonts.monospace}", sans-serif;
      font-size: ${toString (cfg.fontSize - 1)}px;
    }

    .vk-mic button {
      padding: 2px 8px;
      border-radius: 10px;
      background: ${rgba colors.surface0 "0.85"};
      border: 1px solid ${rgba colors.lavender "0.12"};
      color: ${css colors.subtext0};
      box-shadow: none;
    }

/* The English pass, as a switch. Lit when it is costing something. */
    .vk-translate {
      padding: 2px 10px;
      border-radius: 10px;
      background: none;
      background-image: none;
      border: 1px solid ${rgba colors.lavender "0.18"};
      box-shadow: none;
      color: ${css colors.overlay0};
      font-family: "${cfg.fonts.monospace}", sans-serif;
      font-size: ${toString (cfg.fontSize - 1)}px;
    }

    .vk-translate:checked {
      background: ${rgba colors.teal "0.20"};
      border-color: ${rgba colors.teal "0.6"};
      color: ${css colors.text};
    }

    .vk-badge button {
      padding: 1px 8px;
      border-radius: 10px;
      background: ${rgba colors.surface0 "0.85"};
      border: 1px solid ${rgba colors.lavender "0.12"};
      color: ${css colors.lavender};
      box-shadow: none;
    }

    .vk-target {
      border-radius: 12px;
      background: ${rgba colors.surface0 "0.8"};
      padding: 2px;
    }

    .vk-target-button {
      padding: 2px 12px;
      border-radius: 10px;
      background: none;
      background-image: none;
      border: 1px solid transparent;
      box-shadow: none;
      color: ${css colors.subtext0};
    }

    /* Which pane gets typed. This is the one control whose state the user
       has to be able to read at a glance, so the selection is loud. */
    .vk-target-button:checked {
      background: ${rgba colors.blue "0.22"};
      border-color: ${rgba colors.blue "0.7"};
      color: ${css colors.text};
    }
  '';

  arguments = lib.cli.toCommandLineShellGNU { } (
    {
      css = style;
      language = cfg.language;
      output = cfg.output;
      position = cfg.position;
      margin = cfg.margin;
      width = cfg.width;
      host = cfg.host;
      port = cfg.port;
      "idle-timeout" = cfg.idleTimeout;
      "vad-aggressiveness" = cfg.vadAggressiveness;
      "silence-ms" = cfg.silenceMs;
      "start-ms" = cfg.startMs;
      "max-seconds" = cfg.maxSeconds;
      "inject-method" = cfg.injectMethod;
      bidi = cfg.bidi;
      "meter-gain" = cfg.meterGain;
      gain = cfg.gain;
    }
    // lib.optionalAttrs (cfg.threads != null) { threads = cfg.threads; }
    // lib.optionalAttrs (cfg.device != null) { device = cfg.device; }
    // lib.optionalAttrs (!cfg.restoreLayout) { "no-restore-layout" = true; }
    // lib.optionalAttrs (!cfg.translate) { "no-translate" = true; }
  );

  # One key both opens and closes it. pgrep matches the python command line
  # rather than the wrapper's name, because the wrapper execs python3 and so
  # stops being called voice-keyboard the moment it starts working.
  pattern = "-m voice_keyboard";

  toggle = pkgs.writeShellApplication {
    name = "voice-keyboard-toggle";
    runtimeInputs = [
      app
      pkgs.procps
    ];
    text = ''
      # The -- matters. The pattern begins with a dash, and without it pgrep
      # parses the pattern as its own options and matches nothing.
      #
      # SIGUSR1 rather than SIGTERM: the widget hides on it and keeps the
      # model loaded, so bringing the card back is instant instead of another
      # wait for gigabytes to load. "quit" is there for when that is what was
      # actually meant.
      if [ "''${1:-toggle}" = quit ]; then
        pkill -f -- ${lib.escapeShellArg pattern} || true
      elif pgrep -f -- ${lib.escapeShellArg pattern} >/dev/null 2>&1; then
        pkill -USR1 -f -- ${lib.escapeShellArg pattern}
      else
        voice-keyboard ${arguments} ${lib.escapeShellArgs cfg.extraArgs} &
      fi
    '';
  };
in
{
  options.my.home.desktop.voiceKeyboard = {
    enable = lib.mkEnableOption "the whisper.cpp dictation widget";

    model = lib.mkOption {
      type = lib.types.enum [
        "large-v3"
        "large-v3-turbo"
        "medium"
        "medium-q5_0"
        "small"
      ];
      default = "medium-q5_0";
      description = ''
        Which weights to use. This is the entire latency budget: whisper's
        encoder always processes a 30 second window, so a three second
        utterance costs exactly what a thirty second one does, and `audio_ctx`
        is ignored by this build. Measured on an Iris Xe over Vulkan, warm,
        per request: small 1.2s, medium 3.5s, large-v3 6.5s.

        medium-q5_0 by default: the medium tier is the first that transcribed
        Portuguese correctly in testing, and quantising it to 5 bits cut a
        request from 3.4s to 2.0s on the same audio for the same transcript.
        large-v3 at 6.5s a request does not behave like a keyboard. Note large-v3-turbo shares large-v3's
        encoder, so it is no faster here, and it was distilled without the
        translation task -- the English pane degrades badly on it.
      '';
    };

    backend = lib.mkOption {
      type = lib.types.enum [
        "vulkan"
        "cpu"
      ];
      default = "vulkan";
      description = ''
        Where inference runs. Vulkan measured twice as fast on the encoder and
        four times on the decoder than 16 CPU threads on this hardware, so it
        is the default despite being an integrated GPU. It costs a
        from-source build of whisper-cpp.
      '';
    };

    language = lib.mkOption {
      type = lib.types.enum [
        "auto"
        "he"
        "en"
        "pt"
        "es"
      ];
      default = "auto";
      description = ''
        The language being spoken, or auto to settle it from the first
        utterance of each session and reuse it after that.

        Auto-detection is not free: it costs an extra pass, and naming the
        result needs a second one, so the first utterance of a session takes
        roughly three times as long as the rest. Pinning a language skips
        both and is also more accurate. The badge on the widget re-detects on
        click, so pinning the usual language is not a commitment.
      '';
    };

    translate = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Also ask for the English translation of everything said.

        It is a second pass over the same audio, so turning it off roughly
        halves the work per utterance. The pane being typed arrives just as
        quickly either way, but the server is free for the next utterance
        twice as soon, which is what continuous dictation actually feels
        like. Turn it off if the English pane is not being read. Utterances
        already in English never pay for it regardless.
      '';
    };

    output = lib.mkOption {
      type = lib.types.enum [
        "native"
        "english"
      ];
      default = "native";
      description = ''
        Which of the two panes is typed: the words as spoken, or their English
        translation. Switchable on the widget; this is only the starting
        position.
      '';
    };

    autostart = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Start the widget with the session. Off by default: it listens to the
        room when told to and nothing else, and the keybind below brings it up
        in well under a second. The model is loaded on the first press of
        start either way, never at login.
      '';
    };

    toggleKey = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "$mod, V";
      example = "$mod SHIFT, D";
      description = "Hyprland bind that opens and closes the widget. null for none.";
    };

    idleTimeout = lib.mkOption {
      type = lib.types.int;
      default = 600;
      description = ''
        Seconds of disuse after which the model is released, so that a
        multi-gigabyte process is not carried around for the rest of the day.
        0 keeps it loaded.
      '';
    };

    threads = lib.mkOption {
      type = lib.types.nullOr lib.types.ints.positive;
      default = null;
      description = "Threads for whisper. null leaves the choice to whisper.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Address whisper-server listens on. Loopback: it takes audio over
        unauthenticated HTTP and has no business being reachable.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 18080;
      description = ''
        Port for whisper-server. The widget attaches to a server already
        listening here instead of starting a second copy, so a hand-started
        server or a systemd unit is equally usable.
      '';
    };

    device = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "alsa_input.pci-0000_00_1f.3.analog-stereo";
      description = "PipeWire source to record from. null follows the system default.";
    };

    vadAggressiveness = lib.mkOption {
      type = lib.types.ints.between 0 3;
      default = 2;
      description = ''
        How readily webrtcvad calls a frame speech, 0 permissive to 3 strict.
        Raise it in a noisy room, lower it if quiet speech is being missed.
      '';
    };

    silenceMs = lib.mkOption {
      type = lib.types.ints.positive;
      default = 700;
      description = ''
        Pause that ends an utterance and sends it to be transcribed. Shorter
        feels more responsive but cuts mid-sentence at any hesitation.
      '';
    };

    startMs = lib.mkOption {
      type = lib.types.ints.positive;
      default = 250;
      description = "Speech needed to begin an utterance, which is what keeps coughs out.";
    };

    maxSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 25;
      description = ''
        Hard cap on one utterance, so someone who never pauses still gets
        their words typed. Long utterances are expensive: the cost is the
        model's, per request, regardless of how little was said.
      '';
    };

    gain = lib.mkOption {
      type = lib.types.float;
      default = 1.0;
      example = 8.0;
      description = ''
        Multiply the microphone signal before the gate sees it. Some
        microphones -- a webcam capsule a metre away, say -- sit close enough
        to the noise floor that webrtcvad never opens, and the widget appears
        to do nothing while someone talks at it. Raise the capture volume
        first; this is the blunt instrument for when that is not enough, and
        it lifts the noise along with the voice.
      '';
    };

bidi = lib.mkOption {
      type = lib.types.enum [
        "logical"
        "visual"
      ];
      default = "logical";
      description = ''
        Character order for right-to-left text.

        logical is correct Unicode -- first word spoken is first in memory --
        and every editor, browser and chat client lays it out right-to-left
        itself. Terminals do not: they draw storage order left to right, so
        correct Hebrew arrives looking mirrored. visual pre-reorders the text
        so it reads properly there, at the cost of the bytes no longer being
        correct Unicode: anything that *does* implement bidi will then lay
        out already-laid-out text and show it reversed.

        So this follows the window being dictated into, not the language, and
        there is a toggle on the widget for it.
      '';
    };

    injectMethod = lib.mkOption {
      type = lib.types.enum [
        "wtype"
        "clipboard"
      ];
      default = "wtype";
      description = ''
        How the text reaches the focused window. wtype types it over the
        virtual-keyboard protocol with a keymap of its own, which is what
        makes Hebrew work at all. clipboard is the fallback for applications
        that mishandle that -- it clobbers the clipboard and assumes ctrl+v
        means paste, hence not the default.
      '';
    };

    restoreLayout = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Put the XKB layout back after typing. wtype attaches its own keymap,
        which on hyprland can leave the keyboard on a different layout than
        the one in use -- so the next thing typed by hand comes out in the
        wrong script.
      '';
    };

    position = lib.mkOption {
      type = lib.types.enum [
        "bottom"
        "top"
      ];
      default = "bottom";
      description = "Screen edge the card is anchored to.";
    };

    margin = lib.mkOption {
      type = lib.types.int;
      default = 80;
      description = "Gap between the card and that edge, in pixels.";
    };

    width = lib.mkOption {
      type = lib.types.ints.positive;
      default = 560;
      description = "Card width in pixels. The panes ellipsize to fit it.";
    };

    fontSize = lib.mkOption {
      type = lib.types.ints.positive;
      default = 12;
      description = "Base font size in pixels; the native pane is four larger.";
    };

    meterGain = lib.mkOption {
      type = lib.types.float;
      default = 8.0;
      description = "Level meter sensitivity. Only affects the picture, never the gate.";
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "--request-timeout"
        "300"
      ];
      description = "Extra arguments, appended last so they win.";
    };

    colors = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      description = ''
        Palette as bare RRGGBB hex. Defaults to Catppuccin Mocha; a config
        with a palette of its own should pass it in so this widget is themed
        with everything else.
      '';
      default = {
        base = "1e1e2e";
        mantle = "181825";
        surface0 = "313244";
        surface1 = "45475a";
        overlay0 = "6c7086";
        text = "cdd6f4";
        subtext0 = "a6adc8";
        subtext1 = "bac2de";
        blue = "89b4fa";
        lavender = "b4befe";
        green = "a6e3a1";
        teal = "94e2d5";
        yellow = "f9e2af";
        red = "f38ba8";
      };
    };

    fonts = {
      monospace = lib.mkOption {
        type = lib.types.str;
        default = "JetBrainsMono Nerd Font";
        description = "Font for the status line and the controls.";
      };
      sansSerif = lib.mkOption {
        type = lib.types.str;
        default = "Noto Sans";
        description = ''
          Font for the two transcript panes. It needs Hebrew coverage, which
          is why the panes do not use the monospace font.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        home.packages = [
          app
          toggle
        ];
      }

      (lib.mkIf (config.wayland.windowManager.hyprland.enable or false) {
        wayland.windowManager.hyprland.settings = {
          exec-once = lib.optional cfg.autostart "${lib.getExe toggle}";

          bind = lib.optional (cfg.toggleKey != null) "${cfg.toggleKey}, exec, ${lib.getExe toggle}";

          # Matches the namespace ui.py sets on the layer surface.
          #
          # fade, not popin: the surface is the size of the screen and the
          # card is placed inside it, so scaling the surface would swing the
          # card in from somewhere it never was. ignore_alpha keeps the blur
          # on the card rather than under the whole transparent sheet.
          layerrule = [
            "blur on, ignore_alpha 0.1, animation fade, match:namespace voice-keyboard"
          ];
        };
      })
    ]
  );
}
