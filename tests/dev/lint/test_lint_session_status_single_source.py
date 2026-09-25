"""Tests for ``dev/lint/lint_session_status_single_source.py``.

The rule rejects runner-side copies of session status outside
``SessionStatusBook``: status-named state that outlives one call, status values
written into such state under any name, and book writes outside its owners.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import dev.lint.lint_session_status_single_source as lint

_APP = "omnigent/runner/app.py"
_HARNESS = "omnigent/harnesses/demo_native/forwarder.py"


def _found(source: str, rel_path: str = _APP) -> list[tuple[int, str]]:
    return lint.unsuppressed(inspect.cleandoc(source), rel_path)


def _lines(source: str, rel_path: str = _APP) -> list[int]:
    return [line for line, _message in _found(source, rel_path)]


def test_flags_the_published_status_table_the_reaper_trusted() -> None:
    src = """
        def create_runner_app():
            _native_pane_status: dict[str, str] = {}

            def _publish_event(session_id, event_body):
                _status_value = event_body.get("status")
                if isinstance(_status_value, str):
                    _native_pane_status[session_id] = _status_value

            def _native_pane_is_busy(conv_id):
                return _native_pane_status.get(conv_id) == "running"
        """
    assert _found(src) == [
        (2, "`_native_pane_status` keeps session status outside the book"),
        (7, "`_native_pane_status[...]` stores a session status"),
    ]


def test_flags_a_renamed_shadow_by_what_it_stores() -> None:
    src = """
        def create_runner_app():
            _last_published: dict[str, str] = {}

            def _publish_event(session_id, event):
                _last_published[session_id] = event["status"]
        """
    assert _lines(src) == [5]


@pytest.mark.parametrize(
    "declaration",
    [
        "_busy_sessions: set[str] = set()",
        "_idle_since = defaultdict(float)",
        "_turns_in_flight = weakref.WeakKeyDictionary()",
        "_running = {}",
    ],
)
def test_flags_status_named_closure_state(declaration: str) -> None:
    src = f"""
        def create_runner_app():
            {declaration}

            def _on_edge(conv):
                return conv in {declaration.split()[0].rstrip(":")}
        """
    assert _lines(src) == [2]


def test_flags_module_class_and_attribute_state() -> None:
    src = """
        _session_status = {}

        class Watcher:
            _busy = set()
            statuses: dict[str, str] = field(default_factory=dict)

            def __init__(self, app):
                self._last_status = {}
                app.state.pane_idle = {}
        """
    assert _lines(src, _HARNESS) == [1, 4, 5, 8, 9]


@pytest.mark.parametrize(
    "write",
    [
        'self._seen[conv] = "running"',
        "self._seen[conv] = record.status",
        "self._seen[conv] = (status, time.monotonic())",
        'self._seen[conv] = {"status": status}',
        "self._seen[conv] = Edge(status=status)",
        "self._seen.setdefault(conv, status)",
        "self._seen.update({conv: new_status})",
    ],
)
def test_flags_status_values_written_into_instance_state(write: str) -> None:
    src = f"""
        class Relay:
            def on_edge(self, conv, record, status, new_status):
                {write}
        """
    assert _lines(src, _HARNESS) == [3]


def test_ignores_per_call_temporaries_and_record_fields() -> None:
    src = """
        def build(sessions, body):
            statuses = {}
            for conv, record in sessions.items():
                statuses[conv] = record.status
            payload = {}
            payload["status"] = "running"
            body.data[conv] = record.status
            return statuses, payload

        class Relay:
            def on_edge(self, status):
                self._event["status"] = status
        """
    assert _found(src, _HARNESS) == []


def test_ignores_non_status_state() -> None:
    src = """
        _in_flight_send_locks: dict[str, asyncio.Lock] = {}
        _DEFAULTS = {"status": "idle"}

        def create_runner_app():
            _sessions = {}

            def _handler(conv):
                _sessions[conv] = object()
        """
    assert _found(src) == []


def test_state_checks_only_cover_the_runner_process_roots() -> None:
    src = "_session_status = {}\n"
    assert _found(src, "omnigent/server/routes/helpers.py") == []
    assert _found(src, "omnigent/terminals/x.py") != []


def test_flags_book_writes_outside_its_owners() -> None:
    src = """
        def on_idle(resource_registry, conv):
            resource_registry.status_book.record(conv, "idle", source=StatusSource.RELAY)
            resource_registry.status_book.reset(conv, "done")
            return resource_registry.status_book.current(conv)

        def peek(app):
            return app.state.session_status_book._records

        _book = SessionStatusBook()
        """
    assert _found(src, "omnigent/harnesses/demo_native/pane_probe.py") == [
        (2, "`resource_registry.status_book.record` writes the status book"),
        (3, "`resource_registry.status_book.reset` writes the status book"),
        (7, "`app.state.session_status_book._records` reaches into the status book"),
        (9, "a second `SessionStatusBook` is a second source of truth"),
    ]


def test_book_owners_and_the_book_itself_may_write() -> None:
    src = """
        def _record_control_idle(conv_id):
            _status_book.record(conv_id, "idle", source=StatusSource.CONTROL)
        """
    assert _found(src, _APP) == []
    assert _found(src, "omnigent/runner/resource_registry.py") == []
    book = "class SessionStatusBook:\n    def __init__(self):\n        self._records = {}\n"
    assert _found(book, lint.RECORDER_MODULE) == []


def test_readers_match_the_book_api() -> None:
    from omnigent.runner.session_status import SessionStatusBook

    public = {
        name
        for name, _ in inspect.getmembers(SessionStatusBook, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public > lint.BOOK_READERS


def test_inline_disable_needs_this_rules_id() -> None:
    src = """
        # custom-lint: disable-next=session-status-single-source -- turn tasks, not statuses
        _active_turns = {}
        _busy = set()  # custom-lint: disable=session-status-single-source -- lock owners
        _idle_since = {}  # custom-lint: disable=workspace-scoped-cache
        """
    assert _lines(src, "omnigent/native/x.py") == [4]


def test_scan_reads_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "mod.py"
    f.write_text("_ok = 1\n_pane_status = {}\n")
    monkeypatch.setattr(lint, "SCANNED_ROOTS", (lint._repo_relative(f),))
    assert [(hit.line, hit.message) for hit in lint.scan(f)] == [
        (2, "`_pane_status` keeps session status outside the book")
    ]


def test_main_clean_tree_returns_zero() -> None:
    assert lint.main(["lint_session_status_single_source.py"]) == 0


def test_main_flags_dirty_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "dirty.py"
    f.write_text("_busy_panes = set()\n")
    monkeypatch.setattr(lint, "SCANNED_ROOTS", (lint._repo_relative(f),))
    assert lint.main(["lint_session_status_single_source.py", str(f)]) == 1
