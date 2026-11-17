# The ggml weights, as fixed-output derivations.
#
# These are the one part of this tool that cannot be built from source: they
# are gigabytes of downloaded floats. Pinning them by hash rather than letting
# the app fetch them into $HOME on first run is most of the reason for
# packaging any of this — the weights a given generation used are then part of
# the closure, and `nix store diff-closures` says when they change.
{ fetchurl }:
let
  # Every whisper model comes out of the same repo under the same naming
  # scheme, so a model is a name and a hash and nothing else.
  #
  # To add one: nix store prefetch-file --name ggml-<name>.bin \
  #   https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-<name>.bin
  whisper =
    name: hash:
    fetchurl {
      name = "ggml-${name}.bin";
      url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${name}.bin";
      inherit hash;
    };
in
{
  # Keyed by the `model` option's enum in hm-module.nix. Keep the two in step.
  whisper = {
    # 2.9GB. The only tier where translation into English is genuinely good,
    # and the best of these at Hebrew.
    "large-v3" = whisper "large-v3" "sha256-ZNGCtEC5jVIDxPm9VBVE2ExgUZbE97hF36EfsjWU0eI=";

    # 1.5GB and much faster, but distilled without the translation task: the
    # English pane degrades badly on it. Transcription-only fallback.
    "large-v3-turbo" = whisper "large-v3-turbo" "sha256-H8cPd0046xaZk6w5Huo1fvR8iHV+9y7llDh5t+jivGk=";

    # 1.5GB. Measured 3.5s a request on this Iris Xe, and the first tier
    # that got Portuguese right in testing where small mangled it.
    "medium" = whisper "medium" "sha256-bBTVre5fhjlAN7Tk6LWfFnO2zuEOPPCxG72+55wVYgg=";

    # Quantised to 5 bits: 515MB instead of 1.5GB, and measured 1.95s a
    # request against plain medium's 3.39s on the same audio, with an
    # identical transcript. Less weight to move through memory is the whole
    # trick, and it is why this is the default rather than plain medium.
    "medium-q5_0" = whisper "medium-q5_0" "sha256-Gf6ks4DDphjsRyPD7vLreF/7oNBTjPQ/jyNeezs0Ig8=";

    # 466MB. For iterating on the app without waiting on a real model.
    "small" = whisper "small" "sha256-G+OpsgY4Z7k35k4ux0gzZKeZF+FX+pjF2UtcH//qmHs=";
  };

  # Silero, for whisper's own VAD — a different repo and a different scheme,
  # so it does not go through the helper above. Used to trim each cut chunk
  # and to keep whisper from hallucinating boilerplate onto near-silence.
  vad = fetchurl {
    name = "ggml-silero-v6.2.0.bin";
    url = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin";
    hash = "sha256-KqJpt4XutTqCmDogUB3ffB2cSOM6tjpBORrGyff7aYc=";
  };
}
