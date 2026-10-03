"""
:func:`pending_elicitations.reset_for_tests` must clear every module-global
callback it owns, not just the elicitation observer. A count-persist hook
surviving the reset keeps feeding ``session_live_state``, stamping its dedupe
cache so a later test's identical persist write is silently dropped.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.runtime import pending_elicitations
from omnigent.server import session_live_state

_REQUEST: dict[str, Any] = {
    "type": "response.elicitation_request",
    "elicitation_id": "elicit_1",
}


@pytest.fixture(autouse=True)
def _isolate_module_globals() -> Iterator[None]:
    yield
    session_live_state.configure(None)
    pending_elicitations.set_count_persist_hook(None)
    pending_elicitations.set_elicitation_observer(None)
    pending_elicitations.reset_for_tests()


def test_reset_clears_the_count_persist_hook() -> None:
    fired: list[tuple[str, int]] = []
    pending_elicitations.set_count_persist_hook(lambda c, n: fired.append((c, n)))

    pending_elicitations.reset_for_tests()
    pending_elicitations.record_publish("conv_after_reset", _REQUEST)

    assert fired == [], f"hook survived reset_for_tests and fired {fired}"


def test_reset_clears_the_observer() -> None:
    """Contrast case isolating the asymmetry to the count hook."""
    seen: list[str] = []
    pending_elicitations.set_elicitation_observer(lambda c, e: seen.append(c))

    pending_elicitations.reset_for_tests()
    pending_elicitations.record_publish("conv_after_reset", _REQUEST)

    assert seen == []


class _RecordingStore:
    """Conversation-store stand-in recording pending-count writes."""

    def __init__(self) -> None:
        self.pending_writes: list[tuple[str, int]] = []

    def set_pending_elicitation_count(self, conversation_id: str, count: int) -> None:
        self.pending_writes.append((conversation_id, count))


def _flush_writes() -> None:
    done = threading.Event()
    session_live_state.submit("test_barrier", done.set)
    assert done.wait(5), "live-state worker did not drain"


def test_leaked_hook_poisons_later_persist_writes() -> None:
    pending_elicitations.set_count_persist_hook(session_live_state.persist_pending_count)
    pending_elicitations.reset_for_tests()

    store = _RecordingStore()
    session_live_state.configure(store)  # type: ignore[arg-type]
    pending_elicitations.record_publish("conv_1", _REQUEST)
    _flush_writes()
    assert store.pending_writes == [], (
        f"publish after reset reached the store via a leaked hook: {store.pending_writes}"
    )

    session_live_state.persist_pending_count("conv_1", 1)
    _flush_writes()
    assert store.pending_writes == [("conv_1", 1)]
