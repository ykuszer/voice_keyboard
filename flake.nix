{
  description = "Voice keyboard: whisper.cpp dictation for Hyprland, he/en/pt/es";

  # nixpkgs only. The home-manager module is plain Nix and declares its own
  # options, so it needs no home-manager input of its own — the consuming
  # config supplies the module system it is evaluated in.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;

      # Same shape as the fleet config's lib/default.nix: one function, every
      # system it could plausibly run on. Wayland + pipewire, so Linux only.
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      eachSystem = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = eachSystem (
        pkgs:
        let
          models = pkgs.callPackage ./models.nix { };

          # The model is a build input, not a runtime lookup, so each variant
          # is its own derivation. `voice-keyboard` on its own carries no
          # weights at all — that is the one to build while iterating, since
          # the large models are gigabytes and would be in every rebuild's
          # closure otherwise.
          app =
            args:
            pkgs.callPackage ./package.nix (
              {
                inherit models;
              }
              // args
            );
        in
        {
          default = self.packages.${pkgs.stdenv.hostPlatform.system}.voice-keyboard-large-v3;

          voice-keyboard = app { };

          voice-keyboard-large-v3 = app { model = "large-v3"; };
          voice-keyboard-medium = app { model = "medium"; };
          voice-keyboard-medium-q5_0 = app { model = "medium-q5_0"; };
          voice-keyboard-small = app { model = "small"; };

          # Vulkan on an integrated GPU is a gamble worth measuring rather
          # than assuming, so both backends are buildable side by side.
          voice-keyboard-large-v3-cpu = app {
            model = "large-v3";
            vulkan = false;
          };

          inherit (models) vad;
          whisper-large-v3 = models.whisper."large-v3";
        }
      );

      homeManagerModules.default = import ./hm-module.nix self;

      # The pure text handling, which is the only part that can be tested
      # without a microphone, a GPU and a compositor. Everything else is
      # checked by hand through --debug-file and --debug-audio.
      checks = eachSystem (pkgs: {
        tests = pkgs.runCommand "voice-keyboard-tests" { } ''
          # PYTHONPATH, not cwd: python puts the *script's* directory on
          # sys.path for a path invocation, which is tests/, not the root.
          export PYTHONPATH=${./.}
          ${pkgs.python3}/bin/python3 ${./.}/tests/test_text.py | tee $out
        '';
      });

      devShells = eachSystem (pkgs: {
        default = pkgs.mkShellNoCC {
          packages = with pkgs; [
            (python3.withPackages (ps: [
              ps.pygobject3
              ps.webrtcvad
              ps.numpy
              ps.python-bidi
              ps.setuptools
            ]))
            ruff
            nixfmt
            whisper-cpp
            wtype
            pipewire
          ];
        };
      });

      formatter = eachSystem (pkgs: pkgs.nixfmt-tree);
    };
}
