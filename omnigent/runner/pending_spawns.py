"""Reconcile named child admission by immutable attempt identity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx


class PendingSpawnError(ValueError):
    """An uncertain attempt must never fall back to ordinary creation or deletion."""


class PendingSpawnNameConflict(PendingSpawnError):
    """The server confirmed that no child was reserved for this title."""


@dataclass
class PendingSpawnAttempt:
    parent_id: str
    epoch: int
    nonce: str
    child_id: str | None = None
    state: str = "unknown"
    data: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        return f"/v1/sessions/{self.parent_id}/spawns/{self.nonce}"

    def accept(self, response: httpx.Response) -> None:
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or (
            type(data.get("epoch")) is not int
            or data.get("epoch") != self.epoch
            or data.get("nonce") != self.nonce
            or data.get("state") not in {"pending", "admitted", "aborting", "aborted", "unknown"}
        ):
            raise PendingSpawnError("Server returned a mismatched child spawn receipt")
        state = data["state"]
        if state != "unknown":
            child = data.get("child_id")
            if not isinstance(child, str) or UUID(child).hex != child:
                raise PendingSpawnError("Server returned an invalid reserved child ID")
            if self.child_id is not None and child != self.child_id:
                raise PendingSpawnError("Reserved child ID changed during reconciliation")
            self.child_id = child
        self.state = state
        self.data = data

    async def reconcile(self, client: httpx.AsyncClient) -> None:
        self.accept(await client.get(self.url, params={"epoch": self.epoch}, timeout=30.0))

    async def abort(self, client: httpx.AsyncClient) -> bool:
        """Only a server-confirmed aborted receipt permits dropping a work handle."""
        try:
            self.accept(
                await client.post(f"{self.url}/abort", json={"epoch": self.epoch}, timeout=30.0)
            )
        except (httpx.HTTPError, ValueError):
            try:
                await self.reconcile(client)
            except (httpx.HTTPError, ValueError):
                return False
        return self.state == "aborted"

    async def admit(self, client: httpx.AsyncClient, content: list[dict[str, Any]]) -> None:
        try:
            self.accept(
                await client.post(
                    f"{self.url}/input",
                    json={
                        "epoch": self.epoch,
                        "input": {"type": "message", "data": {"role": "user", "content": content}},
                    },
                    timeout=30.0,
                )
            )
        except (httpx.HTTPError, ValueError) as exc:
            try:
                await self.reconcile(client)
            except (httpx.HTTPError, ValueError):
                raise PendingSpawnError(self.retained_message()) from exc
            if self.state != "admitted":
                raise PendingSpawnError(self.retained_message()) from exc
        if self.state != "admitted":
            raise PendingSpawnError(self.retained_message())

    def retained_message(self) -> str:
        return (
            f"Child spawn {self.nonce!r} in epoch {self.epoch} has state {self.state!r}; "
            f"reserved child {self.child_id!r}. Retain this attempt and reconcile its receipt "
            "before retrying."
        )


async def prepare_pending_spawn(
    client: httpx.AsyncClient,
    *,
    parent_id: str,
    create: dict[str, Any],
    file_ids: list[str],
    cost_budget: dict[str, Any] | None,
) -> PendingSpawnAttempt | None:
    """Return None only when the nonmutating capability probe confirms no support."""
    try:
        info = await client.get("/v1/info", timeout=30.0)
        if info.status_code in (404, 405, 501):
            return None
        info.raise_for_status()
        advertised = info.json()
        if not isinstance(advertised, dict):
            raise PendingSpawnError("Server returned invalid spawn capabilities")
        capability = advertised.get("pending_child_spawn_v1")
        if capability is None or capability is False:
            return None
        if capability is not True:
            raise PendingSpawnError("Server returned an invalid spawn capability")
        epoch_response = await client.get(f"/v1/sessions/{parent_id}/spawns/epoch", timeout=30.0)
        if epoch_response.status_code in (404, 405, 501):
            return None
        epoch_response.raise_for_status()
        epoch_data = epoch_response.json()
        epoch = epoch_data.get("epoch") if isinstance(epoch_data, dict) else None
        if type(epoch) is not int or not 1 <= epoch < 2**63:
            raise PendingSpawnError("Server returned an invalid spawn epoch")
    except (httpx.HTTPError, ValueError) as exc:
        raise PendingSpawnError("Could not determine child spawn support or epoch") from exc
    attempt = PendingSpawnAttempt(UUID(parent_id).hex, epoch, uuid4().hex)
    payload = {"epoch": epoch, "nonce": attempt.nonce, "create": create, "file_ids": file_ids}
    if cost_budget is not None:
        payload["cost_budget"] = cost_budget
    try:
        response = await client.post(
            f"/v1/sessions/{attempt.parent_id}/spawns", json=payload, timeout=30.0
        )
        if response.status_code == 409:
            rejected = response.json()
            if isinstance(rejected, dict) and (
                type(rejected.get("epoch")) is int
                and rejected.get("epoch") == epoch
                and rejected.get("nonce") == attempt.nonce
                and rejected.get("state") == "rejected"
                and rejected.get("reason") == "name_conflict"
                and rejected.get("child_id") is None
            ):
                raise PendingSpawnNameConflict("Child name is already reserved")
        attempt.accept(response)
    except PendingSpawnNameConflict:
        raise
    except asyncio.CancelledError:
        await asyncio.shield(attempt.abort(client))
        raise
    except (httpx.HTTPError, ValueError) as exc:
        try:
            await attempt.reconcile(client)
        except (httpx.HTTPError, ValueError):
            raise PendingSpawnError(attempt.retained_message()) from exc
        if attempt.state not in ("pending", "admitted"):
            raise PendingSpawnError(attempt.retained_message()) from exc
    if attempt.state not in ("pending", "admitted") or attempt.child_id is None:
        raise PendingSpawnError(attempt.retained_message())
    return attempt
