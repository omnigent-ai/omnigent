"""A share reaches the account that signs in under the grantee's canonical name.

Accounts sign-in trims and lowercases the username, so a grantee typed as
``Alice`` or `` alice `` must reach the user who signs in as ``alice``.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

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
