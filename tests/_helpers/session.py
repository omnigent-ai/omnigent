"""Bundle and HTTP primitives; callers own session state, checks and cleanup."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO

import httpx


def bundle_files(files: Mapping[str, bytes]) -> bytes:
    """Archive exact member names and bytes, preserving insertion order."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def post_session_bundle(
    post: Callable[..., httpx.Response],
    url: str,
    bundle: bytes | BinaryIO,
    *,
    metadata: Mapping[str, Any] | None = None,
    filename: str = "agent.tar.gz",
    **request_options: Any,
) -> httpx.Response:
    """POST with the caller's client/options; retain its response for exact checks."""
    return post(
        url,
        data={"metadata": json.dumps({} if metadata is None else dict(metadata))},
        files={"bundle": (filename, bundle, "application/gzip")},
        **request_options,
    )


def create_sub_agent_sessions(
    client: httpx.Client, *, agent_id: str, parent_id: str, count: int
) -> list[str]:
    """Create *count* sub-agent sessions of *agent_id* under *parent_id*; return their ids."""
    session_ids = []
    for _ in range(count):
        response = client.post(
            "/v1/sessions", json={"agent_id": agent_id, "parent_session_id": parent_id}
        )
        assert response.status_code == 201, response.text
        session_ids.append(response.json()["id"])
    return session_ids


def bind_session_runner(
    patch: Callable[..., httpx.Response],
    base_url: str,
    session_id: str,
    runner_id: str,
    **request_options: Any,
) -> None:
    """Bind only when requested: this PATCH can start native terminals."""
    response = patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        **request_options,
    )
    response.raise_for_status()


def send_user_text(
    client: httpx.Client,
    *,
    session_id: str,
    text: str,
) -> None:
    """Post text through the session events API using the caller's client."""
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        timeout=30.0,
    )
    resp.raise_for_status()


def marker_turn_prompt(index: int, user_marker: str, assistant_token: str) -> str:
    """Build turn *index*'s prompt: carry a marker, echo a token verbatim.

    :param index: 1-based turn number, e.g. ``3``.
    :param user_marker: Unique token embedded in the user message itself.
    :param assistant_token: Unique token the agent must echo back exactly.
    :returns: The composed prompt text.
    """
    return (
        f"This is turn {index}. Context marker {user_marker}. "
        f"Reply with exactly this token and nothing else: {assistant_token}"
    )


def ordered_message_items(base_url: str, session_id: str) -> list[dict[str, object]]:
    """Return the session's user/assistant message items in render order.

    Filters ``GET /v1/sessions/{id}/items`` to ``type == "message"`` items
    with a ``user`` / ``assistant`` role, preserving the server's position
    order — which is exactly the order the SPA renders bubbles and the TUI
    prints lines.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: Ordered message-item dicts (each with ``role`` + ``content``).
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return [
        item
        for item in resp.json().get("data", [])
        if item.get("type") == "message" and item.get("role") in ("user", "assistant")
    ]


def message_item_text(item: dict[str, object]) -> str:
    """Join every string ``text`` field across a message item's content blocks.

    Works for both user (``input_text``) and assistant (``output_text`` /
    ``text``) blocks, so the same extractor serves both roles.

    :param item: One element from the items API.
    :returns: The concatenated text, or ``""``.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
