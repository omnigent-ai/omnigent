#!/usr/bin/env bash
# Guarded wrapper around `ktlint`, invoked by the android-ktlint-* pre-commit
# hooks. On CI the lint workflow installs ktlint before running pre-commit.
# On developer machines, install ktlint (e.g. `brew install ktlint`) to enable
# the hooks locally; the wrapper exits 0 cleanly if ktlint is absent so that a
# missing binary doesn't block commits on machines without it installed.
set -euo pipefail

if ! command -v ktlint >/dev/null 2>&1; then
  exit 0
fi

exec ktlint "$@"
