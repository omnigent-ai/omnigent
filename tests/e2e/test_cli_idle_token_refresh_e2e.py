"""E2E regression guard: the CLI must refresh an idle-expired login token
instead of 401-ing the user out.

Bug (client commands fail to refresh the token after an idle period)
---------------------------------------------------------------------
``omnigent login <url>`` against an accounts / Databricks-fronted server stores
an access token *and* a login-issued refresh grant in
``~/.omnigent/auth_tokens.json``. The access token is short-lived (~1 h / the
session TTL); the refresh grant lives for weeks. When a user's CLI sits idle
past the access token's expiry and then runs another command, that command must
transparently renew the access token from the stored refresh grant -- exactly
what an unattended runner does via ``omnigent.runner._entry._factory`` (which
calls ``refresh_stored_token``).

On the buggy build the *client command surface* never refreshes: ``omnigent
usage`` / ``session export`` / native-harness reconnects all build their auth
headers through ``omnigent.chat._remote_headers`` /
``_DatabricksTokenAuth.auth_flow``, which read the stored token via
``load_token()`` but never call ``refresh_stored_token()``. So the next command
after idle goes out with the expired bearer, the server answers ``401``, and the
CLI dies telling the user to re-run ``omnigent login`` -- even though the stored
refresh grant is still perfectly valid.

Journey reproduced here (real user actions on the ``cli`` surface)
------------------------------------------------------------------
1. ``omnigent login http://127.0.0.1:<port>`` (accounts auth: types
   username + password) -> stores access token + refresh grant.
2. ``omnigent usage --server <url>`` -> succeeds (baseline).
3. The CLI idles past the access-token lifetime, with no turn. Simulated by
   backdating the stored ``expires_at`` -- the exact on-disk state after the
   token elapses; the refresh grant is left untouched.
4. ``omnigent usage --server <url>`` -> MUST succeed by silently refreshing.
   On the buggy build it exits non-zero with a ``401``.

The test asserts the *fixed* behaviour, so it fails specifically on this bug
(step 4 returns 401 today) and flips green once the client surface refreshes.
A positive control confirms the stored refresh grant really is redeemable
server-side (``POST /oauth/token`` -> 200), so a failure is unambiguously the
client's missing refresh, not a broken server or an unrelated environment fault.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests._helpers.live_server import find_free_port

# Repo root -- this file lives at tests/e2e/<name>.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# 32-byte cookie secret (64 hex chars). The refresh grant is signed with it, so
# it only needs to be stable for this one server subprocess.
_COOKIE_SECRET_HEX = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
_ADMIN_USER = "admin"
_ADMIN_PASSWORD = "idle-refresh-pw-123456"
_SERVER_HEALTH_TIMEOUT_S = 90.0


def _await_health(base_url: str, log_path: Path) -> None:
    """Poll ``/health`` until the server answers 200, or fail with the log tail."""
    deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            # Expected while the server is still booting; keep polling.
            pass
        time.sleep(0.5)
    tail = log_path.read_text()[-3000:] if log_path.exists() else "(no log)"
    raise RuntimeError(f"accounts server did not become healthy. Log:\n{tail}")


@pytest.fixture()
def accounts_server(tmp_path: Path) -> Iterator[str]:
    """Run a real ``omnigent server`` subprocess in accounts mode with the
    login-issued refresh grant mounted.

    Accounts mode (``OMNIGENT_AUTH_PROVIDER=accounts`` + a cookie secret) makes
    ``/auth/login`` password auth available and, because a device-grant store is
    always built for a server-mintable provider, mounts ``/oauth/token`` so the
    login flow can hand the CLI a refresh grant. No runner or LLM is needed --
    the journey is a bare authenticated read (``omnigent usage``).

    :param tmp_path: Per-test temp dir for the DB, artifacts, and server log.
    :yields: The running server's base URL.
    """
    port = find_free_port()
    db_path = tmp_path / "e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_path / "server.log"
    base_url = f"http://127.0.0.1:{port}"

    env = {**os.environ}
    env["OMNIGENT_AUTH_PROVIDER"] = "accounts"
    env["OMNIGENT_AUTH_ENABLED"] = "1"
    env["OMNIGENT_LOCAL_SINGLE_USER"] = ""
    env["OMNIGENT_ACCOUNTS_COOKIE_SECRET"] = _COOKIE_SECRET_HEX
    env["OMNIGENT_ACCOUNTS_BASE_URL"] = base_url
    env["OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME"] = _ADMIN_USER
    env["OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD"] = _ADMIN_PASSWORD
    env["OMNIGENT_ACCOUNTS_AUTO_OPEN"] = "0"
    env["OMNIGENT_ADMIN_CREDENTIALS_PATH"] = str(tmp_path / "admin-creds")
    # Force the accounts branch of the auth-source switch.
    env.pop("OMNIGENT_OIDC_ISSUER", None)
    # A local proxy can never reach 127.0.0.1; keep loopback traffic direct.
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["OMNIGENT_PROCESS_LOG_FILE"] = str(log_path)
    # Import omnigent + the bundled SDKs from this worktree, not a stale install.
    apply_server_env(env, _REPO_ROOT)

    proc = subprocess.Popen(
        [
            server_executable(),
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
        ],
        env=env,
        cwd=compat_server_cwd() or str(_REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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


def _client_env(client_home: Path) -> dict[str, str]:
    """Env for the ``omnigent`` CLI subprocess: an isolated state dir so the
    stored token file is this test's alone, no leaked static bearer, and the
    bundled SDKs importable."""
    env = {**os.environ}
    env["HOME"] = str(client_home)
    env["OMNIGENT_DATA_DIR"] = str(client_home / ".omnigent")
    env.pop("OMNIGENT_CONFIG_HOME", None)
    env.pop("OMNIGENT_REMOTE_AUTH_TOKEN", None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    apply_server_env(env, _REPO_ROOT)
    return env


def _run_cli(
    args: list[str], env: dict[str, str], stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent <args>`` as the user would and capture the result."""
    return subprocess.run(
        [server_executable(), "-m", "omnigent.cli", *args],
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_REPO_ROOT),
    )


def test_cli_refreshes_idle_expired_token(accounts_server: str, tmp_path: Path) -> None:
    """The next CLI command after an idle expiry must refresh, not 401.

    On the buggy build step 4 exits non-zero with a ``401`` because the
    client HTTP path never calls ``refresh_stored_token``.
    """
    base_url = accounts_server
    client_home = tmp_path / "clienthome"
    client_home.mkdir()
    env = _client_env(client_home)

    # -- Step 1: the user signs in (accounts auth: username + password). ----
    login = _run_cli(["login", base_url], env, stdin_text=f"{_ADMIN_USER}\n{_ADMIN_PASSWORD}\n")
    assert login.returncode == 0, f"login failed:\n{login.stdout}\n{login.stderr}"

    tokens_path = client_home / ".omnigent" / "auth_tokens.json"
    tokens = json.loads(tokens_path.read_text())
    entry = tokens[base_url]
    assert entry.get("refresh_token"), (
        "server issued no refresh grant on login -- cannot exercise the refresh path "
        "(is /oauth/token mounted and issue_refresh honored?)"
    )

    # -- Step 2: a command right after login works (baseline). --------------
    baseline = _run_cli(["usage", "--server", base_url], env)
    assert baseline.returncode == 0, (
        f"baseline `omnigent usage` failed before any idle:\n{baseline.stdout}\n{baseline.stderr}"
    )

    # -- Positive control: the stored refresh grant is redeemable server-side.
    # If this fails, the fault is the server/grant, not the client -- so a
    # failure of the main assertion below is unambiguously the missing client
    # refresh.
    refresh_probe = httpx.post(
        f"{base_url}/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": entry["refresh_token"]},
        timeout=10,
        trust_env=False,
    )
    assert refresh_probe.status_code == 200, (
        "stored refresh grant is not redeemable server-side "
        f"(HTTP {refresh_probe.status_code}); precondition for the client refresh is broken"
    )
    assert "access_token" in refresh_probe.json()

    # -- Step 3: the CLI idles past the access-token lifetime, no turn. ------
    # Backdate the stored expiry to exactly the on-disk state after the token
    # elapses; leave the (still valid) refresh grant untouched.
    expired_at = time.time() - 3600.0
    entry["expires_at"] = expired_at
    tokens_path.write_text(json.dumps(tokens))

    # -- Step 4: the next command MUST refresh transparently and succeed. ---
    after_idle = _run_cli(["usage", "--server", base_url], env)
    assert after_idle.returncode == 0, (
        "`omnigent usage` after an idle token expiry failed instead of "
        "refreshing from the stored (still-valid) login grant.\n"
        f"exit={after_idle.returncode}\n--- stdout ---\n{after_idle.stdout}\n"
        f"--- stderr ---\n{after_idle.stderr}"
    )

    # And it must have renewed the stored access token rather than leaving the
    # expired one in place -- proving the success came from a real refresh.
    renewed = json.loads(tokens_path.read_text())[base_url]
    assert renewed["expires_at"] > time.time(), (
        "command succeeded but the stored access token was never renewed "
        f"(expires_at still {renewed['expires_at']!r}); expected a fresh, future expiry"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
