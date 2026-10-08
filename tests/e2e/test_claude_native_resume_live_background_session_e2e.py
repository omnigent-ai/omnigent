"""Resuming a still-live Claude session must not fail with the CLI's guard.

Reproduces OMNI-12131: a user resumes a conversation from the Omnigent client
while the same Claude session is still held alive by a separate live process (a
local terminal / Isaac). The runner's resume launch runs
``claude --resume <external_session_id>`` (built by
:func:`_build_claude_native_base_args`) with no ``--fork-session`` and no
``claude stop``/``attach`` handling, so the real Claude CLI refuses::

    Error: Session <id> is running as a background session (<id>). Run
    `claude attach <id>` to open it, or `claude stop <id>` first to resume it
    here. Add --fork-session to branch off a copy instead.

and exits non-zero, which the client surfaces as "Couldn't resume session".

This drives the real ``claude`` binary against the deterministic mock Messages
API (no interactive login), so it runs in CI. A background session stands in for
the live local-terminal holder; the resume refusal is Claude-CLI behaviour that
does not depend on that wrapper. The assertion encodes the fixed behaviour, so
it is red today and green once resume handles the live-session case.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.app import _build_claude_native_base_args

_CLAUDE_BINARY = os.environ.get("OMNIGENT_E2E_CLAUDE_RESUME_BIN") or shutil.which("claude")
_MODEL = "claude-sonnet-4-20250514"

pytestmark = [
    pytest.mark.skipif(_CLAUDE_BINARY is None, reason="requires the Claude Code binary"),
    pytest.mark.timeout(180),
]


def _claude_env(tmp_path: Path, mock_url: str) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "ANTHROPIC_BASE_URL": mock_url,
        "ANTHROPIC_API_KEY": "mock-key",
        "ANTHROPIC_MODEL": _MODEL,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def _claude(env: dict[str, str], *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_CLAUDE_BINARY, *args],
        env=env,
        capture_output=True,
        text=True,
        **kwargs,  # type: ignore[arg-type]
    )


def _await_live_background_session(env: dict[str, str]) -> str:
    """Return the id of a background session that is registered and parked.

    A background launch parks the session (``state: blocked``) a moment after it
    returns; resuming before it is registered races and reports "no conversation
    found" rather than the refusal under test, so wait for the stable state.
    """
    deadline = time.monotonic() + 30.0
    last = ""
    while time.monotonic() < deadline:
        proc = _claude(env, "agents", "--json", timeout=30)
        last = proc.stdout + proc.stderr
        try:
            agents = json.loads(proc.stdout)
        except json.JSONDecodeError:
            agents = []
        for agent in agents:
            if agent.get("sessionId") and agent.get("state") == "blocked":
                return agent["sessionId"]
        time.sleep(0.5)
    raise AssertionError(f"no live background claude session appeared: {last!r}")


def test_resume_of_still_live_session_is_not_refused(
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """The runner's resume launch must succeed against a still-live session.

    Launches a background session that stays alive, then runs the exact argv the
    runner builds for a resume. Today the Claude CLI refuses with its
    background-session guard and a non-zero exit; the fix must let the resume
    proceed.
    """
    mock = isolated_mock_llm_server_url
    # A fallback lets a later (fixed) resume turn complete; the background holder
    # itself parks without a model call, so no queued response is needed for it.
    for key in (_MODEL, "default"):
        httpx.post(
            f"{mock}/mock/set_fallback",
            json={"key": key, "text": "resumed ok"},
            timeout=5.0,
        ).raise_for_status()

    env = _claude_env(tmp_path, mock)
    external_session_id = ""
    try:
        _claude(env, "--bg", "say hello", timeout=60, check=True)
        external_session_id = _await_live_background_session(env)

        resume_args = _build_claude_native_base_args(
            reasoning_effort=None,
            model_override=_MODEL,
            terminal_launch_args=None,
            resume_external_session_id=external_session_id,
        )
        resume = _claude(
            env,
            *resume_args,
            "-p",
            "continue",
            timeout=90,
            stdin=subprocess.DEVNULL,
        )
    finally:
        if external_session_id:
            _claude(env, "stop", external_session_id, timeout=30)

    combined = resume.stdout + resume.stderr
    assert "running as a background session" not in combined, (
        f"resume refused by Claude CLI background-session guard: {combined!r}"
    )
    assert "--fork-session" not in combined, f"resume hit the fork-session hint: {combined!r}"
    assert resume.returncode == 0, f"resume exited {resume.returncode}: {combined!r}"
