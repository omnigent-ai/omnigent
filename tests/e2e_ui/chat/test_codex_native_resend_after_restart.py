r"""Codex-native web re-send must not double-paste after a server restart.

Journey (a user-observable regression): on a ``codex-native`` session the user
sends a message; its POST response is lost, so the web client keeps the same
``stable_id`` and re-sends after a server restart. The server should recognise
the re-send as the already-delivered message and paste nothing. A build without a durable
re-send guard does not: the in-process ``pending_inputs`` index is wiped by
the restart, and codex user items are persisted under ``uuid5(source_id)``
rather than the web ``stable_id`` -- so nothing recognises the duplicate and
the prompt is pasted into the Codex TUI a second time, leaving two identical
user messages in the transcript (and in the Chat view).

This drives the real Codex CLI in a runner-owned terminal, through a genuine
server restart, exactly as production does: the first send is typed into the
web composer, then the recovery re-send is issued as the same-``stable_id``
``POST /events`` the client performs automatically. It asserts the prompt lands
in the transcript exactly once, which requires the server to keep a durable
``stable_id`` -> committed-item mapping that survives a restart.

The session/server/runner triple is owned by this test (not the shared
``live_server``) because the reproduction requires recycling the server process
while the runner and its live Codex turn survive; run it OUTSIDE
``dev.repro_env exec`` so it drives its own restartable server.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e_ui.conftest import (
    _CODEX_MOCK_MODEL,
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _create_native_codex_session,
    _find_free_port,
    reset_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER_PLACEHOLDER = "Send a message…"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'

# Codex boots in the terminal on the first dispatch; auto-launch + first-run
# pre-accept + WS attach can take a while on a cold CI runner.
_TERMINAL_READY_TIMEOUT_MS = 120_000
_TURN_SETTLE_TIMEOUT_S = 120.0
# After the recovery re-send, a buggy build re-pastes within seconds (the Codex
# terminal is already warm); a fixed build never pastes. Poll long enough that
# the duplicate would have surfaced before asserting it did not.
_RESEND_OBSERVE_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class CodexResendSession:
    base_url: str
    session_id: str
    mock_url: str
    restart_server: Callable[[], None]


def _items(base_url: str, session_id: str) -> list[dict[str, object]]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return list(resp.json().get("data", []))


def _count_user_messages(base_url: str, session_id: str, needle: str) -> int:
    """Number of committed *user* message items whose text contains *needle*."""
    total = 0
    for item in _items(base_url, session_id):
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        text = " ".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        if needle in text:
            total += 1
    return total


def _count_assistant_messages(base_url: str, session_id: str) -> int:
    return sum(
        1
        for item in _items(base_url, session_id)
        if item.get("type") == "message" and item.get("role") == "assistant"
    )


@pytest.fixture
def codex_resend_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[CodexResendSession]:
    """A runner-bound native Codex session on a server this test can recycle.

    Points the real Codex CLI at the in-process mock LLM (so no credentials are
    needed) and preserves the runner + its Codex terminal across a server
    restart, reproducing the production "lost POST -> restart -> re-send" path.
    """
    from omnigent.runner.identity import token_bound_runner_id

    mock_url = mock_llm_server_url
    reset_mock_llm(mock_url)
    # The reply content is irrelevant to the double-paste (it is about the user
    # item), so a single non-exhausting fallback answers every Codex LLM call
    # regardless of how many it makes internally.
    set_fallback_mock_llm(mock_url, _CODEX_MOCK_MODEL, "Acknowledged.")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_codex_resend")
    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    for path in (config_home, source_codex_home, home_dir, state_dir, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)

    (config_home / "config.yaml").write_text(
        f"""\
providers:
  mock-codex:
    kind: key
    default: [openai]
    openai:
      base_url: "{mock_url}/v1"
      api_key: "mock-key"
      wire_api: responses
      models:
        default: {_CODEX_MOCK_MODEL}
""",
        encoding="utf-8",
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = server_tmp / "test.db"
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_command = [
        sys.executable,
        "-c",
        "import omnigent.server.presence as _p; _p._LEAVE_GRACE_S = 1.0; "
        "from omnigent.cli import main; main()",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
        "--agent",
        str(agent_yaml_path),
    ]

    log_handle = open(log_path, "w")  # noqa: SIM115
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None

    def _spawn_server() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            server_command, env=server_env, stdout=log_handle, stderr=subprocess.STDOUT
        )

    def _wait_until_ready(
        server_process: subprocess.Popen[bytes], runner_process: subprocess.Popen[bytes]
    ) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if server_process.poll() is not None:
                last_error = f"server exited early with code {server_process.returncode}"
                break
            if runner_process.poll() is not None:
                last_error = f"runner exited early with code {runner_process.returncode}"
                break
            try:
                health = httpx.get(f"{base_url}/health", timeout=2)
                if health.status_code == 200:
                    status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online") is True:
                        return
                    last_error = f"runner status HTTP {status.status_code}: {status.text[:200]}"
                else:
                    last_error = f"health HTTP {health.status_code}: {health.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        raise RuntimeError(
            f"codex-resend e2e server did not become healthy within {_HEALTH_TIMEOUT_S:.0f}s "
            f"on {base_url} (last_error={last_error}).\n"
            f"Server log:\n{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
            f"Runner log:\n"
            f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
        )

    try:
        proc = _spawn_server()
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )
        _wait_until_ready(proc, runner_proc)
        session_id = _create_native_codex_session(base_url, runner_id)

        def _restart_server() -> None:
            """Recycle only the server, preserving the runner and its Codex turn."""
            nonlocal proc
            assert proc is not None
            assert runner_proc is not None
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            proc = _spawn_server()
            _wait_until_ready(proc, runner_proc)

        yield CodexResendSession(
            base_url=base_url,
            session_id=session_id,
            mock_url=mock_url,
            restart_server=_restart_server,
        )
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if runner_proc is not None and runner_proc.poll() is None:
            runner_proc.send_signal(signal.SIGTERM)
            try:
                runner_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner_proc.kill()
                runner_proc.wait(timeout=5)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()
        runner_log_handle.close()


def _wait_first_turn(base_url: str, session_id: str, needle: str) -> None:
    """Block until the first send has round-tripped (user + assistant committed).

    Once the forwarder mirrors the user item back, its pending_inputs entry is
    drained -- so the restart-then-resend faithfully hits the empty-guard path
    rather than a still-live dedup entry.
    """
    deadline = time.monotonic() + _TURN_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        if (
            _count_user_messages(base_url, session_id, needle) >= 1
            and _count_assistant_messages(base_url, session_id) >= 1
        ):
            return
        time.sleep(1.0)
    raise AssertionError(
        f"first Codex turn did not settle within {_TURN_SETTLE_TIMEOUT_S:.0f}s "
        f"(user={_count_user_messages(base_url, session_id, needle)}, "
        f"assistant={_count_assistant_messages(base_url, session_id)})"
    )


def test_codex_native_resend_double_paste_after_restart(
    browser: Browser,
    codex_resend_session: CodexResendSession,
) -> None:
    base_url = codex_resend_session.base_url
    session_id = codex_resend_session.session_id
    prompt = f"resend probe {uuid.uuid4().hex[:8]}"

    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        record_video_dir=record_dir or None,
    )
    page: Page = context.new_page()

    # Capture the stable_id the composer generates for the first send so the
    # recovery re-send can reuse it exactly like the client does.
    captured: dict[str, str] = {}

    def _capture_stable_id(request: object) -> None:
        req = request  # playwright.sync_api.Request
        try:
            if getattr(req, "method", "") != "POST" or not getattr(req, "url", "").endswith(
                "/events"
            ):
                return
            body = req.post_data  # type: ignore[attr-defined]
            if not body or "input_text" not in body or "stable_id" not in body:
                return
            data = json.loads(body)
            stable_id = data.get("data", {}).get("stable_id")
            if isinstance(stable_id, str) and stable_id and "stable_id" not in captured:
                captured["stable_id"] = stable_id
        except (ValueError, AttributeError):
            return

    page.on("request", _capture_stable_id)

    try:
        page.goto(f"{base_url}/c/{session_id}")

        # Native (terminal-first) sessions default to the Terminal view; the
        # Chat view renders the same canonical transcript as message bubbles.
        toggle = page.get_by_test_id("view-mode-toggle")
        expect(toggle).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
        chat_segment = page.get_by_test_id("view-mode-chat")
        expect(chat_segment).to_be_enabled(timeout=_TERMINAL_READY_TIMEOUT_MS)
        chat_segment.click()

        # --- Send #1 (a real user action: type + Send) ---------------------
        composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(prompt)
        page.get_by_role("button", name="Send", exact=True).click()

        _wait_first_turn(base_url, session_id, prompt)
        assert "stable_id" in captured, "composer send did not carry a stable_id"
        stable_id = captured["stable_id"]
        expect(page.locator(_USER_BUBBLE, has_text=prompt)).to_have_count(1, timeout=30_000)

        # Let the mirrored user item's pending_inputs entry fully drain before
        # the restart wipes the in-memory guard.
        time.sleep(3.0)
        assert _count_user_messages(base_url, session_id, prompt) == 1

        # --- Lose the connection: restart the server (runner + Codex survive) -
        codex_resend_session.restart_server()
        page.reload()
        chat_segment = page.get_by_test_id("view-mode-chat")
        expect(chat_segment).to_be_enabled(timeout=_TERMINAL_READY_TIMEOUT_MS)
        chat_segment.click()
        expect(page.locator(_USER_BUBBLE, has_text=prompt)).to_have_count(1, timeout=60_000)

        # --- Recovery re-send: the same stable_id the client re-POSTs ---------
        resend = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                    "stable_id": stable_id,
                },
            },
            timeout=30.0,
        )
        assert resend.status_code in (200, 202), (
            f"re-send HTTP {resend.status_code}: {resend.text}"
        )

        # A buggy build pastes the prompt a second time; poll until it does (so
        # the failure is caught) or until the observation window elapses.
        count = _count_user_messages(base_url, session_id, prompt)
        deadline = time.monotonic() + _RESEND_OBSERVE_TIMEOUT_S
        while time.monotonic() < deadline:
            count = _count_user_messages(base_url, session_id, prompt)
            if count >= 2:
                break
            time.sleep(1.0)

        # Reflect the final transcript in the Chat view for the recording.
        with contextlib.suppress(Exception):
            page.reload()
            page.get_by_test_id("view-mode-chat").click()
            expect(page.locator(_USER_BUBBLE, has_text=prompt)).to_have_count(
                count, timeout=30_000
            )

        assert count == 1, (
            "after a server restart, re-sending the same stable_id pasted the "
            f"prompt into the Codex session again -- {count} identical user "
            "messages in the transcript (expected exactly 1). The in-memory "
            "pending_inputs guard is wiped by the restart, so only a durable "
            "stable_id -> committed-item mapping can recognise the duplicate."
        )
    finally:
        context.close()
