"""UI journey: Codex-native Chat and native history after the forwarder's subscription is refused.

A codex-native session mirrors the TUI's thread into Chat through a transcript
forwarder that subscribes with ``thread/resume``. Codex can refuse that call
(seen on Codex 0.159.2 as ``-32601 list_turns is not supported yet``) while the
TUI's own connection keeps working. The refusal is injected here at the
app-server WebSocket boundary by the ``codex`` wrapper in
``_codex_native_fault_proxy``; the real Codex CLI (mock model) still runs both
turns. The journey then performs a cold reconnect (runner restart + page reload)
with the refusal still in place and checks that the native history survives the
server-history rebuild without duplicating the already mirrored transcript.

Set ``OMNIGENT_E2E_CODEX_RESUME_FAULT=0`` to run the same journey without the
refusal as a control. ``OMNIGENT_E2E_EVIDENCE_DIR`` copies logs, screenshots and
probe results there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page

from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState, read_bridge_state
from omnigent.harnesses.codex_native.main import _find_codex_rollout
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages._codex_native_fault_proxy import (
    CodexNativeFaultEnvironment,
    spawn_codex_native_fault_environment,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_FAULT_ENABLED = os.environ.get("OMNIGENT_E2E_CODEX_RESUME_FAULT", "1") != "0"
_EVIDENCE_DIR = os.environ.get("OMNIGENT_E2E_EVIDENCE_DIR")
# Window after the second TUI reply for Chat to catch up before the facet is settled.
_CHAT_SYNC_WINDOW_S = 60.0
_TUI_REPLY_TIMEOUT_S = 120.0
_SUBSCRIBE_WARNING = "failed to subscribe to Codex thread"
_ROLLOUT_REFRESH_LOG = "Refreshed Codex resume rollout from server history"


@pytest.fixture
def fault_environment(
    mock_llm_server_url: str, tmp_path: Path
) -> Iterator[CodexNativeFaultEnvironment]:
    """Private server + runner whose Codex app-servers sit behind the fault proxy."""
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the native Codex forwarder journey")
    environment, teardown = spawn_codex_native_fault_environment(
        tmp_path / "env", mock_llm_url=mock_llm_server_url, codex_path=codex_path
    )
    try:
        yield environment
    finally:
        teardown()


def _terminal_resource(env: CodexNativeFaultEnvironment, session_id: str) -> dict[str, Any]:
    response = httpx.get(f"{env.base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    response.raise_for_status()
    for resource in response.json().get("data", []):
        if resource.get("type") == "terminal" and resource.get("metadata", {}).get("running"):
            return resource
    raise AssertionError("no running terminal resource")


def _pane_text(env: CodexNativeFaultEnvironment, session_id: str, *, ansi: bool = False) -> str:
    metadata = _terminal_resource(env, session_id)["metadata"]
    argv = [
        "tmux",
        "-S",
        metadata["tmux_socket"],
        "capture-pane",
        "-p",
        "-t",
        metadata["tmux_target"],
    ]
    if ansi:
        argv.insert(-2, "-e")
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=10).stdout


def _tui_shows_reply(pane: str, token: str) -> bool:
    # The echoed prompt also contains the token; Codex prints replies as "• <text>".
    replied = any(line.strip().startswith("• ") and token in line for line in pane.splitlines())
    return replied and "esc to interrupt" not in pane


def _wait_for_tui_reply(env: CodexNativeFaultEnvironment, session_id: str, token: str) -> str:
    deadline = time.monotonic() + _TUI_REPLY_TIMEOUT_S
    text = ""
    while time.monotonic() < deadline:
        text = _pane_text(env, session_id)
        if _tui_shows_reply(text, token):
            return text
        time.sleep(1.0)
    raise AssertionError(f"TUI never showed the reply {token!r}; pane:\n{text}")


def _wait_for_bridge_state(
    env: CodexNativeFaultEnvironment, session_id: str, *, not_socket: str | None = None
) -> CodexNativeBridgeState:
    deadline = time.monotonic() + 120.0
    bridge_dir = env.bridge_dir(session_id)
    state = None
    while time.monotonic() < deadline:
        state = read_bridge_state(bridge_dir)
        if state is not None and state.thread_id and state.socket_path != not_socket:
            return state
        time.sleep(0.5)
    raise AssertionError(
        f"no fresh bridge state with a thread id at {bridge_dir} "
        f"(last state: {state!r}, excluded socket: {not_socket!r})"
    )


def _run_coroutine(coroutine: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    # The sync Playwright API already drives an event loop on the test thread.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        # Bound the probe itself so a wedged request cannot leave a thread
        # holding the WebSocket open behind a failed test.
        return pool.submit(asyncio.run, asyncio.wait_for(coroutine, timeout=110)).result(
            timeout=120
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _thread_read(ws_url: str, thread_id: str) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        client = CodexAppServerClient(ws_url=ws_url, client_name="omnigent-repro-probe")
        await client.connect()
        try:
            response = await client.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
        except Exception as exc:
            return {"error": repr(exc)}
        finally:
            await client.close()
        thread = (response.get("result") or {}).get("thread") or {}
        turns = thread.get("turns") or []
        return {
            "status": thread.get("status"),
            "turn_count": len(turns),
            "turns": [
                {
                    "id": turn.get("id"),
                    "status": turn.get("status"),
                    "items": [item.get("type") for item in turn.get("items") or []],
                }
                for turn in turns
            ],
        }

    return _run_coroutine(_run())


def _rollout_summary(codex_home: Path, thread_id: str, markers: list[str]) -> dict[str, Any]:
    """Count the turn markers Codex's local rollout still carries (plus record stats)."""
    rollout = _find_codex_rollout(codex_home, thread_id)
    if rollout is None:
        return {"path": None, "records": 0, "markers": 0, "types": {}}
    text = rollout.read_text(encoding="utf-8")
    types: dict[str, int] = {}
    records = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # Codex may still be appending the last record.
            continue
        records += 1
        payload = record.get("payload") or {}
        key = f"{record.get('type')}/{payload.get('type')}"
        types[key] = types.get(key, 0) + 1
    return {
        "path": str(rollout),
        "records": records,
        "markers": sum(marker in text for marker in markers),
        "types": types,
    }


def _session_status(env: CodexNativeFaultEnvironment, session_id: str) -> str | None:
    response = httpx.get(f"{env.base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json().get("status")


def _message_roles(env: CodexNativeFaultEnvironment, session_id: str) -> list[str]:
    response = httpx.get(f"{env.base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0)
    response.raise_for_status()
    roles: list[str] = []
    for item in response.json().get("data", []):
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        role = item.get("role") or data.get("role")
        if role in {"user", "assistant"}:
            roles.append(role)
    return roles


def _runner_log_text(env: CodexNativeFaultEnvironment) -> str:
    log_dir = env.data_dir / "logs" / "runner"
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(log_dir.glob("*.log"))
    )


def _save_evidence(name: str, content: str | bytes) -> None:
    if not _EVIDENCE_DIR:
        return
    target = Path(_EVIDENCE_DIR) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _screenshot(page: Page, name: str) -> None:
    if _EVIDENCE_DIR:
        Path(_EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(_EVIDENCE_DIR) / name))


@pytest.mark.timeout(900)
def test_native_codex_chat_and_history_survive_forwarder_subscription_refusal(
    request: pytest.FixtureRequest,
    fault_environment: CodexNativeFaultEnvironment,
    mock_llm_server_url: str,
) -> None:
    """Both TUI turns reach Chat (session idle) and survive a cold reconnect."""
    env = fault_environment
    if _FAULT_ENABLED:
        env.enable_resume_fault()
    session_id = env.create_session()
    evidence: dict[str, Any] = {
        "fault_enabled": _FAULT_ENABLED,
        "base_url": env.base_url,
        "runner_id": env.runner_id,
        "session_id": session_id,
        "timeline": [],
    }
    nonces = [uuid.uuid4().hex[:8] for _ in range(2)]
    turns = [(f"usr-{i + 1}-{nonces[i]}", f"ast-{i + 1}-{nonces[i]}") for i in range(2)]
    markers = [marker for turn in turns for marker in turn]
    reset_mock_llm(mock_llm_server_url)
    for user_marker, assistant_token in turns:
        configure_mock_llm(
            mock_llm_server_url, [{"text": assistant_token}], key=user_marker, match=user_marker
        )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")
    failures: list[str] = []

    # Recorded page only after the server, runner and session exist.
    page: Page = request.getfixturevalue("page")
    started = time.monotonic()

    def mark(label: str) -> None:
        evidence["timeline"].append({"t": round(time.monotonic() - started, 1), "step": label})
        _log.info("[%5.1fs] %s", time.monotonic() - started, label)

    try:
        page.goto(f"{env.base_url}/c/{session_id}")
        mark("opened session page")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        mark("Terminal view connected to the Codex TUI")
        state = _wait_for_bridge_state(env, session_id)
        thread_id = state.thread_id
        assert thread_id is not None
        evidence["thread_id"] = thread_id
        evidence["app_server_url_before"] = state.socket_path
        page.wait_for_timeout(2_000)
        _screenshot(page, "01-terminal-connected.png")

        # Turn 1: Chat composer → Codex completes it in the TUI.
        _ensure_chat_view(page)
        mark("switched to Chat view")
        user_1, assistant_1 = turns[0]
        _send(page, _turn_prompt(1, user_1, assistant_1))
        mark("sent prompt 1 from the Chat composer")
        _wait_for_tui_reply(env, session_id, assistant_1)
        mark("Codex TUI finished turn 1")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        page.wait_for_timeout(2_500)
        _screenshot(page, "02-tui-after-turn-1.png")
        mark("Terminal view shows reply 1")

        # Turn 2: typed into the TUI.
        user_2, assistant_2 = turns[1]
        _type_into_tui(page, _turn_prompt(2, user_2, assistant_2))
        mark("typed prompt 2 into the TUI and pressed Enter")
        pane_after_turns = _wait_for_tui_reply(env, session_id, assistant_2)
        mark("Codex TUI finished turn 2")
        page.wait_for_timeout(2_500)
        _screenshot(page, "03-tui-after-turn-2.png")
        _save_evidence("pane-after-turns.txt", pane_after_turns)
        _save_evidence("pane-after-turns.ansi", _pane_text(env, session_id, ansi=True))

        # Observation: Chat should show both turns and the session should settle.
        _ensure_chat_view(page)
        mark("switched to Chat view; watching for both turns")
        deadline = time.monotonic() + _CHAT_SYNC_WINDOW_S
        status = None
        while True:
            assistant_count = page.locator(_ASSISTANT).count()
            user_count = page.locator(_USER).count()
            working_count = page.locator(_WORKING).count()
            status = _session_status(env, session_id)
            if assistant_count >= 2 and working_count == 0 and status == "idle":
                break
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(2_000)
        mark(
            f"Chat after wait: {user_count} user / {assistant_count} assistant bubbles, "
            f"working indicator x{working_count}, status={status}"
        )
        _screenshot(page, "04-chat-after-both-turns.png")
        roles = _message_roles(env, session_id)
        runner_log = _runner_log_text(env)
        subscribe_warnings = [
            line for line in runner_log.splitlines() if _SUBSCRIBE_WARNING in line
        ]
        evidence["chat_after_turns"] = {
            "user_bubbles": user_count,
            "assistant_bubbles": assistant_count,
            "working_indicator": working_count,
            "status": status,
            "server_message_roles": roles,
            "subscribe_warnings": subscribe_warnings,
            "list_turns_in_runner_log": "list_turns is not supported yet" in runner_log,
        }
        if assistant_count < 2:
            failures.append(
                f"Chat shows {assistant_count} assistant reply bubble(s) "
                f"{_CHAT_SYNC_WINDOW_S:.0f}s after both TUI turns completed (expected 2); "
                f"server transcript roles={roles}"
            )
        if _FAULT_ENABLED and not any(
            event.get("event") == "thread/resume" and event.get("injected")
            for event in env.proxy_events()
        ):
            failures.append("the fault proxy never refused the forwarder's thread/resume")
        if status != "idle":
            failures.append(
                f"session status is {status!r} after both turns completed (expected 'idle')"
            )
        if working_count:
            failures.append("working indicator still shown after both turns completed")

        # Native history before the reconnect.
        codex_home = env.bridge_dir(session_id) / "codex-home"
        evidence["thread_read_before"] = _thread_read(state.socket_path, thread_id)
        evidence["rollout_before"] = _rollout_summary(codex_home, thread_id, markers)
        rollout_before = evidence["rollout_before"]["path"]
        if rollout_before:
            _save_evidence(
                "rollout-before.jsonl", Path(rollout_before).read_text(encoding="utf-8")
            )
        mark(
            f"native thread/read: {evidence['thread_read_before'].get('turn_count')} turns; "
            f"rollout carries {evidence['rollout_before']['markers']}/4 turn markers"
        )

        # Cold reconnect with the refusal still in place: runner restart, then
        # reopen the session.
        env.restart_runner()
        mark("runner restarted (cold reconnect)")
        page.reload()
        mark("session page reloaded")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        new_state = _wait_for_bridge_state(env, session_id, not_socket=state.socket_path)
        evidence["app_server_url_after"] = new_state.socket_path
        evidence["thread_id_after"] = new_state.thread_id
        page.wait_for_timeout(6_000)
        _screenshot(page, "05-tui-after-reconnect.png")
        pane_after_reconnect = _pane_text(env, session_id)
        _save_evidence("pane-after-reconnect.txt", pane_after_reconnect)
        _save_evidence("pane-after-reconnect.ansi", _pane_text(env, session_id, ansi=True))
        mark("Terminal view reconnected to the resumed Codex TUI")
        evidence["thread_read_after"] = _thread_read(new_state.socket_path, thread_id)
        evidence["rollout_after"] = _rollout_summary(codex_home, thread_id, markers)
        runner_log = _runner_log_text(env)
        evidence["rollout_refresh_lines"] = [
            line for line in runner_log.splitlines() if _ROLLOUT_REFRESH_LOG in line
        ]
        rollout_after = evidence["rollout_after"]["path"]
        if rollout_after:
            _save_evidence("rollout-after.jsonl", Path(rollout_after).read_text(encoding="utf-8"))
        _ensure_chat_view(page)
        deadline = time.monotonic() + _CHAT_SYNC_WINDOW_S
        while True:
            chat_after_reconnect = {
                "user_bubbles": page.locator(_USER).count(),
                "assistant_bubbles": page.locator(_ASSISTANT).count(),
                "working_indicator": page.locator(_WORKING).count(),
                "status": _session_status(env, session_id),
            }
            settled = (
                chat_after_reconnect["status"] == "idle"
                and chat_after_reconnect["assistant_bubbles"] >= 2
                and chat_after_reconnect["working_indicator"] == 0
            )
            if settled or time.monotonic() >= deadline:
                break
            page.wait_for_timeout(2_000)
        page.wait_for_timeout(3_000)
        _screenshot(page, "06-chat-after-reconnect.png")
        chat_after_reconnect["server_message_roles"] = _message_roles(env, session_id)
        evidence["chat_after_reconnect"] = chat_after_reconnect
        turns_after = evidence["thread_read_after"].get("turn_count")
        mark(
            f"after reconnect: thread/read {turns_after} turns; "
            f"rollout carries {evidence['rollout_after']['markers']}/4 turn markers"
        )
        if new_state.thread_id != thread_id:
            failures.append(
                f"resumed session switched Codex thread {thread_id} -> {new_state.thread_id}"
            )
        if turns_after != 2:
            failures.append(
                f"native thread/read returns {turns_after} turn(s) after the cold reconnect "
                f"(before: {evidence['thread_read_before'].get('turn_count')}; expected 2)"
            )
        if evidence["rollout_after"]["markers"] < evidence["rollout_before"]["markers"]:
            failures.append(
                "local Codex rollout lost turn history on cold reconnect: "
                f"{evidence['rollout_before']['markers']} -> "
                f"{evidence['rollout_after']['markers']} of 4 turn markers"
            )
        for token in (assistant_1, assistant_2):
            if not any(
                line.strip().startswith("• ") and token in line
                for line in pane_after_reconnect.splitlines()
            ):
                failures.append(f"resumed Codex TUI does not show the earlier reply {token}")
        # The reconnect must neither lose nor duplicate the mirrored transcript.
        if (
            chat_after_reconnect["user_bubbles"],
            chat_after_reconnect["assistant_bubbles"],
            chat_after_reconnect["status"],
        ) != (2, 2, "idle"):
            failures.append(f"Chat after the cold reconnect: {chat_after_reconnect}")
        if chat_after_reconnect["server_message_roles"] != ["user", "assistant"] * 2:
            failures.append(
                "server transcript after the cold reconnect: "
                f"{chat_after_reconnect['server_message_roles']}"
            )
    finally:
        evidence["failures"] = failures
        _save_evidence("evidence.json", json.dumps(evidence, indent=1, default=str))
        _save_evidence(
            "proxy-events.jsonl",
            env.proxy_log.read_text(encoding="utf-8") if env.proxy_log.exists() else "",
        )
        _save_evidence("runner.log", _runner_log_text(env))
        _save_evidence(
            "log-files.txt",
            "\n".join(str(path) for path in sorted(env.root.rglob("*.log"))),
        )
        _save_evidence("server.log", env.server_log.read_text(encoding="utf-8", errors="replace"))
    assert not failures, "\n".join(failures)
