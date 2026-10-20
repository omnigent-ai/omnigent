"""A Polly sub-agent question answered from the web after Claude's permission hook exits."""

from __future__ import annotations

import json
import os
import signal
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from omnigent.harnesses.claude_native.bridge import read_active_session_id
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.helpers.ui_configuration import _CLAUDE_MOCK_MODEL

_QUESTION = "Which color should the new button use?"
_ANSWER = "Blue"


def _permission_hook_pids(session_id: str) -> list[int]:
    """Return the PermissionRequest hooks waiting for *session_id*, and no others."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        if "omnigent.harnesses.claude_native.hook" not in argv or "--bridge-dir" not in argv[:-1]:
            continue
        bridge_dir = Path(argv[argv.index("--bridge-dir") + 1])
        if "permission-request" in argv and read_active_session_id(bridge_dir) == session_id:
            pids.append(int(entry.name))
    return pids


def _ask_from_subagent(base_url: str, mock_url: str, runner_id: str) -> tuple[str, str, str]:
    tag = uuid.uuid4().hex[:8]
    dispatch, child_input, ask_call_id = f"DISPATCH-{tag}", f"CHILD-ASK-{tag}", f"ask-{tag}"
    for marker in ("<session>", "<user_message>"):
        configure_mock_llm(mock_url, [{"text": "Button color question"}] * 6, match=marker)
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"dispatch-{tag}",
                        "name": "mcp__omnigent__sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "claude_code",
                                "title": "pick-button-color",
                                "args": {
                                    "purpose": "explore",
                                    "input": f"{child_input} Ask the user.",
                                },
                            }
                        ),
                    }
                ]
            },
            {"text": "I asked claude_code to check with you."},
        ],
        match=dispatch,
    )
    question = {
        "question": _QUESTION,
        "header": "Color",
        "multiSelect": False,
        "options": [
            {"label": "Red", "description": "Use red."},
            {"label": _ANSWER, "description": "Use blue."},
        ],
    }
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": ask_call_id,
                        "name": "AskUserQuestion",
                        "arguments": json.dumps({"questions": [question]}),
                    }
                ]
            },
            {"text": f"CHILD-DONE-{tag}"},
        ],
        match=child_input,
    )
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, "Noted.")
    with httpx.Client(base_url=base_url, timeout=30, trust_env=False) as client:
        agents = client.get("/v1/agents", params={"limit": 200}).json()["data"]
        polly_id = next(agent["id"] for agent in agents if agent.get("name") == "polly")
        created = client.post(
            "/v1/sessions",
            json={"agent_id": polly_id},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        created.raise_for_status()
        parent_id = str(created.json()["id"])
        _bind_session_runner(base_url, parent_id, runner_id)
        message = {"type": "input_text", "text": f"{dispatch} Ask me which button color to use."}
        client.post(
            f"/v1/sessions/{parent_id}/events",
            json={"type": "message", "data": {"role": "user", "content": [message]}},
        ).raise_for_status()
        poll_until(
            lambda: client.get(f"/v1/sessions/{parent_id}").json().get("pending_elicitations"),
            timeout_s=180,
            interval_s=1,
        )
        pending = client.get(f"/v1/sessions/{parent_id}").json()["pending_elicitations"]
    return parent_id, pending[0]["params"]["target_session_id"], ask_call_id


def _answer_reached_model(mock_url: str, ask_call_id: str) -> bool:
    requests = httpx.get(f"{mock_url}/mock/requests", timeout=10, trust_env=False).json()
    return any(
        block.get("type") == "tool_result"
        and block.get("tool_use_id") == ask_call_id
        and _ANSWER in json.dumps(block.get("content"))
        for body in requests["requests"]
        for message in body.get("messages") or []
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict)
    )


@pytest.mark.timeout(420)
def test_card_answer_reaches_subagent_after_permission_hook_exits(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    parent_id: str | None = None
    try:
        with _temp_omnigent_mock_config(
            mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
        ):
            parent_id, child_id, ask_call_id = _ask_from_subagent(
                live_server, mock_llm_server_url, runner_id
            )
            hooks = _permission_hook_pids(child_id)
            assert hooks, "Claude's permission hook was not waiting on the question"
            for pid in hooks:
                os.kill(pid, signal.SIGTERM)
            poll_until(lambda: not _permission_hook_pids(child_id), timeout_s=10, interval_s=0.2)

            page = request.getfixturevalue("page")
            page.goto(f"{live_server}/c/{parent_id}")
            card = page.locator('[data-testid="approval-card"][data-state="pending"]').filter(
                has_text=_QUESTION
            )
            expect(card).to_be_visible(timeout=20_000)
            form = card.locator('[data-testid="ask-user-question-form"]')
            form.get_by_role("radio", name=_ANSWER).check()
            form.locator('[data-testid="ask-user-question-submit"]').click()

            deadline = time.monotonic() + 60
            while not _answer_reached_model(mock_llm_server_url, ask_call_id):
                assert time.monotonic() < deadline, "The sub-agent never received the card answer"
                time.sleep(1)
    finally:
        try:
            reset_mock_llm(mock_llm_server_url)
            if parent_id is not None:
                httpx.delete(f"{live_server}/v1/sessions/{parent_id}", timeout=10, trust_env=False)
        finally:
            if respawned is not None:
                respawned.terminate()
                respawned.wait(timeout=10)
