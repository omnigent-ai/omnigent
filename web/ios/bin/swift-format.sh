#!/usr/bin/env bash
# Guarded wrapper around `swift format`, invoked by the web-ios-swift-*
# pre-commit hooks. Developers run pre-commit on macOS where the Swift
# toolchain (and thus `swift format`) ships with Xcode, but the shared CI
# "Pre-commit checks" job runs on ubuntu-latest with no Swift installed.
# Skip cleanly there so `pre-commit run --all-files` stays green; real
# enforcement is local (macOS) by design.
set -euo pipefail

if ! command -v swift >/dev/null 2>&1; then
  exit 0
fi

# swift-format 6+ exposes formatting as the `swift format` subcommand. Older
# toolchains may not; treat its absence the same as a missing toolchain.
if ! swift format --version >/dev/null 2>&1; then
  exit 0
fi

exec swift "$@"
