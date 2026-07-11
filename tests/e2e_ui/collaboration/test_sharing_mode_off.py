"""UI: the Share control is grayed out with an explanatory tooltip when the
server's ``OMNIGENT_SHARING_MODE`` is ``off``.

Companion to ``test_permissions_modal.py::test_local_server_disables_share_
button_with_tooltip`` (which covers the *local-server* disable). The shared
session-scoped ``live_server`` runs the default policy (sharing ``on``) and
can't be reconfigured per test, and the admin ``PUT /v1/sharing`` route is
admin-gated (the headerless ``local`` browser identity isn't an admin here), so
this spins up a dedicated server with ``OMNIGENT_SHARING_MODE=off`` and drives
the real SPA to confirm the server-side kill switch surfaces as a disabled
Share button — not merely a 403 on the grant endpoint.

Served through the public-looking loopback alias (``_PUBLIC_LOOPBACK_HOST``, the
same one ``test_permissions_modal`` uses) so ``isCurrentServerLocal()`` is
false and the *only* reason Share is disabled is the sharing-off policy — the
local-server reason would otherwise mask it.
"""

from __future__ import annotations

import json as _json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _PUBLIC_LOOPBACK_HOST,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _build_hello_world_bundle,
    _find_free_port,
)

# Mirrors AppShell.tsx's shareDisabledReason for the sharing-off case.
_OFF_REASON = "Sharing has been disabled for this Omnigent server."


def _public_loopback_url(base_url: str) -> str:
    """Return *base_url* through the browser's public-looking loopback alias."""
    parsed = urlsplit(base_url)
    if parsed.port is None:
        raise AssertionError(f"e2e base URL missing port: {base_url!r}")
    return urlunsplit((parsed.scheme, f"{_PUBLIC_LOOPBACK_HOST}:{parsed.port}", "", "", ""))


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM with a short grace period, escalating to SIGKILL."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def sharing_off_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A dedicated ``OMNIGENT_SHARING_MODE=off`` server + runner + one session.

    Mirrors the shared ``live_server`` spawn and ``seeded_session`` create/bind
    (a separate instance is required because the shared server runs sharing
    ``on`` and is session-scoped). Yields ``(base_url, session_id)``; no agent
    turn runs — the Share button only needs a session to exist.
    """
    from omnigent.runner.identity import token_bound_runner_id

    port = _find_free_port()
    server_tmp = tmp_path_factory.mktemp("e2e_ui_sharing_off")
    log_path = server_tmp / "server.log"
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML)

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    base_url = f"http://127.0.0.1:{port}"
    pythonpath = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"

    server_env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        # The setting under test — the whole point of a dedicated server.
        "OMNIGENT_SHARING_MODE": "off",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen; closed in finally
    proc = subprocess.Popen(
        [
            sys.executable,
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
        ],
        env=server_env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    runner_log_path = server_tmp / "runner.log"
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    runner_env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    runner_proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=runner_env,
        stdout=runner_log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        # Poll /health + runner status until the server can route (same shape
        # as the shared live_server fixture).
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json()["online"] is True:
                        ready = True
                        break
                    last_error = f"runner status HTTP {status.status_code}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        if not ready:
            log_handle.flush()
            log_text = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"sharing-off server not healthy within {_HEALTH_TIMEOUT_S:.0f}s on "
                f"{base_url} (last_error={last_error}).\n{log_text[-3000:]}"
            )

        # Create a hello_world session and bind it to the runner (mirrors
        # seeded_session). No turn is dispatched.
        bundle = _build_hello_world_bundle()
        create = httpx.post(
            f"{base_url}/v1/sessions",
            data={"metadata": _json.dumps({})},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
            timeout=30.0,
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        ).raise_for_status()

        yield (base_url, session_id)
    finally:
        _terminate(runner_proc)
        runner_log_handle.close()
        _terminate(proc)
        log_handle.close()


def test_sharing_off_disables_share_button_with_tooltip(
    page: Page,
    sharing_off_session: tuple[str, str],
) -> None:
    """``OMNIGENT_SHARING_MODE=off`` grays out the header Share button and
    explains why — the server-side kill switch surfaced in the SPA."""
    base_url, session_id = sharing_off_session
    # Public-looking host so the local-server disable doesn't fire; the only
    # reason Share is disabled here is the sharing-off policy.
    public_url = _public_loopback_url(base_url)
    page.goto(f"{public_url}/c/{session_id}")

    share = page.get_by_role("button", name="Share session")
    expect(share).to_be_visible(timeout=60_000)
    expect(share).to_be_disabled()

    page.get_by_label(f"Share session disabled: {_OFF_REASON}").hover()
    tooltip = page.locator("[data-slot=tooltip-content]", has_text=_OFF_REASON)
    expect(tooltip).to_be_visible(timeout=5_000)
    # A disabled Share control opens no dialog.
    expect(page.get_by_role("dialog")).to_have_count(0)
