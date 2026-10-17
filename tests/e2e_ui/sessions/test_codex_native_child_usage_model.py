r"""UI journey: a Codex Native child thread's tokens belong to the child's model.

Codex Native can run a parent and a built-in child sub-agent on different
models. Codex reports the child's cumulative usage via a model-less
``thread/tokenUsage/updated`` notification; the forwarder routes it to the
child session but stamps it with the *parent's* model, so the child session
and the root rollup show the child's tokens under the parent model.

Journey (real web SPA, live server + runner, real ``codex`` CLI against the
mock ``/v1/responses``): open a Codex Native session pinned to a multi-agent
capable parent model, send a message whose scripted reply spawns one built-in
child on a different model, wait for the child to answer, then read the
Token usage breakdown in the child session and in the parent session.

Pricing comes from the mock codex provider; both models share one rate, so
only the per-model bucket (not the priced rate) is observable here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.native_session import create_native_session
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.helpers.ui_configuration import temp_omnigent_mock_config

from ..messages.test_message_render_parity import _ASSISTANT, _WORKING, _ensure_chat_view, _send
from ..messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)
from .test_codex_native_cached_tokens_cost import _open_agent_info, _session_snapshot

# Spawning a built-in child against the mock Responses API needs the multi-agent
# tooling of codex-cli 0.154.0; on 0.139.0 the scripted spawn never registers a child.
_MIN_CODEX_VERSION_FOR_MOCK_SPAWN = (0, 154, 0)


def _codex_version() -> tuple[int, ...] | None:
    codex = shutil.which("codex")
    if codex is None:
        return None
    try:
        output = subprocess.run(
            [codex, "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    return tuple(int(part) for part in match.groups()) if match else None


_CODEX_VERSION = _codex_version()
pytestmark = pytest.mark.skipif(
    _CODEX_VERSION is None or _CODEX_VERSION < _MIN_CODEX_VERSION_FOR_MOCK_SPAWN,
    reason="codex-cli 0.154.0+ is needed to spawn a built-in child against the mock backend",
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Codex only offers spawn_agent to models with a multi-agent version in its
# bundled catalog; the child is a sibling slug with a different rate card.
_PARENT_MODEL = "gpt-5.6-sol"
_CHILD_MODEL = "gpt-5.6-luna"

_PARENT_USAGE = (
    {
        "input_tokens": 4_000,
        "output_tokens": 300,
        "input_tokens_details": {"cached_tokens": 1_000},
    },
    {"input_tokens": 1_000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 0}},
)
_CHILD_USAGE = {
    "input_tokens": 20_280,
    "output_tokens": 23,
    "input_tokens_details": {"cached_tokens": 9_984},
}
_CHILD_TOTAL_TOKENS = _CHILD_USAGE["input_tokens"] + _CHILD_USAGE["output_tokens"]

_TURN_TIMEOUT_MS = 180_000
_CHILD_TIMEOUT_S = 180.0
_PANEL_HOLD_MS = 3_000


def _create_parent_session(base_url: str, runner_id: str) -> str:
    # A preset title keeps background title inference off the mock queues.
    created = create_native_session(
        httpx,
        base_url,
        harness="codex",
        model=_PARENT_MODEL,
        metadata={"workspace": str(_REPO_ROOT), "title": "Codex child usage model"},
    )
    session_id = str(created["session_id"])
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def _script_spawn_turn(mock_url: str, nonce: str) -> tuple[str, str]:
    """Queue the parent's spawn + reply and the child's reply; return both reply tokens."""
    parent_done = f"PARENT_DONE_{nonce}"
    child_done = f"CHILD_OK_{nonce}"
    spawn_arguments = {
        "task_name": "child_ok",
        "message": f"Reply with exactly this token and nothing else: {child_done}",
        "model": _CHILD_MODEL,
        "fork_turns": "none",
    }
    # Codex declares its tools in an ``additional_tools`` input item, not
    # ``tools``, so the queue must allow tool calls on tool-less requests.
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_spawn_{nonce}",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "arguments": json.dumps(spawn_arguments),
                    }
                ],
                "usage": _PARENT_USAGE[0],
            },
            # Delay the parent's final answer so the child's turn settles first.
            {"text": parent_done, "usage": _PARENT_USAGE[1], "delay": 8.0},
        ],
        key=_PARENT_MODEL,
        required_tools=[],
    )
    configure_mock_llm(mock_url, [{"text": child_done, "usage": _CHILD_USAGE}], key=_CHILD_MODEL)
    # Codex asks the parent model for a thread title on the first turn; answer
    # it separately so it cannot consume the scripted turn responses.
    configure_mock_llm(
        mock_url,
        [{"text": "Codex child usage"}],
        key=f"codex-thread-title-{nonce}",
        match="single-line task title",
    )
    set_fallback_mock_llm(mock_url, _PARENT_MODEL, "")
    set_fallback_mock_llm(mock_url, _CHILD_MODEL, "")
    return parent_done, child_done


def _child_session_id(child: dict) -> str:
    for key in ("session_id", "conversation_id", "id"):
        if isinstance(child.get(key), str):
            return child[key]
    raise AssertionError(f"child session row has no id: {child}")


def _wait_for(description: str, probe: Callable[[], object], *, timeout_s: float) -> object:
    deadline = time.monotonic() + timeout_s
    last: object = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(1.0)
    pytest.fail(f"{description} did not happen within {timeout_s}s; last: {last!r}")


def _child_sessions(base_url: str, parent_id: str) -> list[dict]:
    resp = httpx.get(f"{base_url}/v1/sessions/{parent_id}/child_sessions", timeout=10.0)
    resp.raise_for_status()
    return list(resp.json()["data"])


def _usage_by_model(base_url: str, session_id: str) -> dict:
    return _session_snapshot(base_url, session_id).get("usage_by_model") or {}


def _mock_requests(mock_url: str) -> list[dict]:
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return [r for r in resp.json()["requests"] if isinstance(r, dict)]


def _read_token_usage_rows(page: Page, screenshot: Path) -> dict[str, str]:
    """Open agent info, expand Token usage, and return each model row's text."""
    panel = _open_agent_info(page)
    breakdown = panel.get_by_test_id("agent-info-usage-by-model")
    expect(breakdown).to_be_visible(timeout=30_000)
    if breakdown.get_attribute("open") is None:
        breakdown.locator("summary").press("Enter")
    rows: Locator = breakdown.locator('[data-testid^="agent-info-model-"]')
    expect(rows.first).to_be_visible(timeout=30_000)
    page.wait_for_timeout(_PANEL_HOLD_MS)
    page.screenshot(path=str(screenshot))
    result: dict[str, str] = {}
    for index in range(rows.count()):
        row = rows.nth(index)
        model = (row.get_attribute("data-testid") or "").removeprefix("agent-info-model-")
        result[model] = re.sub(r"\s+", " ", row.inner_text()).strip()
    return result


@pytest.mark.timeout(900)
def test_codex_native_child_usage_lands_under_child_model(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    pytestconfig: pytest.Config,
) -> None:
    """A child thread on another model must show its tokens under that model."""
    base_url = live_server
    output_dir = Path(pytestconfig.getoption("--output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    respawned = _ensure_runner_online(base_url, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    nonce = uuid.uuid4().hex[:8]
    reset_mock_llm(mock_llm_server_url)
    parent_done, child_done = _script_spawn_turn(mock_llm_server_url, nonce)
    try:
        with temp_omnigent_mock_config(
            mock_llm_server_url,
            "codex",
            workflow_owned=bool(_server_state.get("workflow_owned")),
        ):
            parent_id = _create_parent_session(base_url, runner_id)
            try:
                page: Page = request.getfixturevalue("page")
                page.goto(f"{base_url}/c/{parent_id}")
                _open_terminal_view(page)
                _wait_terminal_connected(page)
                _ensure_chat_view(page)
                _send(page, f"Spawn marker {nonce}. Delegate the reply to a child agent.")

                children = _wait_for(
                    "codex child session registration",
                    lambda: _child_sessions(base_url, parent_id),
                    timeout_s=_CHILD_TIMEOUT_S,
                )
                assert len(children) == 1, children
                child_id = _child_session_id(children[0])
                _wait_for(
                    "child session usage",
                    lambda: _usage_by_model(base_url, child_id),
                    timeout_s=_CHILD_TIMEOUT_S,
                )
                expect(page.locator(_ASSISTANT, has_text=parent_done).first).to_be_visible(
                    timeout=_TURN_TIMEOUT_MS
                )
                expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)

                child_requests = [
                    r
                    for r in _mock_requests(mock_llm_server_url)
                    if r.get("model") == _CHILD_MODEL
                ]
                assert child_requests, "the child thread never reached the mock on its own model"
                assert child_done in json.dumps(child_requests), (
                    "the child request did not carry the spawn task"
                )

                page.goto(f"{base_url}/c/{child_id}")
                child_rows = _read_token_usage_rows(page, output_dir / "child-token-usage.png")

                page.goto(f"{base_url}/c/{parent_id}")
                parent_rows = _read_token_usage_rows(page, output_dir / "parent-token-usage.png")
                parent_usage = _usage_by_model(base_url, parent_id)
                child_usage = _usage_by_model(base_url, child_id)
            finally:
                httpx.delete(f"{base_url}/v1/sessions/{parent_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)

    observed = (
        f"child session rows={child_rows} usage_by_model={child_usage}; "
        f"parent session rows={parent_rows} usage_by_model={parent_usage}"
    )
    assert set(child_usage) == {_CHILD_MODEL}, observed
    assert child_rows.get(_CHILD_MODEL), observed
    assert child_usage[_CHILD_MODEL].get("total_tokens") == _CHILD_TOTAL_TOKENS, observed
    assert set(parent_usage) == {_PARENT_MODEL, _CHILD_MODEL}, observed
    assert parent_rows.get(_PARENT_MODEL) and parent_rows.get(_CHILD_MODEL), observed
