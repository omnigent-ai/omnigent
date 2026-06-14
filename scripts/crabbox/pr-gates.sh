#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/common.sh"

"$ROOT/scripts/crabbox/lint.sh"
"$ROOT/scripts/crabbox/pytest.sh"
"$ROOT/scripts/crabbox/e2e.sh"
"$ROOT/scripts/crabbox/e2e-ui.sh"
