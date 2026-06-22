"""Tests for the OIDC admission policy (:mod:`omnigent.server.oidc_access`).

The callback itself isn't driven end-to-end here (that needs an IdP
token-exchange mock — covered by the manual REPL/IdP verification in
the plan); these tests pin the single admit/deny decision the callback
delegates to, across every branch: no-restriction default, env
domains, the runtime-editable file (including mtime reload), the union
of the two, the admin-list bypass, and the invite bypass.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.server.admin_list import AdminList
from omnigent.server.oidc_access import OidcAdmissionPolicy, resolve_allowed_domains_path


def _policy(
    tmp_path: Path,
    *,
    env_domains: frozenset[str] | None = None,
    domains_file: str | None = None,
    admins_file: str = "",
    invited_lookup: object | None = None,
) -> OidcAdmissionPolicy:
    """Build a policy with file contents written into ``tmp_path``."""
    domains_path = tmp_path / "allowed_domains"
    if domains_file is not None:
        domains_path.write_text(domains_file)
    admins_path = tmp_path / "admins"
    admins_path.write_text(admins_file)
    return OidcAdmissionPolicy(
        env_allowed_domains=env_domains,
        domains_file_path=domains_path,
        admin_list=AdminList(admins_path),
        invited_lookup=invited_lookup,  # type: ignore[arg-type]
    )


def test_no_restriction_admits_everyone(tmp_path: Path) -> None:
    """With no env domains and no file, any authenticated email is admitted.

    This preserves the OSS default — a fresh deploy with no domain
    config lets any IdP user in (e.g. GitHub). If this regressed, every
    no-config deploy would 403 all logins.
    """
    policy = _policy(tmp_path)
    assert policy.is_admitted("anyone@anywhere.com") is True
    assert policy.effective_domains() == frozenset()


def test_env_domain_match(tmp_path: Path) -> None:
    """An email on an env-allowlisted domain is admitted; others denied."""
    policy = _policy(tmp_path, env_domains=frozenset({"example.com"}))
    assert policy.is_admitted("alice@example.com") is True
    assert policy.is_admitted("mallory@evil.com") is False


def test_file_domain_match(tmp_path: Path) -> None:
    """A domain listed only in the file is admitted."""
    policy = _policy(tmp_path, domains_file="example.com\n")
    assert policy.is_admitted("alice@example.com") is True
    assert policy.is_admitted("bob@other.com") is False


def test_env_and_file_domains_union(tmp_path: Path) -> None:
    """Effective allowlist is the union of env and file domains."""
    policy = _policy(
        tmp_path,
        env_domains=frozenset({"env-domain.com"}),
        domains_file="file-domain.com\n",
    )
    assert policy.effective_domains() == frozenset({"env-domain.com", "file-domain.com"})
    assert policy.is_admitted("a@env-domain.com") is True
    assert policy.is_admitted("b@file-domain.com") is True
    assert policy.is_admitted("c@nope.com") is False


def test_file_edit_takes_effect_without_restart(tmp_path: Path) -> None:
    """Adding a domain to the file admits it on the next check (mtime reload)."""
    policy = _policy(tmp_path, domains_file="example.com\n")
    assert policy.is_admitted("contractor@partner.com") is False

    domains_path = tmp_path / "allowed_domains"
    domains_path.write_text("example.com\npartner.com\n")
    stat = domains_path.stat()
    os.utime(domains_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert policy.is_admitted("contractor@partner.com") is True


def test_admin_list_bypasses_domain_restriction(tmp_path: Path) -> None:
    """A listed admin is admitted even on a non-allowlisted domain.

    An operator must never be locked out by a domain typo, and may be
    on a different domain than the team.
    """
    policy = _policy(
        tmp_path,
        env_domains=frozenset({"example.com"}),
        admins_file="founder@gmail.com\n",
    )
    assert policy.is_admitted("founder@gmail.com") is True
    assert policy.is_admitted("stranger@gmail.com") is False


class _StubInvitedLookup:
    """Minimal real InvitedEmailLookup for the invite-bypass test."""

    def __init__(self, invited: set[str]) -> None:
        self._invited = invited

    def is_email_invited(self, email: str) -> bool:
        return email in self._invited


def test_invite_bypasses_domain_restriction(tmp_path: Path) -> None:
    """An individually-invited email is admitted off-domain; others aren't."""
    policy = _policy(
        tmp_path,
        env_domains=frozenset({"example.com"}),
        invited_lookup=_StubInvitedLookup({"guest@external.com"}),
    )
    assert policy.is_admitted("guest@external.com") is True
    assert policy.is_admitted("uninvited@external.com") is False


def test_config_allowed_domains_union(tmp_path: Path) -> None:
    """Domains from the server config (``config_allowed_domains``) are admitted.

    Union'd with env + file — here env is None and only the config set
    is configured, so it becomes the effective allowlist.
    """
    admins = tmp_path / "admins"
    admins.write_text("")
    policy = OidcAdmissionPolicy(
        env_allowed_domains=None,
        domains_file_path=tmp_path / "allowed_domains",
        admin_list=AdminList(admins),
        config_allowed_domains=frozenset({"Config.COM"}),
    )
    assert policy.effective_domains() == frozenset({"config.com"})  # lowercased
    assert policy.is_admitted("a@config.com") is True
    assert policy.is_admitted("a@other.com") is False


def test_resolve_allowed_domains_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_OIDC_ALLOWED_DOMAINS_PATH`` wins over the default."""
    monkeypatch.setenv("OMNIGENT_OIDC_ALLOWED_DOMAINS_PATH", "/etc/omnigent/domains")
    assert resolve_allowed_domains_path() == Path("/etc/omnigent/domains")


def test_resolve_allowed_domains_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default co-locates with the data dir as ``<data_dir>/allowed_domains``."""
    monkeypatch.delenv("OMNIGENT_OIDC_ALLOWED_DOMAINS_PATH", raising=False)
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", "/data/admin-credentials")
    assert resolve_allowed_domains_path() == Path("/data/allowed_domains")
