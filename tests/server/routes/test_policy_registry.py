"""Tests for the policy registry route (``GET /v1/policy-registry``)."""

from __future__ import annotations

import httpx


async def test_list_policy_registry(client: httpx.AsyncClient) -> None:
    """GET /v1/policy-registry returns a list of registered handlers."""
    resp = await client.get("/v1/policy-registry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


async def test_policy_registry_entry_shape(client: httpx.AsyncClient) -> None:
    """Each registry entry has handler, description, and params_schema fields."""
    resp = await client.get("/v1/policy-registry")
    assert resp.status_code == 200
    entries = resp.json()["data"]
    if entries:  # registry may be empty if no policy modules are loaded
        for entry in entries:
            assert "handler" in entry
            assert "description" in entry
            assert "params_schema" in entry


async def test_internal_only_policies_excluded_from_registry(
    client: httpx.AsyncClient,
) -> None:
    """
    Internal-only policies (internal_only=True) are excluded from the registry.

    When the policy registry is populated with policies that have
    internal_only=True, they should be filtered out in the GET /v1/policy-registry
    response. This ensures internal-only policies remain in the validation
    allowlist (for sys_session_send to attach them) but don't appear in the
    UI's policy selector.
    """
    from omnigent.policies.registry import get_registry, load_registry

    # Ensure the registry is loaded with all built-in policies.
    load_registry()
    all_entries = get_registry()

    # Get the response from the API.
    resp = await client.get("/v1/policy-registry")
    assert resp.status_code == 200
    public_entries = resp.json()["data"]

    # Extract public handler paths from the API response.
    public_handlers = {entry["handler"] for entry in public_entries}

    # Any internal_only policies should be in the full registry but NOT
    # in the public API response.
    internal_only_handlers = {e.handler for e in all_entries if e.internal_only}

    # Verify internal_only policies are excluded from the public API.
    assert internal_only_handlers.isdisjoint(public_handlers), (
        f"Internal-only policies {internal_only_handlers & public_handlers} "
        "should not appear in the public registry"
    )

    # Verify at least one internal_only policy exists (to make the test meaningful).
    assert len(internal_only_handlers) > 0, (
        "Registry should contain at least one internal_only policy"
    )
