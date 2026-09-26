"""A native web re-send (same ``stable_id``) must not paste the prompt again.

The web client re-sends a message with the same ``stable_id`` when it never
saw the POST response. On a native-terminal session the message was already
typed into the pane, so a re-dispatch duplicates it in the transcript. The
dispatch pre-check answers a still-pending submission with its ``pending_id``
and a committed one with the mirrored item — via the durable ``web_stable_id``
mapping, so the dedup survives a server restart that wipes the in-memory
pending index.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions.helpers import _NativeTerminalEnsureOutcome
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_STABLE_ID = "ab" * 16


@pytest.fixture(autouse=True)
def _clean_pending_inputs() -> Any:
    pending_inputs.reset_for_tests()
    yield
    pending_inputs.reset_for_tests()


@pytest.fixture
def forwards() -> list[httpx.Request]:
    """Message forwards (`/events` POSTs) the dispatch sent to the mocked runner."""
    return []


@pytest.fixture
async def native_session(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    forwards: list[httpx.Request],
) -> Any:
    """A native-terminal session whose runner is a recording mock."""
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        sessions_module,
        "_ensure_native_terminal_ready",
        AsyncMock(return_value=_NativeTerminalEnsureOutcome(error=None)),
    )
    monkeypatch.setattr(
        sessions_module, "_ensure_runner_session_initialized", AsyncMock(return_value=True)
    )

    def _record(request: httpx.Request) -> httpx.Response:
        # The runner also serves session/terminal setup calls; only the
        # `/events` POST types the message into the pane.
        if request.url.path.endswith("/events"):
            forwards.append(request)
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_record), base_url="http://runner"
    ) as runner:
        monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=runner))
        monkeypatch.setattr(
            "omnigent.server.routes._sessions.orchestration._get_runner_client",
            AsyncMock(return_value=runner),
        )
        agent = await create_test_agent(client, name="native-resend-dedup")
        created = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "labels": {
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "claude-code-native-ui",
                },
            },
        )
        assert created.status_code == 201, created.text
        yield created.json()["id"]


async def _post_message(
    client: httpx.AsyncClient, session_id: str, text: str, stable_id: str
) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
                "stable_id": stable_id,
            },
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _echo_mirror(
    client: httpx.AsyncClient, session_id: str, text: str, source_id: str | None
) -> None:
    """Mirror the pasted message back, as the transcript forwarder does."""
    data: dict[str, Any] = {
        "item_type": "message",
        "item_data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        "response_id": "resp_echo",
    }
    if source_id is not None:
        data["source_id"] = source_id
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_conversation_item", "data": data},
    )
    assert resp.status_code == 202, resp.text


async def _user_message_items(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    return [i for i in items if i.get("type") == "message" and i.get("role") == "user"]


async def test_resend_with_a_live_pending_entry_forwards_once(
    client: httpx.AsyncClient,
    native_session: str,
    forwards: list[httpx.Request],
) -> None:
    """Before the mirror lands, a re-send is answered with the same pending_id."""
    first = await _post_message(client, native_session, "hello pane", _STABLE_ID)
    assert first.get("pending_id")
    assert len(forwards) == 1

    second = await _post_message(client, native_session, "hello pane", _STABLE_ID)
    assert second.get("pending_id") == first["pending_id"]
    assert len(forwards) == 1


@pytest.mark.parametrize("source_id", [None, "rec-1:0:message"])
async def test_resend_after_restart_resolves_the_committed_mirror(
    client: httpx.AsyncClient,
    native_session: str,
    forwards: list[httpx.Request],
    source_id: str | None,
) -> None:
    """The reported bug: a re-send after the in-memory index is wiped must not paste again.

    Covers both mirror shapes — committed directly under the stable id (no
    ``source_id``) and under a forwarder-derived id with the durable
    ``web_stable_id`` mapping alongside.
    """
    await _post_message(client, native_session, "restart probe", _STABLE_ID)
    await _echo_mirror(client, native_session, "restart probe", source_id)
    assert not pending_inputs.has_pending(native_session)
    [committed] = await _user_message_items(client, native_session)

    # A restart wipes the process-local pending index.
    pending_inputs.reset_for_tests()

    resend = await _post_message(client, native_session, "restart probe", _STABLE_ID)
    assert resend.get("item_id") == committed["id"]
    assert resend.get("pending_id") is None
    assert len(forwards) == 1
    assert len(await _user_message_items(client, native_session)) == 1


async def test_a_new_stable_id_still_forwards(
    client: httpx.AsyncClient,
    native_session: str,
    forwards: list[httpx.Request],
) -> None:
    """The dedup only answers repeats; a fresh submission reaches the pane."""
    await _post_message(client, native_session, "first", _STABLE_ID)
    await _echo_mirror(client, native_session, "first", "rec-1:0:message")

    await _post_message(client, native_session, "second", "cd" * 16)
    assert len(forwards) == 2
