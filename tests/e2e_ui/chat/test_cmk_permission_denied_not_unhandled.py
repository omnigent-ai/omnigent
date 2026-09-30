"""E2E: a CMK ``PERMISSION_DENIED`` on item persist must not surface as an unhandled 500.

Journey (Databricks agentbricks/mas embedding): a user chats in a long-lived
session whose conversation store encrypts every item payload through the
mas-java CMK RPC (``EncryptPayloads`` / ``DecryptPayloads``) behind the Barnacle
forward-proxy. The proxy edge flips into a short denial burst — the RPC fails
with ``PERMISSION_DENIED`` / ``"Received http2 header with status: 403"``
before it reaches the CMK handler — while the user sends a message. The persist
raises inside ``POST /v1/sessions/{id}/events`` and the raw
``_InactiveRpcError`` falls through to the server's generic catch-all::

    Unhandled exception: <_InactiveRpcError of RPC that terminated with:
        status = StatusCode.PERMISSION_DENIED
        details = "Received http2 header with status: 403" ...

The client gets ``500 {"code": "internal_error"}``; the SPA rolls the user's
bubble back and shows "An internal error occurred." The decrypt side fails the
same way: reloading the session during the burst answers the transcript load
with the same unhandled 500 and the SPA reports it could not load the
conversation.

The reproduction stands in for that deployment: the real ``omnigent server``
runs with the store's batch encode/decode hooks routed through an in-process
gRPC CMK service (``_cmk_denying_store_server``) that can be flipped into
the denial burst, and the real SPA is driven through a healthy turn and then
the failing send (or the failing reload).

Run::

    .venv/bin/python -m pytest tests/e2e_ui/chat/test_cmk_permission_denied_not_unhandled.py \\
        --ui-skip-build -v
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Response, expect

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_server_env
from tests.e2e_ui.chat._cmk_denying_store_server import DENY_FLAG_ENV
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _HEALTH_POLL_INTERVAL_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _create_runner_bound_session,
    _find_free_port,
    configure_mock_llm,
)

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_HEALTHY_PROMPT = "hello before the burst"
_HEALTHY_REPLY = "Hello! Your message was encrypted and stored."
_BURST_PROMPT = "hello during the burst"
_BOOT_TIMEOUT_S = 90.0
_UNHANDLED_MARKER = "Unhandled exception:"
# A logged exception's status line follows its marker within this many characters.
_LOG_ENTRY_SPAN = 800


class CmkServer(NamedTuple):
    """Handles on the stand-in server spawned by :func:`cmk_server`."""

    base_url: str
    runner_id: str
    deny_flag: Path
    server_log: Path


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """
    Terminate *proc*, escalating to SIGKILL after a grace period.

    :param proc: The child process to stop.
    """
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_until_routable(
    server_proc: subprocess.Popen[bytes],
    base_url: str,
    runner_id: str,
    server_log: Path,
) -> None:
    """
    Block until the server is healthy and can route turns through the runner.

    :param server_proc: The server process (an early exit fails fast).
    :param base_url: Server base URL to probe.
    :param runner_id: Runner whose tunnel must report online.
    :param server_log: Server log dumped into the failure message.
    :raises RuntimeError: If the server is not routable within the boot budget.
    """
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if server_proc.poll() is not None:
            last_error = f"server exited early with code {server_proc.returncode}"
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
    log_text = server_log.read_text() if server_log.exists() else ""
    raise RuntimeError(
        f"CMK stand-in server did not become routable within {_BOOT_TIMEOUT_S:.0f}s on "
        f"{base_url} (last_error={last_error}).\nServer log at {server_log}:\n{log_text[-3000:]}"
    )


@pytest.fixture(scope="module")
def cmk_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[CmkServer]:
    """
    Spawn the real server with CMK-routed payload encoding, plus its runner.

    :param built_spa: Ensures the SPA bundle exists before the server mounts it.
    :param mock_llm_server_url: Mock LLM the agent's turns are served from.
    :param tmp_path_factory: Per-module scratch space for logs, DB and artifacts.
    :returns: Handles on the spawned server.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_tmp = tmp_path_factory.mktemp("cmk_denying_server")
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir()
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML)
    deny_flag = server_tmp / "cmk-deny"
    server_log = server_tmp / "server.log"
    runner_log = server_tmp / "runner.log"

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    server_env = apply_server_env(
        {
            **os.environ,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "OPENAI_API_KEY": "mock-key",
            "ANTHROPIC_API_KEY": "",
            "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
            DENY_FLAG_ENV: str(deny_flag),
        },
        _REPO_ROOT,
    )
    runner_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    with server_log.open("w") as server_out, runner_log.open("w") as runner_out:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.e2e_ui.chat._cmk_denying_store_server",
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
            cwd=_REPO_ROOT,
            stdout=server_out,
            stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_out,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_until_routable(server_proc, base_url, runner_id, server_log)
        yield CmkServer(base_url, runner_id, deny_flag, server_log)
    finally:
        _stop(runner_proc)
        _stop(server_proc)


@pytest.fixture
def cmk_session(cmk_server: CmkServer) -> Iterator[str]:
    """
    Create a runner-bound ``hello_world`` session on the stand-in server.

    :param cmk_server: The spawned server.
    :returns: The new session id.
    """
    session_id = _create_runner_bound_session(cmk_server.base_url, cmk_server.runner_id)
    try:
        yield session_id
    finally:
        cmk_server.deny_flag.unlink(missing_ok=True)
        httpx.delete(f"{cmk_server.base_url}/v1/sessions/{session_id}", timeout=10.0)


def _json_body(response: Response) -> dict[str, Any]:
    """
    Decode a JSON object response body, tolerating non-JSON bodies.

    :param response: The captured HTTP response.
    :returns: The decoded object, or ``{}`` when the body is not a JSON object.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _unhandled_permission_denied(server_log: Path, offset: int) -> str | None:
    """
    Find an ``Unhandled exception`` entry booked for a ``PERMISSION_DENIED`` RPC.

    :param server_log: The server's captured log output.
    :param offset: Byte offset the scan starts from (the log is shared across tests).
    :returns: The offending entry's head, or ``None`` when there is none.
    """
    log_text = server_log.read_bytes()[offset:].decode(errors="replace")
    start = 0
    while (index := log_text.find(_UNHANDLED_MARKER, start)) != -1:
        entry = log_text[index : index + _LOG_ENTRY_SPAN]
        if "StatusCode.PERMISSION_DENIED" in entry:
            return entry.splitlines()[0]
        start = index + len(_UNHANDLED_MARKER)
    return None


def _complete_healthy_turn(page: Page, base_url: str, session_id: str, mock_url: str) -> None:
    """
    Open the session and complete one turn while the CMK edge still allows calls.

    :param page: Playwright page driving the SPA.
    :param base_url: Stand-in server base URL.
    :param session_id: Session to open.
    :param mock_url: Mock LLM to script the healthy reply on.
    """
    configure_mock_llm(
        mock_url,
        [{"text": _HEALTHY_REPLY}],
        key="cmk-healthy-turn",
        match=_HEALTHY_PROMPT,
    )
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    composer.fill(_HEALTHY_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).first).to_contain_text(_HEALTHY_REPLY, timeout=30_000)


def test_cmk_permission_denied_on_persist_is_not_an_unhandled_internal_error(
    page: Page,
    cmk_server: CmkServer,
    cmk_session: str,
    mock_llm_server_url: str,
) -> None:
    """
    A denied CMK encrypt during a send must not be answered as an unhandled 500.

    Drives the reconstructed journey: a healthy turn completes, the CMK edge
    flips into its denial burst, and the user sends again. The send may still
    fail — the payload could not be encrypted — but the denial is an upstream
    condition the server must recognise: the client must not receive the
    generic ``500 internal_error`` and the server must not book the RPC error
    as an unhandled exception.

    :param page: Playwright page driving the SPA.
    :param cmk_server: The stand-in server whose CMK edge the test flips.
    :param cmk_session: The runner-bound session under test.
    :param mock_llm_server_url: Mock LLM scripted with the healthy reply.
    """
    log_offset = cmk_server.server_log.stat().st_size
    _complete_healthy_turn(page, cmk_server.base_url, cmk_session, mock_llm_server_url)

    cmk_server.deny_flag.touch()

    events_path = f"/v1/sessions/{cmk_session}/events"
    composer = page.get_by_label("Message the agent")
    composer.fill(_BURST_PROMPT)
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and urlparse(response.url).path == events_path
        )
    ) as posted:
        page.get_by_role("button", name="Send", exact=True).click()
    response = posted.value

    error_pill = page.get_by_test_id("error-pill")
    expect(error_pill).to_be_visible(timeout=15_000)
    headline = page.get_by_test_id("error-headline").inner_text()
    # Hold the failed state on screen so the recording shows what the user sees.
    page.wait_for_timeout(2_000)

    body = _json_body(response)
    code = body.get("error", {}).get("code")
    assert (response.status, code) != (500, "internal_error"), (
        "CMK PERMISSION_DENIED on item persist escaped as an unhandled 500 internal_error; "
        f"the SPA showed {headline!r}"
    )
    unhandled = _unhandled_permission_denied(cmk_server.server_log, log_offset)
    assert unhandled is None, (
        "denied CMK RPC was booked as an unhandled server error: " + unhandled
    )


def test_cmk_permission_denied_on_read_is_not_an_unhandled_internal_error(
    page: Page,
    cmk_server: CmkServer,
    cmk_session: str,
    mock_llm_server_url: str,
) -> None:
    """
    A denied CMK decrypt while reloading the session must not be answered as an unhandled 500.

    Drives the read side of the journey: a healthy turn leaves encrypted items
    behind, the CMK edge flips into its denial burst, and the user reloads the
    session. The transcript may fail to load — nothing can be decrypted — but
    the client must not receive the generic ``500 internal_error`` and the
    server must not book the RPC error as an unhandled exception.

    :param page: Playwright page driving the SPA.
    :param cmk_server: The stand-in server whose CMK edge the test flips.
    :param cmk_session: The runner-bound session under test.
    :param mock_llm_server_url: Mock LLM scripted with the healthy reply.
    """
    log_offset = cmk_server.server_log.stat().st_size
    _complete_healthy_turn(page, cmk_server.base_url, cmk_session, mock_llm_server_url)

    cmk_server.deny_flag.touch()

    session_prefix = f"/v1/sessions/{cmk_session}"
    internal_errors: list[str] = []

    def _record_internal_error(response: Response) -> None:
        if response.status != 500 or not urlparse(response.url).path.startswith(session_prefix):
            return
        if _json_body(response).get("error", {}).get("code") == "internal_error":
            internal_errors.append(urlparse(response.url).path)

    page.on("response", _record_internal_error)
    page.reload()
    transcript_or_failure = page.locator(_ASSISTANT).first.or_(
        page.get_by_role("heading", name="Conversation not found")
    )
    expect(transcript_or_failure).to_be_visible(timeout=15_000)
    shown = page.get_by_role("main").inner_text()
    # Hold the loaded (or failed) state on screen so the recording shows what the user sees.
    page.wait_for_timeout(2_000)

    assert not internal_errors, (
        "CMK PERMISSION_DENIED on item decrypt escaped as an unhandled 500 internal_error on "
        f"{internal_errors[0]}; the SPA showed {shown[:160]!r}"
    )
    unhandled = _unhandled_permission_denied(cmk_server.server_log, log_offset)
    assert unhandled is None, (
        "denied CMK RPC was booked as an unhandled server error: " + unhandled
    )
