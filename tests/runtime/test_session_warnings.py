"""
Unit tests for :mod:`omnigent.runtime.session_warnings`.

The index is a process-global map of session id → degraded-but-running
conditions the chat header shows. Tests here pin the invariants that keep
a banner honest and the map bounded: only allowlisted codes are stored,
payloads are reduced to known string fields, entries dedupe on
``(code, harness)``, and :func:`clear` drops them again — for the codes
a publisher checked, or wholesale on teardown — so a repaired condition
or a deleted session leaves nothing behind while one publisher's repair
never drops another's warning.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnigent.runtime import session_warnings


@pytest.fixture(autouse=True)
def _clean_index() -> Iterator[None]:
    session_warnings._warnings.clear()
    yield
    session_warnings._warnings.clear()


def _warning(**overrides: str) -> dict[str, str]:
    payload = {
        "code": session_warnings.SUBAGENT_ROUTING_UNENFORCED,
        "harness": "codex-native",
        "reason": "SessionStart canary did not fire",
    }
    payload.update(overrides)
    return payload


def test_record_keeps_only_allowlisted_codes() -> None:
    session_warnings.record("conv_a", _warning(code="something_invented"))
    session_warnings.record("conv_a", {"harness": "codex-native"})
    assert session_warnings.snapshot_for("conv_a") == []

    session_warnings.record("conv_a", _warning())
    assert session_warnings.snapshot_for("conv_a") == [_warning()]


def test_record_drops_unknown_fields_and_caps_free_text() -> None:
    session_warnings.record(
        "conv_a",
        {**_warning(reason="x" * 900), "callback_url": "http://evil", "payload": {"deep": 1}},
    )
    (entry,) = session_warnings.snapshot_for("conv_a")
    assert set(entry) == {"code", "harness", "reason"}
    assert len(entry["reason"]) == 500


def test_record_dedupes_on_code_and_harness() -> None:
    session_warnings.record("conv_a", _warning(reason="first"))
    session_warnings.record("conv_a", _warning(reason="second"))
    session_warnings.record("conv_a", _warning(harness="claude-native", reason="other pane"))
    reasons = [entry["reason"] for entry in session_warnings.snapshot_for("conv_a")]
    assert reasons == ["second", "other pane"]


def test_clear_drops_named_codes_or_the_whole_session() -> None:
    session_warnings.record("conv_a", _warning())
    session_warnings.record("conv_b", _warning())

    session_warnings.clear("conv_a", codes=("not_this_one",))
    assert session_warnings.snapshot_for("conv_a") == [_warning()]

    session_warnings.clear("conv_a", codes=session_warnings.EXTERNAL_WARNING_CODES)
    assert session_warnings.snapshot_for("conv_a") == []
    # Clearing the last entry prunes the session's list rather than leaving
    # an empty one behind for the process lifetime.
    assert "conv_a" not in session_warnings._warnings
    assert session_warnings.snapshot_for("conv_b") == [_warning()]

    session_warnings.clear("conv_b")
    assert session_warnings._warnings == {}
    # Clearing an unknown session is a no-op, not a KeyError.
    session_warnings.clear("conv_missing")
    session_warnings.clear("conv_missing", codes=(session_warnings.SUBAGENT_ROUTING_UNENFORCED,))


def test_record_caps_the_harness_field_and_the_entry_count() -> None:
    # ``harness`` is half the dedup key, so a publisher that varied it freely
    # could mint an entry per post; both the value and the count are bounded.
    for index in range(session_warnings._MAX_ENTRIES_PER_SESSION + 4):
        session_warnings.record("conv_a", _warning(harness=f"{index}-{'h' * 200}"))
    entries = session_warnings.snapshot_for("conv_a")
    assert len(entries) == session_warnings._MAX_ENTRIES_PER_SESSION
    assert all(len(entry["harness"]) == session_warnings._MAX_HARNESS_LEN for entry in entries)
    # The newest observation is kept; the oldest was evicted for it.
    assert entries[-1]["harness"].startswith("11-")


def test_snapshot_is_a_copy() -> None:
    session_warnings.record("conv_a", _warning())
    snapshot = session_warnings.snapshot_for("conv_a")
    snapshot[0]["reason"] = "mutated"
    assert session_warnings.snapshot_for("conv_a") == [_warning()]
