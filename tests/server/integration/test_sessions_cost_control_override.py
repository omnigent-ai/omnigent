"""Integration tests for the session-scoped ``cost_control_mode_override`` column.

Mirrors ``test_sessions_model_override.py``: PATCH writes the column,
the snapshot reads it back, and create-time values land before the
first turn. The clearing contract differs from ``model_override``:
``"off"`` is a real stored value, so the clear signal is an explicit
JSON ``null`` (field present) rather than a clear alias — an omitted
field leaves the stored value unchanged.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
) -> dict[str, Any]:
    """
    Create a bare session and return the JSON body.

    :param client: The test HTTP client.
    :param agent_id: Agent id to bind, e.g. ``"ag_abc123"``.
    :returns: The session response body.
    """
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "initial_items": []},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.parametrize("mode", ["on", "off"])
async def test_patch_cost_control_override_round_trips_through_snapshot(
    client: httpx.AsyncClient,
    mode: str,
) -> None:
    """PATCH writes the column and ``GET`` returns the same value.

    This is the contract the web "Cost Optimized" toggle depends
    on: the PATCH response hydrates the optimistic store state, and
    the next snapshot (reload, another client) must agree with it.

    :param mode: The override value under test, ``"on"`` or ``"off"``.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Fresh sessions have no override — the spec default applies.
    assert session.get("cost_control_mode_override") is None

    patch = await client.patch(
        f"/v1/sessions/{sid}",
        json={"cost_control_mode_override": mode},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["cost_control_mode_override"] == mode

    # GET reflects the new value — what the advisor pipeline and a
    # second client read.
    get = await client.get(f"/v1/sessions/{sid}")
    assert get.status_code == 200
    assert get.json()["cost_control_mode_override"] == mode


async def test_patch_cost_control_override_explicit_null_clears(
    client: httpx.AsyncClient,
) -> None:
    """An explicit JSON ``null`` clears the override back to unset.

    ``"off"`` is a real stored value for this field, so (unlike
    ``model_override``'s ``"default"`` alias) the only clear path is
    sending the field with a ``null`` value.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    seed = await client.patch(
        f"/v1/sessions/{sid}",
        json={"cost_control_mode_override": "on"},
    )
    assert seed.json()["cost_control_mode_override"] == "on"

    clear = await client.patch(
        f"/v1/sessions/{sid}",
        json={"cost_control_mode_override": None},
    )
    assert clear.status_code == 200, clear.text
    # None (not "on") proves the explicit null reached the unset path
    # rather than being read as "field absent, leave unchanged".
    assert clear.json()["cost_control_mode_override"] is None

    get = await client.get(f"/v1/sessions/{sid}")
    assert get.json()["cost_control_mode_override"] is None


async def test_patch_without_field_leaves_override_unchanged(
    client: httpx.AsyncClient,
) -> None:
    """A PATCH that omits the field must not clear a stored override.

    The clear signal is field *presence* with a null value, so this
    pins the other half of that contract: unrelated PATCHes (title
    edits, runner binds) leave the stored switch alone. A regression
    here would silently reset cost control on every rename.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    await client.patch(
        f"/v1/sessions/{sid}",
        json={"cost_control_mode_override": "off"},
    )

    rename = await client.patch(
        f"/v1/sessions/{sid}",
        json={"title": "unrelated rename"},
    )
    assert rename.status_code == 200, rename.text
    # Still "off" — the omitted field did not take the clear path.
    assert rename.json()["cost_control_mode_override"] == "off"


@pytest.mark.parametrize(
    "bad_value",
    [
        pytest.param("", id="empty-string"),
        pytest.param("On", id="wrong-case"),
        pytest.param("true", id="boolean-ish"),
        pytest.param("optimize", id="spec-mode-not-switch"),
        # "default" clears model_override but is NOT valid here — the
        # clear path for this field is an explicit null.
        pytest.param("default", id="model-override-clear-alias"),
    ],
)
async def test_patch_cost_control_override_rejects_invalid(
    client: httpx.AsyncClient,
    bad_value: str,
) -> None:
    """Values outside ``on`` / ``off`` fail loud with 400.

    The persisted value gates the advisor pipeline's behavior, so a
    typo must not silently persist as a string consumers ignore.

    :param bad_value: The malformed override under test.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.patch(
        f"/v1/sessions/{sid}",
        json={"cost_control_mode_override": bad_value},
    )
    assert resp.status_code == 400, (
        f"cost_control_mode_override {bad_value!r} should 400, got {resp.status_code}: {resp.text}"
    )

    # The rejected PATCH must not have mutated the row.
    get = await client.get(f"/v1/sessions/{sid}")
    assert get.json()["cost_control_mode_override"] is None


@pytest.mark.parametrize("mode", ["on", "off"])
async def test_create_session_with_cost_control_override_persists(
    client: httpx.AsyncClient,
    mode: str,
) -> None:
    """Create-time override lands on the row and the snapshot.

    The new-session dialog sets the switch before the first turn, so
    the value must be persisted by the time the create returns — the
    advisor pipeline reads it from the very first turn.

    :param mode: The override value under test, ``"on"`` or ``"off"``.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "cost_control_mode_override": mode,
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    # The create response itself must carry the override — the web UI
    # navigates straight into the session using this snapshot shape.
    assert created["cost_control_mode_override"] == mode

    get = await client.get(f"/v1/sessions/{created['id']}")
    assert get.status_code == 200
    assert get.json()["cost_control_mode_override"] == mode


async def test_create_session_rejects_invalid_cost_control_override(
    client: httpx.AsyncClient,
) -> None:
    """Create with a malformed switch 400s and creates no session."""
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "cost_control_mode_override": "frugal",
        },
    )
    assert resp.status_code == 400, (
        f"cost_control_mode_override 'frugal' should 400, got {resp.status_code}: {resp.text}"
    )
