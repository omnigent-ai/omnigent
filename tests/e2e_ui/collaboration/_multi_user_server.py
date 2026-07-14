"""Shared helper: spawn a dedicated *multi-user* header-auth Omnigent server.

The suite's shared ``live_server`` runs single-user
(``OMNIGENT_LOCAL_SINGLE_USER=1``, set in ``tests/conftest.py``), where the
Share affordances are intentionally hidden. Tests that need to exercise the
Share button / modal / kebab (or the sharing-off disable) must therefore run
against a server that is *not* single-user — a header-auth deploy with more
than one possible user, exactly like a Databricks Apps / SSO-proxy install.

This spins one up: the single-user marker is cleared, an admin identity is
declared via ``OMNIGENT_ADMINS`` so a header-identified browser can manage,
and a hello_world session is created and runner-bound. Served through the
public-looking loopback alias (``_PUBLIC_LOOPBACK_HOST``) so
``isCurrentServerLocal()`` is false and the Share affordances aren't masked by
the local-server disable.
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
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _PUBLIC_LOOPBACK_HOST,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _build_hello_world_bundle,
    _find_free_port,
)

# Admin identity the browser presents via X-Forwarded-Email. Declared in
# OMNIGENT_ADMINS so it resolves as an admin (manage on any session, so the
# Share button renders on the local-owned seeded session).
ADMIN_EMAIL = "admin@ui.test"


@dataclass
class MultiUserServer:
    """A running multi-user server plus one runner-bound session.

    :param base_url: Loopback base URL (``http://127.0.0.1:<port>``) for REST.
    :param public_url: The same server via the public-looking loopback alias,
        so the browser's ``isCurrentServerLocal()`` is false.
    :param session_id: A hello_world session bound to the runner.
    """

    base_url: str
    public_url: str
    session_id: str


def public_loopback_url(base_url: str) -> str:
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


def spawn_multi_user_server(
    mock_llm_server_url: str,
    server_tmp,
    *,
    extra_server_env: dict[str, str] | None = None,
) -> Iterator[MultiUserServer]:
    """Spawn a multi-user server + runner + one session; yield a handle.

    Mirrors the shared ``live_server`` spawn but with the single-user marker
    cleared and an admin declared. ``extra_server_env`` overrides/augments the
    server env (e.g. ``OMNIGENT_SHARING_MODE=off``).

    :param mock_llm_server_url: Session-scoped mock LLM base (no real creds).
    :param server_tmp: A per-test temp dir (``tmp_path_factory.mktemp(...)``).
    :param extra_server_env: Extra env vars for the server process.
    :yields: A :class:`MultiUserServer` handle.
    """
    from omnigent.runner.identity import token_bound_runner_id

    port = _find_free_port()
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
        # The whole point: NOT single-user. Clear the marker the suite sets so
        # /v1/info reports single_user:false and the Share chrome stays.
        "OMNIGENT_LOCAL_SINGLE_USER": "",
        # A header-identified admin so the browser (X-Forwarded-Email) can
        # manage the local-owned session and thus see the Share button.
        "OMNIGENT_ADMINS": ADMIN_EMAIL,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
    }
    if extra_server_env:
        server_env.update(extra_server_env)

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
        # The runner registers headerless over loopback → owns sessions as the
        # reserved "local" user (unchanged from the shared fixture); the admin
        # browser still manages them via is_admin.
        "OMNIGENT_LOCAL_SINGLE_USER": "",
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
                f"multi-user server not healthy within {_HEALTH_TIMEOUT_S:.0f}s on "
                f"{base_url} (last_error={last_error}).\n{log_text[-3000:]}"
            )

        # Create a hello_world session (headerless → owned by "local") and bind
        # it to the runner. No turn runs — Share only needs a session to exist.
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

        yield MultiUserServer(
            base_url=base_url,
            public_url=public_loopback_url(base_url),
            session_id=session_id,
        )
    finally:
        _terminate(runner_proc)
        runner_log_handle.close()
        _terminate(proc)
        log_handle.close()
