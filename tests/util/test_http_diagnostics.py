"""HTTP diagnostics retain bounded class/errno evidence, never exception content."""

import errno
import json

import httpx
import pytest

from omnigent.util.http_diagnostics import http_failure_attributes


@pytest.mark.parametrize("explicit_cause", [True, False])
def test_http_failure_preserves_nested_os_cause_without_request_content(
    explicit_cause: bool,
) -> None:
    request = httpx.Request(
        "POST", "https://private.example/path?token=private", content=b"private"
    )
    error = httpx.ReadError("private", request=request)
    nested = RuntimeError("private")
    nested.__cause__ = OSError(errno.ECONNRESET, "private")
    if explicit_cause:
        error.__cause__ = nested
    else:
        error.__context__ = nested

    attrs = http_failure_attributes(error)

    assert attrs["exception_cause_chain"] == "ReadError/RuntimeError/ConnectionResetError"
    assert attrs["transport_errno"] == errno.ECONNRESET
    assert attrs["transport_phase"] == "read"
    assert attrs["exception_chain_truncated"] is False
    assert attrs["exception_suppressed_context"] is False
    assert "private" not in json.dumps(attrs)


@pytest.mark.parametrize("cycle", [True, False])
def test_http_failure_bounds_cyclic_or_long_exception_chains(cycle: bool) -> None:
    error = httpx.ReadError("private")
    previous: BaseException = error
    for _ in range(8):
        previous.__cause__ = RuntimeError("private")
        previous = previous.__cause__
    if cycle:
        error.__cause__ = error

    attrs = http_failure_attributes(error)

    assert attrs["exception_chain_truncated"] is True
    assert len(str(attrs["exception_cause_chain"]).split("/")) == (1 if cycle else 5)


def test_http_failure_retains_class_and_errno_from_suppressed_context() -> None:
    error = httpx.WriteError("private")
    error.__context__ = OSError(errno.ECONNRESET, "private")
    error.__suppress_context__ = True

    attrs = http_failure_attributes(error)

    assert attrs["exception_cause_chain"] == "WriteError/ConnectionResetError"
    assert attrs["transport_errno"] == errno.ECONNRESET
    assert attrs["exception_suppressed_context"] is True
    assert attrs["transport_phase"] == "write"
