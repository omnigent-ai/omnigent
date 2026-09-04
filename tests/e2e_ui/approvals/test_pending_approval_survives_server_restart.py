r"""E2E: a pending approval must survive a server-only restart.

An outstanding approval prompt lives only in the server's in-memory
``pending_elicitations`` index — no table in ``omnigent/db/db_models.py``
stores an elicitation. Only the *count* is mirrored to
``omnigent_conversation_metadata.pending_elicitation_count`` so any replica
can render the sidebar badge. Restart just the server (deploy, crash,
``omni server stop && omni server start``) while the runner keeps its parked
future alive, and the prompt is gone: ``GET /v1/sessions/{id}`` replays no
elicitation, the chat renders no approval card, and there is nothing left to
answer — the runner's parked turn is silently refused a day later
(``omnigent/runner/pending_approvals._DEFAULT_WAIT_SECONDS``).

Journey (user-observable, from the report):

1. start a session on an agent whose policy returns ASK for a gated shell
   command (the ``blast_radius`` guardrail with ``gate_pushes: true`` — the
   same recipe as the ``approval_session`` fixture; the report's
   ``ask_on_os_tools`` is one example of "any policy that returns ASK"),
2. get the agent to trip it (the mock LLM deterministically emits the gated
   ``sys_os_shell("git push origin main")`` call) so the approval card and
   the sidebar "Needs response" badge appear — do NOT answer it,
3. restart only the server, leaving the runner process running (it re-tunnels
   into the new server process on the same port and database),
4. reload the UI.

Expected (the regression contract this test pins): the approval card is still
rendered, still answerable, and the sidebar badge and the session agree that
one approval is waiting.

The test spawns its OWN server+runner pair rather than the shared
session-scoped ``live_server`` fixture, so restarting the server cannot
poison the rest of the suite.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e_ui.conftest import (
    _APPROVAL_AGENT_YAML,
    _BUILD_OUTPUT,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _create_bundled_session,
    _find_free_port,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_APPROVAL_CARD = '[data-testid="approval-card"]'

# The agent must boot, take a turn, and emit the gated tool call before the
# card appears — cold-start can be slow under CI load.
_AGENT_TURN_TIMEOUT_MS = 120_000

# Server boot + sibling-runner tunnel (re)connect budget. The runner's
# reconnect loop backs off between attempts, so the post-restart wait needs
# more headroom than the cold boot.
_STACK_READY_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.5


@pytest.fixture
def browser_context_args(browser_context_args: dict) -> dict:
    """Record the sync ``page`` fixture's context when recording is requested.

    The conftest's ``OMNIGENT_E2E_RECORD_DIR`` hook only patches Playwright's
    *async* API; tests that take the sync pytest-playwright ``page`` fixture
    (this one does) get no video from it. Injecting ``record_video_dir`` here
    films the journey into that dir. Env-gated, so ordinary runs are
    unaffected.
    """
    args = {**browser_context_args}
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        Path(record_dir).mkdir(parents=True, exist_ok=True)
        args.setdefault("record_video_dir", record_dir)
    return args


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _listed_pending_count(base_url: str, session_id: str) -> int | None:
    """Return the session-list payload's badge count for *session_id*.

    This is the field the sidebar renders (``pending_elicitations_count``,
    computed as ``max(live_count, persisted_count)`` for runner-bound rows),
    so it is the API-level truth behind the "Needs response" badge.
    """
    resp = httpx.get(f"{base_url}/v1/sessions", timeout=10.0)
    resp.raise_for_status()
    for conv in resp.json().get("data", []):
        if conv.get("id") == session_id:
            count = conv.get("pending_elicitations_count")
            return int(count) if count is not None else None
    return None


def _sidebar_row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _awaiting_badge(row: Locator) -> Locator:
    """Locate the row's "Needs response" tag (the awaiting state badge)."""
    return row.locator('[data-testid="session-state-badge"][data-state="awaiting"]')


@dataclass
class _RestartableStack:
    """A dedicated omnigent server + sibling runner the test may restart.

    ``stop_server``/``start_server`` recycle ONLY the server process — the
    runner subprocess (its parked approval future included) and the SQLite
    database survive, which is exactly the report's "server-only restart"
    topology (deploy / crash / ``omni server stop && omni server start``).
    """

    base_url: str
    mock_llm_url: str
    runner_id: str
    server_argv: list[str]
    server_env: dict[str, str]
    server_cwd: str | None
    server_log: Path
    runner_log: Path
    server_proc: subprocess.Popen[bytes]
    runner_proc: subprocess.Popen[bytes]

    def stop_server(self) -> None:
        """Stop only the server process (the ``omni server stop`` half)."""
        assert self.server_proc.poll() is None, "server process already exited"
        self.server_proc.send_signal(signal.SIGTERM)
        try:
            self.server_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.server_proc.kill()
            self.server_proc.wait(timeout=5)

    def start_server(self) -> None:
        """Start a fresh server process on the same port and database."""
        log_handle = open(self.server_log, "a")  # noqa: SIM115 — lives for Popen lifetime
        self.server_proc = subprocess.Popen(
            self.server_argv,
            env=self.server_env,
            cwd=self.server_cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        _wait_stack_ready(self)

    def restart_server(self) -> None:
        """Server-only restart: the runner keeps running throughout."""
        self.stop_server()
        assert self.runner_proc.poll() is None, "runner died with the server — invalid topology"
        self.start_server()


def _wait_stack_ready(stack: _RestartableStack, timeout_s: float = _STACK_READY_TIMEOUT_S) -> None:
    """Poll until the server is healthy AND the sibling runner is online.

    Runner-online is the signal that the WebSocket tunnel (re)connected — the
    same readiness contract ``live_server`` uses at cold boot, reused here
    after each restart so assertions never race the reconnect.

    :raises RuntimeError: with the server/runner log tails on timeout.
    """
    deadline = time.monotonic() + timeout_s
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if stack.server_proc.poll() is not None:
            last_error = f"server exited early with code {stack.server_proc.returncode}"
            break
        try:
            health = httpx.get(f"{stack.base_url}/health", timeout=2)
            if health.status_code == 200:
                status = httpx.get(
                    f"{stack.base_url}/v1/runners/{stack.runner_id}/status", timeout=2
                )
                if status.status_code == 200 and status.json()["online"] is True:
                    return
                last_error = f"runner status HTTP {status.status_code}: {status.text[:200]}"
            else:
                last_error = f"health HTTP {health.status_code}: {health.text[:200]}"
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_INTERVAL_S)
    server_tail = stack.server_log.read_text()[-2000:] if stack.server_log.exists() else ""
    runner_tail = stack.runner_log.read_text()[-2000:] if stack.runner_log.exists() else ""
    raise RuntimeError(
        f"stack not ready within {timeout_s:.0f}s on {stack.base_url} "
        f"(last_error={last_error}).\nserver log tail:\n{server_tail}\n"
        f"runner log tail:\n{runner_tail}"
    )


@pytest.fixture
def restart_stack(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_RestartableStack]:
    """Spawn a dedicated, restartable ``omnigent server`` + sibling runner.

    Mirrors the ``live_server`` fixture's spawn recipe (same argv/env shape,
    same mock-LLM wiring, same readiness contract) but scoped to this test so
    the server process can be stopped and restarted without breaking the
    shared session-scoped stack other tests use.
    """
    import secrets as _secrets

    from omnigent.runner.identity import token_bound_runner_id

    port = _find_free_port()
    stack_tmp = tmp_path_factory.mktemp("approval_restart_stack")
    server_log = stack_tmp / "server.log"
    runner_log = stack_tmp / "runner.log"
    db_path = stack_tmp / "test.db"
    artifact_dir = stack_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = stack_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML)

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    base_url = f"http://127.0.0.1:{port}"

    # Isolate the spawned stack from any ambient provider registry. A repro
    # host's config home can declare gateway providers whose ``api_key_ref``
    # secrets aren't available to a spawned subprocess; the openai-agents
    # spawn-env builder eagerly expands every provider family, so an
    # unresolvable secret would fail turn setup before the gated tool call is
    # even reached. An empty config home means the harness relies solely on
    # the mock-LLM ``OPENAI_BASE_URL`` env below — the same clean-config
    # posture the shared ``live_server`` fixture assumes in normal CI.
    config_home = stack_tmp / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    data_dir = stack_tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    server_env: dict[str, str] = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(data_dir),
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
    }
    apply_server_env(server_env, _REPO_ROOT)
    server_argv = [
        server_executable(),
        "-c",
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
    server_cwd = compat_server_cwd()
    server_handle = open(server_log, "w")  # noqa: SIM115 — lives for Popen lifetime
    server_proc = subprocess.Popen(
        server_argv,
        env=server_env,
        cwd=server_cwd,
        stdout=server_handle,
        stderr=subprocess.STDOUT,
    )

    runner_env = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(data_dir),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    runner_handle = open(runner_log, "w")  # noqa: SIM115 — lives for Popen lifetime
    runner_proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=runner_env,
        stdout=runner_handle,
        stderr=subprocess.STDOUT,
    )

    stack = _RestartableStack(
        base_url=base_url,
        mock_llm_url=mock_llm_server_url,
        runner_id=runner_id,
        server_argv=server_argv,
        server_env=server_env,
        server_cwd=server_cwd,
        server_log=server_log,
        runner_log=runner_log,
        server_proc=server_proc,
        runner_proc=runner_proc,
    )
    try:
        _wait_stack_ready(stack)
        # LLM-backed guardrail classifier fallback, mirroring live_server: any
        # policy-LLM call the server makes must get a valid ALLOW verdict.
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )
        yield stack
    finally:
        for proc, grace in ((stack.runner_proc, 5), (stack.server_proc, 10)):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


@pytest.mark.timeout(600)
def test_pending_approval_survives_server_restart(
    page: Page,
    restart_stack: _RestartableStack,
) -> None:
    """Gated tool call → pending card + badge → server-only restart → the card must survive."""
    stack = restart_stack
    base_url = stack.base_url

    # Unique model per run so the mock queue can't be stolen by another
    # session's turn (same isolation trick as the approval_session fixture).
    approval_model = f"approval-restart-{uuid.uuid4().hex[:8]}"
    agent_yaml_text = _APPROVAL_AGENT_YAML.replace("gpt-4o-mini", approval_model)
    configure_mock_llm(
        stack.mock_llm_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_git_push",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": "git push origin main"}),
                    }
                ]
            }
        ],
        key=approval_model,
    )
    # Post-approval wrap-up call (only reached once the fix makes the restored
    # prompt answerable and someone approves it).
    set_fallback_mock_llm(stack.mock_llm_url, approval_model, "Command executed.")

    session_id = _create_bundled_session(base_url, stack.runner_id, agent_yaml_text)

    # -- Journey steps 1–2: trip the ASK policy; card + badge appear ---------
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
    expect(card.get_by_text("Approval required")).to_be_visible()
    # The server is genuinely parked on this prompt, not an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"
    # The sidebar row advertises the pending approval ("Needs response").
    expect(_awaiting_badge(_sidebar_row(page, session_id))).to_be_visible(timeout=30_000)
    count_before = _listed_pending_count(base_url, session_id)
    assert count_before, f"list payload shows no pending approval before restart: {count_before!r}"

    # -- Journey step 3: restart ONLY the server; the runner survives --------
    stack.restart_server()
    assert stack.runner_proc.poll() is None, "runner must outlive the server restart"

    # -- Journey step 4: reload the UI ---------------------------------------
    page.goto(f"{base_url}/c/{session_id}")
    # Load gate: the transcript rendered. Not the idle composer placeholder -
    # while an approval is pending the composer sits in its disabled awaiting
    # state, so the idle placeholder appears only when the prompt was lost.
    expect(page.get_by_text("Run the command now.").first).to_be_visible(timeout=30_000)

    # Probes for the failure message: what the badge (list payload) claims vs
    # what the session snapshot can actually replay.
    listed_after = _listed_pending_count(base_url, session_id)
    snapshot_after = _pending_elicitations(base_url, session_id)

    # THE regression contract (currently broken): the approval outstanding
    # when the server went down is still rendered and still answerable when
    # it comes back. On the buggy build the elicitation was never persisted,
    # so the snapshot replays nothing and no card ever renders.
    card_after = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    try:
        expect(card_after).to_be_visible(timeout=30_000)
    except AssertionError as exc:
        raise AssertionError(
            "After a server-only restart the session renders no "
            "pending approval card — the parked prompt was never persisted and is "
            "unanswerable (the runner's turn will be silently refused ~24h later). "
            f"Immediately after reload the session-list badge field reported "
            f"pending_elicitations_count={listed_after!r} while the session snapshot "
            f"replayed {len(snapshot_after)} pending elicitation(s); at failure time "
            f"the list reports {_listed_pending_count(base_url, session_id)!r} and the "
            f"snapshot replays {len(_pending_elicitations(base_url, session_id))}."
        ) from exc

    # Still answerable: the restored card exposes its Approve control.
    expect(card_after.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    assert _pending_elicitations(base_url, session_id), (
        "session snapshot replays no pending elicitation after restart"
    )
    # And the badge agrees with the session: one approval is waiting.
    expect(_awaiting_badge(_sidebar_row(page, session_id))).to_be_visible(timeout=30_000)
