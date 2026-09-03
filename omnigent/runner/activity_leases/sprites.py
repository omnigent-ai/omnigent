"""Sprites Tasks API adapter for the provider-neutral activity lease."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import httpx

from omnigent.runner.activity_lease import (
    ActivityLeaseError,
)
from omnigent.runner.activity_lease import (
    register_activity_lease_provider as _register_activity_lease_provider,
)

_MANAGEMENT_SOCKET = Path("/.sprite/api.sock")
_TASK_EXPIRE = "5m"


def sprite_activity_task_name(runner_id: str) -> str:
    """Return a Sprite Tasks API-compatible name for *runner_id*."""
    normalized = re.sub(r"[^a-z0-9-]+", "-", runner_id.lower()).strip("-")
    return f"omnigent-{normalized or 'runner'}"


class SpriteActivityLease:
    """Activity hold backed by the local Sprites Tasks API."""

    provider = "sprites"

    def __init__(self, task_name: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._task_path = f"/v1/tasks/{quote(task_name, safe='')}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="http://sprite",
            transport=httpx.AsyncHTTPTransport(uds=str(_MANAGEMENT_SOCKET)),
            timeout=5.0,
        )

    @classmethod
    def for_runner(cls, runner_id: str) -> SpriteActivityLease:
        """Build a lease namespaced to a stable runner id."""
        return cls(sprite_activity_task_name(runner_id))

    async def refresh(self) -> None:
        """Create or extend the local Sprite activity task."""
        try:
            response = await self._client.put(
                self._task_path,
                json={"expire": _TASK_EXPIRE},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ActivityLeaseError("Sprites activity task refresh failed") from exc

    async def release(self) -> None:
        """Delete the local Sprite activity task; absence is success."""
        try:
            response = await self._client.delete(self._task_path)
            if response.status_code != 404:
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ActivityLeaseError("Sprites activity task release failed") from exc

    async def aclose(self) -> None:
        """Close an internally owned HTTP client."""
        if self._owns_client:
            await self._client.aclose()


def _sprite_activity_lease_for_runner(runner_id: str) -> SpriteActivityLease:
    """Build the registered Sprites adapter for a runner."""
    return SpriteActivityLease.for_runner(runner_id)


def register_activity_lease_provider() -> None:
    """Register the Sprites adapter with the shared runner lease registry."""
    _register_activity_lease_provider("sprites", _sprite_activity_lease_for_runner)
