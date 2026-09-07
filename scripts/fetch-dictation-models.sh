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
