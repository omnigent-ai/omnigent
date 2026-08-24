#!/usr/bin/env bash
#
# setup-recorders.sh — install the reproduction recorders into a managed
# (Databricks Sandbox / Lakebox) sandbox at runtime, so repro-agent's Step 4 can
# capture video.
#
# The managed sandbox image is platform-owned (built by the execution-sandbox-images
# train; not per-deployment customizable), so it does NOT ship the recorders. The
# agent installs them at runtime instead — the sandbox has pip + outbound network
# to the Databricks pip proxy. Run from the cloned omnigent workspace (the cwd that
# has pyproject.toml / the tests/ tree).
#
# Best-effort by design: each step is independent and non-fatal. What can't be
# installed just means that lane degrades (Step 4 keeps `recordings: []` for it) —
# it must never block the reproduction. Idempotent: skips whatever is already
# present, so re-running (or running on a recorder-equipped image) is a no-op.
#
# Not needed in LOCAL mode (a maintainer's checkout already has its own deps).
# If a deployment ever bakes the recorders into its sandbox image, the checks
# below find them present and this no-ops.

set -uo pipefail  # intentionally NOT -e: keep going past a failed lane

# pip inherits whatever index the sandbox already configures (the managed image
# points pip at the Databricks proxy at build time). Don't hardcode an index here.

echo "→ setup-recorders: installing reproduction recorders (best-effort)"

# 1) pytest-playwright — the only recorder plugin Step 4 needs (stock
#    `--video on --screenshot on`; no visual-snapshot plugin). Install just this
#    at the repo's pinned floor rather than the whole `.[test]` extra: a full
#    editable `.[test]` install would reinstall omnigent over the platform image's
#    pre-baked copy (heavy, and can conflict). It pulls in `playwright` + `pytest`.
if python3 -c "import pytest_playwright" 2>/dev/null; then
  echo "  ok: pytest-playwright already present"
else
  pip3 install --break-system-packages "pytest-playwright>=0.7" \
    && echo "  ok: installed pytest-playwright" \
    || echo "  WARN: pip install pytest-playwright failed — web/terminal recording will degrade"
fi

# 2) Chromium — the headless browser the Playwright driver + recorder use.
if python3 -m playwright install chromium; then
  echo "  ok: chromium installed"
else
  echo "  WARN: playwright install chromium failed — web/terminal recording will degrade"
fi

# 3) ffmpeg — webm→mp4 conversion (recordings work without it; .webm is kept).
if command -v ffmpeg >/dev/null 2>&1; then
  echo "  ok: ffmpeg already present"
else
  if command -v sudo >/dev/null 2>&1; then APT="sudo apt-get"; else APT="apt-get"; fi
  ( $APT update && $APT install -y --no-install-recommends ffmpeg ) >/dev/null 2>&1 \
    && echo "  ok: installed ffmpeg" \
    || echo "  note: ffmpeg unavailable — mp4 conversion skipped (.webm/.gif kept)"
fi

# 4) vhs — optional; renders the cli-lane tape to mp4. Absent ⇒ the agent still
#    authors the tape and notes rendering was skipped.
if command -v vhs >/dev/null 2>&1; then
  echo "  ok: vhs already present"
else
  echo "  note: vhs absent — cli-lane tape will be authored but not rendered"
fi

echo "→ setup-recorders: done (missing tools degrade their lane; reproduction is unaffected)"
