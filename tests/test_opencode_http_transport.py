"""Unit tests for :class:`omnigent.opencode_http_transport.OpenCodeHttpTransport`.

Covers the payload builder + every transport method over an injected fake
``OpenCodeClient`` (the documented ``client_factory`` test seam), so the
opencode-native HTTP/SSE wire surface stays covered without a live server.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.native_server_transport import (
    NativeLaunchConfig,
    NativePermissionDecision,
    NativePrompt,
)
from omnigent.opencode_http_transport import OpenCodeHttpTransport, build_prompt_payload

# ── build_prompt_payload + part/model helpers ──────────────────────────────


def test_build_prompt_payload_text_only() -> None:
    assert build_prompt_payload(NativePrompt(text="hi")) == {
        "parts": [{"type": "text", "text": "hi"}]
    }


def test_build_prompt_payload_system_and_model_split() -> None:
    body = build_prompt_payload(
        NativePrompt(text="hi", system_prompt="be brief", model="anthropic/claude-opus-4")
    )
    assert body["system"] == "be brief"
    assert body["model"] == {"providerID": "anthropic", "modelID": "claude-opus-4"}


def test_build_prompt_payload_bare_model_id_is_dropped() -> None:
    # No ``provider/model`` slash → not a valid opencode model object → omitted.
    assert "model" not in build_prompt_payload(NativePrompt(text="hi", model="just-a-name"))


def test_build_prompt_payload_image_and_file_attachments() -> None:
    prompt = NativePrompt(
        text="look",
        attachments=(
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {
                "type": "input_file",
                "file_data": "data:application/pdf;base64,BBBB",
                "filename": "a.pdf",
            },
            {"type": "input_file", "url": "data:text/plain;base64,CCCC"},
            {"type": "input_image"},  # no url → skipped
        ),
    )
    parts = build_prompt_payload(prompt)["parts"]
    assert {"type": "file", "mime": "image/png", "url": "data:image/png;base64,AAAA"} in parts
    pdf = next(p for p in parts if p.get("filename") == "a.pdf")
    assert pdf["mime"] == "application/pdf"
    # The url-only file part falls back to its data-URI mime; the empty image is dropped.
    assert any(p.get("mime") == "text/plain" for p in parts)
    assert sum(1 for p in parts if p["type"] == "file") == 3


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

    async def create_session(self, payload: Any = None) -> SimpleNamespace:
        self.calls.append(("create_session", payload))
        return SimpleNamespace(id="ses_new")

    async def prompt_async(self, session_id: str, payload: Any) -> dict[str, Any]:
        self.calls.append(("prompt_async", (session_id, payload)))
        return {"ok": True}

    async def abort(self, session_id: str) -> bool:
        self.calls.append(("abort", session_id))
        return True

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_messages", session_id))
        return [{"info": {"id": "msg_1"}}]

    async def fork(self, session_id: str, payload: Any = None) -> SimpleNamespace:
        self.calls.append(("fork", (session_id, payload)))
        return SimpleNamespace(id="ses_fork")

    async def reply_permission(self, request_id: str, reply: Any) -> bool:
        self.calls.append(("reply_permission", (request_id, reply)))
        return True

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
    assert out == {"ok": True}
    assert ("prompt_async", ("ses_1", {"parts": [{"type": "text", "text": "hi"}]})) in client.calls
    assert client.closed


async def test_abort() -> None:
    client = _FakeClient()
    assert await _transport(client).abort("ses_1") is True


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
    assert ("fork", ("ses_1", {"messageID": "msg_9"})) in client.calls
    assert ("fork", ("ses_1", None)) in client.calls


async def test_reply_permission_maps_decision() -> None:
    client = _FakeClient()
    await _transport(client).reply_permission(
        NativePermissionDecision(request_id="per_1", decision="allow_always", message="ok")
    )
    assert ("reply_permission", ("per_1", {"reply": "always", "message": "ok"})) in client.calls


def test_build_tui_attach_command_uses_launch_server_url() -> None:
    transport = OpenCodeHttpTransport(client_factory=lambda: _FakeClient())
    argv, env = transport.build_tui_attach_command(
        _launch(server_url="http://127.0.0.1:1234", terminal_launch_args=("--foo",)),
        "ses_1",
    )
    assert argv[0] == "attach"
    assert "http://127.0.0.1:1234" in argv
    assert "ses_1" in argv
    assert "--foo" in argv
    assert env == {}  # no server handle → empty terminal env


async def test_no_connection_coordinates_raises() -> None:
    # No factory / server / bridge_dir → the client builder fails loud.
    with pytest.raises(RuntimeError):
        await OpenCodeHttpTransport().abort("ses_1")
