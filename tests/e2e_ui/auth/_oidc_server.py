"""Shared helper: spawn a dedicated *OIDC-mode* Omnigent server pointed at a
fake in-process IdP, so the SPA's login-redirect journey can be filmed.

The shared ``live_server`` runs single-user with auth off, and the accounts
helper (``_accounts_server.py``) drives a password form — neither exercises the
OIDC redirect an SSO deployment uses. This spins up ``omnigent server`` in
stock OIDC mode (``OMNIGENT_AUTH_PROVIDER=oidc``) with the issuer pointed at a
:func:`tests.e2e_ui.auth._fake_idp.fake_idp` instance, so navigating the SPA
bounces through ``/auth/login`` → the fake IdP sign-in page → ``/auth/callback``
→ an authenticated session — all headless, all filmable.

Served through the public-looking loopback alias (``_PUBLIC_LOOPBACK_HOST``,
mapped to 127.0.0.1 by the browser launch args) so the redirect URI / cookie
origin and the browser's origin agree.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from tests.e2e_ui.auth._accounts_server import _terminate, public_loopback_url
from tests.e2e_ui.auth._fake_idp import FakeIdP, fake_idp
from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _find_free_port,
)


@dataclass
class OIDCServer:
    """A running OIDC-mode server wired to a fake IdP.

    :param base_url: Loopback base URL (``http://127.0.0.1:<port>``) for REST.
    :param public_url: The same server via the public-looking loopback alias,
        so the browser and the server share one origin (cookies stick).
    :param idp: The fake IdP the server authenticates against.
    """

    base_url: str
    public_url: str
    idp: FakeIdP


def spawn_oidc_server(mock_llm_server_url: str, server_tmp) -> Iterator[OIDCServer]:
    """Spawn an OIDC-mode server wired to a fake IdP; yield a handle.

    The fake IdP must be up *before* the server boots, because stock OIDC mode
    fetches the discovery document at construction time
    (``OIDCConfig.from_env``). The redirect URI is on the public-loopback alias
    so the session cookie is issued for the browser-visible origin.

    :param mock_llm_server_url: Session-scoped mock LLM base (no real creds).
    :param server_tmp: A per-test temp dir (``tmp_path_factory.mktemp(...)``).
    :yields: An :class:`OIDCServer` handle.
    """
    with fake_idp() as idp:
        port = _find_free_port()
        log_path = server_tmp / "server.log"
        db_path = server_tmp / "test.db"
        artifact_dir = server_tmp / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        agent_yaml_path = server_tmp / "hello_world.yaml"
        agent_yaml_path.write_text(_TEST_AGENT_YAML)

        base_url = f"http://127.0.0.1:{port}"
        public_url = public_loopback_url(base_url)
        # The callback (and thus the session cookie) must be issued for the
        # browser-visible origin, so derive the redirect URI from public_url.
        redirect_uri = f"{public_url}/auth/callback"
        pythonpath = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"

        server_env = {
            **os.environ,
            "PYTHONPATH": pythonpath,
            "OMNIGENT_AUTH_PROVIDER": "oidc",
            "OMNIGENT_AUTH_ENABLED": "1",
            "OMNIGENT_LOCAL_SINGLE_USER": "",
            "OMNIGENT_OIDC_ISSUER": idp.issuer,
            "OMNIGENT_OIDC_CLIENT_ID": idp.client_id,
            "OMNIGENT_OIDC_CLIENT_SECRET": idp.client_secret,
            "OMNIGENT_OIDC_REDIRECT_URI": redirect_uri,
            "OMNIGENT_OIDC_COOKIE_SECRET": secrets.token_hex(32),
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
                    f"oidc server not healthy within {_HEALTH_TIMEOUT_S:.0f}s on "
                    f"{base_url} (last_error={last_error}).\n{log_text[-3000:]}"
                )

            yield OIDCServer(base_url=base_url, public_url=public_url, idp=idp)
        finally:
            _terminate(proc)
            log_handle.close()
