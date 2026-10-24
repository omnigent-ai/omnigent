"""Acknowledged persistence of a harness-owned session reference."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, Field


class NativeSessionCheckpointRequest(BaseModel):
    """Internal harness request; the runner keeps control-plane credentials."""

    type: Literal["native_session.checkpoint_requested"] = "native_session.checkpoint_requested"
    checkpoint_id: str = Field(min_length=1, max_length=128)
    native_session_id: str = Field(min_length=1, max_length=1024)
    sequence_number: int | None = None


class NativeSessionCheckpointAck(BaseModel):
    """Reply delivered on the harness's existing session event channel."""

    type: Literal["native_session_checkpoint"] = "native_session_checkpoint"
    checkpoint_id: str = Field(min_length=1, max_length=128)
    success: bool
    error: str | None = None


async def read_native_session_reference(
    server_client: httpx.AsyncClient, conversation_id: str
) -> str | None:
    """Read fresh metadata so a reset or interrupted write cannot leave stale state."""
    response = await server_client.get(
        f"/v1/sessions/{conversation_id}",
        params={"include_items": "false", "include_liveness": "false", "include_usage": "false"},
        timeout=15.0,
    )
    response.raise_for_status()
    try:
        snapshot = response.json()
    except ValueError as exc:
        raise ValueError("Invalid native session metadata.") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("Invalid native session metadata.")
    reference = snapshot.get("external_session_id")
    if reference is not None and (
        not isinstance(reference, str) or not reference.strip() or len(reference) > 1024
    ):
        raise ValueError("Invalid native session reference.")
    return reference


async def checkpoint_native_session(
    server_client: httpx.AsyncClient,
    harness_client: httpx.AsyncClient,
    conversation_id: str,
    request: NativeSessionCheckpointRequest,
) -> bool:
    """Persist through the existing session API, then acknowledge the result."""
    success = False
    try:
        response = await server_client.patch(
            f"/v1/sessions/{conversation_id}",
            json={"external_session_id": request.native_session_id},
            timeout=15.0,
        )
        response.raise_for_status()
        snapshot = response.json()
        success = (
            isinstance(snapshot, dict)
            and snapshot.get("external_session_id") == request.native_session_id
        )
    except (httpx.HTTPError, ValueError):
        # Error bodies may contain operator configuration or credentials.
        pass
    acknowledgement = NativeSessionCheckpointAck(
        checkpoint_id=request.checkpoint_id,
        success=success,
        error=None
        if success
        else ("Could not persist the native session reference; no prompt was sent."),
    )
    response = await harness_client.post(
        f"/v1/sessions/{conversation_id}/events",
        json=acknowledgement.model_dump(exclude_none=True),
        timeout=15.0,
    )
    response.raise_for_status()
    return success
