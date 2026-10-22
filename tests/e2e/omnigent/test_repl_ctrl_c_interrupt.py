"""Escape cancels an active model request and the REPL can complete another turn."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import httpx

from tests._helpers.polling import poll_until
from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
)
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-repl-cancel"
_HARNESS = "openai-agents"
_LONG_PROMPT = (
    "Count slowly from 1 to 100. Print one number per line, "
    "with a short verbal description after each number "
    "explaining what the number could mean. Take your time."
)
_FOLLOW_UP_PROMPT = "say hi"
_FOLLOW_UP_REPLY = "Hi there! How can I help you today?"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_INITIAL_RUNNING_BUDGET = 30.0
_CANCEL_ACK_TIMEOUT = 30.0
_FOLLOWUP_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0


def test_repl_cancel_re_arms_for_next_turn(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """Cancel a blocked model request, then observe the follow-up reply before exit."""
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "This cancelled response must not finish.", "block": True},
            {"text": _FOLLOW_UP_REPLY},
        ],
        key=_MODEL,
    )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(r"❯ ", timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _LONG_PROMPT)
        # A spinner can precede runner startup; wait for the model request itself.
        with httpx.Client(base_url=mock_llm_server_url, timeout=5.0, trust_env=False) as mock:
            poll_until(
                lambda: mock.get("/gate/pending").json()["pending"],
                timeout_s=_INITIAL_RUNNING_BUDGET,
                interval_s=0.1,
            )
        child.send("\x1b")
        child.expect("cancelled", timeout=_CANCEL_ACK_TIMEOUT)

        submit_prompt(child, _FOLLOW_UP_PROMPT)
        # The input prompt is redrawn during a turn, so it cannot signal completion.
        child.expect_exact(_FOLLOW_UP_REPLY, timeout=_FOLLOWUP_COMPLETION_TIMEOUT)
        followup_output = strip_ansi((child.before or "") + child.after)
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)
        # Cancellation disconnects the client but leaves the mock request gated.
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0, trust_env=False)

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "follow_up_assistant_response_rendered": "◆" in followup_output
        and _FOLLOW_UP_REPLY in followup_output,
        "follow_up_user_prompt_echoed": "❯" in followup_output
        and _FOLLOW_UP_PROMPT in followup_output,
    }
    diffs = compare_snapshot("test_repl_ctrl_c_interrupt", observed)
    assert diffs == [], (
        "Snapshot mismatch for cancellation re-arm:\n"
        + "\n".join(diffs)
        + f"\n\nfollow-up turn stripped (last 2000):\n{followup_output[-2000:]}"
    )
