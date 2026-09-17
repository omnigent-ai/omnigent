r"""E2E: ``/compact`` on a codex-native session must actually compact.

Guarded hazard: after a Codex (codex-native) session runs a turn to
completion, ``/compact`` from the web composer can silently do nothing — no
"Compacting conversation…" spinner, no "Conversation compacted" marker, no
error. The compact POST 200s: the server forwards the control to the runner,
whose ``_inject_codex_compact`` (``omnigent/runner/app.py``) types
``/compact`` into the Codex TUI's tmux pane and presses Enter. Codex's
slash-command popup renders asynchronously, so an Enter sent back-to-back
with the typed command is swallowed by the still-opening popup and the
command never submits — the TUI is left with ``/compact`` sitting
un-submitted in its composer. The injector must settle between typing and
submitting (the same race its sibling ``_inject_codex_permission_mode``
documents and settles around).

This test drives the real user journey end to end against a live Codex TUI:
run one composer turn to completion, run ``/compact``, and require the
compaction to become user-visible — either the in-progress
``compacting-indicator`` or the durable "Conversation compacted" marker. If
the injection races the popup, neither ever appears and this fails.

Codex is launched against the session-scoped mock LLM server (which serves the
OpenAI Responses API the ``codex-native`` harness speaks). The runner resolves
its provider from ``OMNIGENT_CONFIG_HOME``; this test spawns its **own**
server + runner with a writable config home holding a ``key`` openai/responses
provider pointed at the mock — the same routing ``mocked_native_codex_session``
uses for the Codex parity sidecar, but against the plain mock LLM so no cargo
sidecar or gateway credential is required. (The shared ``live_server`` runner
inherits a read-only, gateway-only ``OMNIGENT_CONFIG_HOME`` that parks Codex on
its sign-in screen, hence the dedicated server here.)
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _codex_cli_supports_mocked_app_server,
    _create_native_codex_session,
    _find_free_port,
    _write_mock_codex_provider_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

# Both surfaces render from the same canonical transcript, so the composer /
# turn-settle helpers are shared with the render-parity suites (the same way
# the native codex render-parity suite reuses them).
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

# The compact must surface *something* user-visible within this budget: the
# forwarder's "Compacting conversation…" spinner (compaction_in_progress) or
# the persisted "Conversation compacted" marker. The mock LLM answers Codex's
# summarization request instantly, so this is generous headroom for the tmux
# inject + Codex compaction + forwarder round-trip.
_COMPACTION_FEEDBACK_TIMEOUT_MS = 90_000

# Give the runner's /compact tmux injection time to land in the TUI before the
# terminal-view peek below films the pane state.
_INJECT_SETTLE_MS = 8_000


@pytest.fixture
def codex_native_mock_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Spawn a codex-native session against the mock Responses server.

    Owns its own server + runner (not the shared ``live_server``) so the
    runner's ``OMNIGENT_CONFIG_HOME`` can be a writable dir holding a mock
    openai/responses provider — the shared runner's config home is read-only
    and gateway-only, which parks Codex on its sign-in screen. The runner
    auto-launches Codex in the session terminal on bind, routed to the mock
    LLM, so a real turn runs to completion and ``/compact`` injects into a live
    Codex TUI.

    :param built_spa: Ensures the SPA bundle is on disk before the server boots.
    :param mock_llm_server_url: Session-scoped mock LLM (Responses) base URL.
    :param tmp_path_factory: Pytest temp path factory.
    :returns: ``(base_url, session_id)``.
    """
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the codex-native /compact e2e")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for the codex-native /compact e2e")

    from omnigent.runner.identity import token_bound_runner_id

    server_tmp = tmp_path_factory.mktemp("e2e_ui_codex_compact_server")
    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    for path in (source_codex_home, home_dir, state_dir, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)

    # Route native Codex at the plain mock LLM's Responses API (the same
    # provider shape mocked_native_codex_session uses for the sidecar).
    _write_mock_codex_provider_config(
        config_home, f"{mock_llm_server_url}/v1", model=_CODEX_MOCK_MODEL
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")

    import secrets as _secrets

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home_dir),
        # The server's own hello_world agent isn't used by this journey, but
        # point it at the mock too so nothing reaches a real provider.
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    log_handle = open(log_path, "w")  # noqa: SIM115 — closed in finally
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
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
            ],
            env=server_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            if runner_proc.poll() is not None:
                last_error = f"runner exited early with code {runner_proc.returncode}"
                break
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2)
                if resp.status_code == 200:
                    status_resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                        ready = True
                        break
                    last_error = (
                        f"runner status HTTP {status_resp.status_code}: {status_resp.text[:200]}"
                    )
                else:
                    last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)

        if not ready:
            raise RuntimeError(
                f"codex-compact e2e server did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
                f"Server log:\n{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
                f"Runner log:\n"
                f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
            )

        session_id = _create_native_codex_session(base_url, runner_id, model=_CODEX_MOCK_MODEL)
        yield (base_url, session_id)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for child in (runner_proc, proc):
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        runner_log_handle.close()
        log_handle.close()


@pytest.mark.timeout(600)
def test_codex_native_compact_compacts_after_completed_turn(
    page: Page,
    codex_native_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """``/compact`` after a completed Codex turn produces a visible compaction.

    Journey: run a Codex session turn to completion → run ``/compact`` from
    the web composer → the compaction must become user-visible. If the
    injected command never submits in the Codex TUI (the popup swallowed the
    Enter), nothing at all happens and the final expectation times out.

    :param page: Playwright page fixture.
    :param codex_native_mock_session: ``(base_url, session_id)`` for a
        runner-bound codex-native session whose LLM backend is the mock server.
    :param mock_llm_server_url: Mock LLM server base URL for queueing replies.
    :returns: None.
    """
    base_url, session_id = codex_native_mock_session
    _log.info("codex-native mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # Codex auto-launches in the session terminal on bind; wait for the live
    # TUI so the turn below runs against a booted harness.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # Queue the turn's reply by content marker; every other model call Codex
    # makes — including the /compact summarization request — is served by the
    # model-keyed fallback.
    nonce = uuid.uuid4().hex[:8]
    user_marker, assistant_token = f"usr-1-{nonce}", f"ast-1-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}],
        key=user_marker,
        match=user_marker,
    )
    set_fallback_mock_llm(
        mock_llm_server_url,
        _CODEX_MOCK_MODEL,
        "Conversation summary: one completed echo turn.",
    )

    # Steps 1–2 of the reported journey: run a Codex turn to completion.
    _send(page, _turn_prompt(1, user_marker, assistant_token))
    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    _log.info("turn 1 settled; running /compact")

    # Step 3: run /compact from the composer. With the slash-command
    # suggestions menu open and "/compact" highlighted, Enter completes the
    # selection, which runs the argument-less builtin immediately → the
    # compact control POST.
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    composer.click()
    composer.fill("/compact")
    composer.press("Enter")

    # The bug is *silence*, not an error: the POST 200s (tmux send-keys
    # succeeded), so no inline composer error may appear on either build.
    expect(page.get_by_text("Compact failed", exact=False)).to_have_count(0)

    # Peek at the TUI pane so a video of this run shows the pane state (a
    # lost submit leaves "/compact" sitting un-submitted in Codex's composer),
    # then return to the chat view for the decisive assertion.
    page.wait_for_timeout(_INJECT_SETTLE_MS)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    page.wait_for_timeout(2_000)
    _ensure_chat_view(page)

    # Step 4 — the observable failure: the compaction must become
    # user-visible, as the in-progress spinner or the durable completed
    # marker. If the TUI never submits the command (swallowed Enter), then
    # neither ever appears and this times out.
    compaction_feedback = page.get_by_test_id("compacting-indicator").or_(
        page.get_by_text("Conversation compacted", exact=False)
    )
    expect(compaction_feedback.first).to_be_visible(timeout=_COMPACTION_FEEDBACK_TIMEOUT_MS)
