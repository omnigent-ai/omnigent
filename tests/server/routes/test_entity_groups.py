"""Tests for the Entity Groups + Entities API (built-in merge, CRUD, icons).

Exercised through the shared ``client`` fixture (single-user mode: no auth
provider). Built-in Jira/GitHub groups + actions are code-owned and merged into
the list/get responses; this guards that merge, the read-only enforcement, group
CRUD, entity grouping, and the custom-icon upload/serve roundtrip.
"""

from __future__ import annotations

import httpx

# ── Built-in merge + read-only ──────────────────────────────────────


async def test_list_groups_includes_builtins_first(client: httpx.AsyncClient) -> None:
    """GET /entity-groups returns the code-owned built-ins (Jira, GitHub) first."""
    resp = await client.get("/v1/entity-groups")
    assert resp.status_code == 200, resp.text
    groups = resp.json()
    builtin = [g for g in groups if g["is_builtin"]]
    names = [g["name"] for g in builtin]
    assert "Jira" in names and "GitHub" in names
    assert {g["id"] for g in builtin} >= {"grp_builtin_jira", "grp_builtin_github"}
    # Built-ins are first.
    assert groups[0]["is_builtin"] is True


async def test_list_entities_includes_builtin_actions(client: httpx.AsyncClient) -> None:
    """GET /entities returns the built-in Jira/GitHub actions with their group_id."""
    resp = await client.get("/v1/entities")
    assert resp.status_code == 200
    entities = resp.json()
    by_id = {e["id"]: e for e in entities}
    assert by_id["ent_builtin_jira_get_ticket"]["group_id"] == "grp_builtin_jira"
    assert by_id["ent_builtin_github_open_pr"]["group_id"] == "grp_builtin_github"
    assert by_id["ent_builtin_jira_get_ticket"]["is_builtin"] is True


async def test_builtin_group_is_read_only(client: httpx.AsyncClient) -> None:
    """PATCH/DELETE on a built-in group is rejected (403)."""
    assert (
        await client.patch("/v1/entity-groups/grp_builtin_jira", json={"name": "X"})
    ).status_code == 403
    assert (await client.delete("/v1/entity-groups/grp_builtin_jira")).status_code == 403


async def test_builtin_entity_is_read_only(client: httpx.AsyncClient) -> None:
    """PATCH/DELETE on a built-in entity is rejected (403)."""
    assert (
        await client.patch("/v1/entities/ent_builtin_jira_get_ticket", json={"title": "X"})
    ).status_code == 403
    assert (
        await client.delete("/v1/entities/ent_builtin_jira_get_ticket")
    ).status_code == 403


async def test_get_builtin_group_and_entity(client: httpx.AsyncClient) -> None:
    """Built-ins are fetchable by id."""
    g = await client.get("/v1/entity-groups/grp_builtin_github")
    assert g.status_code == 200 and g.json()["icon_key"] == "github"
    e = await client.get("/v1/entities/ent_builtin_github_open_pr")
    assert e.status_code == 200 and e.json()["title"] == "Open PR"


# ── Group CRUD + entity grouping ────────────────────────────────────


async def test_group_crud_and_entity_assignment(client: httpx.AsyncClient) -> None:
    """Create a group, assign an entity to it, and read it back."""
    grp = (await client.post("/v1/entity-groups", json={"name": "Deploy"})).json()
    assert grp["id"].startswith("grp_") and grp["is_builtin"] is False

    ent = (
        await client.post(
            "/v1/entities",
            json={"title": "Ship it", "instruction": "deploy", "group_id": grp["id"]},
        )
    ).json()
    assert ent["group_id"] == grp["id"]

    # Move to ungrouped via empty string.
    cleared = (
        await client.patch(f"/v1/entities/{ent['id']}", json={"group_id": ""})
    ).json()
    assert cleared["group_id"] is None


async def test_delete_group_ungroups_entities(client: httpx.AsyncClient) -> None:
    """Deleting a user group leaves its entities ungrouped, not dangling."""
    grp = (await client.post("/v1/entity-groups", json={"name": "Temp"})).json()
    ent = (
        await client.post(
            "/v1/entities",
            json={"title": "E", "instruction": "i", "group_id": grp["id"]},
        )
    ).json()
    assert (await client.delete(f"/v1/entity-groups/{grp['id']}")).status_code == 204
    after = (await client.get(f"/v1/entities/{ent['id']}")).json()
    assert after["group_id"] is None


# ── Icon upload / serve ─────────────────────────────────────────────

_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


async def test_icon_upload_and_serve(client: httpx.AsyncClient) -> None:
    """Uploading an icon sets icon_url; GET serves the bytes with nosniff."""
    grp = (await client.post("/v1/entity-groups", json={"name": "Pic"})).json()
    up = await client.post(
        f"/v1/entity-groups/{grp['id']}/icon",
        files={"file": ("icon.png", _PNG_1PX, "image/png")},
    )
    assert up.status_code == 200, up.text
    assert up.json()["icon_url"] == f"/v1/entity-groups/{grp['id']}/icon"

    served = await client.get(f"/v1/entity-groups/{grp['id']}/icon")
    assert served.status_code == 200
    assert served.content == _PNG_1PX
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"


async def test_icon_upload_rejects_bad_content_type(client: httpx.AsyncClient) -> None:
    """A non-image upload is rejected (400)."""
    grp = (await client.post("/v1/entity-groups", json={"name": "Bad"})).json()
    resp = await client.post(
        f"/v1/entity-groups/{grp['id']}/icon",
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400


async def test_icon_serve_404_when_absent(client: httpx.AsyncClient) -> None:
    """A group with no uploaded icon serves 404."""
    grp = (await client.post("/v1/entity-groups", json={"name": "NoIcon"})).json()
    assert (await client.get(f"/v1/entity-groups/{grp['id']}/icon")).status_code == 404


async def test_icon_upload_rejects_builtin(client: httpx.AsyncClient) -> None:
    """Uploading an icon to a built-in group is rejected (403)."""
    resp = await client.post(
        "/v1/entity-groups/grp_builtin_jira/icon",
        files={"file": ("icon.png", _PNG_1PX, "image/png")},
    )
    assert resp.status_code == 403
