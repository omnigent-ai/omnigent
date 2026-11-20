"""A cold-resumed Claude session keeps adding to the session cost.

Drives a real server, runner, tmux pane and ``claude`` binary; only the model is
scripted, with token usage that Claude Code prices itself. Relaunching a closed
terminal rebuilds Claude's transcript from Omnigent items and starts a new
Claude process. The session's cost must keep growing from what it already
spent: the server keeps only the highest total it is sent, so a process that
counts from $0 would leave the cost where it was.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, send_user_text
from tests.e2e._native_resume_helpers import (
    poll_external_session_id,
    poll_for_assistant_marker,
    wait_for_terminal_ready,
)
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_MODEL = "claude-sonnet-4-5"
_TERMINAL_PATH = "/v1/sessions/{session_id}/resources/terminals"
# Claude Code prices input at $3 per million tokens for this model: about $0.30
# before the resume and $0.03 after it, so the resumed process's own count stays
# below what the session spent before it.
_FIRST_TURN_INPUT_TOKENS = 100_000
_RESUMED_TURN_INPUT_TOKENS = 10_000


def _session_cost(client: httpx.Client, session_id: str) -> float:
    response = client.get(f"/v1/sessions/{session_id}", params={"include_usage": "true"})
    response.raise_for_status()
    return float(response.json().get("total_cost_usd") or 0.0)


def _settled_cost(client: httpx.Client, session_id: str, *, above: float) -> float:
    """Wait for the session cost to pass *above* and stop moving; return it."""
    readings = [_session_cost(client, session_id)]

    def settled() -> bool:
        readings.append(_session_cost(client, session_id))
        return readings[-1] > above and readings[-1] == readings[-2]

    try:
        poll_until(settled, timeout_s=60, interval_s=1.0)
    except AssertionError as error:
        error.add_note(f"session cost readings: {readings[-5:]}")
        raise
    return readings[-1]


def _run_priced_turn(
    client: httpx.Client, mock_url: str, session_id: str, *, input_tokens: int
) -> str:
    """Send one prompt whose answer reports *input_tokens*; return the prompt."""
    prompt, reply = f"cost-turn-{uuid.uuid4().hex}", f"priced-{uuid.uuid4().hex}"
    # Only Claude's agent loop advertises Bash, so its side requests can't
    # consume the priced answer.
    configure_mock_llm(
        mock_url,
        [{"text": reply, "usage": {"input_tokens": input_tokens}}],
        match=prompt,
        required_tools=["Bash"],
    )
    set_fallback_mock_llm(mock_url, prompt, reply)
    send_user_text(client, session_id=session_id, text=prompt)
    poll_for_assistant_marker(client, conversation_id=session_id, marker=reply, timeout=90)
    return prompt


def _await_exit_record(claude_config: Path, claude_session_id: str) -> None:
    """
    Wait for a closed Claude to append the ``cost-state`` record it writes on exit.

    Appended after the relaunch rebuilt the transcript, the record would land in
    the new transcript and restore the old total whether or not Omnigent seeds it.
    """

    def recorded() -> bool:
        for path in claude_config.glob(f"projects/*/{claude_session_id}.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines()[-3:]:
                with contextlib.suppress(ValueError):
                    if json.loads(line).get("type") == "cost-state":
                        return True
        return False

    # A Claude that writes no exit record can't race the rebuild either.
    with contextlib.suppress(AssertionError):
        poll_until(recorded, timeout_s=20)


def _ensure_terminal(client: httpx.Client, session_id: str) -> None:
    client.post(
        _TERMINAL_PATH.format(session_id=session_id),
        json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
    ).raise_for_status()
    wait_for_terminal_ready(client, conversation_id=session_id, harness="claude", timeout=90)


@pytest.mark.posix_only
@pytest.mark.timeout(300)
def test_cold_resumed_claude_keeps_counting_the_session_cost(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """The turn after a relaunch adds to the session cost instead of being dropped."""
    if shutil.which("claude") is None or shutil.which("tmux") is None:
        pytest.skip("requires real Claude Code and tmux")
    if any(
        path.exists()
        for path in (
            Path("/etc/claude-code/managed-settings.json"),
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
        )
    ):
        pytest.skip("requires a clean environment without machine-wide Claude routing")

    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "mock": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": isolated_mock_llm_server_url,
                            "api_key": "local-test-key",
                            "models": {"default": _MODEL},
                        },
                    }
                },
            }
        )
    )
    claude_config = tmp_path / "claude-config"
    claude_config.mkdir()
    runtime_env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CLAUDE_CONFIG_DIR": str(claude_config),
        "ANTHROPIC_BASE_URL": isolated_mock_llm_server_url,
        "ANTHROPIC_API_KEY": "local-test-key",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "TERM": "xterm-256color",
        "PYTHONPATH": str(_REPO),
    }
    base_env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG"}}
    with (
        ExitStack() as cleanup,
        server_runner(
            tmp_path / "stack",
            base_env=base_env,
            server_env=runtime_env,
            server_cwd=_REPO,
            poll_interval=0.1,
        ) as stack,
    ):
        claude_config.joinpath(".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "customApiKeyResponses": {"approved": ["local-test-key"]},
                    "projects": {str(stack.workspace): {"hasTrustDialogAccepted": True}},
                }
            )
        )
        stack.start_runner(env=runtime_env, cwd=_REPO)
        client = cleanup.enter_context(
            httpx.Client(base_url=stack.base_url, trust_env=False, timeout=60)
        )
        session_id = str(
            create_native_session(client, stack.base_url, harness="claude")["session_id"]
        )
        bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id)
        _ensure_terminal(client, session_id)
        first_prompt = _run_priced_turn(
            client,
            isolated_mock_llm_server_url,
            session_id,
            input_tokens=_FIRST_TURN_INPUT_TOKENS,
        )
        poll_until(lambda: _session_cost(client, session_id) > 0, timeout_s=60)
        # A relaunch resumes only a session whose Claude id was captured.
        claude_session_id = poll_external_session_id(
            client, conversation_id=session_id, timeout=60
        )

        # Closing the pane ends this Claude process; the relaunch cold-resumes
        # the session from its Omnigent items in a new one.
        client.delete(
            f"{_TERMINAL_PATH.format(session_id=session_id)}/terminal_claude_main"
        ).raise_for_status()
        _await_exit_record(claude_config, claude_session_id)
        _ensure_terminal(client, session_id)
        # The first process and its forwarder are gone, so nothing else raises this.
        spent = _session_cost(client, session_id)
        assert spent > 0
        resumed_prompt = _run_priced_turn(
            client,
            isolated_mock_llm_server_url,
            session_id,
            input_tokens=_RESUMED_TURN_INPUT_TOKENS,
        )
        resumed = [
            request
            for request in get_mock_requests(isolated_mock_llm_server_url)
            if request.get("stream") and resumed_prompt in json.dumps(request.get("messages"))
        ]
        # The resumed process answered with the first turn in its history.
        assert resumed and first_prompt in json.dumps(resumed[-1]["messages"])

        total = _settled_cost(client, session_id, above=spent)
        # The resumed turn costs a tenth of the first; counting the session's
        # earlier spend twice would roughly double the total instead.
        assert total < spent * 1.5, (spent, total)
