"""Tests for the process-wide server-shutdown signal."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from omnigent.server import shutdown_state


@pytest.fixture(autouse=True)
def _reset_shutdown_state() -> Iterator[None]:
    shutdown_state.reset_for_tests()
    yield
    shutdown_state.reset_for_tests()


def test_unmarked_process_is_not_shutting_down() -> None:
    assert shutdown_state.server_shutting_down() is False


def test_explicit_mark_is_fresh_within_the_window_then_expires() -> None:
    shutdown_state.mark_server_shutting_down()
    assert shutdown_state.server_shutting_down() is True
    expired = time.monotonic() + shutdown_state.SHUTDOWN_WINDOW_S
    assert shutdown_state.server_shutting_down(now=expired) is False


@pytest.mark.parametrize(
    ("code", "expected"),
    [(1012, True), (1000, False), (1001, False), (1006, False), (None, False)],
)
def test_only_a_server_initiated_close_code_marks_shutdown(
    code: int | None, expected: bool
) -> None:
    assert shutdown_state.note_tunnel_close_code(code) is expected
    assert shutdown_state.server_shutting_down() is expected
