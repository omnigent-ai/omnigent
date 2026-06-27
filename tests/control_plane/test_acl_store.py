"""Unit tests for the agent visibility + ACL store.

Covers round-trip persistence, the implicit org default for ungoverned
agents, audience set-semantics, the ``can_view`` / ``can_manage``
predicates (incl. the denial paths), and the DB constraint on the ACL
``level`` column (the new-type validation the build contract requires).
"""

from __future__ import annotations

import pytest

from control_plane.acl_store import AgentAclStore, group_principal
from control_plane.models import (
    VISIBILITY_ORG,
    VISIBILITY_RESTRICTED,
    create_control_plane_tables,
)
from omnigent.db.utils import get_or_create_engine


@pytest.fixture
def store(db_uri: str) -> AgentAclStore:
    """An AgentAclStore on a per-test SQLite DB with CP tables created."""
    create_control_plane_tables(get_or_create_engine(db_uri))
    return AgentAclStore(db_uri)


def test_unknown_agent_defaults_to_org(store: AgentAclStore) -> None:
    """An agent with no stored row is org-visible and unowned."""
    vis = store.get_visibility("ag_missing")
    assert vis.visibility == VISIBILITY_ORG
    assert vis.owner_id is None
    assert vis.audience_users == ()
    assert vis.audience_groups == ()


def test_set_owner_then_restrict_round_trip(store: AgentAclStore) -> None:
    """Owner + restricted audience persist and read back normalized."""
    store.set_owner("ag_1", "Alice@DB.com", visibility=VISIBILITY_ORG)
    vis = store.set_visibility(
        "ag_1",
        VISIBILITY_RESTRICTED,
        audience_users=["Bob@x.com", " "],
        audience_groups=["FSI-Team"],
    )
    assert vis.visibility == VISIBILITY_RESTRICTED
    assert vis.owner_id == "Alice@DB.com"  # owner preserved verbatim
    assert vis.audience_users == ("bob@x.com",)  # normalized + blank dropped
    assert vis.audience_groups == ("fsi-team",)

    # Re-read from a fresh store instance to prove durability.
    again = AgentAclStore(store.storage_location).get_visibility("ag_1")
    assert again.audience_users == ("bob@x.com",)
    assert again.audience_groups == ("fsi-team",)


def test_set_visibility_replaces_audience(store: AgentAclStore) -> None:
    """Setting visibility replaces the entire audience (set semantics)."""
    store.set_visibility("ag_1", VISIBILITY_RESTRICTED, audience_users=["a@x.com", "b@x.com"])
    updated = store.set_visibility("ag_1", VISIBILITY_RESTRICTED, audience_users=["c@x.com"])
    assert updated.audience_users == ("c@x.com",)


def test_org_visibility_clears_audience(store: AgentAclStore) -> None:
    """Switching back to org drops any prior restricted audience."""
    store.set_visibility("ag_1", VISIBILITY_RESTRICTED, audience_users=["a@x.com"])
    back = store.set_visibility("ag_1", VISIBILITY_ORG)
    assert back.visibility == VISIBILITY_ORG
    assert back.audience_users == ()


def test_invalid_visibility_raises(store: AgentAclStore) -> None:
    """An unknown visibility mode is rejected before any write."""
    with pytest.raises(ValueError, match="unknown visibility"):
        store.set_visibility("ag_1", "public")


def test_batch_map_covers_all_ids(store: AgentAclStore) -> None:
    """get_visibility_map returns an entry for every requested id."""
    store.set_visibility("ag_1", VISIBILITY_RESTRICTED, audience_groups=["g1"])
    m = store.get_visibility_map(["ag_1", "ag_2", "ag_missing"])
    assert set(m) == {"ag_1", "ag_2", "ag_missing"}
    assert m["ag_1"].visibility == VISIBILITY_RESTRICTED
    assert m["ag_2"].visibility == VISIBILITY_ORG


def test_delete_agent_removes_records(store: AgentAclStore) -> None:
    """delete_agent clears both the visibility row and the ACL audience."""
    store.set_visibility("ag_1", VISIBILITY_RESTRICTED, audience_users=["a@x.com"])
    store.delete_agent("ag_1")
    vis = store.get_visibility("ag_1")
    assert vis.visibility == VISIBILITY_ORG
    assert vis.owner_id is None


# ── can_view predicate (incl. denial paths) ──────────────────────


def _restricted(store: AgentAclStore) -> None:
    store.set_owner("ag_r", "owner@x.com")
    store.set_visibility(
        "ag_r",
        VISIBILITY_RESTRICTED,
        audience_users=["bob@x.com"],
        audience_groups=["fsi"],
        owner_id="owner@x.com",
    )


def test_can_view_org_agent_visible_to_all(store: AgentAclStore) -> None:
    vis = store.get_visibility("ag_org")  # default org
    assert AgentAclStore.can_view(vis, user_id="anyone@x.com", groups=frozenset(), is_admin=False)


def test_can_view_restricted_allows_owner_audience_group(store: AgentAclStore) -> None:
    _restricted(store)
    vis = store.get_visibility("ag_r")
    assert AgentAclStore.can_view(vis, user_id="owner@x.com", groups=frozenset(), is_admin=False)
    assert AgentAclStore.can_view(vis, user_id="bob@x.com", groups=frozenset(), is_admin=False)
    assert AgentAclStore.can_view(
        vis, user_id="carol@x.com", groups=frozenset({"fsi"}), is_admin=False
    )


def test_can_view_restricted_denies_outsider(store: AgentAclStore) -> None:
    """The core denial: a non-audience, non-owner, non-admin is denied."""
    _restricted(store)
    vis = store.get_visibility("ag_r")
    assert not AgentAclStore.can_view(
        vis, user_id="dave@x.com", groups=frozenset({"other"}), is_admin=False
    )


def test_can_view_admin_sees_everything(store: AgentAclStore) -> None:
    _restricted(store)
    vis = store.get_visibility("ag_r")
    assert AgentAclStore.can_view(vis, user_id="dave@x.com", groups=frozenset(), is_admin=True)


def test_can_manage_owner_and_admin_only(store: AgentAclStore) -> None:
    _restricted(store)
    vis = store.get_visibility("ag_r")
    assert AgentAclStore.can_manage(vis, user_id="owner@x.com", is_admin=False)
    assert not AgentAclStore.can_manage(vis, user_id="bob@x.com", is_admin=False)
    assert AgentAclStore.can_manage(vis, user_id="bob@x.com", is_admin=True)


# ── DB constraint validation (new tables) ────────────────────────


def test_acl_level_check_constraint(store: AgentAclStore) -> None:
    """The cp_agent_acl.level CHECK rejects out-of-range levels.

    Validates the new table's constraint round-trips through SQLite
    (constraint enforcement is on, matching the build contract's
    requirement to validate any new schema).
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    engine = get_or_create_engine(store.storage_location)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO cp_agent_acl (principal, agent_id, level) "
                    "VALUES ('x@y.com', 'ag_1', 2)"
                )
            )


def test_visibility_mode_check_constraint(store: AgentAclStore) -> None:
    """The cp_agent_visibility.visibility CHECK rejects bad modes."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    engine = get_or_create_engine(store.storage_location)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO cp_agent_visibility "
                    "(agent_id, visibility, created_at) VALUES ('ag_1', 'public', 1)"
                )
            )


def test_group_principal_token() -> None:
    """Group principals carry the documented prefix, normalized."""
    assert group_principal("FSI-Team") == "group:fsi-team"
