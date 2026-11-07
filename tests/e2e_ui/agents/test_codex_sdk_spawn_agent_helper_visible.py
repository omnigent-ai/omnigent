"""UI journey: a helper spawned by Codex's built-in ``spawn_agent`` must be visible.

A session on the ``codex`` (SDK) harness drives a Codex app-server through
``CodexExecutor``. Codex's own multi-agent tools stay enabled there, so the
model can start a helper with ``spawn_agent``; the helper runs as a second
Codex thread inside the same app-server process. The executor surfaces that
helper as an Omnigent child session (an Agents rail row) and tracks its
activity and completion, the same way the codex-native wrapper does.

Journey (real web SPA, prepared live server + runner, real ``codex`` CLI
app-server pointed at the mock ``/v1/responses``):

1. open a fresh headless ``codex`` session with the Agents tab shown
2. ask it to start a helper with ``spawn_agent``; the scripted parent turn
   calls ``spawn_agent`` and only answers after a delay, so the helper's
   child turn (a real shell command, then a final text) runs while the
   parent turn is still active
3. verify: after the helper ran and the parent answered, the Agents tab shows
   the helper's child row and ``child_sessions`` lists it

The test also asserts the helper really ran (its own model requests reached
the mock) and that Codex accepted the spawn, so a failure pins invisibility
rather than a spawn that never happened.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
)
from tests.e2e_ui.sessions.test_codex_multistep_turn_usage import _build_codex_bundle

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="the codex CLI binary is required for the headless-codex spawn_agent e2e",
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_CHILD_ROW = '[data-testid="subagent-row"]'
_MAIN_ROW = '[data-testid="subagent-main-row"]'

_SPAWN_TOOL = "spawn_agent"
_PARENT_INTERIM = "Helper started; waiting for it to finish."
_PARENT_FINAL = "The helper reported that it finished."
_HELPER_FINAL = "Helper task finished."
_HELPER_SLEEP_S = 6
# Hold the parent turn open well past the helper's work so the child row is
# observed while active even under CI scheduling jitter; derive it from the
# helper's sleep so the two stay coupled.
_PARENT_REPLY_DELAY_S = float(_HELPER_SLEEP_S + 40)
# Codex injects this into the parent turn once the helper completes.
_SUBAGENT_NOTIFICATION = "<subagent_notification>"


def _create_codex_session(base_url: str, runner_id: str, model: str, title: str) -> str:
    name = f"codex-spawn-{uuid.uuid4().hex[:8]}"
    create_resp = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        _build_codex_bundle(name, model),
        metadata={"title": title},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def _send_api_message(base_url: str, session_id: str, text: str) -> None:
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    )
    resp.raise_for_status()


def _user_texts(request: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for item in request.get("input") or []:
        if isinstance(item, dict) and item.get("role") == "user":
            for block in item.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts.append(block["text"])
    return texts


def _model_requests(mock_llm_server_url: str, token: str) -> list[dict[str, Any]]:
    """Return captured requests whose user-role text carries *token*.

    Matching on user text keeps the parent's requests (whose spawn arguments
    quote the helper's prompt) out of the helper's request list.
    """
    resp = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return [r for r in resp.json()["requests"] if any(token in t for t in _user_texts(r))]


def _wait_for_model_requests(
    mock_llm_server_url: str, token: str, count: int, timeout_s: float
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    requests: list[dict[str, Any]] = []
    while True:
        requests = _model_requests(mock_llm_server_url, token)
        if len(requests) >= count:
            return requests
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    raise AssertionError(
        f"expected {count} model requests carrying {token!r} within {timeout_s:.0f}s, "
        f"saw {len(requests)}"
    )


def _advertised_spawn_tool(request: dict[str, Any]) -> tuple[str | None, str] | None:
    """Return ``(namespace, name)`` of the spawn tool Codex offered, or ``None``."""
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("name") == _SPAWN_TOOL:
            return None, _SPAWN_TOOL
        for nested in tool.get("tools") or []:
            if isinstance(nested, dict) and nested.get("name") == _SPAWN_TOOL:
                return tool.get("name"), _SPAWN_TOOL
    return None


def _probe_spawn_tool(
    base_url: str, runner_id: str, mock_llm_server_url: str, model: str
) -> tuple[str | None, str] | None:
    """Run one throwaway codex turn and read the spawn tool Codex advertises."""
    token = f"codexspawn-probe-{uuid.uuid4().hex[:6]}"
    configure_mock_llm(mock_llm_server_url, [{"text": "Probe reply."}], key=model, match=token)
    session_id = _create_codex_session(base_url, runner_id, model, "Codex spawn_agent probe")
    try:
        _send_api_message(base_url, session_id, f"Say hello. {token}")
        request = _wait_for_model_requests(mock_llm_server_url, token, 1, timeout_s=180)[0]
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    return _advertised_spawn_tool(request)


def _function_call_output(request: dict[str, Any], call_id: str) -> str | None:
    for item in request.get("input") or []:
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == call_id
        ):
            output = item.get("output")
            return None if output is None else json.dumps(output)
    return None


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(900)
def test_codex_spawn_agent_helper_appears_in_agents_rail(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A Codex-spawned helper must show up as a child session in the Agents rail.

    The parent's scripted turn calls ``spawn_agent`` and the helper's scripted
    child turn issues a tool call before answering. The helper's model
    requests reach the mock while its turn is active, so the Agents tab must
    show its child row and ``child_sessions`` must list it.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        uid = uuid.uuid4().hex[:6]
        model = f"mock-codex-spawn-{uid}"

        spawn_tool = _probe_spawn_tool(live_server, runner_id, mock_llm_server_url, model)
        if spawn_tool is None:
            pytest.skip("Codex offers no helper-spawning tool here, so no helper can run unseen")
        namespace, spawn_name = spawn_tool

        parent_token = f"codexspawn-parent-{uid}"
        helper_token = f"codexspawn-helper-task-{uid}"
        spawn_call_id = f"call_spawn_{uid}"
        helper_shell = f"sleep {_HELPER_SLEEP_S}; echo helper-done-{uid}"
        spawn_call: dict[str, str] = {
            "call_id": spawn_call_id,
            "name": spawn_name,
            "arguments": json.dumps(
                {"message": f"Run `{helper_shell}` and then say you are done. {helper_token}"}
            ),
        }
        if namespace:
            spawn_call["namespace"] = namespace
        configure_mock_llm(
            mock_llm_server_url,
            [
                {"tool_calls": [spawn_call]},
                {"text": _PARENT_INTERIM, "delay": _PARENT_REPLY_DELAY_S},
                {"text": _PARENT_FINAL},
            ],
            key=model,
            match=parent_token,
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": f"call_helper_shell_{uid}",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": helper_shell}),
                        }
                    ]
                },
                {"text": _HELPER_FINAL},
            ],
            key=f"{model}-helper",
            match=helper_token,
        )

        session_id = _create_codex_session(
            live_server, runner_id, model, "Codex spawn_agent helper visibility"
        )
        request.addfinalizer(
            lambda: httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        )
        page: Page = request.getfixturevalue("page")
        clock = time.monotonic()

        def mark(label: str) -> None:
            print(f"[t+{time.monotonic() - clock:6.1f}s] {label}", flush=True)

        page.goto(f"{live_server}/c/{session_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
        agents_tab.click()
        expect(agents_tab).to_have_attribute("aria-selected", "true")
        expect(rail.locator(_MAIN_ROW)).to_be_visible(timeout=30_000)
        child_rows = rail.locator(_CHILD_ROW)
        expect(child_rows).to_have_count(0)
        mark(f"session {session_id} open, Agents tab shown, no child rows")

        _send(
            page,
            "Use your built-in spawn_agent tool to start a helper that waits "
            f"{_HELPER_SLEEP_S} seconds and then prints helper-done. "
            f"Do not use sys_session_send. {parent_token}",
        )
        mark("prompt sent")

        _wait_for_model_requests(mock_llm_server_url, helper_token, 1, timeout_s=180)
        # The helper's row must surface as soon as its turn begins, while the
        # ``sleep`` tool call is still running, not only after it completes.
        expect(child_rows).to_have_count(1, timeout=60_000)
        mark(f"helper's first model request seen; child rows: {child_rows.count()}")
        # The helper's second model request carries its tool-call result: the
        # child turn really ran a tool round-trip inside Codex while the parent
        # turn was open.
        helper_requests = _wait_for_model_requests(
            mock_llm_server_url, helper_token, 2, timeout_s=180
        )
        # The helper's row must be visible while its turn is still running.
        expect(child_rows).to_have_count(1, timeout=60_000)
        rows_while_helper_ran = child_rows.count()
        mark(f"helper's tool round-trip finished; child rows: {rows_while_helper_ran}")

        expect(page.locator(_ASSISTANT).filter(has_text=_PARENT_FINAL).first).to_be_visible(
            timeout=240_000
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=120_000)
        mark("parent turn finished")

        parent_requests = _model_requests(mock_llm_server_url, parent_token)
        assert len(parent_requests) >= 2, (
            "expected the parent turn to make at least 2 model requests, "
            f"saw {len(parent_requests)}"
        )
        spawn_output = _function_call_output(parent_requests[1], spawn_call_id)
        assert spawn_output is not None, "Codex returned no result for the spawn_agent call"
        # The helper's second request carries the result of its tool call, which
        # proves the child turn ran a full tool round-trip while visible. The mock
        # harness does not execute the shell, so assert the round-trip, not output.
        shell_output = _function_call_output(helper_requests[1], f"call_helper_shell_{uid}")
        assert shell_output is not None, (
            "the helper's tool-call result never reached its second model request"
        )

        def _completion_notices() -> list[str]:
            notices: list[str] = []
            for request_payload in _model_requests(mock_llm_server_url, parent_token):
                notices.extend(
                    text
                    for text in _user_texts(request_payload)
                    if _SUBAGENT_NOTIFICATION in text and _HELPER_FINAL in text
                )
            return notices

        # The helper's completion notice reaches the parent model only after the
        # helper finishes, which can trail the parent's rendered reply on a slow
        # host, so poll rather than reading one request.
        notice_deadline = time.monotonic() + 60.0
        completion_notices = _completion_notices()
        while not completion_notices and time.monotonic() < notice_deadline:
            time.sleep(1.0)
            completion_notices = _completion_notices()
        assert completion_notices, "Codex never told the parent model that the helper completed"

        mark("checking the Agents tab for the helper's row")
        expect(rail.locator(_MAIN_ROW)).to_be_visible()
        expect(child_rows).to_have_count(1, timeout=30_000)
        expect(child_rows.first.get_by_test_id("subagent-status-avatar")).to_have_attribute(
            "aria-label", "Done", timeout=30_000
        )

        # Read child_sessions only after the row renders; the API listing can lag
        # the UI slightly, so poll instead of trusting one early snapshot.
        def _child_sessions() -> list[dict[str, Any]]:
            return httpx.get(
                f"{live_server}/v1/sessions/{session_id}/child_sessions", timeout=10.0
            ).json()["data"]

        children_deadline = time.monotonic() + 30.0
        children = _child_sessions()
        while not children and time.monotonic() < children_deadline:
            time.sleep(1.0)
            children = _child_sessions()
        print(
            f"child rows while helper ran: {rows_while_helper_ran}; "
            f"child_sessions after parent turn: {len(children)}; "
            f"spawn result: {spawn_output[:200]}; "
            f"completion notice to parent model: {completion_notices[0][:200]}"
        )
        assert len(children) == 1, f"expected one child session for the helper, saw {children}"
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)
