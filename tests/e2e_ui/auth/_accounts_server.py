"""Shared helper: spawn a dedicated *accounts-mode* Omnigent server with the
device-authorization grant enabled.

The suite's shared ``live_server`` runs single-user
(``OMNIGENT_LOCAL_SINGLE_USER=1``, set in ``tests/conftest.py``) with auth
disabled, so it has no password login form and no ``/oauth/*`` device routes.
The forced-reauthentication flow under test needs all three: accounts mode (the
``/login`` password form + a session cookie whose ``iat`` the consent page
checks), the device grant mounted (``OMNIGENT_DEVICE_GRANT_ENABLED=1``), and a
seeded admin the browser can sign in as.

This spins one up with a pre-seeded admin (``OMNIGENT_ACCOUNTS_INIT_ADMIN_*``)
and exposes :meth:`AccountsServer.start_device_flow` so a test can mint a
``pending`` grant exactly as the Slack client would (``POST
/oauth/device/authorize``) and then drive the browser consent page.

Served through the public-looking loopback alias (``_PUBLIC_LOOPBACK_HOST``,
mapped to 127.0.0.1 by the browser launch args) so the accounts base URL and
the browser's origin agree.
"""

from __future__ import annotations

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
    _find_free_port,
)

# Seeded admin the browser signs in as. Bootstrapped on first boot from the
# INIT_ADMIN env vars below (no store poking needed).
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "e2e-admin-pw-123456"


@dataclass
class DeviceFlow:
    """A pending device grant minted via ``POST /oauth/device/authorize``.

    :param user_code: The short code shown to / typed by the user.
    :param verification_uri: The bare consent URL (no code prefilled).
    :param verification_uri_complete: The one-click consent URL (code
        prefilled) — what the Slack client links.
    """

    user_code: str
    verification_uri: str
    verification_uri_complete: str


@dataclass
class AccountsServer:
    """A running accounts-mode server with the device grant enabled.

    :param base_url: Loopback base URL (``http://127.0.0.1:<port>``) for REST.
    :param public_url: The same server via the public-looking loopback alias,
        so the browser and the accounts base URL share one origin.
    """

    base_url: str
    public_url: str

    def start_device_flow(self) -> DeviceFlow:
        """Mint a pending device grant, as the Slack client would.

        ``POST /oauth/device/authorize`` is public (no client secret configured
        here), so this needs no auth — it returns the codes + verification URLs.
        """
        resp = httpx.post(
            f"{self.base_url}/oauth/device/authorize",
            json={"client_id": "e2e-test"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        # Rewrite the verification URIs onto the public loopback host so the
        # browser (which resolves that alias) and the server agree on origin.
        return DeviceFlow(
            user_code=str(data["user_code"]),
            verification_uri=_swap_host(str(data["verification_uri"]), self.public_url),
            verification_uri_complete=_swap_host(
                str(data["verification_uri_complete"]), self.public_url
            ),
        )


def _swap_host(url: str, public_url: str) -> str:
    """Return ``url`` with its scheme+host replaced by ``public_url``'s."""
    pub = urlsplit(public_url)
    orig = urlsplit(url)
    return urlunsplit((pub.scheme, pub.netloc, orig.path, orig.query, orig.fragment))


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


def spawn_accounts_server(mock_llm_server_url: str, server_tmp) -> Iterator[AccountsServer]:
    """Spawn an accounts-mode server with the device grant; yield a handle.

    Mirrors the shared ``live_server`` spawn but in accounts mode with a
    pre-seeded admin and the ``/oauth/*`` device routes mounted. No runner is
    bound — the flow under test (login → consent → approve) needs no agent turn.

    :param mock_llm_server_url: Session-scoped mock LLM base (no real creds).
    :param server_tmp: A per-test temp dir (``tmp_path_factory.mktemp(...)``).
    :yields: An :class:`AccountsServer` handle.
    """
    import secrets

    port = _find_free_port()
    log_path = server_tmp / "server.log"
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML)

    base_url = f"http://127.0.0.1:{port}"
    public_url = public_loopback_url(base_url)
    pythonpath = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"

    server_env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        # Accounts mode + a pre-seeded admin so the browser can sign in with a
        # known password. The base URL must be the browser-visible origin (the
        # public loopback alias) so the session cookie is issued for it.
        "OMNIGENT_AUTH_PROVIDER": "accounts",
        "OMNIGENT_AUTH_ENABLED": "1",
        "OMNIGENT_LOCAL_SINGLE_USER": "",
        "OMNIGENT_ACCOUNTS_COOKIE_SECRET": secrets.token_hex(32),
        "OMNIGENT_ACCOUNTS_BASE_URL": public_url,
        "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": ADMIN_USERNAME,
        "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "OMNIGENT_ACCOUNTS_AUTO_OPEN": "0",
        "OMNIGENT_ADMIN_CREDENTIALS_PATH": str(server_tmp / "admin-creds"),
        # Mount the /oauth/* device routes (opt-in, default-off).
        "OMNIGENT_DEVICE_GRANT_ENABLED": "1",
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
                    ready = True
                    break
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        if not ready:
            log_handle.flush()
            log_text = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"accounts server not healthy within {_HEALTH_TIMEOUT_S:.0f}s on "
                f"{base_url} (last_error={last_error}).\n{log_text[-3000:]}"
            )

        yield AccountsServer(base_url=base_url, public_url=public_url)
    finally:
        _terminate(proc)
        log_handle.close()
