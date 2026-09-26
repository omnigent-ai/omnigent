"""Unit tests for :class:`omnigent.harnesses.opencode_native.http_transport.OpenCodeHttpTransport`.

Covers the payload builder + every transport method over an injected fake
``OpenCodeClient`` (the documented ``client_factory`` test seam), so the
opencode-native HTTP/SSE wire surface stays covered without a live server.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.harnesses.opencode_native.http_transport import (
    OpenCodeHttpTransport,
    build_prompt_payload,
)
from omnigent.native.native_server_transport import (
    NativeLaunchConfig,
    NativePermissionDecision,
    NativePrompt,
)

# ── build_prompt_payload ─────────────────────────────────────────────────────


def test_build_prompt_payload_text_only() -> None:
    assert build_prompt_payload("hi", ()) == {"text": "hi", "files": [], "delivery": "steer"}


def test_build_prompt_payload_queue_delivery() -> None:
    assert build_prompt_payload("hi", (), delivery="queue")["delivery"] == "queue"


def test_build_prompt_payload_data_uri_attachments_become_files() -> None:
    body = build_prompt_payload(
        "look",
        (
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {
                "type": "input_file",
                "file_data": "data:application/pdf;base64,BBBB",
                "filename": "a.pdf",
            },
            {"type": "input_file", "url": "data:text/plain;base64,CCCC"},
            {"type": "input_image"},  # no uri → skipped
            {"type": "input_image", "image_url": "https://example.com/cat.png"},  # not inline
        ),
    )
    assert body["files"] == [
        {"uri": "data:image/png;base64,AAAA"},
        {"uri": "data:application/pdf;base64,BBBB", "name": "a.pdf"},
        {"uri": "data:text/plain;base64,CCCC"},
    ]
    assert set(body) == {"text", "files", "delivery"}


# ── transport methods over a fake client ────────────────────────────────────


class _FakeClient:
    """Records protocol calls; returns canned results for the transport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = False
        self.existing: SimpleNamespace | None = None

    async def get_session(self, session_id: str) -> SimpleNamespace | None:
        self.calls.append(("get_session", session_id))
        return self.existing

    async def create_session(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("create_session", kwargs))
        return SimpleNamespace(id="ses_new")

    async def prompt(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("prompt", (session_id, kwargs)))
        return {"id": "msg_1"}

    async def interrupt(self, session_id: str) -> bool:
        self.calls.append(("interrupt", session_id))
        return True

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_messages", session_id))
        return [{"info": {"id": "msg_1"}}]

    async def fork(self, session_id: str, *, before: str | None = None) -> SimpleNamespace:
        self.calls.append(("fork", (session_id, before)))
        return SimpleNamespace(id="ses_fork")

    async def reply_permission(self, request_id: str, reply: Any) -> bool:
        self.calls.append(("reply_permission", (request_id, reply)))
        return True

    async def set_model(
        self,
        session_id: str,
        *,
        provider_id: str,
        model_id: str,
        variant: str | None = None,
    ) -> None:
        self.calls.append(("set_model", (session_id, provider_id, model_id)))

    async def events(self) -> Any:
        self.calls.append(("events", None))
        yield SimpleNamespace(
            id="evt_1", type="message.updated", properties={"k": "v"}, raw={"r": 1}
        )

    async def aclose(self) -> None:
        self.closed = True


def _transport(client: _FakeClient) -> OpenCodeHttpTransport:
    return OpenCodeHttpTransport(client_factory=lambda: client)


def _launch(**kwargs: Any) -> NativeLaunchConfig:
    return NativeLaunchConfig(omnigent_session_id="conv_1", workspace="/w", **kwargs)


async def test_create_session_when_no_external_id() -> None:
    client = _FakeClient()
    sid = await _transport(client).create_or_resume_session(_launch())
    assert sid == "ses_new"
    assert client.closed
    assert ("create_session", {"title": "omnigent:conv_1", "directory": "/w"}) in client.calls


async def test_resume_returns_existing_session() -> None:
    client = _FakeClient()
    client.existing = SimpleNamespace(id="ses_old")
    sid = await _transport(client).create_or_resume_session(_launch(external_session_id="ses_old"))
    assert sid == "ses_old"


async def test_resume_falls_back_to_create_when_session_gone() -> None:
    client = _FakeClient()  # existing is None
    sid = await _transport(client).create_or_resume_session(_launch(external_session_id="gone"))
    assert sid == "ses_new"


async def test_send_prompt_builds_payload_and_closes() -> None:
    client = _FakeClient()
    out = await _transport(client).send_prompt("ses_1", NativePrompt(text="hi"))
    assert out == {"id": "msg_1"}
    assert (
        "prompt",
        ("ses_1", {"text": "hi", "files": [], "delivery": "steer"}),
    ) in client.calls
    assert client.closed


async def test_send_prompt_honors_queue_delivery() -> None:
    client = _FakeClient()
    await _transport(client).send_prompt(
        "ses_1", NativePrompt(text="later", metadata={"delivery": "queue"})
    )
    assert client.calls[-1] == (
        "prompt",
        ("ses_1", {"text": "later", "files": [], "delivery": "queue"}),
    )


async def test_send_prompt_drops_system_prompt() -> None:
    client = _FakeClient()
    await _transport(client).send_prompt(
        "ses_1", NativePrompt(text="hi", system_prompt="be brief")
    )
    assert "system" not in client.calls[-1][1][1]


async def test_send_prompt_switches_model_once_without_bridge_state() -> None:
    client = _FakeClient()
    transport = _transport(client)
    await transport.send_prompt("ses_1", NativePrompt(text="a", model="acme/model-x"))
    await transport.send_prompt("ses_1", NativePrompt(text="b", model="acme/model-x"))
    assert [c for c in client.calls if c[0] == "set_model"] == [
        ("set_model", ("ses_1", "acme", "model-x"))
    ]
    assert [c[0] for c in client.calls] == ["set_model", "prompt", "prompt"]


async def test_send_prompt_ignores_unqualified_model() -> None:
    client = _FakeClient()
    await _transport(client).send_prompt("ses_1", NativePrompt(text="a", model="just-a-name"))
    assert [c[0] for c in client.calls] == ["prompt"]


async def test_abort_interrupts() -> None:
    client = _FakeClient()
    assert await _transport(client).abort("ses_1") is True
    assert ("interrupt", "ses_1") in client.calls


async def test_events_maps_to_native_event() -> None:
    client = _FakeClient()
    events = [event async for event in _transport(client).events("ses_1")]
    assert len(events) == 1
    assert (events[0].id, events[0].type, events[0].payload) == (
        "evt_1",
        "message.updated",
        {"k": "v"},
    )
    assert client.closed


async def test_list_history() -> None:
    client = _FakeClient()
    assert await _transport(client).list_history("ses_1") == [{"info": {"id": "msg_1"}}]


async def test_fork_with_and_without_message_id() -> None:
    client = _FakeClient()
    transport = _transport(client)
    assert await transport.fork("ses_1") == "ses_fork"
    assert await transport.fork("ses_1", at_message_id="msg_9") == "ses_fork"
    assert ("fork", ("ses_1", "msg_9")) in client.calls
    assert ("fork", ("ses_1", None)) in client.calls


async def test_reply_permission_maps_decision() -> None:
    client = _FakeClient()
    await _transport(client).reply_permission(
        NativePermissionDecision(request_id="per_1", decision="allow_always", message="ok")
    )
    assert ("reply_permission", ("per_1", {"reply": "always", "message": "ok"})) in client.calls


async def test_no_connection_coordinates_raises() -> None:
    # No factory / server / bridge_dir → the client builder fails loud.
    with pytest.raises(RuntimeError):
        await OpenCodeHttpTransport().abort("ses_1")
