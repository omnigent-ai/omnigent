#!/bin/sh
# Build the omnigent-workspace-helper wheel for the machine this runs on.
#
#   scripts/build_workspace_helper_wheel.sh OUT_DIR [COMPATIBILITY]
#
# COMPATIBILITY is maturin's platform tag floor (manylinux_2_28 or
# musllinux_1_2); run Linux builds inside the matching quay.io/pypa image so
# the binary links against that floor's C library. Omit it on macOS. Set
# WORKSPACE_HELPER_BUILD_VERSION and WORKSPACE_HELPER_BUILD_COMMIT for a release.
set -eu
out=$(cd "$1" && pwd)
compatibility=${2:-}
root=$(cd "$(dirname "$0")/.." && pwd)
toolchain=$(sed -n 's/^channel = "\(.*\)"$/\1/p' "$root/rust/rust-toolchain.toml")

if ! command -v rustup >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs |
    sh -s -- -y --profile minimal --default-toolchain none
  PATH="$HOME/.cargo/bin:$PATH"
fi
# The pinned toolchain without the CI-only components the toolchain file adds.
rustup toolchain install --profile minimal "$toolchain"
export RUSTUP_TOOLCHAIN="$toolchain"

python=${PYTHON:-python3}
"$python" -m venv "$out/.maturin"
"$out/.maturin/bin/python" -m pip install --quiet "maturin>=1.9,<2"
cd "$root/rust/workspace-helper"
if [ -n "$compatibility" ]; then
  "$out/.maturin/bin/maturin" build --release --out "$out" --compatibility "$compatibility"
else
  "$out/.maturin/bin/maturin" build --release --out "$out"
fi
rm -rf "$out/.maturin"
