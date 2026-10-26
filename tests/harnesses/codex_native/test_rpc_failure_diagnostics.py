"""Content-free transport evidence and bounded local descriptor probes."""

from __future__ import annotations

import contextlib
import errno
import sys
from pathlib import Path

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidMessage, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from omnigent.harnesses.codex_native import diagnostics
from omnigent.harnesses.codex_native.app_server import CodexAppServerClient


@pytest.mark.parametrize(
    ("error", "field", "value"),
    [
        (OSError(errno.EMFILE, "private path"), "rpc_os_errno", errno.EMFILE),
        (
            ConnectionClosedError(Close(1011, "private reason"), None),
            "rpc_close_received_code",
            1011,
        ),
        (
            InvalidStatus(Response(503, "private reason", Headers(), b"private body")),
            "rpc_http_status",
            503,
        ),
    ],
)
def test_exception_facts_omit_messages_and_addresses(
    error: Exception, field: str, value: int
) -> None:
    client = CodexAppServerClient(socket_path=Path("/private/socket"))
    wrapper = InvalidMessage("private response")
    wrapper.__cause__ = error
    result = diagnostics.collect_codex_rpc_failure_diagnostics(client, wrapper)
    assert result[field] == value
    assert result["rpc_error_type_1"] == type(error).__name__
    assert result["rpc_transport"] == "unix"
    assert "private" not in str(result)


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX descriptor probe")
@pytest.mark.parametrize("probe_error", [False, True])
def test_descriptor_probe_is_bounded_and_cannot_replace_failure(
    monkeypatch: pytest.MonkeyPatch, probe_error: bool
) -> None:
    import resource

    inspected = 0

    def entries():
        nonlocal inspected
        while True:
            inspected += 1
            assert inspected <= 4097
            yield object()

    def scan(path: str):
        if probe_error:
            raise OSError(errno.EMFILE, "private path")
        return contextlib.nullcontext(entries())

    client = CodexAppServerClient(ws_url="ws://private-host:1234")
    with monkeypatch.context() as patch:
        patch.setattr(diagnostics.os, "scandir", scan)
        patch.setattr(resource, "getrlimit", lambda _kind: (8192, 16384))
        result = diagnostics.collect_codex_rpc_failure_diagnostics(client, RuntimeError("private"))
    assert result["executor_fd_soft_limit"] == 8192
    assert result["executor_fd_hard_limit"] == 16384
    if probe_error:
        assert result["executor_fd_probe_errno"] == errno.EMFILE
        assert "executor_fd_count" not in result
    else:
        assert result["executor_fd_count"] == 4096
        assert result["executor_fd_count_capped"] is True
    assert "private" not in str(result)


def test_broken_snapshot_does_not_mask_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CodexAppServerClient(ws_url="ws://private-host:1234")

    def broken_snapshot():
        raise RuntimeError("private snapshot failure")

    monkeypatch.setattr(client, "connection_diagnostics", broken_snapshot)
    result = diagnostics.collect_codex_rpc_failure_diagnostics(client, ConnectionError("private"))
    assert result["rpc_diagnostics_error_type"] == "RuntimeError"
    assert "private" not in str(result)
