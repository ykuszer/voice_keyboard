# The app: one python process, wrapped so that every binary and every typelib
# it reaches for is a store path.
{
  lib,
  writeShellApplication,
  linkFarm,
  python3,
  pipewire,
  whisper-cpp,
  wtype,
  wl-clipboard,
  ydotool,
  hyprland,
  gtk3,
  gtk-layer-shell,
  glib,
  pango,
  gdk-pixbuf,
  librsvg,
  atk,
  harfbuzz,
  gobject-introspection,

  models,

  # Which weights to bake in as the default, or null for a build that carries
  # none and must be given --model. Keys of models.whisper.
  model ? null,

  # Offload to the GPU through Vulkan. Costs a from-source build of
  # whisper-cpp; on an integrated GPU it is not obviously a win, hence an
  # option rather than a decision.
  vulkan ? true,
}:
let
  whisper = if vulkan then whisper-cpp.override { vulkanSupport = true; } else whisper-cpp;

  python = python3.withPackages (ps: [
    ps.pygobject3
    ps.webrtcvad
    ps.numpy
    # UAX #9, for --bidi visual. Reordering right-to-left text correctly is
    # not something to approximate by hand.
    ps.python-bidi
    # webrtcvad 2.0.10 still does `import pkg_resources` at module scope,
    # which python 3.13 no longer ships. Without this it fails on import.
    ps.setuptools
  ]);

  # PYTHONPATH wants the directory *containing* the package, and copying
  # ./voice_keyboard into the store gives a path whose basename is already
  # the package name. A link farm is the cheap way to get a parent directory
  # holding just that one name.
  pysrc = linkFarm "voice-keyboard-src" [
    {
      name = "voice_keyboard";
      path = ./voice_keyboard;
    }
  ];

  weights = if model == null then null else models.whisper.${model};
in
writeShellApplication {
  name = "voice-keyboard";

  runtimeInputs = [
    python
    pipewire # pw-record
    whisper # whisper-server
    wtype # the virtual keyboard
    hyprland # hyprctl, to put the xkb layout back after wtype
    wl-clipboard # the clipboard fallback in inject.py
    ydotool # ...and the ctrl+v that goes with it
  ];

  # pygobject finds its typelibs through GI_TYPELIB_PATH, which nothing sets
  # for a plain script; GTK and the layer-shell binding both have to be named
  # here or the imports fail at startup. Same list as the fleet config's
  # switcher daemon, for the same reason.
  #
  # Defaults are passed before "$@" so that anything baked in here can still
  # be overridden on the command line: argparse keeps the last occurrence.
  text = ''
    export GDK_PIXBUF_MODULE_FILE=${librsvg}/${gdk-pixbuf.binaryDir}/loaders.cache
    export GI_TYPELIB_PATH=${
      lib.makeSearchPath "lib/girepository-1.0" [
        gtk3
        gtk-layer-shell
        glib.out
        pango.out
        gdk-pixbuf.out
        atk
        harfbuzz
        # Gtk's own typelib depends on cairo's, which lives here.
        gobject-introspection.out
      ]
    }
    export PYTHONPATH=${pysrc}''${PYTHONPATH:+:$PYTHONPATH}

    exec python3 -m voice_keyboard \
      --whisper-server ${lib.getExe' whisper "whisper-server"} \
      --vad-model ${models.vad} \
      ${lib.optionalString (weights != null) "--model ${weights}"} "$@"
  '';

  meta = {
    description = "Multilingual whisper.cpp dictation widget for Hyprland";
    longDescription = ''
      Click start and talk. Each utterance is cut at your natural pauses,
      transcribed in whatever of Hebrew, English, Portuguese or Spanish you
      spoke, translated to English, shown in both, and typed into whatever
      window has focus.
    '';
    mainProgram = "voice-keyboard";
    platforms = lib.platforms.linux;
  };
}
