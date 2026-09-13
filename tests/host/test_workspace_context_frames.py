"""Workspace context wire compatibility and stream privacy."""

import json
from unittest.mock import patch

import pytest

from omnigent.host.frames import (
    HostHelloFrame,
    HostWorkspaceContextRequestFrame,
    HostWorkspaceContextResultFrame,
    HostWorkspaceContextStreamFrame,
    decode_host_frame,
    encode_host_frame,
)


@pytest.mark.parametrize(
    "frame",
    [
        HostWorkspaceContextRequestFrame("req", "create", "owner", params={"workspace": "/tmp"}),
        HostWorkspaceContextRequestFrame("req", "attach", "owner", "draft", {"read_only": True}),
        HostWorkspaceContextResultFrame("req", "ok", payload={"id": "draft"}),
        HostWorkspaceContextResultFrame("req", "error", error_status=404, error="Missing"),
        HostWorkspaceContextStreamFrame("channel", "aGVsbG8=", True),
        HostWorkspaceContextStreamFrame("channel", '{"type":"resize","cols":80,"rows":24}'),
        HostWorkspaceContextStreamFrame("channel", close_code=1000),
    ],
)
def test_workspace_context_frames_round_trip(frame):
    assert decode_host_frame(encode_host_frame(frame)) == frame


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        (
            "request",
            {"request_id": "r", "op": "create", "user_id": "u", "context_id": "", "params": []},
        ),
        ("result", {"request_id": "r", "status": "ok", "payload": []}),
        ("result", {"request_id": "r", "status": "error", "error_status": True}),
        ("stream", {"channel_id": "c", "data": "", "binary": "true", "close_code": None}),
        ("stream", {"channel_id": "c", "data": "", "binary": False, "close_code": True}),
    ],
)
def test_workspace_context_frames_reject_malformed_fields(kind, fields):
    with pytest.raises(ValueError):
        decode_host_frame(json.dumps({"kind": f"host.workspace_context_{kind}", **fields}))


def test_old_hello_does_not_advertise_workspace_contexts():
    hello = decode_host_frame(
        json.dumps(
            {
                "kind": "host.hello",
                "version": "1",
                "frame_protocol_version": 1,
                "name": "old",
            }
        )
    )
    assert isinstance(hello, HostHelloFrame)
    assert not hello.workspace_contexts
    current = HostHelloFrame("1", 1, "current", workspace_contexts=True)
    assert decode_host_frame(encode_host_frame(current)) == current


def test_terminal_stream_bytes_are_never_telemetry_message_bodies():
    frame = HostWorkspaceContextStreamFrame("c", "c2Vuc2l0aXZl", True)
    with patch("omnigent.runtime.telemetry.record_message_payload") as record:
        encode_host_frame(frame)
    record.assert_not_called()
