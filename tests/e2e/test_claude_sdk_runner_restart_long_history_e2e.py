"""A long claude-sdk session keeps answering after its runner restarts.

While the runner is up, the live Claude CLI compacts its own context, so a
session whose server-side history is larger than the model's context window
keeps working. A runner restart loses that CLI; the next turn must still fit
the window instead of failing with ``Prompt is too long``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from tests._helpers.live_server import terminate_process
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_requests,
    poll_session_until_terminal,
    send_user_message_to_session,
)

_MODEL = "claude-sonnet-4-20250514"
_CONTEXT_WINDOW_TOKENS = 100_000
_WARM_TURNS = 12
_REPLY_WORDS = 5_500
_COMPACTION_PROMPT = "Your task is to create a detailed summary of the conversation so far"


def _filler(turn: int) -> str:
    return " ".join(f"w{turn}x{index % 997}" for index in range(_REPLY_WORDS))


def _ask(
    client: httpx.Client, mock_url: str, session_id: str, token: str, reply: str
) -> dict[str, Any]:
    configure_mock_llm(mock_url, [{"text": reply}] * 3, match=token)
    response_id = send_user_message_to_session(
        client, session_id=session_id, content=f"Continue the filler. {token}"
    )
    return poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=240
    )


def _runner_online(client: httpx.Client, runner_id: str) -> bool:
    return bool(client.get(f"/v1/runners/{runner_id}/status").json().get("online"))


def test_long_claude_sdk_session_answers_after_runner_restart(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    mock_url = isolated_mock_llm_server_url
    httpx.post(
        f"{mock_url}/mock/context_window", json={"max_input_tokens": _CONTEXT_WINDOW_TOKENS}
    ).raise_for_status()
    configure_mock_llm(
        mock_url,
        [{"text": "<summary>Earlier turns exchanged filler text.</summary>"}] * 10,
        match=_COMPACTION_PROMPT,
    )
    runner_env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(_CONTEXT_WINDOW_TOKENS)}
    spec = {
        "name": "long-claude-sdk-session",
        "prompt": "You are a terse assistant.",
        "executor": {
            "harness": "claude-sdk",
            "model": _MODEL,
            "context_window": _CONTEXT_WINDOW_TOKENS,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_url},
        },
    }
    uid = uuid.uuid4().hex[:6]
    with (
        server_runner(tmp_path / "stack") as stack,
        httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        stack.start_runner(env=runner_env)
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"long-claude-sdk-session.yaml": yaml.safe_dump(spec).encode()}),
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        bind_session_runner(client.patch, "", session_id, stack.runner_id)

        for turn in range(1, _WARM_TURNS + 1):
            warm = _ask(client, mock_url, session_id, f"warm-{turn:02d}-{uid}", _filler(turn))
            assert warm["status"] == "completed", (
                f"warm turn {turn} failed before any restart: {warm['error']}\n{stack.log_tail()}"
            )
        assert any(_COMPACTION_PROMPT in json.dumps(r) for r in get_mock_requests(mock_url)), (
            "the warm Claude CLI never compacted, so the history did not outgrow the window"
        )
        history = client.get(f"/v1/sessions/{session_id}").json()["items"]
        assert len(json.dumps(history)) > 4 * _CONTEXT_WINDOW_TOKENS

        previous_runner = stack.runner
        terminate_process(previous_runner)
        poll_until(lambda: not _runner_online(client, stack.runner_id), timeout_s=60)
        stack.runner = None
        stack.start_runner(env=runner_env)

        # Equal-length tokens let the mock route each turn by its newest token.
        after = _ask(client, mock_url, session_id, f"restart-1-{uid}", "Still here.")
        assert after["status"] == "completed", (
            f"the first turn after the runner restart failed: {after['error']}"
        )
        assert "Still here." in json.dumps(after["output"])
        follow_up = _ask(client, mock_url, session_id, f"restart-2-{uid}", "Still here too.")
        assert follow_up["status"] == "completed", (
            f"the follow-up turn after the runner restart failed: {follow_up['error']}"
        )
        assert "Still here too." in json.dumps(follow_up["output"])
