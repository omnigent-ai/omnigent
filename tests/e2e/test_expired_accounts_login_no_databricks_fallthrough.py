"""Exercise expired accounts auth against a live server over a real socket."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import jwt
import pytest

from omnigent.runner._entry import (
    _InitialAuthTokenFactory,
    _make_auth_token_factory,
    _RunnerDatabricksAuth,
)
from omnigent.runner.identity import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    RUNNER_DELEGATED_AUTH_ENV_VAR,
    RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
)
from omnigent.server.oidc import mint_session_token
from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests._helpers.live_server import find_free_port
from tests.server.helpers import build_agent_bundle

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Shared with the subprocess so locally minted accounts tokens validate.
_COOKIE_SECRET_HEX = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
_OWNER = "alice@example.com"
_OWNER_PASSWORD = "alice-password-12345"
_SERVER_HEALTH_TIMEOUT_S = 40.0


def _await_health(base_url: str, log_path: Path) -> None:
    """Wait for server health and include its log tail on timeout."""
    deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            # Connection failures are expected during startup.
            pass
        time.sleep(0.5)
    tail = log_path.read_text()[-3000:] if log_path.exists() else "(no log)"
    raise RuntimeError(f"accounts server did not become healthy. Log:\n{tail}")


@pytest.fixture()
def accounts_server(tmp_path: Path) -> Iterator[str]:
    """Run the production server lifecycle with accounts auth enabled."""
    port = find_free_port()
    db_path = tmp_path / "e2e.db"
    db_uri = f"sqlite:///{db_path}"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_path / "server.log"
    base_url = f"http://localhost:{port}"

    env = {**os.environ}
    env["OMNIGENT_AUTH_PROVIDER"] = "accounts"
    env["OMNIGENT_ACCOUNTS_COOKIE_SECRET"] = _COOKIE_SECRET_HEX
    env["OMNIGENT_ACCOUNTS_BASE_URL"] = base_url
    env["OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME"] = _OWNER
    env["OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD"] = _OWNER_PASSWORD
    env["OMNIGENT_ACCOUNTS_AUTO_OPEN"] = "0"
    env["OMNIGENT_ADMIN_CREDENTIALS_PATH"] = str(tmp_path / "admin-credentials")
    env["OMNIGENT_CONFIG_HOME"] = str(tmp_path / "config")
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "data")
    env["HOME"] = str(tmp_path)
    # Prevent ambient OIDC configuration from overriding accounts auth.
    env.pop("OMNIGENT_OIDC_ISSUER", None)
    apply_server_env(env, _REPO_ROOT)

    log_handle = open(log_path, "w")  # noqa: SIM115 — handle lives for the subprocess
    proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            db_uri,
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        _await_health(base_url, log_path)
        yield base_url
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


def _login_owner(base_url: str) -> str:
    response = httpx.post(
        f"{base_url}/auth/login",
        json={"username": _OWNER, "password": _OWNER_PASSWORD},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _seed_owned_session(base_url: str) -> str:
    """Create an owned session whose agent-contents route requires auth."""
    owner_cookie = _login_owner(base_url)
    bundle = build_agent_bundle(name="e2e-accounts-jwt-expiry-agent")
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        create = http.post(
            "/v1/sessions",
            headers={
                "Authorization": f"Bearer {owner_cookie}",
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
            },
            data={"metadata": "{}"},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
    assert create.status_code in (200, 201), (create.status_code, create.text)
    return create.json()["session_id"]


async def _get_agent_contents(
    base_url: str,
    path: str,
    auth: _RunnerDatabricksAuth,
) -> httpx.Response:
    """Issue one real-socket GET through the runner's callback auth."""
    async with httpx.AsyncClient(
        base_url=base_url,
        auth=auth,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        follow_redirects=False,
        timeout=30.0,
    ) as client:
        return await client.get(path)


def test_expired_accounts_login_does_not_fall_through_to_databricks(
    accounts_server: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reject an expired host bearer without selecting Databricks auth."""
    base_url = accounts_server
    session_id = _seed_owned_session(base_url)
    contents_path = f"/v1/sessions/{session_id}/agent/contents"

    from omnigent.inner.databricks_executor import DatabricksAuthError

    def _no_databricks_creds(*args: object, **kwargs: object) -> tuple[object, str]:
        """Stand in for _resolve_databricks_auth on a host with no Databricks config."""
        raise DatabricksAuthError("this host has no Databricks config")

    # Model a local host with no delegated mint or Databricks credential.
    monkeypatch.setenv("RUNNER_SERVER_URL", base_url)
    monkeypatch.delenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(RUNNER_DELEGATED_AUTH_ENV_VAR, raising=False)
    monkeypatch.setenv(RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR, "")
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url, **_kw: None)
    monkeypatch.setattr("omnigent.cli_auth.refresh_stored_token", lambda _url, **_kw: None)
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        _no_databricks_creds,
    )
    import omnigent.runner._entry as entry

    monkeypatch.setattr(entry, "_runner_auth_factory", None, raising=False)

    valid_owner_bearer = _login_owner(base_url)
    claims = jwt.decode(valid_owner_bearer, options={"verify_signature": False})
    expired_host_bearer = mint_session_token(
        _OWNER,
        bytes.fromhex(_COOKIE_SECRET_HEX),
        -3600,
        "accounts",
        account_generation=claims["account_generation"],
    )
    factory = _InitialAuthTokenFactory(expired_host_bearer, base_url)

    with caplog.at_level(logging.INFO):
        # The first callback invalidates the rejected bootstrap bearer.
        first = asyncio.run(
            _get_agent_contents(base_url, contents_path, _RunnerDatabricksAuth(factory))
        )
        assert first.status_code in (401, 403), (first.status_code, first.text)

        # The second callback has no local credential to present.
        raised: httpx.RequestError | None = None
        second: httpx.Response | None = None
        try:
            second = asyncio.run(
                _get_agent_contents(base_url, contents_path, _RunnerDatabricksAuth(factory))
            )
        except httpx.RequestError as exc:
            raised = exc

    if raised is not None:
        message = str(raised)
        assert "databricks" not in message.lower(), (
            f"expired accounts JWT fell through to the Databricks auth path: {message!r}"
        )
        assert "omnigent login" in message.lower(), (
            f"callback stopped without naming the omnigent login remedy: {message!r}"
        )
    else:
        assert second is not None
        assert second.status_code in (401, 403), (second.status_code, second.text)

    remedy_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "databricks auth login" not in remedy_logs.lower(), (
        f"remedy log points an accounts-mode host at Databricks:\n{remedy_logs}"
    )


def test_expired_stored_login_blocks_sdk_after_real_callback(
    accounts_server: str,
    tmp_path: Path,
) -> None:
    session_id = _seed_owned_session(accounts_server)
    payload = {
        "server_url": accounts_server,
        "contents_path": f"/v1/sessions/{session_id}/agent/contents",
        "owner_bearer": _login_owner(accounts_server),
    }
    env = {**os.environ}
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "runner-data")
    env["RUNNER_SERVER_URL"] = accounts_server
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    for key in list(env):
        if key.startswith("DATABRICKS_") or key in {
            RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
            RUNNER_DELEGATED_AUTH_ENV_VAR,
            RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
            "OPENAI_API_KEY",
        }:
            env.pop(key)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; "
            "from tests.e2e.test_expired_accounts_login_no_databricks_fallthrough "
            "import _stored_login_probe; _stored_login_probe(json.load(sys.stdin))",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=45,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr[-3000:]


def _stored_login_probe(payload: dict[str, str]) -> None:
    import omnigent.inner.databricks_executor as databricks_executor
    from omnigent.cli_auth import store_token
    from omnigent.inner.databricks_executor import _DatabricksBearerAuth

    server_url = payload["server_url"]
    contents_path = payload["contents_path"]
    owner_bearer = payload["owner_bearer"]
    sdk_calls = 0

    class _AmbientConfig:
        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "Bearer ambient-databricks-token"}

    def _resolve_sdk(*args: object, **kwargs: object) -> tuple[_DatabricksBearerAuth, str]:
        nonlocal sdk_calls
        sdk_calls += 1
        return _DatabricksBearerAuth(_AmbientConfig(), profile_name=None), server_url

    databricks_executor._resolve_databricks_auth = _resolve_sdk

    store_token(
        server_url,
        token=owner_bearer,
        user_id=_OWNER,
        expires_at=time.time() + 3600,
    )
    factory = _make_auth_token_factory(server_url)
    assert factory is not None
    auth = _RunnerDatabricksAuth(factory, server_url=server_url)
    response = asyncio.run(_get_agent_contents(server_url, contents_path, auth))
    assert response.status_code == 200, (response.status_code, response.text)

    store_token(
        server_url,
        token=owner_bearer,
        user_id=_OWNER,
        expires_at=time.time() - 1,
    )
    with pytest.raises(httpx.RequestError) as excinfo:
        asyncio.run(_get_agent_contents(server_url, contents_path, auth))

    message = str(excinfo.value)
    assert "Omnigent login" in message and server_url in message
    assert "Databricks" not in message
    assert sdk_calls == 0
