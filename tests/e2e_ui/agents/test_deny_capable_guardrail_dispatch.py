"""Verify deny-capable guardrails still allow a listed sub-agent dispatch."""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'

_PARENT_TURN_DONE = "PARENT_DISPATCH_TURN_DONE"
_WORKER_DONE = "WORKER_PING_DONE"

_TURN_TIMEOUT_MS = 240_000

_ALLOWLIST_THEN_DENY = (
    'event.type != "tool_call"'
    ' ? {"result": "ALLOW"}'
    " : has(event.data.name)"
    " && type(event.data.name) == string"
    ' && event.data.name.matches("^(ToolSearch|sys_session_send|sys_read_inbox)$")'
    ' ? {"result": "ALLOW"}'
    ' : {"result": "DENY"}'
)


def _parent_config(name: str, model: str) -> dict[str, Any]:
    """Build the guarded parent spec."""
    return {
        "spec_version": 1,
        "name": name,
        "prompt": (
            "You are an orchestrator. When asked to run, dispatch the "
            "worker sub-agent with sys_session_send.\n"
        ),
        "executor": {"model": model, "config": {"harness": "openai-agents"}},
        "tools": {"agents": ["worker"]},
        "guardrails": {
            "policies": {
                "allowlist_then_deny": {
                    "type": "function",
                    "on": ["tool_call"],
                    "function": {
                        "path": "omnigent.policies.builtins.cel.cel_policy",
                        "arguments": {"expression": _ALLOWLIST_THEN_DENY},
                    },
                }
            }
        },
        "os_env": {"type": "caller_process", "cwd": "."},
    }


def _worker_config(model: str) -> dict[str, Any]:
    """Build the worker spec."""
    return {
        "spec_version": 1,
        "name": "worker",
        "prompt": "You are a worker. Acknowledge the task you were given and finish.\n",
        "executor": {"model": model, "config": {"harness": "openai-agents"}},
        "os_env": {"type": "caller_process", "cwd": "."},
    }


@dataclass(frozen=True)
class DenyCapablePolicySession:
    """A runner-bound guarded session."""

    base_url: str
    session_id: str


@pytest.fixture
def deny_capable_policy_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DenyCapablePolicySession]:
    """Create a guarded parent session with scripted parent and worker replies."""
    uid = uuid.uuid4().hex[:8]
    agent_name = f"deny_capable_probe_{uid}"
    parent_model = f"denycapable-parent-{uid}"
    child_model = f"denycapable-child-{uid}"

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_worker",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": "ping",
                                "args": "Reply exactly PING and finish.",
                            }
                        ),
                    },
                ],
            },
            {"text": _PARENT_TURN_DONE},
        ],
        key=parent_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, parent_model, "PARENT_WAKE_DONE")
    set_fallback_mock_llm(mock_llm_server_url, child_model, _WORKER_DONE)

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, config in (
            ("config.yaml", _parent_config(agent_name, parent_model)),
            ("agents/worker/config.yaml", _worker_config(child_model)),
        ):
            data = yaml.dump(config).encode()
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield DenyCapablePolicySession(base_url=live_server, session_id=session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _dispatch_outputs(base_url: str, session_id: str) -> list[str]:
    """Return the parent's sub-agent dispatch outputs."""
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snap.raise_for_status()
    flattened = [
        {"type": item.get("type"), **(item.get("data") or {})}
        for item in snap.json().get("items", [])
    ]
    dispatch_call_ids = {
        p.get("call_id")
        for p in flattened
        if p.get("type") == "function_call" and p.get("name") == "sys_session_send"
    }
    return [
        str(p.get("output", ""))
        for p in flattened
        if p.get("type") == "function_call_output" and p.get("call_id") in dispatch_call_ids
    ]


def _show_failed_dispatch(page: Page) -> bool:
    """Best-effort: expose the failed call before the assertion."""
    shown = False
    try:
        worked = page.get_by_test_id("turn-worked-fold")
        if worked.count():
            worked.first.locator('[data-slot="collapsible-trigger"]').first.click()
            page.wait_for_timeout(500)
        group = page.get_by_text(re.compile(r"^(Called|Ran) \d+ tools?$"))
        if group.count():
            group.first.click()
            page.wait_for_timeout(500)
        call = page.get_by_role("button", name=re.compile(r"^sys_session_send\("))
        if call.count():
            call.first.click()
        error_text = page.get_by_text("requires parent session inbox")
        expect(error_text.first).to_be_visible(timeout=10_000)
        shown = True
        page.wait_for_timeout(1_500)
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Agents")).click()
        page.wait_for_timeout(2_000)
    except Exception:
        pass
    return shown


@pytest.mark.timeout(600)
def test_deny_capable_guardrail_does_not_break_dispatch(
    page: Page,
    deny_capable_policy_session: DenyCapablePolicySession,
) -> None:
    """A policy that allows sys_session_send by name must not break dispatch."""
    chat = deny_capable_policy_session
    page.goto(f"{chat.base_url}/c/{chat.session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("RUN: dispatch the worker sub-agent to ping.")
    page.get_by_role("button", name="Send", exact=True).click()

    turn_settled = True
    try:
        expect(page.locator(_ASSISTANT, has_text=_PARENT_TURN_DONE).first).to_be_visible(
            timeout=_TURN_TIMEOUT_MS
        )
    except AssertionError:
        turn_settled = False

    outputs = _dispatch_outputs(chat.base_url, chat.session_id)
    if not outputs:
        pytest.fail(
            "the scripted parent turn produced no sys_session_send output"
            + ("" if turn_settled else " and the turn never settled")
        )

    failed = [o for o in outputs if "task_id" not in o]
    if failed:
        shown = _show_failed_dispatch(page)
        pytest.fail(
            "sys_session_send did not return a launching sub-agent handle even "
            "though the guardrails policy allows it by name — the deny-capable "
            f"policy broke sub-agent dispatch. Tool output: {failed[0]!r} "
            f"(error surfaced in the transcript: {shown})"
        )

    # Scope lookups to the desktop rail to avoid hidden mobile duplicates.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    rows = rail.locator(_SUBAGENT_ROW)
    expect(rows.first).to_be_visible(timeout=60_000)
    expect(rows.first).to_contain_text("ping")
    assert rows.first.get_attribute("data-child-session-id"), (
        "subagent row is missing data-child-session-id"
    )
