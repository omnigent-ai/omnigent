"""``read_json_line`` stays within its deadline, whatever the child writes."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from tests._helpers.live_server import terminate_process
from tests._helpers.stdio_jsonrpc import read_json_line

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="select() on pipes is POSIX-only")


def _child(source: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", source], stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True
    )


def test_a_partial_line_times_out_instead_of_blocking() -> None:
    child = _child(
        "import sys, time; sys.stdout.write('{\"id\": 1'); sys.stdout.flush(); time.sleep(30)"
    )
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            read_json_line(child.stdout, timeout_s=0.5)
        assert time.monotonic() - started < 5
    finally:
        terminate_process(child, timeout_s=5)


def test_lines_written_together_are_read_one_at_a_time() -> None:
    child = _child(
        'import sys, time; sys.stdout.write(\'{"id": 1}\\n{"id": 2}\\n{"id"\');'
        " sys.stdout.flush(); time.sleep(0.3); sys.stdout.write(': 3}\\n'); sys.stdout.flush();"
        " time.sleep(30)"
    )
    try:
        assert [read_json_line(child.stdout, timeout_s=5)["id"] for _ in range(3)] == [1, 2, 3]
    finally:
        terminate_process(child, timeout_s=5)


def test_a_closed_output_is_reported_at_once() -> None:
    child = _child("pass")
    try:
        started = time.monotonic()
        with pytest.raises(EOFError):
            read_json_line(child.stdout, timeout_s=10)
        assert time.monotonic() - started < 5
    finally:
        terminate_process(child, timeout_s=5)
