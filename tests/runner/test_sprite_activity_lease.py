"""Sprites adapter tests for the runner activity lease."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.activity_lease import (
    _ACTIVITY_LEASE_FACTORIES,
    ACTIVITY_LEASE_PROVIDER_ENV_VAR,
    ActivityLeaseError,
    activity_lease_from_env,
)
from omnigent.runner.activity_leases.sprites import (
    SpriteActivityLease,
    sprite_activity_task_name,
)

pytestmark = pytest.mark.asyncio


async def test_registration_selects_sprites_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider layer registers its adapter with the shared framework."""
    monkeypatch.setenv(ACTIVITY_LEASE_PROVIDER_ENV_VAR, "sprites")
    try:
        lease = activity_lease_from_env("runner-123")
        assert isinstance(lease, SpriteActivityLease)
        await lease.aclose()
    finally:
        _ACTIVITY_LEASE_FACTORIES.pop("sprites", None)


async def test_task_name_sanitizes_runner_id() -> None:
    """Delegated runner ids become valid Sprite task names."""
    assert sprite_activity_task_name("runner_token_34D104") == "omnigent-runner-token-34d104"


async def test_refresh_and_release_use_sprite_tasks_api() -> None:
    """The adapter translates generic lease operations to local task calls."""
    requests: list[tuple[str, str, object | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        return httpx.Response(204 if request.method == "DELETE" else 200)

    async with httpx.AsyncClient(
        base_url="http://sprite",
        transport=httpx.MockTransport(handler),
    ) as client:
        lease = SpriteActivityLease("omnigent-runner-123", client=client)
        await lease.refresh()
        await lease.release()
        await lease.aclose()

    assert requests == [
        ("PUT", "/v1/tasks/omnigent-runner-123", {"expire": "5m"}),
        ("DELETE", "/v1/tasks/omnigent-runner-123", None),
    ]


async def test_missing_task_is_already_released() -> None:
    """A task that expired independently makes release idempotent."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(
        base_url="http://sprite",
        transport=httpx.MockTransport(handler),
    ) as client:
        await SpriteActivityLease("missing", client=client).release()


async def test_provider_transport_failure_is_normalized() -> None:
    """HTTP failures surface through the provider-neutral exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(
        base_url="http://sprite",
        transport=httpx.MockTransport(handler),
    ) as client:
        lease = SpriteActivityLease("broken", client=client)
        with pytest.raises(ActivityLeaseError, match="refresh"):
            await lease.refresh()
