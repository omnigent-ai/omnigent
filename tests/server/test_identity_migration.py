"""Tests for the accounts → OIDC identity remap.

Covers :func:`omnigent.server.identity_migration.remap_identities` and
``build_domain_mapping`` against a real SQLite database, plus the
``omnigent debug migrate-accounts-to-oidc`` CLI wrapper via Click's
``CliRunner``.

The load-bearing properties: every user-id-bearing column is repointed,
``is_admin`` survives the move, dry runs mutate nothing, grant
collisions merge to the higher level, and an existing distinct NEW id
is refused without ``--force``.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.cli import cli
from omnigent.db.db_models import (
    SqlAccountToken,
    SqlComment,
    SqlHost,
    SqlPolicy,
)
from omnigent.db.enum_codecs import (
    encode_account_token_kind,
    encode_comment_status,
    encode_host_status,
    encode_policy_scope,
    encode_policy_type,
)
from omnigent.db.utils import get_or_create_engine
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.identity_migration import build_domain_mapping, remap_identities
from omnigent.server.passwords import hash_password
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def _conversation(db_uri: str) -> str:
    """Create a conversation and return its id (FK target for grants)."""
    return SqlAlchemyConversationStore(db_uri).create_conversation().id


# ── build_domain_mapping ──────────────────────────────────────────


def test_build_domain_mapping_skips_emails_and_reserved(db_uri: str) -> None:
    """Bare usernames map to ``user@domain``; emails / reserved are skipped.

    A user already keyed by email needs no remap, and the ``local`` /
    ``__public__`` sentinels must never be rewritten.
    """
    account_store = SqlAlchemyAccountStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"))
    account_store.create_user_with_password("bob", hash_password("password123"))
    account_store.create_user_with_password("carol@already.com", hash_password("password123"))
    # The "local" sentinel row is created by migrations; ensure it exists.
    SqlAlchemyPermissionStore(db_uri).ensure_user("local", is_admin=True)

    engine = get_or_create_engine(db_uri)
    mapping = build_domain_mapping(engine, "example.com")

    assert mapping == {"alice": "alice@example.com", "bob": "bob@example.com"}


def test_build_domain_mapping_strips_leading_at(db_uri: str) -> None:
    """A ``--domain @example.com`` value is tolerated (leading @ stripped)."""
    SqlAlchemyAccountStore(db_uri).create_user_with_password("alice", hash_password("password123"))
    mapping = build_domain_mapping(get_or_create_engine(db_uri), "@example.com")
    assert mapping == {"alice": "alice@example.com"}


# ── remap_identities: the core move ───────────────────────────────


def test_remap_moves_user_grant_and_admin(db_uri: str) -> None:
    """A committed remap moves the user row + grant and preserves is_admin."""
    account_store = SqlAlchemyAccountStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"), is_admin=True)
    conv_id = _conversation(db_uri)
    perm_store.grant("alice", conv_id, level=3)

    report = remap_identities(
        get_or_create_engine(db_uri),
        {"alice": "alice@example.com"},
        dry_run=False,
    )

    assert report.committed is True
    # Old principal is gone; new one exists and kept admin.
    assert account_store.get_user("alice") is None
    new_user = account_store.get_user("alice@example.com")
    assert new_user is not None
    assert new_user.is_admin is True
    # The grant moved to the new id at the same level.
    moved = perm_store.get("alice@example.com", conv_id)
    assert moved is not None and moved.level == 3
    assert perm_store.get("alice", conv_id) is None


def test_remap_repoints_comments_policies_tokens_hosts(db_uri: str) -> None:
    """Every user-id-bearing column is repointed, not just users/grants."""
    account_store = SqlAlchemyAccountStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"))
    engine = get_or_create_engine(db_uri)

    # Seed one row per referencing table keyed on "alice".
    with Session(engine) as s:
        s.add(
            SqlComment(
                id="747618b4b2dd94383e50ddf180ceddc3",
                conversation_id="8af356d908005a65f872c246158c6293",
                path="a.py",
                start_index=0,
                end_index=1,
                body="hi",
                status=encode_comment_status("draft"),
                created_at=1,
                # created_at scaled to epoch-µs, matching the store's invariant.
                updated_at=1_000_000,
                created_by="alice",
            )
        )
        s.add(
            SqlPolicy(
                id="12a6858438cb1aa1b9e00dc79bb04dd9",
                name="p",
                session_id=None,
                scope=encode_policy_scope("default"),
                created_at=1,
                type=encode_policy_type("python"),
                handler="x.y",
                created_by="alice",
            )
        )
        s.add(
            SqlAccountToken(
                id="tok_1",
                kind=encode_account_token_kind("invite"),
                user_id=None,
                created_by="alice",
                created_at=1,
                expires_at=10_000_000_000,
            )
        )
        s.add(
            SqlHost(
                user_id="alice",
                name="laptop",
                host_id="a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",
                status=encode_host_status("offline"),
                created_at=1,
                updated_at=1,
            )
        )
        s.commit()

    remap_identities(engine, {"alice": "alice@example.com"}, dry_run=False)

    with Session(engine) as s:
        assert (
            s.get(
                SqlComment,
                (0, "8af356d908005a65f872c246158c6293", "747618b4b2dd94383e50ddf180ceddc3"),
            ).created_by
            == "alice@example.com"
        )
        assert (
            s.get(SqlPolicy, (0, "12a6858438cb1aa1b9e00dc79bb04dd9")).created_by
            == "alice@example.com"
        )
        assert s.get(SqlAccountToken, (0, "tok_1")).created_by == "alice@example.com"
        host_owners = s.execute(select(SqlHost.user_id)).scalars().all()
        assert host_owners == ["alice@example.com"]


def test_dry_run_mutates_nothing_but_reports(db_uri: str) -> None:
    """A dry run reports would-change counts but leaves the DB untouched."""
    account_store = SqlAlchemyAccountStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"), is_admin=True)
    conv_id = _conversation(db_uri)
    perm_store.grant("alice", conv_id, level=3)

    report = remap_identities(
        get_or_create_engine(db_uri),
        {"alice": "alice@example.com"},
        dry_run=True,
    )

    assert report.committed is False
    # Report still reflects what *would* change.
    assert report.per_table.get("session_permissions") == 1
    assert report.per_table.get("users") == 1
    # But nothing actually moved.
    assert account_store.get_user("alice") is not None
    assert account_store.get_user("alice@example.com") is None
    assert perm_store.get("alice", conv_id) is not None


def test_grant_collision_merges_to_higher_level(db_uri: str) -> None:
    """When NEW already has a grant on the same conversation, levels merge to max."""
    account_store = SqlAlchemyAccountStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"))
    account_store.create_user_with_password("alice@example.com", hash_password("password123"))
    conv_id = _conversation(db_uri)
    perm_store.grant("alice", conv_id, level=3)  # old has manage
    perm_store.grant("alice@example.com", conv_id, level=1)  # new has read

    # NEW exists distinctly → needs force to merge.
    report = remap_identities(
        get_or_create_engine(db_uri),
        {"alice": "alice@example.com"},
        dry_run=False,
        force=True,
    )

    assert report.committed is True
    merged = perm_store.get("alice@example.com", conv_id)
    assert merged is not None and merged.level == 3  # max(3, 1)
    assert perm_store.get("alice", conv_id) is None


def test_refuses_existing_new_without_force(db_uri: str) -> None:
    """Mapping onto an existing distinct NEW id is refused unless --force."""
    account_store = SqlAlchemyAccountStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"))
    account_store.create_user_with_password("alice@example.com", hash_password("password123"))

    report = remap_identities(
        get_or_create_engine(db_uri),
        {"alice": "alice@example.com"},
        dry_run=False,
        force=False,
    )

    assert report.refused == ["alice -> alice@example.com"]
    # Both rows untouched.
    assert account_store.get_user("alice") is not None
    assert account_store.get_user("alice@example.com") is not None


def test_skipped_missing_old_user(db_uri: str) -> None:
    """An old id with no users row is recorded in skipped_missing."""
    report = remap_identities(
        get_or_create_engine(db_uri),
        {"ghost": "ghost@example.com"},
        dry_run=False,
    )
    assert report.skipped_missing == ["ghost"]
    assert report.per_table == {}


# ── CLI wrapper ───────────────────────────────────────────────────


def test_cli_dry_run_by_default(db_uri: str) -> None:
    """``migrate-to-oidc`` without --commit is a dry run that changes nothing."""
    account_store = SqlAlchemyAccountStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"), is_admin=True)

    result = CliRunner().invoke(
        cli,
        ["debug", "migrate-accounts-to-oidc", db_uri, "--domain", "example.com"],
    )

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "alice  ->  alice@example.com" in result.output
    # Surfaces the IdP-email-mismatch reminder so the operator verifies
    # the targets match what their IdP returns before committing.
    assert "must match the email your IdP returns" in result.output
    # No --commit → unchanged.
    assert account_store.get_user("alice") is not None
    assert account_store.get_user("alice@example.com") is None


def test_cli_commit_applies(db_uri: str) -> None:
    """``--commit`` applies the remap."""
    account_store = SqlAlchemyAccountStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"), is_admin=True)

    result = CliRunner().invoke(
        cli,
        ["debug", "migrate-accounts-to-oidc", db_uri, "--domain", "example.com", "--commit"],
    )

    assert result.exit_code == 0, result.output
    assert "COMMITTED" in result.output
    assert account_store.get_user("alice") is None
    assert account_store.get_user("alice@example.com") is not None


def test_cli_requires_a_mapping(db_uri: str) -> None:
    """With neither --domain nor --map, the command errors (nothing to do)."""
    result = CliRunner().invoke(cli, ["debug", "migrate-accounts-to-oidc", db_uri])
    assert result.exit_code != 0
    assert "nothing to migrate" in result.output


def test_cli_map_overrides_domain(db_uri: str) -> None:
    """An explicit --map pair wins over the --domain-derived mapping."""
    account_store = SqlAlchemyAccountStore(db_uri)
    account_store.create_user_with_password("alice", hash_password("password123"))

    result = CliRunner().invoke(
        cli,
        [
            "debug",
            "migrate-accounts-to-oidc",
            db_uri,
            "--domain",
            "example.com",
            "--map",
            "alice=alice@corp.com",
            "--commit",
        ],
    )

    assert result.exit_code == 0, result.output
    assert account_store.get_user("alice@corp.com") is not None
    assert account_store.get_user("alice@example.com") is None


@pytest.mark.parametrize("bad", ["aliceonly", "=new", "old="])
def test_cli_rejects_malformed_map(db_uri: str, bad: str) -> None:
    """``--map`` without a valid OLD=NEW shape is rejected."""
    result = CliRunner().invoke(cli, ["debug", "migrate-accounts-to-oidc", db_uri, "--map", bad])
    assert result.exit_code != 0
