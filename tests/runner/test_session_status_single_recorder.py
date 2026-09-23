"""Guards: session status has one store, one set of writers and one read API.

A second, private copy of session status (a dict of "last published" values)
once disagreed with reality and pinned finished native panes forever: the
pane reaper read it as a veto, and nothing but the runner's own publishes ever
wrote it. These source scans fail when such a copy comes back, when a code
path writes status state outside the audited recorders, or when the reaper or
the claude ``/model`` path reads status from anywhere but the book's reader
API. Each scanner is also run against a planted offender, so a guard that
stops matching anything fails too. The ``session-status-single-source``
custom-lint rule catches the same shapes at commit time; the last section keeps
it registered and in step with these lists.
"""

from __future__ import annotations

import ast
import functools
import inspect
from collections.abc import Iterable
from pathlib import Path

import pytest

import dev.lint.custom_lint as custom_lint
import dev.lint.lint_session_status_single_source as status_lint
import omnigent
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.session_status import SessionStatusBook
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import NullServerClient

_PACKAGE_ROOT = Path(omnigent.__file__).parent
_BOOK_MODULE = Path("runner/session_status.py")
_APP_MODULE = Path("runner/app.py")
_REGISTRY_MODULE = Path("runner/resource_registry.py")
_REAPER_MODULE = Path("terminals/pane_reaper.py")

_Sources = list[tuple[Path, ast.Module]]

# Functions allowed to call ``<status book>.record(...)``: each records one
# channel at observation time (see ``SessionStatusBook``).
_RECORDERS = frozenset(
    {
        "_publish_event",  # RUNNER: what the runner queues on its own SSE stream
        "_publish_status",  # PTY / STATUS_FILE: the registry's watcher closure
        "note_external_session_status",  # RELAY
        "_record_control_idle",  # CONTROL: accepted native interrupt/stop
    }
)

# The book's public API, split into readers and writers. Every public method
# must be listed in exactly one, so a new writer cannot slip in unaudited.
_READERS = frozenset(
    {
        "current",
        "claim",
        "blocked",
        "age_s",
        "last_dispatch_at",
        "last_control_idle_at",
        "session_ids",
        "status_view",
        "edge_mark",
    }
)
# writer -> the (module, function) pairs allowed to call it.
_WRITER_CALLERS: dict[str, frozenset[tuple[Path, str]]] = {
    "record": frozenset(),  # audited by name in _RECORDERS
    "reset": frozenset(
        {
            (_REGISTRY_MODULE, "reset_session_status"),
            (_REGISTRY_MODULE, "_finalize_terminal_exit"),
        }
    ),
    "forget": frozenset({(_REGISTRY_MODULE, "cleanup_session"), (_APP_MODULE, "delete_session")}),
    "transfer": frozenset({(_REGISTRY_MODULE, "transfer_terminal")}),
}

# What the server heard, kept only for the wire dedup; never a status source.
_WIRE_BASELINE = "_server_delivery_baseline"
_WIRE_BASELINE_USERS = frozenset(
    {
        "__init__",
        "_claim_status_edge",
        "_sync_status_edge",
        "_take_session_status_memo",
        "resync_session_statuses",
    }
)

# Status caches the reaper and model paths must never read: the book's view,
# the wire baseline, the SSE queue, the registry memos, and retired names.
_FORBIDDEN_STATUS_SOURCES = frozenset(
    {
        "native_pane_status",
        "status_view",
        _WIRE_BASELINE,
        "_session_event_queues",
        "_session_event_queues_ref",
        "_set_session_status_memo",
        "_take_session_status_memo",
        "_native_pane_status",
        "_published_session_status",
        "_last_session_status",
        "_active_session_turns",
        "_session_activity_epoch",
    }
)

# Retired status stores. Nothing in the package may name them: a leftover
# reference (say, from a merge) reads a store that no longer exists.
_RETIRED_STATUS_NAMES = frozenset({"_native_pane_status", "_published_session_status"})

# app.py functions that decide from session status: the reaper's busy check and
# its hold reasons, the claude /model mid-turn check, and the runner idle
# watchdog's native-turn hold. Each must read status only through the book's
# reader API.
_STATUS_READING_PATHS: dict[str, frozenset[str]] = {
    "_native_session_hold_reasons": frozenset({"blocked"}),
    "_native_pane_is_busy": frozenset({"claim"}),
    "_native_pane_close_snapshot": frozenset({"claim"}),
    "_native_sidecars_still_needed": frozenset(),
    "_handle_claude_native_model_change": frozenset({"current"}),
    "_native_turn_in_flight": frozenset({"blocked", "current", "last_dispatch_at", "age_s"}),
}
# Reads that judge a session's state. Each may only happen in an audited decider
# above; a new one joins _STATUS_READING_PATHS and needs a behaviour test where
# the recorded status goes stale (see tests/terminals/test_native_pane_stale_status_*.py,
# and tests/runner/test_runner_idle_active_work.py for the runner's hold).
_JUDGING_READERS = frozenset({"current", "claim", "blocked"})

# Dicts whose names mention "status" in the runner, reaper and native code are
# presumed status caches unless listed here with what they hold.
_STATUS_NAMED_DICTS = frozenset(
    {
        (Path("runner/github_resource.py"), "_GH_STATUS_MAP"),  # GitHub check-run labels
        (_REGISTRY_MODULE, "self._status_pollers"),  # claude status-file pollers
        (_REGISTRY_MODULE, "self._last_session_status"),  # exit-classification memo
    }
)
_STATUS_DICT_SCOPES = ("runner", "terminals", "native")

_BOOK_INTERNALS = frozenset(
    {
        "_records",
        "_dispatch_at",
        "_control_idle_at",
        "_merge_duplicate",
        "_note_projections",
        "_record_locked",
        "_log_edge",
    }
)

# The registry's own status plumbing (wire dedup, exit memo): calling it from
# elsewhere would route an edge around the recorders.
_REGISTRY_STATUS_HELPERS = frozenset(
    name
    for name, member in vars(SessionResourceRegistry).items()
    if name.startswith("_") and not name.startswith("__") and "status" in name and callable(member)
)

# What ``_publish_event`` may mutate: the queue map (to create a stream) and the
# queue itself. A second container written there would be a shadow cache.
_PUBLISHER_MUTATORS = frozenset(
    {"add", "append", "extend", "insert", "update", "setdefault", "pop", "discard", "remove"}
)


# ── source scanning ──────────────────────────────────────────────────────


@functools.cache
def _package_sources() -> tuple[tuple[Path, ast.Module], ...]:
    modules = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        modules.append((path.relative_to(_PACKAGE_ROOT), tree))
    return tuple(modules)


def _parse(path: str, source: str) -> _Sources:
    return [(Path(path), ast.parse(inspect.cleandoc(source)))]


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _is_book(node: ast.expr) -> bool:
    return "status_book" in _dotted(node)


def _identifier(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _enclosing_functions(tree: ast.Module) -> dict[ast.AST, str]:
    owners: dict[ast.AST, str] = {}

    def _visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            owners[child] = name
            _visit(child, name)

    _visit(tree, "<module>")
    return owners


def _functions_named(tree: ast.Module, name: str) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]


def _dict_bindings(sources: Iterable[tuple[Path, ast.Module]]) -> list[tuple[Path, str, int]]:
    """Every name bound to a dict literal/constructor: (module, name, line)."""
    found = []
    for path, tree in sources:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            is_dict = isinstance(value, ast.Dict | ast.DictComp) or (
                isinstance(value, ast.Call)
                and _dotted(value.func) in {"dict", "defaultdict", "collections.defaultdict"}
            )
            if is_dict:
                found.extend((path, _dotted(target), node.lineno) for target in targets)
    return found


def _book_calls(sources: Iterable[tuple[Path, ast.Module]]) -> list[tuple[Path, str, str, int]]:
    """Every ``<status book>.<method>(...)`` call: (module, method, owner, line)."""
    calls = []
    for path, tree in sources:
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _is_book(node.func.value)
            ):
                calls.append((path, node.func.attr, owners.get(node, "<module>"), node.lineno))
    return calls


def _unaudited_records(sources: Iterable[tuple[Path, ast.Module]]) -> list[str]:
    return [
        f"{path}:{line} in {owner}"
        for path, method, owner, line in _book_calls(sources)
        if method == "record" and owner not in _RECORDERS
    ]


def _unaudited_writes(sources: Iterable[tuple[Path, ast.Module]]) -> list[str]:
    offenders = []
    for path, method, owner, line in _book_calls(sources):
        allowed = _WRITER_CALLERS.get(method)
        if method == "record" or allowed is None:
            continue
        if (path, owner) not in allowed:
            offenders.append(f"{path}:{line} {method} in {owner}")
    return offenders


def _status_cache_reads(
    sources: Iterable[tuple[Path, ast.Module]], module: Path, function: str
) -> tuple[list[str], set[str]]:
    """Forbidden cache references and book methods used inside *function*."""
    offenders: list[str] = []
    book_methods: set[str] = set()
    for path, tree in sources:
        if path != module:
            continue
        for fn in _functions_named(tree, function):
            for node in ast.walk(fn):
                ident = _identifier(node)
                if ident in _FORBIDDEN_STATUS_SOURCES:
                    offenders.append(f"{path}:{node.lineno} {function} reads {ident}")
                if isinstance(node, ast.Attribute) and _is_book(node.value):
                    if node.attr not in _READERS:
                        offenders.append(f"{path}:{node.lineno} {function} uses {node.attr}")
                    book_methods.add(node.attr)
    return sorted(offenders), book_methods


def _references(
    sources: Iterable[tuple[Path, ast.Module]], names: frozenset[str]
) -> list[tuple[Path, str, str, int, str]]:
    """(module, identifier, owner, line, ctx) for every reference to *names*."""
    found = []
    for path, tree in sources:
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            ident = _identifier(node)
            if ident in names:
                ctx = type(getattr(node, "ctx", ast.Load())).__name__
                found.append((path, ident, owners.get(node, "<module>"), node.lineno, ctx))
    return found


def _retired_name_references(sources: Iterable[tuple[Path, ast.Module]]) -> list[str]:
    return sorted(
        f"{path}:{line} {ident} in {owner}"
        for path, ident, owner, line, _ctx in _references(sources, _RETIRED_STATUS_NAMES)
    )


# ── one store ────────────────────────────────────────────────────────────


def test_no_module_references_a_retired_status_store() -> None:
    # Package-wide, not only in the audited deciders.
    assert _retired_name_references(_package_sources()) == []


def test_no_module_keeps_a_pane_status_dict() -> None:
    offenders = [
        f"{path}:{line} {name}"
        for path, name, line in _dict_bindings(_package_sources())
        if "pane_status" in name
    ]
    assert offenders == []


def test_no_runner_module_keeps_a_status_dict_outside_the_book() -> None:
    offenders = [
        f"{path}:{line} {name}"
        for path, name, line in _dict_bindings(_package_sources())
        if path.parts[0] in _STATUS_DICT_SCOPES
        and "status" in name.lower()
        and (path, name) not in _STATUS_NAMED_DICTS
    ]
    assert offenders == [], (
        "a dict named for status outside the SessionStatusBook is a shadow cache; "
        "record into the book instead, or list it in _STATUS_NAMED_DICTS with why"
    )


def test_one_status_book_per_runner() -> None:
    constructed = set()
    for path, tree in _package_sources():
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _identifier(node.func) == "SessionStatusBook":
                constructed.add((path, owners.get(node, "<module>")))
    # The registry owns the book; the app builds one only for a stub registry.
    assert constructed == {(_REGISTRY_MODULE, "__init__"), (_APP_MODULE, "create_runner_app")}


def test_the_wire_baseline_stays_inside_the_registry_dedup() -> None:
    users = {
        (path, owner)
        for path, _ident, owner, _line, _ctx in _references(
            _package_sources(), frozenset({_WIRE_BASELINE})
        )
    }
    assert users == {(_REGISTRY_MODULE, owner) for owner in _WIRE_BASELINE_USERS}


# ── one set of writers ───────────────────────────────────────────────────


def test_the_book_api_is_fully_classified() -> None:
    public = {
        name
        for name, _ in inspect.getmembers(SessionStatusBook, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == _READERS | set(_WRITER_CALLERS)
    assert not _READERS & set(_WRITER_CALLERS)


def test_status_is_recorded_only_by_the_audited_recorders() -> None:
    assert _unaudited_records(_package_sources()) == []
    recorders = {
        owner for _p, method, owner, _l in _book_calls(_package_sources()) if method == "record"
    }
    assert recorders == _RECORDERS


def test_every_other_book_write_has_an_audited_caller() -> None:
    assert _unaudited_writes(_package_sources()) == []
    callers: dict[str, set[tuple[Path, str]]] = {}
    for path, method, owner, _line in _book_calls(_package_sources()):
        if method in _WRITER_CALLERS and method != "record":
            callers.setdefault(method, set()).add((path, owner))
    assert callers == {m: set(c) for m, c in _WRITER_CALLERS.items() if m != "record"}


def test_no_caller_reaches_into_the_book() -> None:
    offenders: list[str] = []
    for path, tree in _package_sources():
        if path == _BOOK_MODULE:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _BOOK_INTERNALS
                and _is_book(node.value)
            ):
                offenders.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in {
                "_published_session_status",
            }:
                offenders.append(f"{path}:{node.lineno} {node.attr}")
    assert offenders == []


def test_only_the_publisher_puts_events_on_the_session_stream() -> None:
    # A session.status queued without _publish_event would reach the server
    # and never the book.
    queue_users = {
        owner
        for path, _ident, owner, _line, _ctx in _references(
            _package_sources(), frozenset({"_session_event_queues"})
        )
        if path == _APP_MODULE
    }
    assert queue_users == {
        "create_runner_app",  # binds it
        "_drain_session_streams",  # None sentinels
        "_publish_event",
        "_initialize_session",  # creates the queue
        "_event_generator",  # requeues an event it failed to send
        "delete_session",  # None sentinel, then drops the queue
    }
    putters: set[str] = set()
    for path, tree in _package_sources():
        if path != _APP_MODULE:
            continue
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "put_nowait"
                and owners.get(node) in queue_users
                and not (
                    len(node.args) == 1
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value is None
                )
            ):
                putters.add(owners[node])
    assert putters == {"_publish_event", "_event_generator"}
    # The module-level handle is bound once and handed to the app, no further.
    handle_users = {
        (path, owner)
        for path, _ident, owner, _line, _ctx in _references(
            _package_sources(), frozenset({"_session_event_queues_ref"})
        )
    }
    assert handle_users == {(_APP_MODULE, "<module>"), (_APP_MODULE, "create_runner_app")}
    # The app.state handle to the queues is for tests only.
    exposed = [
        (path, owner, ctx)
        for path, _ident, owner, _line, ctx in _references(
            _package_sources(), frozenset({"session_event_queues"})
        )
    ]
    assert exposed == [(_APP_MODULE, "create_runner_app", "Store")]


def _registry_helper_calls(sources: Iterable[tuple[Path, ast.Module]]) -> list[str]:
    return [
        f"{path}:{line} {ident} in {owner}"
        for path, ident, owner, line, _ctx in _references(sources, _REGISTRY_STATUS_HELPERS)
        if path != _REGISTRY_MODULE
    ]


def _publisher_side_writes(sources: Iterable[tuple[Path, ast.Module]]) -> list[str]:
    """Containers ``_publish_event`` writes other than the SSE queue map."""
    offenders: list[str] = []
    for path, tree in sources:
        if path != _APP_MODULE:
            continue
        for fn in _functions_named(tree, "_publish_event"):
            for node in ast.walk(fn):
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Subscript) and (
                        _dotted(target.value) != "_session_event_queues"
                    ):
                        offenders.append(f"{path}:{node.lineno} {_dotted(target.value)}[...]")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _PUBLISHER_MUTATORS
                ):
                    offenders.append(f"{path}:{node.lineno} {_dotted(node.func)}()")
    return offenders


def test_the_registry_status_helpers_are_not_called_from_outside() -> None:
    assert {"_claim_status_edge", "_sync_status_edge", "_set_session_status_memo"} <= (
        _REGISTRY_STATUS_HELPERS
    )
    assert _registry_helper_calls(_package_sources()) == []


def test_the_publisher_keeps_no_other_container() -> None:
    assert _publisher_side_writes(_package_sources()) == []


# ── one read API ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("function", sorted(_STATUS_READING_PATHS))
def test_status_deciders_read_only_through_the_book(function: str) -> None:
    sources = list(_package_sources())
    assert any(
        _functions_named(tree, function) for path, tree in sources if path == _APP_MODULE
    ), f"{function} is gone from app.py; update _STATUS_READING_PATHS"
    offenders, used = _status_cache_reads(sources, _APP_MODULE, function)
    assert offenders == []
    assert _STATUS_READING_PATHS[function] <= used


def _unaudited_judging_reads(sources: Iterable[tuple[Path, ast.Module]]) -> list[str]:
    return [
        f"{path}:{line} {method} in {owner}"
        for path, method, owner, line in _book_calls(sources)
        if method in _JUDGING_READERS
        and not (path == _APP_MODULE and owner in _STATUS_READING_PATHS)
    ]


def test_status_is_judged_only_in_the_audited_deciders() -> None:
    assert _unaudited_judging_reads(_package_sources()) == []


def test_the_pane_reaper_never_touches_a_status_store() -> None:
    # The reaper decides from the app's assessment, never from status directly.
    names = frozenset({"status_book", "_status_book", "SessionStatusBook"}) | (
        _FORBIDDEN_STATUS_SOURCES
    )
    found = [
        f"{path}:{line} {ident}"
        for path, ident, _owner, line, _ctx in _references(_package_sources(), names)
        if path == _REAPER_MODULE
    ]
    assert found == []


def test_production_code_never_reads_the_status_view() -> None:
    # app.state.native_pane_status is for tests and diagnostics only.
    refs = [
        (path, owner, ctx)
        for path, _ident, owner, _line, ctx in _references(
            _package_sources(), frozenset({"native_pane_status"})
        )
    ]
    assert refs == [(_APP_MODULE, "create_runner_app", "Store")]


def test_the_app_exposes_status_only_as_a_read_only_view() -> None:
    registry = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=registry)
    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=resources,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    assert app.state.session_status_book is resources.status_book
    with pytest.raises(TypeError):
        app.state.native_pane_status["conv"] = "running"


# ── the scanners catch planted offenders ─────────────────────────────────


def test_scanners_flag_a_reintroduced_shadow_cache() -> None:
    planted = _parse(
        "runner/app.py",
        """
        def create_runner_app():
            _native_pane_status: dict[str, str] = {}

            def _publish_event(session_id, event):
                _native_pane_status[session_id] = event["status"]

            def _native_pane_assess(pane):
                return _native_pane_status.get(pane) == "running"
        """,
    )
    assert [name for _p, name, _l in _dict_bindings(planted)] == ["_native_pane_status"]
    offenders, _used = _status_cache_reads(planted, _APP_MODULE, "_native_pane_assess")
    assert offenders == ["runner/app.py:8 _native_pane_assess reads _native_pane_status"]


def test_scanners_flag_a_reaper_reading_the_view_or_a_rogue_writer() -> None:
    planted = _parse(
        "runner/app.py",
        """
        def _native_pane_assess(pane):
            if app.state.native_pane_status.get(pane) == "running":
                return True
            _status_book.reset(pane, "why")
            return _status_book.claim(pane)

        def _rogue_idle(conv):
            _status_book.record(conv, "idle", source=StatusSource.RUNNER)
        """,
    )
    offenders, used = _status_cache_reads(planted, _APP_MODULE, "_native_pane_assess")
    assert offenders == [
        "runner/app.py:2 _native_pane_assess reads native_pane_status",
        "runner/app.py:4 _native_pane_assess uses reset",
    ]
    assert used == {"reset", "claim"}
    assert _unaudited_records(planted) == ["runner/app.py:8 in _rogue_idle"]
    assert _unaudited_writes(planted) == ["runner/app.py:4 reset in _native_pane_assess"]


def test_scanners_flag_a_retired_status_store_anywhere() -> None:
    planted = _parse(
        "runner/app.py",
        """
        def create_runner_app():
            def _native_turn_in_flight(session_id):
                return _native_pane_status.get(session_id) == "running"

            def _publish_terminal_exit(event):
                _native_pane_status.pop(event.session_id, None)
        """,
    )
    assert _retired_name_references(planted) == [
        "runner/app.py:3 _native_pane_status in _native_turn_in_flight",
        "runner/app.py:6 _native_pane_status in _publish_terminal_exit",
    ]


def test_scanners_flag_a_publisher_shadow_or_a_routed_around_relay() -> None:
    planted = _parse(
        "runner/app.py",
        """
        def _publish_event(session_id, event):
            _session_event_queues[session_id] = queue
            _last_published[session_id] = event["status"]
            _turns_in_flight.add(session_id)

        def _handle_relay(conv, status):
            resource_registry._sync_status_edge(conv, status)
        """,
    )
    assert _publisher_side_writes(planted) == [
        "runner/app.py:3 _last_published[...]",
        "runner/app.py:4 _turns_in_flight.add()",
    ]
    assert _registry_helper_calls(planted) == [
        "runner/app.py:7 _sync_status_edge in _handle_relay"
    ]


def test_scanners_flag_a_new_decider_that_trusts_the_record() -> None:
    planted = _parse(
        "runner/app.py",
        """
        def _pane_is_busy(conv_id):
            record = _status_book.current(conv_id)
            return record is not None and record.status == "running"
        """,
    )
    assert _unaudited_judging_reads(planted) == ["runner/app.py:2 current in _pane_is_busy"]


# ── the commit-time lint rule ────────────────────────────────────────────


def test_the_status_lint_rule_is_registered_and_in_step() -> None:
    assert status_lint.RULE_NAME in {rule.name for rule in custom_lint.RULES}
    assert status_lint.BOOK_READERS == _READERS
    assert f"omnigent/{_BOOK_MODULE.as_posix()}" == status_lint.RECORDER_MODULE
    assert {
        f"omnigent/{module.as_posix()}" for module in (_APP_MODULE, _REGISTRY_MODULE)
    } == status_lint.BOOK_OWNERS


def test_no_module_keeps_a_status_copy_the_lint_rule_rejects() -> None:
    # Scans the package directly, so files git does not track yet count too.
    offenders = [
        f"{path}:{line} {message}"
        for path, _tree in _package_sources()
        for line, message in status_lint.unsuppressed(
            (_PACKAGE_ROOT / path).read_text(encoding="utf-8"),
            f"omnigent/{path.as_posix()}",
        )
    ]
    assert offenders == [], status_lint.HINT
