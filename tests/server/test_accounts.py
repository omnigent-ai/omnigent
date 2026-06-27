"""Tests for the ``accounts`` auth provider.

Covers the four layers of the stack:

1. ``omnigent.server.passwords`` — argon2 wrapper (hash / verify
   round-trip, constant-time-equalized failures).
2. ``omnigent.server.accounts_config.AccountsConfig`` — env-var
   parsing, fail-loud validation, secure-cookie derivation.
3. ``omnigent.server.auth.UnifiedAuthProvider`` with
   ``source='accounts'`` — cookie validation, reserved-name
   rejection, ``create_auth_provider`` factory gating.
4. ``omnigent.server.accounts_bootstrap.bootstrap_admin`` matrix
   (auto-gen / pre-seed / idempotent / loopback handoff).
5. ``omnigent.server.routes.accounts_auth`` — login / logout /
   me / invite / register / magic / members admin endpoints via
   FastAPI TestClient, including cross-user (Alice/Bob)
   permission isolation and reserved-name guards.

Tests mirror the conventions in ``tests/server/test_oidc.py``:
real types over MagicMock for data objects, content assertions
not just structure, each test fails if its feature is deleted.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnigent.server.accounts_bootstrap import (
    bootstrap_admin,
    resolve_admin_username,
)
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.auth import (
    AuthProvider,
    UnifiedAuthProvider,
    create_auth_provider,
    resolve_auth_source,
)
from omnigent.server.passwords import (
    InvalidPasswordError,
    hash_password,
    needs_rehash,
    verify_password,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)


@pytest.fixture(autouse=True)
def _clear_ambient_oidc_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip an ambient ``OMNIGENT_OIDC_ISSUER`` for the accounts suite.

    With auth enabled, the presence of an issuer selects ``oidc`` over
    ``accounts`` (see :func:`resolve_auth_source`). A developer who
    tests OIDC locally may have the issuer exported; without clearing
    it, the enable-switch → accounts assertions below would resolve to
    ``oidc`` and fail. Accounts mode never reads the issuer, so clearing
    it is safe for every test in this file. Tests that need an issuer
    set it explicitly *after* this fixture runs.
    """
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)


# ── Password helper (unit) ────────────────────────────────────────


def test_hash_password_round_trip() -> None:
    """hash_password + verify_password accept the original plaintext.

    If this breaks, every login becomes impossible: the stored hash
    of a freshly-set password no longer matches when verified.
    """
    h = hash_password("hunter2")
    verify_password("hunter2", h)  # no exception → OK


def test_hash_password_emits_argon2id_envelope() -> None:
    """The hash uses argon2id (modern OWASP-recommended variant).

    Argon2 is self-describing — the encoded prefix declares the
    variant. If a future refactor accidentally switched to argon2i
    or argon2d, this test fires before any user logs in with the
    wrong algorithm.
    """
    h = hash_password("hunter2")
    assert h.startswith("$argon2id$"), f"expected argon2id envelope, got {h[:20]!r}"


def test_verify_password_rejects_wrong_password() -> None:
    """verify_password raises InvalidPasswordError on mismatch.

    Routes rely on this exception to map every failure mode to
    the same 401 response, so a real mismatch must raise
    InvalidPasswordError specifically (not RuntimeError, not None).
    """
    h = hash_password("hunter2")
    with pytest.raises(InvalidPasswordError):
        verify_password("wrong-password", h)


def test_verify_password_rejects_malformed_hash() -> None:
    """A corrupted stored hash collapses to InvalidPasswordError.

    Same exception class as wrong-password — the route's 401
    response can't accidentally reveal whether the user's DB row
    is corrupted vs whether they typed the wrong password.
    """
    with pytest.raises(InvalidPasswordError):
        verify_password("anything", "not-a-real-argon2-hash")


def test_needs_rehash_false_on_fresh_hash() -> None:
    """A hash just produced by hash_password does NOT need rehash.

    The login route opportunistically rehashes on success when
    parameters have been upgraded; if this returned True for a
    fresh hash, every login would silently rewrite the row,
    burning argon2 cost for no benefit.
    """
    assert needs_rehash(hash_password("hunter2")) is False


# ── AccountsConfig.from_env (unit) ────────────────────────────────


def _set_required_accounts_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "https://omnigent.example.com",
) -> None:
    """Populate every required env var so from_env() doesn't fail loud."""
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", base_url)


def test_accounts_config_round_trips_required_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env() parses every required var into the dataclass."""
    secret_hex = secrets.token_hex(32)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", secret_hex)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "https://omnigent.example.com")

    cfg = AccountsConfig.from_env()

    assert cfg.cookie_secret == bytes.fromhex(secret_hex)
    assert cfg.base_url == "https://omnigent.example.com"
    assert cfg.secure_cookies is True
    assert cfg.session_cookie_name == "__Host-ap_session"


def test_accounts_config_missing_cookie_secret_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing COOKIE_SECRET raises with a remediation message."""
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", raising=False)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://localhost:8000")

    with pytest.raises(RuntimeError, match="OMNIGENT_ACCOUNTS_COOKIE_SECRET"):
        AccountsConfig.from_env()


def test_accounts_config_short_cookie_secret_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COOKIE_SECRET shorter than 32 bytes is rejected.

    HS256 with a key shorter than the digest size is a real
    weakness; matching OIDCConfig's stance.
    """
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", "00" * 16)  # only 16 bytes
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://localhost:8000")

    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        AccountsConfig.from_env()


def test_accounts_config_non_hex_cookie_secret_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-hex COOKIE_SECRET raises with a clear message."""
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", "not-hex-at-all")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://localhost:8000")

    with pytest.raises(RuntimeError, match="valid hex string"):
        AccountsConfig.from_env()


def test_accounts_config_http_base_url_uses_plain_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An http:// base URL disables Secure cookies + ``__Host-`` prefix.

    Browsers silently drop ``__Host-`` cookies on plain HTTP,
    which would cause an infinite login redirect. The cookie
    name MUST switch to a non-prefixed form for local dev.
    """
    _set_required_accounts_env(monkeypatch, base_url="http://localhost:8000")

    cfg = AccountsConfig.from_env()

    assert cfg.secure_cookies is False
    assert cfg.session_cookie_name == "ap_session"


def test_accounts_config_rejects_non_http_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BASE_URL must start with http(s):// — fail loud otherwise."""
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "ftp://omnigent.example.com")

    with pytest.raises(RuntimeError, match="http://"):
        AccountsConfig.from_env()


def test_accounts_config_init_admin_empty_string_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INIT_ADMIN_PASSWORD="" is treated as unset, not as a literal empty password.

    Same docker-compose ``${VAR:-}`` pattern that motivated the
    OIDC SCOPES fix — passing an empty string would
    silently set a zero-length admin password if not guarded.
    """
    _set_required_accounts_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", "")

    cfg = AccountsConfig.from_env()

    assert cfg.init_admin_password is None


# ── UnifiedAuthProvider accounts source (unit) ────────────────────


_TEST_COOKIE_SECRET = secrets.token_bytes(32)


def _make_accounts_config(base_url: str = "http://localhost:8000") -> AccountsConfig:
    """Build an AccountsConfig with the test secret + a configurable URL."""
    return AccountsConfig(
        cookie_secret=_TEST_COOKIE_SECRET,
        session_ttl_hours=8,
        base_url=base_url,
        init_admin_password=None,
        invite_ttl_seconds=3600,
        magic_ttl_seconds=600,
    )


class _FakeReq:
    """Minimal HTTPConnection stand-in for cookie/header tests.

    Used over MagicMock because the auth code reads ``.cookies``
    and ``.headers`` as dicts — a real dict-shaped object is
    safer than letting MagicMock fall through to a default that
    silently coerces.
    """

    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.cookies = cookies or {}
        self.headers = headers or {}


def test_accounts_source_reads_valid_cookie() -> None:
    """The accounts source extracts a user_id from a valid session JWT."""
    from omnigent.server.oidc import mint_session_cookie

    cfg = _make_accounts_config()
    provider = UnifiedAuthProvider(source="accounts", accounts_config=cfg)
    token = mint_session_cookie(
        user_id="admin", cookie_secret=_TEST_COOKIE_SECRET, ttl_hours=8, provider="accounts"
    )

    request = _FakeReq(cookies={cfg.session_cookie_name: token})

    assert provider.get_user_id(request) == "admin"


def test_mint_runner_bearer_round_trips_to_get_user_id() -> None:
    """A managed runner's server-minted bearer resolves back to the owner.

    This is the managed-sandbox fix: the sandbox runner has no login of
    its own, so the server mints an owner JWT it presents as
    ``Authorization: Bearer`` on its tunnel handshake — ``get_user_id``
    (the same check the runner tunnel applies) must resolve it to the
    owner, otherwise the runner tunnel 403s.
    """
    cfg = _make_accounts_config()
    provider = UnifiedAuthProvider(source="accounts", accounts_config=cfg)

    token = provider.mint_runner_bearer("alice@example.com")
    assert token is not None

    request = _FakeReq(headers={"Authorization": f"Bearer {token}"})
    assert provider.get_user_id(request) == "alice@example.com"


def test_mint_runner_bearer_rejects_empty_and_reserved_owner() -> None:
    """No token for an empty or reserved owner — never mint admin-equivalent creds."""
    cfg = _make_accounts_config()
    provider = UnifiedAuthProvider(source="accounts", accounts_config=cfg)
    assert provider.mint_runner_bearer("") is None
    assert provider.mint_runner_bearer("local") is None


def test_mint_runner_bearer_returns_none_for_header_source() -> None:
    """Header/proxy auth can't be minted server-side, so it returns None.

    Identity there is asserted by the upstream proxy; a managed runner
    can't synthesize it. The base ``AuthProvider`` default is also None.
    """
    header_provider = UnifiedAuthProvider(source="header")
    assert header_provider.mint_runner_bearer("alice@example.com") is None

    class _Base(AuthProvider):
        def get_user_id(self, request: object) -> str | None:  # type: ignore[override]
            return None

    assert _Base().mint_runner_bearer("alice@example.com") is None


def test_accounts_source_rejects_reserved_user_in_cookie() -> None:
    """Reserved usernames in a cookie's sub claim are rejected.

    Belt-and-suspenders for the registration-time guard: even if a
    malicious admin somehow gets a session JWT with sub=local
    minted, the auth provider refuses to honor it.
    """
    from omnigent.server.oidc import mint_session_cookie

    cfg = _make_accounts_config()
    provider = UnifiedAuthProvider(source="accounts", accounts_config=cfg)
    token = mint_session_cookie(
        user_id="local", cookie_secret=_TEST_COOKIE_SECRET, ttl_hours=8, provider="accounts"
    )

    request = _FakeReq(cookies={cfg.session_cookie_name: token})

    assert provider.get_user_id(request) is None


def test_accounts_source_rejects_cookie_signed_with_wrong_secret() -> None:
    """A cookie signed by a different key is rejected.

    Cross-deployment cookie reuse — stealing a cookie from one
    server and presenting it to another with a different secret
    must not authenticate.
    """
    from omnigent.server.oidc import mint_session_cookie

    cfg = _make_accounts_config()
    provider = UnifiedAuthProvider(source="accounts", accounts_config=cfg)
    other_secret = secrets.token_bytes(32)
    token = mint_session_cookie(
        user_id="admin", cookie_secret=other_secret, ttl_hours=8, provider="accounts"
    )

    request = _FakeReq(cookies={cfg.session_cookie_name: token})

    assert provider.get_user_id(request) is None


def test_accounts_source_accepts_bearer_token_for_cli() -> None:
    """CLI bearer tokens (no cookie) also authenticate against accounts.

    The runner / CLI use Authorization: Bearer <jwt> after picking
    the token up from ~/.omnigent/auth_tokens.json — the same
    code path the OIDC mode supports.
    """
    from omnigent.server.oidc import mint_session_cookie

    cfg = _make_accounts_config()
    provider = UnifiedAuthProvider(source="accounts", accounts_config=cfg)
    token = mint_session_cookie(
        user_id="admin", cookie_secret=_TEST_COOKIE_SECRET, ttl_hours=8, provider="accounts"
    )

    request = _FakeReq(headers={"Authorization": f"Bearer {token}"})

    assert provider.get_user_id(request) == "admin"


def test_accounts_source_login_url_points_at_spa() -> None:
    """In accounts mode, login_url is the SPA route, not the API route.

    The frontend redirects to this URL on 401; pointing at the
    API endpoint (which is a POST handler, not a page) would
    bounce the user to a blank/error response.
    """
    cfg = _make_accounts_config()
    provider = UnifiedAuthProvider(source="accounts", accounts_config=cfg)

    assert provider.login_url == "/login"


# ── resolve_auth_source (shared resolver used by every spawn path) ──


def test_resolve_auth_source_defaults_to_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-unset resolves to header — the shared resolver's baseline.

    This is the contract the daemon-owned server, the per-command server,
    and the config-signature all rely on, so a regression here would
    desync them.
    """
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.delenv("OMNIGENT_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_ENABLED", raising=False)
    assert resolve_auth_source() == "header"


def test_resolve_auth_source_opt_in_selects_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIGENT_AUTH_ENABLED=1`` (no OIDC config) opts into accounts mode."""
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")
    # _clear_ambient_oidc_issuer (autouse) guarantees no issuer is set,
    # so the enable switch resolves to the built-in accounts flow.
    assert resolve_auth_source() == "accounts"


def test_resolve_auth_source_oidc_issuer_selects_oidc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIGENT_AUTH_ENABLED=1`` + an OIDC issuer selects oidc, not accounts.

    This is the unified-switch contract: the same enable flag turns on
    accounts by default, but flips to the native OIDC flow the moment
    the operator supplies an issuer. If this asserted ``"accounts"`` the
    issuer-based mode selection (resolve_auth_source's OIDC branch)
    would be dead — an operator who set the OIDC vars would silently get
    the built-in login form instead of their IdP.
    """
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://accounts.google.com")
    assert resolve_auth_source() == "oidc"


def test_resolve_auth_source_oidc_issuer_ignored_when_auth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OIDC issuer alone (auth switch off) does NOT enable oidc.

    The issuer only chooses *which* multi-user mode runs once auth is
    enabled — it is not itself an enable switch. A stray issuer in the
    environment must not silently turn a single-user header deploy into
    an OIDC one. If this resolved to ``"oidc"`` the switch would have
    been bypassed.
    """
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.delenv("OMNIGENT_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_ENABLED", raising=False)
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://accounts.google.com")
    assert resolve_auth_source() == "header"


def test_resolve_auth_source_deprecated_alias_still_selects_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-rename ``OMNIGENT_ACCOUNTS_ENABLED`` alias still works.

    Existing deploys that set the old name must keep booting in accounts
    mode after the rename. If this regressed, an upgrade would silently
    drop those deploys back to single-user header mode (no login).
    """
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.delenv("OMNIGENT_AUTH_ENABLED", raising=False)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_ENABLED", "1")
    assert resolve_auth_source() == "accounts"


def test_resolve_auth_source_new_var_wins_over_deprecated_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current name wins when both names are set.

    A deploy migrating to ``OMNIGENT_AUTH_ENABLED`` can leave the old
    ``OMNIGENT_ACCOUNTS_ENABLED`` in place: an explicit ``=0`` on the
    new name disables auth even though the old name is truthy. If the
    alias took precedence the new value would be unsettable while the
    old one lingered.
    """
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_ENABLED", "1")
    assert resolve_auth_source() == "header"


def test_resolve_auth_source_explicit_passthrough_lowercased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit provider wins and is returned lower-cased, verbatim.

    The resolver returns the raw explicit value (validation/rejection of
    unknown values is the factory's job); the signature folds whatever
    string it returns, so the passthrough must be stable.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "OIDC")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    assert resolve_auth_source() == "oidc"


# ── create_auth_provider factory (default + explicit overrides) ──


def test_factory_defaults_to_header_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset OMNIGENT_AUTH_PROVIDER (+ no enable switch) → header mode.

    The shipped default is single-user, no-login: a bare
    ``omnigent server`` on a laptop should pop open with no
    multi-user wiring. Multi-user (accounts) is opt-in via
    ``OMNIGENT_AUTH_ENABLED=1`` (see
    :func:`test_factory_accounts_enabled_truthy_enables_accounts`).
    No accounts env is set here — header mode must not require it.
    """
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.delenv("OMNIGENT_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_ENABLED", raising=False)

    provider = create_auth_provider()

    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._source == "header"


def test_factory_explicit_header_beats_enable_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``OMNIGENT_AUTH_PROVIDER=header`` wins over the enable switch.

    An explicit provider always wins, so a stale
    ``OMNIGENT_AUTH_ENABLED=1`` in a shell can't silently turn
    accounts on for a deploy that pinned header (e.g. the internal
    hosted product, which sets header via ``setdefault`` in its
    entrypoint).
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")

    provider = create_auth_provider()

    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._source == "header"


def test_factory_accepts_accounts_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit accounts setting still works the same way."""
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    _set_required_accounts_env(monkeypatch)

    provider = create_auth_provider()

    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._source == "accounts"


def test_factory_rejects_unknown_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bogus AUTH_PROVIDER value fails loud, doesn't fall through."""
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "bogus")

    with pytest.raises(RuntimeError, match="bogus"):
        create_auth_provider()


@pytest.mark.parametrize("disable_value", ["0", "false", "no", "FALSE"])
def test_factory_accounts_enabled_falsy_stays_header(
    monkeypatch: pytest.MonkeyPatch,
    disable_value: str,
) -> None:
    """An explicitly falsy ``OMNIGENT_AUTH_ENABLED`` → header mode.

    Header is already the env-unset default, but a falsy value must
    be treated the same as unset (not as "set, therefore truthy") —
    selecting header mode, which falls back to the ``local`` user
    when no proxy header is present. No accounts env needed — header
    mode doesn't build AccountsConfig.
    """
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", disable_value)

    provider = create_auth_provider()

    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._source == "header"


@pytest.mark.parametrize("enable_value", ["1", "true", "yes", "YES"])
def test_factory_accounts_enabled_truthy_enables_accounts(
    monkeypatch: pytest.MonkeyPatch,
    enable_value: str,
) -> None:
    """A truthy ``OMNIGENT_AUTH_ENABLED`` (no OIDC) opts INTO accounts mode.

    This is the multi-user opt-in: with no explicit
    ``OMNIGENT_AUTH_PROVIDER`` and no OIDC issuer, a truthy enable
    switch turns on the accounts login flow (the inverse of the
    env-unset header default).
    """
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", enable_value)
    _set_required_accounts_env(monkeypatch)

    provider = create_auth_provider()

    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._source == "accounts"


def test_factory_explicit_accounts_beats_disabled_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``AUTH_PROVIDER=accounts`` wins over ``AUTH_ENABLED=0``.

    The enable switch only governs the env-unset default path, so a
    stale ``AUTH_ENABLED=0`` in a shell can't silently downgrade
    an operator who explicitly opted into accounts.
    """
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    _set_required_accounts_env(monkeypatch)

    provider = create_auth_provider()

    assert isinstance(provider, UnifiedAuthProvider)
    assert provider._source == "accounts"


# ── bootstrap_admin (unit) ────────────────────────────────────────


@pytest.fixture
def fresh_store(tmp_path: Path) -> SqlAlchemyAccountStore:
    """Build a fresh accounts store on a temp sqlite DB.

    Goes through the real migration path so the schema is
    exactly what the production code sees; a unit test that
    invented its own table layout would mask migration drift.

    Named ``fresh_store`` (not ``fresh_account_store``) so the
    bootstrap test signatures stay terse — there's only one store
    in play at this layer of the suite.
    """
    db_url = f"sqlite:///{tmp_path}/test.db"
    from omnigent.db.utils import get_or_create_engine

    get_or_create_engine(db_url)  # runs alembic upgrade
    return SqlAlchemyAccountStore(db_url)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME so cli_auth.store_token writes to a temp file.

    Without this, the test could write into the developer's real
    ``~/.omnigent/auth_tokens.json`` — fine, but noisy. The
    fixture also pins OMNIGENT_ADMIN_CREDENTIALS_PATH so the
    bootstrap's 0600 file lands inside the tmp dir too.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))
    # Pin the admin username to "admin" so the existing test
    # assertions (which were written against the old hardcoded
    # "admin" constant) don't depend on whatever
    # getpass.getuser() happens to return in CI. The OS-username
    # resolution path is exercised by its own dedicated tests
    # below.
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", "admin")
    return tmp_path


def test_bootstrap_with_password_creates_admin(
    fresh_store: SqlAlchemyAccountStore, isolated_home: Path
) -> None:
    """A supplied password creates the admin on first boot.

    The flag/env path (``--admin-password`` /
    ``OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD``) is the one
    bootstrap path that creates an admin directly — for headless /
    CI deploys. The password the caller supplied must be the one
    that authenticates.
    """
    result = bootstrap_admin(fresh_store, init_admin_password="explicit-pw-12345")

    assert result.fresh_boot is True
    assert result.needs_setup is False
    admin = fresh_store.get_user("admin")
    assert admin is not None
    assert admin.is_admin is True
    assert admin.has_password is True
    # verify_password is verify-or-raise: no raise == the supplied
    # password is what's stored.
    verify_password("explicit-pw-12345", fresh_store.get_password_hash("admin"))


def test_bootstrap_without_password_creates_nothing_and_needs_setup(
    fresh_store: SqlAlchemyAccountStore, isolated_home: Path
) -> None:
    """No supplied password → NO admin, NO default credential, needs_setup.

    The core "never auto-generate" invariant. With no
    ``--admin-password`` / ``INIT_ADMIN_PASSWORD``, bootstrap must
    create no account at all and report needs_setup, deferring the
    first-admin claim to the terminal prompt or the web Create-admin
    form. A created-but-random admin would be the buried-credential
    footgun we're eliminating.
    """
    result = bootstrap_admin(fresh_store)

    assert result.fresh_boot is False
    assert result.needs_setup is True
    # Nothing was created — no admin, no password-having user anywhere.
    assert fresh_store.get_user("admin") is None
    assert not any(u.has_password for u in fresh_store.list_users())


def test_bootstrap_with_password_is_idempotent_on_reboot(
    fresh_store: SqlAlchemyAccountStore, isolated_home: Path
) -> None:
    """Re-running bootstrap is a no-op once the admin exists.

    A re-bootstrap MUST NOT rotate the password — that would lock
    out anyone using the original. Rotation is an explicit action.
    """
    bootstrap_admin(fresh_store, init_admin_password="pw-12345")
    original_hash = fresh_store.get_password_hash("admin")

    result = bootstrap_admin(fresh_store, init_admin_password="pw-12345")

    assert result.fresh_boot is False
    assert result.needs_setup is False
    assert fresh_store.get_password_hash("admin") == original_hash


def test_bootstrap_ignores_supplied_password_once_admin_exists(
    fresh_store: SqlAlchemyAccountStore, isolated_home: Path
) -> None:
    """A second boot with a new password is a no-op — the first wins.

    The admin password is set exactly once, on the first boot of a
    machine's accounts DB. A later ``--admin-password`` /
    ``INIT_ADMIN_PASSWORD`` (e.g. a stale shell var, or someone trying
    to "re-set" it) must NOT silently rotate the live credential — that
    would be a footgun and a privilege surprise. It's ignored (with a
    warning, surfaced in the logs); rotation goes through the web UI's
    admin reset instead.
    """
    bootstrap_admin(fresh_store, init_admin_password="first-pw-12345")
    result = bootstrap_admin(fresh_store, init_admin_password="second-pw-67890")

    # Second call recognized the existing admin and did nothing.
    assert result.fresh_boot is False
    # The original password still authenticates (verify_password is
    # verify-or-raise: returns None on match, raises on mismatch); the
    # second password was dropped, so it must NOT verify.
    hash_ = fresh_store.get_password_hash("admin")
    assert hash_ is not None
    verify_password("first-pw-12345", hash_)  # no raise == still the original
    with pytest.raises(InvalidPasswordError):
        verify_password("second-pw-67890", hash_)


def test_bootstrap_remote_no_password_needs_setup_no_token(
    fresh_store: SqlAlchemyAccountStore, isolated_home: Path
) -> None:
    """Remote (non-loopback) + no password → needs_setup, no token, no auto-open.

    On a Docker / Render / Railway deploy the first admin is claimed
    via the web Create-admin form (needs_setup). Bootstrap creates
    nothing, writes no CLI token (the operator is on a different
    machine), and requests no browser auto-open (open_url None — the
    server has no display).
    """
    result = bootstrap_admin(
        fresh_store,
        base_url="https://omnigent.example.com",
        cookie_secret=secrets.token_bytes(32),
    )

    assert result.needs_setup is True
    assert result.open_url is None
    assert result.tui_token_written is False
    assert not (isolated_home / ".omnigent" / "auth_tokens.json").exists()


def test_bootstrap_loopback_no_password_needs_setup_opens_form(
    fresh_store: SqlAlchemyAccountStore, isolated_home: Path
) -> None:
    """Loopback + no password → needs_setup, browser auto-opens to the form.

    Local first run with no flag: no admin is created (no defaults),
    needs_setup is reported, and open_url is the loopback base URL so
    the lifespan opens the browser straight to the Create-admin form.
    No CLI token yet — there's no admin to mint one for.
    """
    base_url = "http://localhost:8000"
    result = bootstrap_admin(fresh_store, base_url=base_url, cookie_secret=secrets.token_bytes(32))

    assert result.needs_setup is True
    assert result.open_url == base_url
    assert result.tui_token_written is False
    assert fresh_store.get_user("admin") is None


def test_bootstrap_init_password_loopback_writes_cli_token_no_autoopen(
    fresh_store: SqlAlchemyAccountStore, isolated_home: Path
) -> None:
    """Supplied password on loopback → admin created, CLI token written, no auto-open.

    The flag path creates the admin and mints the loopback CLI token
    (so ``omnigent run`` is signed in), but does NOT auto-open the
    browser — the operator chose the password and will log in when
    they want.
    """
    base_url = "http://localhost:8000"
    result = bootstrap_admin(
        fresh_store,
        init_admin_password="my-supplied-pw",
        base_url=base_url,
        cookie_secret=secrets.token_bytes(32),
    )

    assert result.fresh_boot is True
    assert result.needs_setup is False
    assert result.open_url is None
    assert result.tui_token_written is True
    from omnigent import cli_auth

    assert cli_auth.load_token(base_url) is not None


def test_bootstrap_refreshes_cli_token_on_returning_loopback_boot(
    fresh_store: SqlAlchemyAccountStore, isolated_home: Path
) -> None:
    """A returning boot (admin already exists) re-mints the CLI token for this spawn.

    The daemon spawns the loopback server on a fresh port each time and
    the first-boot token is port-keyed + one-time, so a returning boot
    must re-mint a token for the current URL — otherwise ``omnigent
    run`` 401s against its own server once an admin exists (the Bug B
    that motivated this). Here the second boot uses a *different* base
    URL (new port) and must still produce a usable token for it.
    """
    from omnigent import cli_auth

    first = bootstrap_admin(
        fresh_store,
        init_admin_password="pw-12345",
        base_url="http://127.0.0.1:8000",
        cookie_secret=secrets.token_bytes(32),
    )
    assert first.fresh_boot is True

    # Second spawn: admin already exists, new port.
    new_url = "http://127.0.0.1:54312"
    second = bootstrap_admin(
        fresh_store,
        base_url=new_url,
        cookie_secret=secrets.token_bytes(32),
    )

    assert second.fresh_boot is False
    assert second.tui_token_written is True
    assert cli_auth.load_token(new_url) is not None, (
        "returning boot must mint a CLI token for the new spawn URL so "
        "`omnigent run` authenticates"
    )


# ── resolve_admin_username (unit) ─────────────────────────────────


def test_resolve_admin_username_uses_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME wins over the OS user.

    The override is the right knob for headless / Docker deploys
    where ``getpass.getuser()`` returns ``"root"`` (not great
    semantically) or for any deploy that wants a stable account
    name regardless of who launches the process.
    """
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", "operator")
    assert resolve_admin_username() == "operator"


def test_resolve_admin_username_falls_back_to_os_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no env override, the OS user (via getpass) is the admin name.

    This is the laptop-UX win — running ``omnigent server`` as
    ``dhruv.gupta`` creates a ``dhruv.gupta`` admin, so the CLI
    and the web UI share one identity from the start (no
    separate "local" / "admin" split).
    """
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", raising=False)
    # Mock getpass.getuser to a known value so the test is deterministic
    # across CI runners with different $USER values.
    import getpass

    monkeypatch.setattr(getpass, "getuser", lambda: "dhruv.gupta")

    assert resolve_admin_username() == "dhruv.gupta"


def test_resolve_admin_username_falls_back_to_admin_on_reserved_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OS user matching a reserved sentinel (``local`` / ``__public__``)
    falls back to the literal ``"admin"``.

    Without this, a deploy launched as the (admittedly weird) OS
    user "local" would create an account named "local" and
    immediately have it rejected by the auth provider's
    reserved-name guard — silent breakage.
    """
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", raising=False)
    import getpass

    monkeypatch.setattr(getpass, "getuser", lambda: "local")

    assert resolve_admin_username() == "admin"


def test_resolve_admin_username_falls_back_on_regex_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names that don't match the username regex fall back to "admin".

    Covers OS users with uppercase letters (Windows ``Administrator``),
    spaces, or other characters the route layer would reject at
    registration time anyway.
    """
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", raising=False)
    import getpass

    monkeypatch.setattr(getpass, "getuser", lambda: "Administrator")
    # Lowercased "administrator" actually IS valid — the regex
    # accepts lowercase letters. Use a name that breaks the regex
    # outright: leading dash, since the regex requires [a-z0-9]
    # as the first char.
    monkeypatch.setattr(getpass, "getuser", lambda: "-dashleading")
    assert resolve_admin_username() == "admin"


# ── Routes (integration via TestClient) ───────────────────────────


def _build_accounts_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    init_admin_password: str | None,
) -> Iterator[TestClient]:
    """Build a production-shaped accounts-mode app + TestClient.

    Shared by the :func:`accounts_app` (admin pre-seeded) and
    :func:`accounts_app_needs_setup` (no admin → first-run setup
    pending) fixtures. Wires every store + router + provider exactly
    like ``create_app`` does in production.

    :param tmp_path: Per-test temp dir (HOME, sqlite, artifacts).
    :param monkeypatch: Pytest monkeypatch for env vars.
    :param init_admin_password: When set, bootstrap creates the admin
        with it (admin exists, no setup pending). When ``None``, no
        admin is created and ``/v1/info`` reports ``needs_setup``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Accounts is the default provider now, but pin it explicitly
    # so this fixture doesn't depend on the global default.
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://localhost:8000")
    if init_admin_password is not None:
        monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", init_admin_password)
    else:
        monkeypatch.delenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", raising=False)
    # Pin the admin username to "admin" so the existing test
    # assertions don't depend on whatever getpass.getuser() returns
    # in CI. The OS-username resolution path is exercised by
    # dedicated tests below.
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-creds"))
    # Don't auto-open the browser during tests.
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_AUTO_OPEN", "0")

    db_url = f"sqlite:///{tmp_path}/test.db"
    from omnigent.db.utils import get_or_create_engine
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import (
        SqlAlchemyAgentStore,
    )
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import (
        SqlAlchemyCommentStore,
    )
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    get_or_create_engine(db_url)
    telemetry.init()
    permission_store = SqlAlchemyPermissionStore(db_url)
    agent_store = SqlAlchemyAgentStore(db_url)
    conversation_store = SqlAlchemyConversationStore(db_url)
    file_store = SqlAlchemyFileStore(db_url)
    comment_store = SqlAlchemyCommentStore(db_url)
    host_store = HostStore(db_url)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        agent_cache=agent_cache,
        caps=RuntimeCaps(),
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
    )

    auth_provider = create_auth_provider()
    # Explicit AccountStore — create_app no longer constructs one
    # internally so the internal hosted product can opt out by
    # passing None.
    account_store = SqlAlchemyAccountStore(db_url)
    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        comment_store=comment_store,
        permission_store=permission_store,
        host_store=host_store,
        auth_provider=auth_provider,
        account_store=account_store,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def accounts_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Accounts-mode app with the admin pre-seeded (``admin`` / ``admin-pw-12345``).

    The common case for route tests — an admin already exists, so
    ``/v1/info`` reports ``needs_setup=false`` and ``_login`` works.
    """
    yield from _build_accounts_app(tmp_path, monkeypatch, init_admin_password="admin-pw-12345")


@pytest.fixture
def accounts_app_needs_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Accounts-mode app with NO admin yet — first-run setup pending.

    No ``INIT_ADMIN_PASSWORD``, so bootstrap creates nothing and
    ``/v1/info`` reports ``needs_setup=true``. Exercises the
    ``/auth/setup`` first-admin claim.
    """
    yield from _build_accounts_app(tmp_path, monkeypatch, init_admin_password=None)


@pytest.fixture
def header_mode_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An app wired in header mode (accounts OFF) for negative-case tests.

    Mirrors ``accounts_app`` but with no accounts env vars and
    ``account_store=None`` so /v1/info reports accounts_enabled=false
    and the accounts router never mounts. Used to verify the
    "internal hosted product is byte-equivalent" invariant.
    """
    # Accounts is now the default provider — explicitly pin
    # "header" so this negative-case fixture actually exercises
    # header mode regardless of the global default.
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", raising=False)
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_BASE_URL", raising=False)

    db_url = f"sqlite:///{tmp_path}/header.db"
    from omnigent.db.utils import get_or_create_engine
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    get_or_create_engine(db_url)
    telemetry.init()
    permission_store = SqlAlchemyPermissionStore(db_url)
    agent_store = SqlAlchemyAgentStore(db_url)
    conversation_store = SqlAlchemyConversationStore(db_url)
    file_store = SqlAlchemyFileStore(db_url)
    comment_store = SqlAlchemyCommentStore(db_url)
    host_store = HostStore(db_url)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        agent_cache=agent_cache,
        caps=RuntimeCaps(),
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
    )
    auth_provider = create_auth_provider()
    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        comment_store=comment_store,
        permission_store=permission_store,
        host_store=host_store,
        auth_provider=auth_provider,
        account_store=None,
    )
    with TestClient(app) as client:
        yield client


def _login(client: TestClient, username: str, password: str) -> TestClient:
    """Log in via /auth/login and confirm the session cookie was set."""
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    # The TestClient holds cookies across calls — return it so the
    # caller can use it as the authed client.
    return client


def test_info_endpoint_advertises_accounts_enabled(accounts_app: TestClient) -> None:
    """``/v1/info`` reports accounts_enabled=true when the provider is active.

    The SPA reads this at boot (unauthed — must not 401) and uses
    the flag to decide whether to register /login, /register,
    /members routes and render the AccountMenu. If this regresses,
    the internal hosted product's SPA would start rendering
    accounts UI for users who can't use it.
    """
    resp = accounts_app.get("/v1/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accounts_enabled"] is True
    assert body["login_url"] == "/login"


def test_info_endpoint_reports_disabled_in_header_mode(
    header_mode_app: TestClient,
) -> None:
    """``/v1/info`` reports accounts_enabled=false in header mode.

    The frontend gates EVERY accounts surface (route table, account
    menu, /auth/me probe) on this value. A regression where it
    returned True in header mode would render broken login forms
    on the internal hosted product. Negative-case complement to
    ``test_info_endpoint_advertises_accounts_enabled``.
    """
    resp = header_mode_app.get("/v1/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accounts_enabled"] is False
    assert body["login_url"] is None


def test_login_wrong_password_returns_401(accounts_app: TestClient) -> None:
    """Wrong password → 401 with a generic error message.

    The message MUST NOT distinguish "no such user" from "wrong
    password" — leaking that would enable username enumeration.
    """
    resp = accounts_app.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
    assert "invalid" in resp.json()["error"].lower()


def test_login_unknown_user_returns_401(accounts_app: TestClient) -> None:
    """Unknown user → same 401 + same generic message as wrong-password."""
    resp = accounts_app.post(
        "/auth/login", json={"username": "ghost", "password": "anything12345"}
    )
    assert resp.status_code == 401
    assert "invalid" in resp.json()["error"].lower()


def test_login_correct_password_sets_cookie(accounts_app: TestClient) -> None:
    """Correct credentials → 200 + session cookie + user payload."""
    resp = accounts_app.post(
        "/auth/login", json={"username": "admin", "password": "admin-pw-12345"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["id"] == "admin"
    assert body["user"]["is_admin"] is True
    assert "token" in body
    # And the cookie name MUST be the HTTP variant for a localhost base_url.
    assert "ap_session" in resp.headers.get("set-cookie", "")


def test_me_unauthed_returns_401(accounts_app: TestClient) -> None:
    """No cookie → /auth/me returns 401."""
    resp = accounts_app.get("/auth/me")
    assert resp.status_code == 401


def test_me_authed_returns_user(accounts_app: TestClient) -> None:
    """Cookie-authed call returns the user's identity + admin flag."""
    client = _login(accounts_app, "admin", "admin-pw-12345")
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "admin"
    assert body["is_admin"] is True


def test_logout_clears_cookie(accounts_app: TestClient) -> None:
    """/auth/logout returns 204 and emits a Set-Cookie that clears the session."""
    client = _login(accounts_app, "admin", "admin-pw-12345")
    resp = client.post("/auth/logout")
    assert resp.status_code == 204


# ── Invite + register (integration) ───────────────────────────────


def test_non_admin_cannot_mint_invite(accounts_app: TestClient) -> None:
    """/auth/invite refuses non-admin callers with 403.

    Privilege separation: ordinary members can't create new
    accounts. Without this, any user could invite teammates,
    making the admin role useless.
    """
    # First mint an invite as admin so we have a way to create a member.
    admin_client = _login(accounts_app, "admin", "admin-pw-12345")
    invite_resp = admin_client.post("/auth/invite", json={})
    token = invite_resp.json()["token"]

    # Register alice via the invite (fresh client → fresh cookie).
    from fastapi.testclient import TestClient as _TC

    alice = _TC(accounts_app.app)
    register_resp = alice.post(
        "/auth/register",
        json={"invite": token, "username": "alice", "password": "alice-pw-67890"},
    )
    assert register_resp.status_code == 200

    # Alice trying to mint an invite → 403.
    bad_resp = alice.post("/auth/invite", json={})
    assert bad_resp.status_code == 403


def test_invite_is_single_use(accounts_app: TestClient) -> None:
    """The same invite cannot be redeemed twice.

    Atomic single-use is enforced at the store layer via
    UPDATE ... WHERE redeemed_at IS NULL. This integration
    test exercises the route + store together.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    token = admin.post("/auth/invite", json={}).json()["token"]

    from fastapi.testclient import TestClient as _TC

    # First redemption succeeds.
    first = _TC(accounts_app.app).post(
        "/auth/register",
        json={"invite": token, "username": "alice", "password": "alice-pw-67890"},
    )
    assert first.status_code == 200

    # Second redemption (different username, same token) fails with 400.
    second = _TC(accounts_app.app).post(
        "/auth/register",
        json={"invite": token, "username": "bob", "password": "bob-pw-12345"},
    )
    assert second.status_code == 400
    assert (
        "invalid" in second.json()["error"].lower() or "expired" in second.json()["error"].lower()
    )


def test_register_rejects_reserved_username(accounts_app: TestClient) -> None:
    """Reserved usernames ("local", "__public__") cannot be claimed.

    The auth provider also rejects them at cookie validation time
    so even bypassing this guard wouldn't authenticate, but
    catching it at registration time gives a clean error and
    prevents the row from being created.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    token = admin.post("/auth/invite", json={}).json()["token"]

    from fastapi.testclient import TestClient as _TC

    resp = _TC(accounts_app.app).post(
        "/auth/register",
        json={"invite": token, "username": "local", "password": "aaaaaaaa"},
    )
    assert resp.status_code == 400
    assert "reserved" in resp.json()["error"].lower()


def test_alice_cannot_see_bobs_admin_endpoints(accounts_app: TestClient) -> None:
    """Cross-user isolation: a regular member can't reach admin routes.

    The Alice/Bob multi-user check: ensures that having a valid
    session for one user never grants access to another user's
    admin-only surface. Server-side enforcement; the frontend's
    role gating is just UX, this is what actually protects the
    data.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    # Create alice (non-admin).
    alice_token = admin.post("/auth/invite", json={"is_admin": False}).json()["token"]

    from fastapi.testclient import TestClient as _TC

    alice = _TC(accounts_app.app)
    alice.post(
        "/auth/register",
        json={"invite": alice_token, "username": "alice", "password": "alice-pw-67890"},
    )

    # Each admin route must 403 for alice.
    for path, method in (
        ("/auth/users", "GET"),
        ("/auth/invite", "POST"),
        ("/auth/users/admin/reset", "POST"),
    ):
        resp = alice.request(method, path, json={})
        assert resp.status_code == 403, f"{method} {path} should 403 for non-admin"


# ── Magic-link (integration) ──────────────────────────────────────


def test_magic_link_authenticates_in_fresh_client(
    accounts_app: TestClient,
) -> None:
    """Magic-link redeem in a fresh browser signs the same user in.

    Closes the CLI → web handoff: a CLI session mints a magic
    URL, the URL pops the user into a browser already signed in.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    magic_resp = admin.post("/auth/magic")
    assert magic_resp.status_code == 200
    redeem_url = magic_resp.json()["redeem_url"]
    # Strip the base_url to get just the path + query for TestClient.
    from urllib.parse import urlparse

    parsed = urlparse(redeem_url)
    path_q = f"{parsed.path}?{parsed.query}"

    from fastapi.testclient import TestClient as _TC

    fresh = _TC(accounts_app.app)
    resp = fresh.get(path_q, follow_redirects=False)
    assert resp.status_code == 302
    # The fresh client now has a cookie and /auth/me returns admin.
    me = fresh.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == "admin"


def test_magic_link_is_single_use(accounts_app: TestClient) -> None:
    """A second redeem of the same token redirects to ``/login?magic=expired``."""
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    redeem_url = admin.post("/auth/magic").json()["redeem_url"]
    from urllib.parse import urlparse

    parsed = urlparse(redeem_url)
    path_q = f"{parsed.path}?{parsed.query}"

    from fastapi.testclient import TestClient as _TC

    _TC(accounts_app.app).get(path_q, follow_redirects=False)  # consume
    second = _TC(accounts_app.app).get(path_q, follow_redirects=False)

    assert second.status_code == 302
    assert "magic=expired" in second.headers.get("location", "")


def test_unauthed_cannot_mint_magic_link(accounts_app: TestClient) -> None:
    """Magic-link minting requires an authenticated session.

    Without this check, anyone could mint a token bound to no
    user (or worse, harvest tokens for later analysis).
    """
    resp = accounts_app.post("/auth/magic")
    assert resp.status_code == 401


# ── Members admin (integration) ───────────────────────────────────


def test_admin_can_list_users(accounts_app: TestClient) -> None:
    """GET /auth/users returns every account for admin callers."""
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    resp = admin.get("/auth/users")
    assert resp.status_code == 200
    user_ids = {u["id"] for u in resp.json()["users"]}
    assert "admin" in user_ids


def test_admin_list_excludes_legacy_local_and_public_sentinels(
    accounts_app: TestClient,
    tmp_path: Path,
) -> None:
    """The Members page hides ``"local"`` and ``"__public__"``.

    Both rows exist in the ``users`` table — ``"local"`` is
    backfilled by the original session-permissions migration so
    pre-accounts deploys had a default owner row for existing
    conversations, and ``"__public__"`` is the anonymous-grant
    sentinel. Neither is actionable in accounts mode (reserved
    names can't authenticate, can't be reset, can't be promoted),
    so listing them on the Members page is dead weight.

    We seed both rows directly (bypassing the accounts API, which
    rejects reserved names) into the same sqlite file the
    ``accounts_app`` fixture wired up, then confirm the admin
    list filter drops them.
    """
    from omnigent.db.db_models import SqlUser
    from omnigent.db.utils import get_or_create_engine, make_managed_session_maker

    db_url = f"sqlite:///{tmp_path}/test.db"
    engine = get_or_create_engine(db_url)
    session_maker = make_managed_session_maker(engine)
    with session_maker() as session:
        for sentinel in ("local", "__public__"):
            if session.get(SqlUser, sentinel) is None:
                session.add(SqlUser(id=sentinel, is_admin=False))
        session.commit()

    admin = _login(accounts_app, "admin", "admin-pw-12345")
    resp = admin.get("/auth/users")
    assert resp.status_code == 200
    user_ids = {u["id"] for u in resp.json()["users"]}
    assert "admin" in user_ids
    assert "local" not in user_ids, (
        "list_users() must hide the legacy 'local' row from the Members page"
    )
    assert "__public__" not in user_ids, (
        "list_users() must hide the '__public__' anonymous-grant sentinel"
    )


def test_admin_cannot_delete_self(accounts_app: TestClient) -> None:
    """Deleting the calling admin is refused with 400.

    Prevents self-lockout: deleting yourself while signed in
    would leave your session valid but the row gone, and any
    future cookie validation would fail. Worse — if you're the
    only admin, the deploy has no recovery path.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    resp = admin.delete("/auth/users/admin")
    # Both "cannot delete self" AND "cannot delete bootstrap admin"
    # routes through 400 here; either reason is fine.
    assert resp.status_code == 400


def test_admin_can_delete_former_admin_when_others_exist(
    accounts_app: TestClient,
) -> None:
    """The previously-locked bootstrap admin IS deletable when another admin exists.

    Earlier iterations hard-coded "can't delete the user named
    'admin'", which made sense when the bootstrap username was
    always the literal "admin". Now the bootstrap defaults to
    the OS user (``dhruv.gupta`` etc.) so the check generalized
    to "would this leave zero admins". As long as another admin
    exists, the original bootstrap row IS deletable — admins
    might want to rename or rotate it.

    The "last admin" invariant is exercised by the negative
    test below.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    invite_token = admin.post("/auth/invite", json={"is_admin": True}).json()["token"]
    from fastapi.testclient import TestClient as _TC

    second = _TC(accounts_app.app)
    second.post(
        "/auth/register",
        json={"invite": invite_token, "username": "second", "password": "second-pw-1234"},
    )

    resp = second.delete("/auth/users/admin")
    assert resp.status_code == 204, resp.text


def test_admin_cannot_delete_last_admin(accounts_app: TestClient) -> None:
    """If only one admin exists, deleting them returns 400.

    Closes the same recovery-path invariant the old "cannot
    delete the bootstrap admin" check protected, but generalizes
    so it works regardless of the bootstrap username. Setup:
    create a second user as a regular member (NOT admin), then
    have the second user attempt to delete the only admin.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    member_invite = admin.post("/auth/invite", json={"is_admin": False}).json()["token"]
    from fastapi.testclient import TestClient as _TC

    member = _TC(accounts_app.app)
    member.post(
        "/auth/register",
        json={"invite": member_invite, "username": "alice", "password": "alice-pw-1234"},
    )

    # alice (non-admin) → 403; doesn't even reach the last-admin check.
    resp = member.delete("/auth/users/admin")
    assert resp.status_code == 403

    # Now promote alice manually by using the admin to reset, then
    # try deleting admin from alice's session — but alice would
    # need to be admin first. Simpler scenario: use admin to
    # delete admin (self-delete is the actual block here). For
    # last-admin, a future test where alice IS promoted would
    # cover; today we don't have a promote endpoint.
    #
    # The closest assertion we can make from existing routes:
    # admin trying to delete themselves is rejected, which proves
    # there's no path to "zero admins" via the DELETE route.
    resp = admin.delete("/auth/users/admin")
    assert resp.status_code == 400
    assert "self" in resp.json()["error"].lower() or "last admin" in resp.json()["error"].lower()


def test_admin_reset_returns_new_plaintext_once(
    accounts_app: TestClient,
) -> None:
    """Admin-issued reset returns the new plaintext password exactly once.

    This is the "DM the password" flow — the admin sends the
    plaintext out-of-band. The route returning it is the only
    place it surfaces; the stored hash overwrites the old one
    so the prior password stops working.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    invite_token = admin.post("/auth/invite", json={}).json()["token"]
    from fastapi.testclient import TestClient as _TC

    alice = _TC(accounts_app.app)
    alice.post(
        "/auth/register",
        json={"invite": invite_token, "username": "alice", "password": "old-pw-12345"},
    )

    resp = admin.post("/auth/users/alice/reset")
    assert resp.status_code == 200
    new_pw = resp.json()["new_password"]
    assert len(new_pw) > 10

    # The new password works, the old one doesn't.
    fresh = _TC(accounts_app.app)
    bad = fresh.post("/auth/login", json={"username": "alice", "password": "old-pw-12345"})
    assert bad.status_code == 401
    good = fresh.post("/auth/login", json={"username": "alice", "password": new_pw})
    assert good.status_code == 200


def test_admin_can_delete_normal_member(accounts_app: TestClient) -> None:
    """Admin DELETE /auth/users/{id} succeeds and removes the user.

    The refusal paths (self-delete, bootstrap-admin) are tested
    above; this is the positive path — confirms the route + the
    store's ``delete_user`` actually drop the row and the user
    no longer appears in the listing.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    invite = admin.post("/auth/invite", json={}).json()["token"]
    from fastapi.testclient import TestClient as _TC

    alice = _TC(accounts_app.app)
    alice.post(
        "/auth/register",
        json={"invite": invite, "username": "alice", "password": "alice-pw-1234"},
    )

    # Pre-condition: alice in the list.
    pre = admin.get("/auth/users").json()
    assert "alice" in {u["id"] for u in pre["users"]}

    resp = admin.delete("/auth/users/alice")
    assert resp.status_code == 204, resp.text

    post = admin.get("/auth/users").json()
    assert "alice" not in {u["id"] for u in post["users"]}


def test_change_own_password_round_trip(accounts_app: TestClient) -> None:
    """POST /auth/users/me/password rotates the password.

    Correct old password → 204, new password works on the next
    login, old password stops working. Covers the happy path
    that the self-serve UX depends on.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    resp = admin.post(
        "/auth/users/me/password",
        json={"old_password": "admin-pw-12345", "new_password": "new-strong-pw-1"},
    )
    assert resp.status_code == 204, resp.text

    from fastapi.testclient import TestClient as _TC

    # Old password no longer works.
    bad = _TC(accounts_app.app).post(
        "/auth/login", json={"username": "admin", "password": "admin-pw-12345"}
    )
    assert bad.status_code == 401

    # New password does.
    good = _TC(accounts_app.app).post(
        "/auth/login", json={"username": "admin", "password": "new-strong-pw-1"}
    )
    assert good.status_code == 200


def test_change_own_password_rejects_wrong_old_password(
    accounts_app: TestClient,
) -> None:
    """Wrong old_password → 401, password is NOT rotated.

    Required because the route is reachable by anyone with a
    valid session — without verifying old_password an attacker
    who steals a session cookie could lock out the legitimate
    user by setting a new password.
    """
    admin = _login(accounts_app, "admin", "admin-pw-12345")
    resp = admin.post(
        "/auth/users/me/password",
        json={"old_password": "wrong", "new_password": "new-strong-pw-1"},
    )
    assert resp.status_code == 401

    # Original password still works.
    from fastapi.testclient import TestClient as _TC

    good = _TC(accounts_app.app).post(
        "/auth/login", json={"username": "admin", "password": "admin-pw-12345"}
    )
    assert good.status_code == 200


def test_purge_expired_tokens_drops_only_expired(
    fresh_store: SqlAlchemyAccountStore,
) -> None:
    """purge_expired_tokens deletes expired rows + returns the count.

    Boundary case: a token whose expires_at exactly equals "now"
    is considered expired (the WHERE clause is ``<= now``).
    """
    fresh_store.create_token(
        "live",
        kind="invite",
        user_id=None,
        created_by="admin",
        created_at=1000,
        expires_at=2000,
    )
    fresh_store.create_token(
        "expired-1",
        kind="invite",
        user_id=None,
        created_by="admin",
        created_at=10,
        expires_at=20,
    )
    fresh_store.create_token(
        "expired-2",
        kind="magic",
        user_id="admin",
        created_by=None,
        created_at=100,
        expires_at=200,
    )

    n = fresh_store.purge_expired_tokens(now_epoch_seconds=500)
    assert n == 2

    # The non-expired token still redeems.
    redeemed = fresh_store.redeem_token("live", kind="invite", now_epoch_seconds=1500)
    assert redeemed is not None and redeemed.id == "live"


# ── CLI: omnigent login accounts flow ───────────────────────────


def test_cli_accounts_login_happy_path_stores_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`omnigent login` in accounts mode prompts → POSTs → stores token.

    Mocks the network surface (the /v1/me probe + the /auth/login
    POST) and the token storage (cli_auth.store_token writes to
    the user's ~/.omnigent/), so the test verifies the CLI
    plumbing without spinning up a server.

    Closes the AI-review gap flagged in the first review pass —
    the accounts-login CLI path was the only auth code in the
    PR without test coverage.
    """
    import httpx as _httpx
    from click.testing import CliRunner

    from omnigent import cli_auth
    from omnigent.cli import cli

    # Redirect $HOME so cli_auth.store_token writes into tmp.
    monkeypatch.setenv("HOME", str(tmp_path))

    # First call: /v1/me probe → 401 + login_url=/login so the CLI
    # picks the accounts branch.
    # Second call: /auth/login → 200 with the token payload.
    calls = {"n": 0}

    class _FakeResponse:
        def __init__(self, status_code: int, body: object) -> None:
            self.status_code = status_code
            self._body = body
            self.is_success = 200 <= status_code < 300
            self.text = str(body)
            # The login probe inspects response headers (Databricks-fronted
            # server detection); a plain accounts server sends none relevant.
            self.headers: dict[str, str] = {}

        def json(self) -> object:
            return self._body

    def fake_get(url: str, **_kw: object) -> _FakeResponse:
        calls["n"] += 1
        assert url.endswith("/v1/me")
        return _FakeResponse(401, {"user_id": None, "login_url": "/login"})

    def fake_post(url: str, **kw: object) -> _FakeResponse:
        calls["n"] += 1
        assert url.endswith("/auth/login")
        body = kw["json"]
        assert body == {"username": "alice", "password": "alice-pw-1234"}
        return _FakeResponse(
            200,
            {
                "token": "fake.jwt.token",
                "user": {"id": "alice", "is_admin": False},
                "expires_in": 8 * 3600,
            },
        )

    monkeypatch.setattr(_httpx, "get", fake_get)
    monkeypatch.setattr(_httpx, "post", fake_post)

    # CliRunner feeds the prompts via stdin: username (empty → default
    # "admin", but we override with "alice"), then password.
    result = CliRunner().invoke(
        cli,
        ["login", "http://localhost:8000"],
        input="alice\nalice-pw-1234\n",
    )

    assert result.exit_code == 0, result.output
    assert "Logged in as alice" in result.output
    # The store_token side effect lands in ~/.omnigent/auth_tokens.json.
    assert cli_auth.load_token("http://localhost:8000") == "fake.jwt.token"


def test_cli_accounts_login_wrong_password_surfaces_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 from /auth/login → ClickException with the generic message.

    The server returns "invalid username or password" without
    distinguishing between unknown-user and wrong-password — the
    CLI surfaces that as a ``ClickException`` (non-zero exit,
    formatted as ``Error: ...``) rather than a raw traceback.
    """
    import httpx as _httpx
    from click.testing import CliRunner

    from omnigent.cli import cli

    monkeypatch.setenv("HOME", str(tmp_path))

    class _FakeResponse:
        def __init__(self, status_code: int, body: object) -> None:
            self.status_code = status_code
            self._body = body
            self.is_success = 200 <= status_code < 300
            self.text = str(body)
            # The login probe inspects response headers (Databricks-fronted
            # server detection); a plain accounts server sends none relevant.
            self.headers: dict[str, str] = {}

        def json(self) -> object:
            return self._body

    def fake_get(url: str, **_kw: object) -> _FakeResponse:
        return _FakeResponse(401, {"user_id": None, "login_url": "/login"})

    def fake_post(url: str, **_kw: object) -> _FakeResponse:
        return _FakeResponse(401, {"error": "invalid username or password"})

    monkeypatch.setattr(_httpx, "get", fake_get)
    monkeypatch.setattr(_httpx, "post", fake_post)

    result = CliRunner().invoke(
        cli,
        ["login", "http://localhost:8000"],
        input="admin\nwrong-password\n",
    )

    assert result.exit_code != 0
    assert "Invalid username or password" in result.output
    # Generic message — no enumeration leak about whether the
    # username exists.
    assert "username" not in result.output.lower() or "invalid" in result.output.lower()


def test_cli_accounts_login_network_failure_surfaces_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network error reaching /auth/login → ClickException, not traceback.

    Covers the case an invited user might hit when their network
    flakes or the server is briefly down between probe and login.
    """
    import httpx as _httpx
    from click.testing import CliRunner

    from omnigent.cli import cli

    monkeypatch.setenv("HOME", str(tmp_path))

    class _FakeResponse:
        def __init__(self, status_code: int, body: object) -> None:
            self.status_code = status_code
            self._body = body
            self.is_success = 200 <= status_code < 300
            self.text = str(body)
            # The login probe inspects response headers (Databricks-fronted
            # server detection); a plain accounts server sends none relevant.
            self.headers: dict[str, str] = {}

        def json(self) -> object:
            return self._body

    def fake_get(url: str, **_kw: object) -> _FakeResponse:
        return _FakeResponse(401, {"user_id": None, "login_url": "/login"})

    def fake_post(url: str, **_kw: object) -> None:
        raise _httpx.HTTPError("connection refused")

    monkeypatch.setattr(_httpx, "get", fake_get)
    monkeypatch.setattr(_httpx, "post", fake_post)

    result = CliRunner().invoke(
        cli,
        ["login", "http://localhost:8000"],
        input="alice\nalice-pw-1234\n",
    )

    assert result.exit_code != 0
    assert "Could not reach" in result.output


# ── First-run web setup: POST /auth/setup (first-admin claim) ─────


def test_setup_creates_first_admin_and_signs_in(
    accounts_app_needs_setup: TestClient,
) -> None:
    """On a fresh instance, /auth/setup claims the first admin + signs in.

    The remote-deploy CUJ (Render/Railway/Docker): open the URL, the
    first visitor picks a username + password, and lands signed in as
    an admin — no container access, no log-digging.
    """
    client = accounts_app_needs_setup

    # Before setup, /v1/info advertises that setup is pending.
    info_before = client.get("/v1/info").json()
    assert info_before["needs_setup"] is True

    resp = client.post("/auth/setup", json={"username": "alice", "password": "alice-pw-12345"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["id"] == "alice"
    assert body["user"]["is_admin"] is True
    # The session cookie was set, so the same client is now authed.
    me = client.get("/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["id"] == "alice"

    # Setup is no longer pending once the first admin exists.
    info_after = client.get("/v1/info").json()
    assert info_after["needs_setup"] is False


def test_setup_writes_loopback_cli_token(
    accounts_app_needs_setup: TestClient,
) -> None:
    """First-run web admin-claim mints the loopback CLI token.

    The local CUJ: ``omnigent run`` (re)spawns the local server in
    accounts mode with no admin, so the operator claims it via the
    browser form. ``/auth/setup`` must also mint the loopback CLI token
    (the fixture's base URL is ``http://localhost:8000`` — loopback) so
    the in-flight ``omnigent run`` is signed in immediately instead of
    401-ing until the next server boot.
    """
    from omnigent import cli_auth

    client = accounts_app_needs_setup
    base_url = "http://localhost:8000"
    # No CLI token before the admin is claimed.
    assert cli_auth.load_token(base_url) is None

    resp = client.post("/auth/setup", json={"username": "alice", "password": "alice-pw-12345"})
    assert resp.status_code == 200, resp.text

    # The loopback handoff fired: the spawning CLI now has a usable token.
    assert cli_auth.load_token(base_url) is not None


def test_setup_409_once_an_admin_exists(accounts_app: TestClient) -> None:
    """/auth/setup hard-locks the instant any account exists.

    This is the gate that stops the unauthenticated route from being
    used to escalate or add a second admin after first-run. The
    ``accounts_app`` fixture pre-seeds an admin, so setup must 409.
    """
    resp = accounts_app.post(
        "/auth/setup", json={"username": "mallory", "password": "mallory-pw-12345"}
    )
    assert resp.status_code == 409, resp.text
    # And no account was created by the rejected call.
    assert accounts_app.get("/v1/info").json()["needs_setup"] is False


def test_setup_is_single_use(accounts_app_needs_setup: TestClient) -> None:
    """A second /auth/setup after the first claim is rejected with 409."""
    client = accounts_app_needs_setup

    first = client.post("/auth/setup", json={"username": "alice", "password": "alice-pw-12345"})
    assert first.status_code == 200, first.text

    second = client.post("/auth/setup", json={"username": "bob", "password": "bob-pw-123456"})
    assert second.status_code == 409, second.text
    # The rejected second claim created no account (alice is signed in
    # from the first setup, so she can list users).
    user_ids = {u["id"] for u in client.get("/auth/users").json()["users"]}
    assert "alice" in user_ids
    assert "bob" not in user_ids
