"""Shared helper: spawn a dedicated *multi-user* header-auth Omnigent server.

The suite's shared ``live_server`` runs single-user
(``OMNIGENT_LOCAL_SINGLE_USER=1``, set in ``tests/conftest.py``), where the
Share affordances are intentionally hidden. Tests that need to exercise the
Share button / modal / kebab (or the sharing-off disable) must therefore run
against a server that is *not* single-user — a header-auth deploy with more
than one possible user, exactly like a Databricks Apps / SSO-proxy install.

This spins one up: the single-user marker is cleared, an admin identity is
declared via ``OMNIGENT_ADMINS`` so a header-identified browser can manage, and
an admin-owned hello_world session is created. Served through the
public-looking loopback alias (``_PUBLIC_LOOPBACK_HOST``) so
``isCurrentServerLocal()`` is false and the Share affordances aren't masked by
the local-server disable.

No runner is bound: the Share button / modal / settings nav all key off the
session existing plus the admin identity (``canShare`` in AppShell.tsx needs a
top-level session at manage level, not an online runner), and no agent turn is
dispatched. This also sidesteps the runner-ownership rule — a loopback runner
registers as the reserved ``local`` user, which an admin-owned session can't
bind to — while keeping the create authenticated (a multi-user server 401s
headerless writes, so a ``local``-owned session can't be created here anyway).
"""

from __future__ import annotations

import json as _json
import os
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

# Admin identity the browser presents via X-Forwarded-Email. Listed in the
# admin-list file (OMNIGENT_ADMIN_LIST_PATH) so it resolves as an admin —
# is_admin:true on /v1/me → the Settings Admin group renders, and manage on any
# session → the Share button shows.
ADMIN_EMAIL = "admin@ui.test"


@dataclass
class MultiUserServer:
    """A running multi-user server plus one admin-owned session (no runner).

    :param base_url: Loopback base URL (``http://127.0.0.1:<port>``) for REST.
    :param public_url: The same server via the public-looking loopback alias,
        so the browser's ``isCurrentServerLocal()`` is false.
    :param session_id: A hello_world session owned by the admin identity.
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
    """Spawn a multi-user server + one admin-owned session; yield a handle.

    Mirrors the shared ``live_server`` spawn but with the single-user marker
    cleared and an admin declared, and NO runner (the Share/settings chrome
    under test needs only a session to exist, not an online runner).
    ``extra_server_env`` overrides/augments the server env (e.g.
    ``OMNIGENT_SHARING_MODE=off``).

    :param mock_llm_server_url: Session-scoped mock LLM base (no real creds).
    :param server_tmp: A per-test temp dir (``tmp_path_factory.mktemp(...)``).
    :param extra_server_env: Extra env vars for the server process.
    :yields: A :class:`MultiUserServer` handle.
    """
    port = _find_free_port()
    log_path = server_tmp / "server.log"
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML)
    # Declare the admin roster via the admin-list file (one identity per line;
    # see omnigent/server/admin_list.py). There is no admin *env var* — the
    # roster is the config ``admins:`` list or this file — so point
    # OMNIGENT_ADMIN_LIST_PATH at it. This makes ADMIN_EMAIL resolve as admin
    # so /v1/me reports is_admin:true and the Settings Admin group renders.
    admins_path = server_tmp / "admins"
    admins_path.write_text(f"{ADMIN_EMAIL}\n")

    base_url = f"http://127.0.0.1:{port}"
    pythonpath = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
    # Requests authenticated as the admin identity. A multi-user header-auth
    # server 401s headerless requests, so every REST call here carries it.
    admin_headers = {"X-Forwarded-Email": ADMIN_EMAIL}

    server_env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        # The whole point: NOT single-user. Clear the marker the suite sets so
        # /v1/info reports single_user:false and the Share chrome stays.
        "OMNIGENT_LOCAL_SINGLE_USER": "",
        # A header-identified admin so the browser (X-Forwarded-Email) can
        # manage its session (Share button) and see the admin settings group.
        "OMNIGENT_ADMIN_LIST_PATH": str(admins_path),
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

    try:
        # /health is unauthed; wait for it (no runner to poll).
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    ready = True
                    break
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

        # Create an admin-owned hello_world session (authenticated, so it's
        # owned by ADMIN_EMAIL — headerless would 401 here). No runner bind and
        # no turn: the Share modal / button / settings nav only need a
        # top-level session to exist at manage level, which the owner has.
        bundle = _build_hello_world_bundle()
        create = httpx.post(
            f"{base_url}/v1/sessions",
            data={"metadata": _json.dumps({})},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
            headers=admin_headers,
            timeout=30.0,
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]

        yield MultiUserServer(
            base_url=base_url,
            public_url=public_loopback_url(base_url),
            session_id=session_id,
        )
    finally:
        _terminate(proc)
        log_handle.close()
