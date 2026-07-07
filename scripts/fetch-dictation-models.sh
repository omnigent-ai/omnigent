#!/usr/bin/env bash
# Downloads the sherpa-onnx models the server dictation engine expects
# (designs/server-dictation.md) into ~/.omnigent/models/dictation/:
#   asr/    streaming Nemotron transducer (int8, ~650 MB) — the recognizer
#   punct/  online CNN-BiLSTM punctuation (int8, ~38 MB) — live re-punctuation
#
# Both are Apache-2.0 upstream releases packaged by k2-fsa. If these exact
# URLs move, the catalogs are:
#   https://k2-fsa.github.io/sherpa/onnx/pretrained_models/index.html
#   https://k2-fsa.github.io/sherpa/onnx/punctuation/pretrained_models.html
# Any streaming transducer dir (encoder/decoder/joiner + tokens.txt) works;
# point OMNIGENT_DICTATION_MODEL_DIR / OMNIGENT_DICTATION_PUNCT_DIR at
# alternates.
set -euo pipefail

DEST="${OMNIGENT_DICTATION_MODEL_ROOT:-$HOME/.omnigent/models/dictation}"
ASR_TARBALL="sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
PUNCT_TARBALL="sherpa-onnx-online-punct-en-2024-08-06"
ASR_GH="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
PUNCT_GH="https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models"

mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

dl() { # dl <url> <out>
  if command -v wget >/dev/null 2>&1; then wget -O "$2" "$1"
  else curl -fL -o "$2" "$1"; fi
}

fetch() { # fetch <tarball-stem> <base-url> <dest-subdir> <label>
  local stem="$1" base="$2" sub="$3" label="$4"
  if [ -n "$(ls -A "$DEST/$sub" 2>/dev/null)" ]; then
    echo ">> $sub/ already populated, skipping $label"
    return
  fi
  echo ">> downloading $label ($stem)"
  dl "$base/$stem.tar.bz2" "$TMP/$stem.tar.bz2"
  tar -xjf "$TMP/$stem.tar.bz2" -C "$TMP"
  rm -rf "$DEST/$sub"
  mv "$TMP/$stem" "$DEST/$sub"
}

fetch "$ASR_TARBALL" "$ASR_GH" "asr" "streaming ASR model (~650 MB)"
fetch "$PUNCT_TARBALL" "$PUNCT_GH" "punct" "punctuation model (~38 MB)"

echo ">> dictation models ready under $DEST"
ls -d "$DEST"/*/

# aarch64 Linux: the sherpa-onnx wheel does not bundle libonnxruntime.so
# (x86_64 wheels do). Pair it with the onnxruntime wheel matching the
# version sherpa-onnx was built against and link it where the binding's
# $ORIGIN RPATH looks.
if [ "$(uname -sm)" = "Linux aarch64" ]; then
  python - <<'EOF' || true
import glob, os, sys
try:
    import sherpa_onnx  # noqa: F401
    print(">> sherpa-onnx imports cleanly; no fixup needed")
    sys.exit(0)
except ImportError:
    pass
try:
    import onnxruntime
except ImportError:
    print("!! aarch64: install the onnxruntime wheel matching your sherpa-onnx build")
    print("   (e.g. `pip install onnxruntime==1.24.4` for sherpa-onnx 1.13.x),")
    print("   then re-run this script to link libonnxruntime.so for the binding.")
    sys.exit(0)
capi = os.path.join(os.path.dirname(onnxruntime.__file__), "capi")
libs = sorted(glob.glob(os.path.join(capi, "libonnxruntime.so*")))
spec_dirs = [p for p in sys.path if os.path.isdir(os.path.join(p, "sherpa_onnx", "lib"))]
if libs and spec_dirs:
    link = os.path.join(spec_dirs[0], "sherpa_onnx", "lib", "libonnxruntime.so")
    if not os.path.exists(link):
        os.symlink(libs[-1], link)
        print(f">> linked {libs[-1]} -> {link}")
    import importlib
    importlib.invalidate_caches()
    import sherpa_onnx  # noqa: F401
    print(">> sherpa-onnx imports cleanly after fixup")
EOF
fi
