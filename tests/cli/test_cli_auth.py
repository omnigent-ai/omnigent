"""Unit tests for CLI OIDC token storage (omnigent/cli_auth.py).

Tests the store/load/clear lifecycle for session tokens persisted
by ``omnigent login``.
"""

from __future__ import annotations

import contextlib
import time

import pytest


@pytest.fixture()
def token_dir(tmp_path, monkeypatch):
    """Redirect the token file to a temp directory.

    Patches ``state_dir`` to return ``tmp_path`` so tests don't
    touch ``~/.omnigent``. (The machine's own host identity and the runner
    slice-key env var are isolated globally by the ``_no_ambient_host``
    autouse fixture in ``conftest.py``.)

    :param tmp_path: Pytest temp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The temp directory path.
    """
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    return tmp_path


def test_store_and_load_token(token_dir) -> None:
    """A stored token can be loaded back by server URL.

    This is the happy path: ``omnigent login`` stores a token,
    ``omnigent run --server`` loads it.
    """
    from omnigent.cli_auth import load_token, store_token

    store_token(
        server_url="http://localhost:8000",
        token="jwt-abc",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    result = load_token("http://localhost:8000")
    # Token must be the exact value stored.
    assert result == "jwt-abc", f"Expected 'jwt-abc', got {result!r}."


def test_load_returns_none_when_no_file(token_dir) -> None:
    """load_token returns None when no token file exists.

    The first time a user runs ``omnigent run --server`` without
    having run ``omnigent login``, there should be no crash.
    """
    from omnigent.cli_auth import load_token

    assert load_token("http://localhost:8000") is None


def test_load_returns_none_for_unknown_server(token_dir) -> None:
    """load_token returns None for a server with no stored token.

    A token stored for one server must not leak to another.
    """
    from omnigent.cli_auth import load_token, store_token

    store_token(
        server_url="http://localhost:8000",
        token="jwt-abc",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    assert load_token("http://other-server:9000") is None


def test_load_returns_none_for_expired_token(token_dir) -> None:
    """load_token returns None when the stored token has expired.

    Expired tokens must not be used — the user needs to re-run
    ``omnigent login``.
    """
    from omnigent.cli_auth import load_token, store_token

    store_token(
        server_url="http://localhost:8000",
        token="jwt-expired",
        user_id="alice@example.com",
        expires_at=time.time() - 1,  # Already expired.
    )

    assert load_token("http://localhost:8000") is None


def test_clear_token(token_dir) -> None:
    """clear_token removes a stored token for a server.

    After clearing, load_token must return None.
    """
    from omnigent.cli_auth import clear_token, load_token, store_token

    store_token(
        server_url="http://localhost:8000",
        token="jwt-abc",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )
    clear_token("http://localhost:8000")

    assert load_token("http://localhost:8000") is None


def test_trailing_slash_normalization(token_dir) -> None:
    """Server URLs are normalized (trailing slash stripped).

    ``http://localhost:8000/`` and ``http://localhost:8000`` must
    resolve to the same stored token.
    """
    from omnigent.cli_auth import load_token, store_token

    store_token(
        server_url="http://localhost:8000/",
        token="jwt-slash",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    # Load without trailing slash.
    assert load_token("http://localhost:8000") == "jwt-slash"


def test_file_permissions(token_dir) -> None:
    """Token file is created with 0o600 (user-only read/write).

    Tokens are sensitive — they must not be world-readable.
    """

    from omnigent.cli_auth import store_token

    store_token(
        server_url="http://localhost:8000",
        token="jwt-abc",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    path = token_dir / "auth_tokens.json"
    mode = path.stat().st_mode & 0o777
    # 0o600 = user read + write only.
    assert mode == 0o600, (
        f"Token file should have 0o600 permissions, got {oct(mode)}. "
        f"This means the token could be readable by other users."
    )


def test_store_overwrites_existing(token_dir) -> None:
    """Storing a token for the same server overwrites the old one.

    Re-running ``omnigent login`` should update the token, not
    append.
    """
    from omnigent.cli_auth import load_token, store_token

    store_token(
        server_url="http://localhost:8000",
        token="old-token",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )
    store_token(
        server_url="http://localhost:8000",
        token="new-token",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    assert load_token("http://localhost:8000") == "new-token"


def test_multiple_servers(token_dir) -> None:
    """Tokens for different servers are stored independently.

    A user may have accounts on multiple servers.
    """
    from omnigent.cli_auth import load_token, store_token

    store_token(
        server_url="http://localhost:8000",
        token="token-a",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )
    store_token(
        server_url="https://prod.example.com",
        token="token-b",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    assert load_token("http://localhost:8000") == "token-a"
    assert load_token("https://prod.example.com") == "token-b"


# ── Databricks Apps pointer records ────────────────────────────────


def test_store_and_load_databricks_record(token_dir) -> None:
    """A stored Databricks pointer record resolves back to its workspace.

    ``omnigent login <apps-url>`` stores the record; the server-auth
    chain looks up the workspace host to mint fresh tokens.
    """
    from omnigent.cli_auth import load_databricks_workspace_host, store_databricks_auth

    store_databricks_auth(
        server_url="https://myapp-123.aws.databricksapps.com",
        workspace_host="https://example.databricks.com",
        user_id="alice@example.com",
    )

    host = load_databricks_workspace_host("https://myapp-123.aws.databricksapps.com")
    assert host == "https://example.databricks.com", (
        f"Expected the stored workspace host back, got {host!r}. A miss means "
        "the auth chain would silently fall through to ambient credentials."
    )


def test_databricks_request_headers_org_only(token_dir) -> None:
    """A recorded ?o= selector surfaces as the workspace-routing header.

    When the bare host is the account, the request routes by this header
    (equivalently to ``?o=``). A record with no org id (single-workspace
    host) yields no header, so those callers are unaffected.
    """
    from omnigent.cli_auth import databricks_request_headers, store_databricks_auth

    store_databricks_auth(
        server_url="https://acme.databricks.com/api/2.0/omnigent",
        workspace_host="https://acme.databricks.com",
        org_id="2850744067564480",
    )
    assert databricks_request_headers("https://acme.databricks.com/api/2.0/omnigent") == {
        "X-Databricks-Org-Id": "2850744067564480"
    }

    store_databricks_auth(
        server_url="https://single.databricks.com/api/2.0/omnigent",
        workspace_host="https://single.databricks.com",
    )
    assert databricks_request_headers("https://single.databricks.com/api/2.0/omnigent") == {}


def test_databricks_request_headers_pairs_bearer_and_org(token_dir) -> None:
    """The paired minter always emits the bearer and the ?o= header together.

    The static-dict seams (WS handshakes, hook-config replay) call this so a
    workspace request can never carry ``Authorization`` without the routing
    header. A missing token or selector is omitted, so single-workspace and
    local-unauthenticated callers are unaffected.
    """
    from omnigent.cli_auth import databricks_request_headers, store_databricks_auth

    store_databricks_auth(
        server_url="https://acme.databricks.com/api/2.0/omnigent",
        workspace_host="https://acme.databricks.com",
        org_id="2850744067564480",
    )
    recorded = "https://acme.databricks.com/api/2.0/omnigent"
    # Bearer + org travel together.
    assert databricks_request_headers(recorded, bearer_token="tok") == {
        "Authorization": "Bearer tok",
        "X-Databricks-Org-Id": "2850744067564480",
    }
    # Recorded selector but no token (local/unauth): org still rides, no bearer.
    assert databricks_request_headers(recorded) == {"X-Databricks-Org-Id": "2850744067564480"}
    # No record (unknown server): bearer only, no routing header.
    assert databricks_request_headers("https://other.example.com", bearer_token="tok") == {
        "Authorization": "Bearer tok"
    }


def test_databricks_request_headers_slice_key(token_dir) -> None:
    """The slice key is emitted only on a host-sharded deployment.

    Callers pass ``host_id=<host_id>`` unconditionally; the builder
    itself gates on the server being a host-sharded mount, so a new caller
    never has to reason about the deployment. On an unsharded server the key
    is dropped. When emitted, it travels alongside the bearer and ?o= header.
    """
    from omnigent.cli_auth import databricks_request_headers, store_databricks_auth

    # Unsharded server: the slice key is DROPPED even though it was passed.
    assert databricks_request_headers("https://other.example.com", host_id="host_abc123") == {}
    assert databricks_request_headers("http://127.0.0.1:6767", host_id="host_abc123") == {}

    # Workspace API mount: the key rides, alongside the bearer and ?o= selector.
    store_databricks_auth(
        server_url="https://acme.databricks.com/api/2.0/omnigent",
        workspace_host="https://acme.databricks.com",
        org_id="2850744067564480",
    )
    recorded = "https://acme.databricks.com/api/2.0/omnigent"
    assert databricks_request_headers(recorded, bearer_token="tok", host_id="host_abc123") == {
        "Authorization": "Bearer tok",
        "X-Databricks-Org-Id": "2850744067564480",
        "X-Databricks-Omnigent-Slice-Key": "host_abc123",
    }

    # Omitted (default) → no slice-key header even on the workspace mount.
    assert "X-Databricks-Omnigent-Slice-Key" not in databricks_request_headers(recorded)


def test_databricks_request_headers_runner_env_default(
    token_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a runner, an unspecified host_id defaults to the runner's own.

    A runner process exports its host_id at launch (``OMNIGENT_RUNNER_SLICE_KEY``);
    the builder picks it up when a caller names no host, so the runner's server
    traffic (transcript posts, uploads, policy checks) keys by host_id and
    spreads across replicas instead of piling onto the single workspace-key pod.
    An explicit host_id still wins, the env is honoured only on the workspace
    mount, and CLI / daemon callers (no such env) are unaffected.
    """
    from omnigent.cli_auth import databricks_request_headers, store_databricks_auth
    from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR

    store_databricks_auth(
        server_url="https://acme.databricks.com/api/2.0/omnigent",
        workspace_host="https://acme.databricks.com",
    )
    mount = "https://acme.databricks.com/api/2.0/omnigent"

    # No env, no explicit host_id → no key (CLI / daemon unaffected).
    monkeypatch.delenv(RUNNER_SLICE_KEY_ENV_VAR, raising=False)
    assert "X-Databricks-Omnigent-Slice-Key" not in databricks_request_headers(mount)

    # Runner env set → the key defaults to it.
    monkeypatch.setenv(RUNNER_SLICE_KEY_ENV_VAR, "host_runner")
    assert databricks_request_headers(mount)["X-Databricks-Omnigent-Slice-Key"] == "host_runner"

    # An explicit host_id still overrides the env.
    assert (
        databricks_request_headers(mount, host_id="host_explicit")[
            "X-Databricks-Omnigent-Slice-Key"
        ]
        == "host_explicit"
    )

    # Gated: even with the env set, an unsharded server emits nothing.
    assert databricks_request_headers("http://127.0.0.1:6767") == {}


def test_databricks_request_headers_slice_key_kill_switch(
    token_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OMNIGENT_HOST_SLICE_KEY_ENABLED=0`` disables slice-key emission.

    A per-process kill switch: slice-key emission runs in sidecar-less
    processes that can't evaluate a server-side flag, so this env var lets a bad
    rollout fall back to the server's default (workspace-id) routing without a
    redeploy. Emission is ON by default; only the exact value "0" turns it off.
    """
    from omnigent.cli_auth import databricks_request_headers, store_databricks_auth

    store_databricks_auth(
        server_url="https://acme.databricks.com/api/2.0/omnigent",
        workspace_host="https://acme.databricks.com",
    )
    mount = "https://acme.databricks.com/api/2.0/omnigent"

    # Default (env unset): the key is emitted on the workspace mount.
    monkeypatch.delenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", raising=False)
    assert (
        databricks_request_headers(mount, host_id="host_1")["X-Databricks-Omnigent-Slice-Key"]
        == "host_1"
    )

    # Kill switch flipped to "0": no key, same mount and host_id.
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "0")
    assert "X-Databricks-Omnigent-Slice-Key" not in databricks_request_headers(
        mount, host_id="host_1"
    )

    # Only the exact "0" disables it — any other value leaves emission on.
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    assert (
        databricks_request_headers(mount, host_id="host_1")["X-Databricks-Omnigent-Slice-Key"]
        == "host_1"
    )


def test_databricks_request_headers_folds_extra_headers(token_dir, monkeypatch) -> None:
    """OMNIGENT_DATABRICKS_EXTRA_HEADERS rides every request built via the helper.

    Databricks deployments set it to opaque request-routing selector headers so
    a request pins to a specific server instance. Because it is folded into this
    one helper, any caller that routes through it gets the selectors for free; a
    caller that hand-rolls a bare bearer misses them. Malformed / unset input is
    a no-op (prod-safe).
    """
    from omnigent.cli_auth import databricks_request_headers, store_databricks_auth

    store_databricks_auth(
        server_url="https://acme.databricks.com/api/2.0/omnigent",
        workspace_host="https://acme.databricks.com",
        org_id="2850744067564480",
    )
    recorded = "https://acme.databricks.com/api/2.0/omnigent"
    monkeypatch.setenv(
        "OMNIGENT_DATABRICKS_EXTRA_HEADERS",
        '{"x-databricks-route-hint": "instance-abc"}',
    )
    # Extra header travels alongside the bearer + ?o= routing header.
    assert databricks_request_headers(recorded, bearer_token="tok") == {
        "Authorization": "Bearer tok",
        "X-Databricks-Org-Id": "2850744067564480",
        "x-databricks-route-hint": "instance-abc",
    }
    # It rides even for an unknown server with no token (routing-only request).
    assert databricks_request_headers("https://other.example.com") == {
        "x-databricks-route-hint": "instance-abc",
    }
    # Malformed JSON is ignored (no crash) so a bad value can't break requests.
    monkeypatch.setenv("OMNIGENT_DATABRICKS_EXTRA_HEADERS", "not-json")
    assert databricks_request_headers("https://other.example.com", bearer_token="tok") == {
        "Authorization": "Bearer tok",
    }
    # Unset (prod default) is a no-op.
    monkeypatch.delenv("OMNIGENT_DATABRICKS_EXTRA_HEADERS", raising=False)
    assert databricks_request_headers("https://other.example.com", bearer_token="tok") == {
        "Authorization": "Bearer tok",
    }


def test_load_token_returns_none_for_databricks_record(token_dir) -> None:
    """A Databricks pointer record carries NO bearer — load_token must miss.

    Databricks OAuth tokens expire after ~1h, so the record deliberately
    stores only the workspace host. If load_token returned anything here,
    the JWT path would send a garbage Authorization header.
    """
    from omnigent.cli_auth import load_token, store_databricks_auth

    store_databricks_auth(
        server_url="https://myapp-123.aws.databricksapps.com",
        workspace_host="https://example.databricks.com",
    )

    assert load_token("https://myapp-123.aws.databricksapps.com") is None


def test_load_databricks_host_returns_none_for_jwt_record(token_dir) -> None:
    """A session-JWT record is not a Databricks pointer record.

    The Databricks resolution path must not fire for servers the user
    logged into via accounts/OIDC — those send the stored JWT instead.
    """
    import time

    from omnigent.cli_auth import load_databricks_workspace_host, store_token

    store_token(
        server_url="http://localhost:8000",
        token="jwt-abc",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )

    assert load_databricks_workspace_host("http://localhost:8000") is None


def test_databricks_record_normalizes_workspace_trailing_slash(token_dir) -> None:
    """The stored workspace host is normalized (trailing slash stripped).

    ``Config(host=...)`` treats ``https://ws`` and ``https://ws/`` as
    distinct cache keys in some SDK paths — store one canonical form.
    """
    from omnigent.cli_auth import load_databricks_workspace_host, store_databricks_auth

    store_databricks_auth(
        server_url="https://myapp-123.aws.databricksapps.com/",
        workspace_host="https://example.databricks.com/",
    )

    # Lookup without the trailing slash hits the same record, and the
    # stored host comes back canonical.
    host = load_databricks_workspace_host("https://myapp-123.aws.databricksapps.com")
    assert host == "https://example.databricks.com"


def test_databricks_record_overwrites_jwt_record(token_dir) -> None:
    """Re-logging into a server replaces its record wholesale.

    A server that switched deployment shape (accounts → Databricks Apps)
    must not keep serving the stale JWT.
    """
    import time

    from omnigent.cli_auth import (
        load_databricks_workspace_host,
        load_token,
        store_databricks_auth,
        store_token,
    )

    store_token(
        server_url="https://server.example.com",
        token="old-jwt",
        user_id="alice@example.com",
        expires_at=time.time() + 3600,
    )
    store_databricks_auth(
        server_url="https://server.example.com",
        workspace_host="https://example.databricks.com",
    )

    # The JWT is gone; the pointer record answers instead.
    assert load_token("https://server.example.com") is None
    assert (
        load_databricks_workspace_host("https://server.example.com")
        == "https://example.databricks.com"
    )


# ── Login-issued refresh grants (client side) ─────────────────────


def test_store_token_persists_refresh_material(token_dir) -> None:
    """A refresh token stored at login survives the round trip."""
    import json

    from omnigent.cli_auth import store_token

    store_token(
        "http://localhost:6767",
        token="jwt",
        user_id="a@x",
        expires_at=time.time() + 3600,
        refresh_token="refresh-1",
    )
    data = json.loads((token_dir / "auth_tokens.json").read_text())
    assert data["http://localhost:6767"]["refresh_token"] == "refresh-1"


def test_stored_token_status_classification(token_dir) -> None:
    """absent / expired / ok are distinguished — the host uses this to say
    "your login expired" instead of dialing into a misleading 403."""
    from omnigent.cli_auth import store_token, stored_token_status

    assert stored_token_status("http://localhost:6767") == "absent"
    store_token("http://localhost:6767", token="jwt", user_id="a@x", expires_at=time.time() - 10)
    assert stored_token_status("http://localhost:6767") == "expired"
    store_token("http://localhost:6767", token="jwt", user_id="a@x", expires_at=time.time() + 3600)
    assert stored_token_status("http://localhost:6767") == "ok"


def test_refresh_stored_token_renews_and_rotates(token_dir, monkeypatch) -> None:
    """An expired entry with refresh material renews via /oauth/token and
    persists the rotated pair."""
    import json

    import httpx

    from omnigent.cli_auth import load_token, refresh_stored_token, store_token

    store_token(
        "http://localhost:6767",
        token="stale",
        user_id="a@x",
        expires_at=time.time() - 10,
        refresh_token="refresh-1",
    )
    assert load_token("http://localhost:6767") is None  # expired

    posted: dict[str, object] = {}

    def _fake_post(url, *, data=None, timeout=None):
        posted["url"] = url
        posted["data"] = data
        return httpx.Response(
            200,
            json={
                "access_token": "fresh",
                "refresh_token": "refresh-2",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", _fake_post)
    assert refresh_stored_token("http://localhost:6767") == "fresh"
    assert posted["url"] == "http://localhost:6767/oauth/token"
    assert posted["data"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    # Rotated pair persisted; the fresh token now loads normally.
    data = json.loads((token_dir / "auth_tokens.json").read_text())
    entry = data["http://localhost:6767"]
    assert entry["token"] == "fresh" and entry["refresh_token"] == "refresh-2"
    assert load_token("http://localhost:6767") == "fresh"


def test_refresh_stored_token_no_material_is_none(token_dir) -> None:
    """Nothing to refresh (no entry, or no refresh token) → None, no I/O."""
    from omnigent.cli_auth import refresh_stored_token, store_token

    assert refresh_stored_token("http://localhost:6767") is None
    store_token("http://localhost:6767", token="jwt", user_id="a@x", expires_at=time.time() - 10)
    assert refresh_stored_token("http://localhost:6767") is None


def test_refresh_stored_token_refused_leaves_entry(token_dir, monkeypatch) -> None:
    """A server refusal (revoked / aged-out grant / old server 404) returns
    None and leaves the stored entry untouched."""
    import json

    import httpx

    from omnigent.cli_auth import refresh_stored_token, store_token

    store_token(
        "http://localhost:6767",
        token="stale",
        user_id="a@x",
        expires_at=time.time() - 10,
        refresh_token="refresh-1",
    )

    def _fake_post(url, *, data=None, timeout=None):
        return httpx.Response(
            400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", _fake_post)
    assert refresh_stored_token("http://localhost:6767") is None
    entry = json.loads((token_dir / "auth_tokens.json").read_text())["http://localhost:6767"]
    assert entry["refresh_token"] == "refresh-1"


def test_refresh_stored_token_skips_when_already_fresh(token_dir, monkeypatch) -> None:
    """A concurrent refresher already renewed → return the valid token
    without a network call (the lock-then-recheck path)."""
    import httpx

    from omnigent.cli_auth import refresh_stored_token, store_token

    store_token(
        "http://localhost:6767",
        token="already-fresh",
        user_id="a@x",
        expires_at=time.time() + 3600,
        refresh_token="refresh-1",
    )

    def _boom(*args, **kwargs):
        raise AssertionError("no network call expected")

    monkeypatch.setattr(httpx, "post", _boom)
    assert refresh_stored_token("http://localhost:6767") == "already-fresh"


def test_refresh_no_material_does_not_touch_lock_file(token_dir, monkeypatch) -> None:
    """With no refresh material, the refresh must not create a lock file.

    Regression: creating the lock before checking raised OSError on a
    read-only state directory, which the runner's auth factory caught —
    skipping its Databricks SDK fallback and leaving valid credentials
    unused.
    """
    from omnigent.cli_auth import refresh_stored_token, store_token

    store_token("http://localhost:6767", token="jwt", user_id="a@x", expires_at=time.time() - 10)

    def _boom(*_a, **_kw):
        raise AssertionError("lock file must not be created when nothing to refresh")

    monkeypatch.setattr("builtins.open", _boom)
    assert refresh_stored_token("http://localhost:6767") is None
    assert not (token_dir / "auth_tokens.lock").exists()


def test_refresh_survives_unwritable_state_dir(token_dir, monkeypatch) -> None:
    """A lock/persist failure degrades to None instead of raising, so the
    caller can still fall back to its other credential sources."""
    import omnigent.cli_auth as ca
    from omnigent.cli_auth import refresh_stored_token, store_token

    store_token(
        "http://localhost:6767",
        token="stale",
        user_id="a@x",
        expires_at=time.time() - 10,
        refresh_token="refresh-1",
    )

    @contextlib.contextmanager
    def _unwritable():
        raise OSError("read-only file system")
        yield  # pragma: no cover

    monkeypatch.setattr(ca, "_token_file_lock", _unwritable)
    assert refresh_stored_token("http://localhost:6767") is None


def test_load_token_min_remaining_declines_near_expiry(token_dir) -> None:
    """A token inside the renewal window reads as unusable so the caller
    refreshes instead of sending one that lapses mid-handshake."""
    from omnigent.cli_auth import REFRESH_MIN_REMAINING_SECONDS, load_token, store_token

    store_token(
        "http://localhost:6767",
        token="jwt",
        user_id="a@x",
        expires_at=time.time() + (REFRESH_MIN_REMAINING_SECONDS / 2),
    )
    # Default (0) still accepts a not-yet-expired token — unchanged behaviour.
    assert load_token("http://localhost:6767") == "jwt"
    # The renewal-aware caller declines it.
    assert (
        load_token("http://localhost:6767", min_remaining_seconds=REFRESH_MIN_REMAINING_SECONDS)
        is None
    )


def test_load_entry_tolerates_wrong_shaped_json(token_dir) -> None:
    """A token file holding valid JSON of the wrong shape reads as empty
    rather than raising AttributeError into the caller."""
    from omnigent.cli_auth import load_token, stored_token_status

    for bad in ("[]", "null", '"a string"'):
        (token_dir / "auth_tokens.json").write_text(bad)
        assert load_token("http://localhost:6767") is None
        assert stored_token_status("http://localhost:6767") == "absent"


def test_refresh_rejects_unusable_response_fields(token_dir, monkeypatch) -> None:
    """A 200 with null/non-string tokens or a non-finite expires_in must not
    clobber the working stored credential."""
    import json

    import httpx

    from omnigent.cli_auth import refresh_stored_token, store_token

    def _seed():
        store_token(
            "http://localhost:6767",
            token="stale",
            user_id="a@x",
            expires_at=time.time() - 10,
            refresh_token="refresh-1",
        )

    # null access_token → decline, stored pair untouched.
    _seed()
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **_kw: httpx.Response(
            200,
            json={"access_token": None, "refresh_token": "r2"},
            request=httpx.Request("POST", url),
        ),
    )
    assert refresh_stored_token("http://localhost:6767") is None
    entry = json.loads((token_dir / "auth_tokens.json").read_text())["http://localhost:6767"]
    assert entry["token"] == "stale" and entry["refresh_token"] == "refresh-1"

    # Non-finite expires_in → falls back to a sane lifetime, not "never expires".
    _seed()
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **_kw: httpx.Response(
            200,
            json={"access_token": "fresh", "refresh_token": "r2", "expires_in": "NaN"},
            request=httpx.Request("POST", url),
        ),
    )
    assert refresh_stored_token("http://localhost:6767") == "fresh"
    entry = json.loads((token_dir / "auth_tokens.json").read_text())["http://localhost:6767"]
    assert entry["expires_at"] < time.time() + 4000
