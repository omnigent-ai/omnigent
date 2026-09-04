"""Real Claude-native compaction + cold-resume canary (explicit opt-in).

Prerequisites: an idle developer machine, ``claude`` installed and logged in
under the real ``$HOME``, ``tmux``, and a valid Databricks CLI profile. CI must
never set the opt-in variable below. Run exactly:

    OMNIGENT_E2E_CLAUDE_NATIVE_COMPACTION_RESUME=1 \
    uv run --frozen --group test pytest \
      tests/e2e/test_claude_native_compaction_resume_live.py \
      --profile oss \
      --llm-api-key "$(databricks auth token -p oss |
        python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')" \
      -vv -s

``OMNIGENT_CLAUDE_NATIVE_CANARY_SESSIONS`` controls the number of independent
sessions (default/minimum 3). Cases alternate local-artifact reuse and forced
server reconstruction. If an installed Claude build refuses ``/compact`` at
low context, bounded fallback pressure is controlled by
``OMNIGENT_NATIVE_CANARY_FILL_TURNS`` and ``..._FILL_CHARS``.

To inspect a failure manually, open the printed
``failed-native-canary-session.json`` (server items and both ids) and the
printed native-artifact quarantine under pytest's retained ``tmp_path``.
Native files are moved only into that test-owned quarantine, restoration is
attempted in ``finally``, and session teardown targets only this test's session.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.e2e._native_compaction_resume_canary import (
    ResumeMode,
    canary_cases,
    run_native_compaction_resume_canary,
)

_OPT_IN = "OMNIGENT_E2E_CLAUDE_NATIVE_COMPACTION_RESUME"
_COUNT = "OMNIGENT_CLAUDE_NATIVE_CANARY_SESSIONS"

pytestmark = pytest.mark.skipif(
    os.environ.get(_OPT_IN) != "1" or shutil.which("claude") is None,
    reason=(
        "real Claude compaction/resume canary requires authenticated `claude`; "
        f"set {_OPT_IN}=1 explicitly"
    ),
)


@pytest.mark.parametrize(("session_index", "mode"), canary_cases(_COUNT))
def test_claude_native_compaction_cold_resume_live(
    session_index: int,
    mode: ResumeMode,
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Compact a distinct real Claude conversation, cold-resume, and recall."""
    profile = request.config.getoption("--profile")
    assert profile, "live canary requires --profile (for example: --profile oss)"
    run_native_compaction_resume_canary(
        harness="claude",
        mode=mode,
        session_index=session_index,
        server=resume_test_server,
        profile=profile,
        tmp_path=tmp_path,
    )
