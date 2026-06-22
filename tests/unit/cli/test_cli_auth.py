"""Unit tests for CLI OIDC token storage (omnigent/cli_auth.py).

Tests the store/load/clear lifecycle for session tokens persisted
by ``omnigent login``.
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture()
def token_dir(tmp_path, monkeypatch):
    """Redirect the token file to a temp directory.

    Patches ``state_dir`` to return ``tmp_path`` so tests don't
    touch ``~/.omnigent``.

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
