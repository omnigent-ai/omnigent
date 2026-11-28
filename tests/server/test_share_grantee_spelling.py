"""A share reaches the account that signs in under the grantee's canonical name.

Accounts sign-in trims and lowercases the username, so a grantee typed as
``Alice`` or `` alice `` must reach the user who signs in as ``alice``.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from omnigent.server.auth import LEVEL_MANAGE, LEVEL_READ
from tests.server.test_account_revocation import (  # noqa: F401
    _create_schedule,
    create_session,
    setup_app,
)

_GRANTEE_SPELLINGS = ["alice", "Alice", " alice "]


@pytest.mark.parametrize("grantee", _GRANTEE_SPELLINGS)
def test_session_share_reaches_the_signed_in_account(setup_app, grantee):  # noqa: F811
    admin, alice, _, _ = setup_app
    session_id = create_session(admin).json()["session_id"]
    shared = admin.put(
        f"/v1/sessions/{session_id}/permissions", json={"user_id": grantee, "level": 1}
    )
    assert shared.status_code == 200, shared.text
    assert alice.get("/auth/me").json()["id"] == "alice"
    assert alice.get(f"/v1/sessions/{session_id}").status_code == 200


@pytest.mark.parametrize("setup_app", [True], indirect=True)
@pytest.mark.parametrize("grantee", _GRANTEE_SPELLINGS)
def test_automation_share_reaches_the_signed_in_account(setup_app, grantee):  # noqa: F811
    admin, alice, _, _ = setup_app
    task_id = _create_schedule(admin)
    shared = admin.put(
        f"/v1/scheduled-tasks/{task_id}/permissions", json={"user_id": grantee, "level": 1}
    )
    assert shared.status_code == 200, shared.text
    listed = alice.get("/v1/scheduled-tasks/shared")
    assert listed.status_code == 200, listed.text
    assert [task["id"] for task in listed.json()["scheduled_tasks"]] == [task_id]


@pytest.mark.parametrize("grantee", _GRANTEE_SPELLINGS)
def test_session_share_is_revoked_in_the_spelling_it_was_granted_in(setup_app, grantee):  # noqa: F811
    admin, alice, _, _ = setup_app
    session_id = create_session(admin).json()["session_id"]
    path = f"/v1/sessions/{session_id}/permissions"
    assert admin.put(path, json={"user_id": grantee, "level": 1}).status_code == 200
    revoked = admin.delete(f"{path}/{quote(grantee)}")
    assert revoked.status_code == 204, revoked.text
    assert [grant["user_id"] for grant in admin.get(path).json()["permissions"]] == ["admin"]
    assert alice.get(f"/v1/sessions/{session_id}").status_code == 404


def _share_with_a_legacy_spelling(app, legacy_id: str, alice_level: int) -> str:
    admin, _, stores, _ = app
    session_id = create_session(admin).json()["session_id"]
    path = f"/v1/sessions/{session_id}/permissions"
    stores["permission_store"].ensure_user(legacy_id)
    stores["permission_store"].grant(legacy_id, session_id, LEVEL_READ)
    assert admin.put(path, json={"user_id": "alice", "level": alice_level}).status_code == 200
    return path


@pytest.mark.parametrize(
    ("caller", "alice_level", "legacy_id"),
    [
        ("admin", LEVEL_READ, "Alice"),
        ("admin", LEVEL_READ, "Admin"),
        ("alice", LEVEL_MANAGE, "Alice"),
    ],
)
def test_session_revoke_removes_a_legacy_mixed_case_grant_as_given(
    setup_app,  # noqa: F811
    caller,
    alice_level,
    legacy_id,
):
    admin, alice, _, _ = setup_app
    path = _share_with_a_legacy_spelling(setup_app, legacy_id, alice_level)
    revoked = {"admin": admin, "alice": alice}[caller].delete(f"{path}/{legacy_id}")
    assert revoked.status_code == 204, revoked.text
    listed = admin.get(path).json()["permissions"]
    assert sorted(grant["user_id"] for grant in listed) == ["admin", "alice"]


def test_read_only_grantee_cannot_revoke_a_legacy_grant_in_another_spelling(setup_app):  # noqa: F811
    admin, alice, _, _ = setup_app
    path = _share_with_a_legacy_spelling(setup_app, "Alice", LEVEL_READ)
    assert alice.delete(f"{path}/Alice").status_code == 403
    listed = admin.get(path).json()["permissions"]
    assert sorted(grant["user_id"] for grant in listed) == ["Alice", "admin", "alice"]


@pytest.mark.parametrize("spelling", _GRANTEE_SPELLINGS)
def test_read_only_grantee_leaves_a_session_in_any_spelling_of_their_name(setup_app, spelling):  # noqa: F811
    admin, alice, _, _ = setup_app
    session_id = create_session(admin).json()["session_id"]
    path = f"/v1/sessions/{session_id}/permissions"
    assert admin.put(path, json={"user_id": "alice", "level": 1}).status_code == 200
    left = alice.delete(f"{path}/{quote(spelling)}")
    assert left.status_code == 204, left.text
    assert [grant["user_id"] for grant in admin.get(path).json()["permissions"]] == ["admin"]
    assert alice.get(f"/v1/sessions/{session_id}").status_code == 404


@pytest.mark.parametrize("setup_app", [True], indirect=True)
@pytest.mark.parametrize("grantee", _GRANTEE_SPELLINGS)
def test_automation_share_is_revoked_in_the_spelling_it_was_granted_in(setup_app, grantee):  # noqa: F811
    admin, alice, _, _ = setup_app
    path = f"/v1/scheduled-tasks/{_create_schedule(admin)}/permissions"
    assert admin.put(path, json={"user_id": grantee, "level": 1}).status_code == 200
    revoked = admin.delete(f"{path}/{quote(grantee)}")
    assert revoked.status_code == 204, revoked.text
    assert admin.get(path).json()["permissions"] == []
    assert alice.get("/v1/scheduled-tasks/shared").json()["scheduled_tasks"] == []
