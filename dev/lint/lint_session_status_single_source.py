"""Flag copies of session status kept outside ``SessionStatusBook``.

The runner once kept a private dict of the last ``session.status`` it had
published, and the native-pane reaper treated a ``running`` there as a veto.
Relayed forwarder edges, edges the wire dedup swallowed, and interrupts never
wrote that dict, so finished codex and antigravity panes stayed ``running``
forever and were never reaped. Session status now has one store,
:class:`omnigent.runner.session_status.SessionStatusBook`, written by audited
recorders and read through its reader API.

This rule flags the shapes that bring a second copy back:

* **Status-named state** under ``omnigent/{runner,terminals,native,harnesses}``:
  an empty mutable container (``{}``, ``set()``, ``defaultdict(...)``, a weak
  map, a ``cachetools`` cache, ``field(default_factory=dict)``) named for status
  or liveness (``status``, ``busy``, ``idle``, ``running``, ``active_turn``,
  ...) that outlives one call: a module global, a class attribute, an attribute
  (``self.x`` / ``app.state.x``), or a local of an enclosing function that a
  nested function uses (the ``create_runner_app`` closure idiom).
* **Status-valued writes** in the same roots, whatever the container is called:
  ``x[key] = event["status"]`` / ``"running"`` / ``status`` / ``record.status``
  (also ``setdefault`` and ``update({...})``) into a container that outlives
  the call.
* **Book writes outside its owners**, anywhere in ``omnigent/``: a call to a
  non-reader method of a ``*status_book*`` receiver, a reach into its private
  members, or a second ``SessionStatusBook(...)``, outside
  ``runner/app.py`` / ``runner/resource_registry.py``. Which function inside
  those modules may write is audited by
  ``tests/runner/test_session_status_single_recorder.py``.

A container that is not a copy of status (a lock map, the runner's own turn
tasks, the reaper's idle clock) is exempted inline, with what it holds, via
``# custom-lint: disable=session-status-single-source -- <reason>`` on its line
(or ``disable-next`` on the line above); see :mod:`dev.lint._framework`.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dev.lint._framework import disabled_rules_by_line

# Where status-named state and status-valued writes are flagged: the runner
# process's status, liveness and native-harness code.
SCANNED_ROOTS = (
    "omnigent/runner/",
    "omnigent/terminals/",
    "omnigent/native/",
    "omnigent/harnesses/",
)
# Where the book-ownership checks run.
BOOK_SCAN_ROOT = "omnigent/"
RECORDER_MODULE = "omnigent/runner/session_status.py"
# Modules that own the book: the recorders and the audited writers live here.
BOOK_OWNERS = frozenset({"omnigent/runner/app.py", "omnigent/runner/resource_registry.py"})
# The book's read API. Every other public method writes. Kept equal to the
# guard test's reader set, which is pinned to the class itself.
BOOK_READERS = frozenset(
    {
        "current",
        "claim",
        "blocked",
        "age_s",
        "last_dispatch_at",
        "last_control_idle_at",
        "last_control_idle_wall",
        "session_ids",
        "status_view",
        "edge_mark",
    }
)

STATUS_NAME_RE = re.compile(
    r"status|busy|idle|running|active_turn|turn_active|turn_state|turns?_in_?flight",
    re.IGNORECASE,
)
STATUS_VALUES = frozenset({"running", "waiting", "idle", "failed"})
# A variable that holds a status value: ``status``, ``new_status``, ``_status_value``.
STATUS_VARIABLE_RE = re.compile(r"status(_?value|_?str)?$", re.IGNORECASE)

_EMPTY_ONLY_CALLS = frozenset({"dict", "set", "OrderedDict", "Counter"})
_CONTAINER_CALLS = frozenset(
    {
        "defaultdict",
        "WeakKeyDictionary",
        "WeakValueDictionary",
        "WeakSet",
        "Cache",
        "LRUCache",
        "TTLCache",
        "LFUCache",
        "RRCache",
        "FIFOCache",
        "MRUCache",
    }
)
_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_SELF_NAMES = frozenset({"self", "cls"})


@dataclass(frozen=True)
class Hit:
    """One violation in one file."""

    path: Path
    line: int
    message: str


def _repo_relative(path: Path) -> str:
    """Return a stable repo-relative POSIX path when possible."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_dotted(node.value)}[...]"
    return "<expr>"


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _is_container(value: ast.expr) -> bool:
    """``True`` for an empty mutable container that is filled at runtime."""
    if isinstance(value, ast.Dict) and not value.keys:
        return True
    if not isinstance(value, ast.Call):
        return False
    name = _callee_name(value)
    if name in _EMPTY_ONLY_CALLS:
        return not value.args and not value.keywords
    if name in _CONTAINER_CALLS:
        return True
    if name == "field":
        return any(
            kw.arg == "default_factory"
            and isinstance(kw.value, ast.Name | ast.Attribute)
            and (_dotted(kw.value).rsplit(".", 1)[-1] in _EMPTY_ONLY_CALLS | _CONTAINER_CALLS)
            for kw in value.keywords
        )
    return False


def _is_status_value(value: ast.expr) -> bool:
    """``True`` when *value* is (or carries) a session status."""
    if isinstance(value, ast.Constant):
        return value.value in STATUS_VALUES
    if isinstance(value, ast.Subscript):
        return isinstance(value.slice, ast.Constant) and value.slice.value == "status"
    if isinstance(value, ast.Attribute):
        return value.attr == "status"
    if isinstance(value, ast.Name):
        return STATUS_VARIABLE_RE.search(value.id) is not None
    if isinstance(value, ast.Call):
        if (
            isinstance(value.func, ast.Attribute)
            and value.func.attr == "get"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and value.args[0].value == "status"
        ):
            return True
        return any(kw.arg == "status" and _is_status_value(kw.value) for kw in value.keywords)
    if isinstance(value, ast.Tuple | ast.List):
        return any(_is_status_value(elt) for elt in value.elts)
    if isinstance(value, ast.Dict):
        return any(
            isinstance(key, ast.Constant) and key.value == "status" for key in value.keys
        ) or any(_is_status_value(item) for item in value.values)
    return False


class _Scopes:
    """Parent links and per-function local names for one module."""

    def __init__(self, tree: ast.Module) -> None:
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self._locals: dict[ast.AST, frozenset[str]] = {}

    def scope_of(self, node: ast.AST) -> ast.AST | None:
        """The innermost function or class *node* runs in, or ``None`` (module)."""
        parent = self.parents.get(node)
        while parent is not None and not isinstance(parent, (*_FUNCTION_TYPES, ast.ClassDef)):
            parent = self.parents.get(parent)
        return parent

    def locals_of(self, fn: ast.AST) -> frozenset[str]:
        """Names bound in *fn*'s own scope (``global``/``nonlocal`` excluded)."""
        cached = self._locals.get(fn)
        if cached is not None:
            return cached
        names: set[str] = set()
        declared_outer: set[str] = set()
        if isinstance(fn, _FUNCTION_TYPES):
            args = fn.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                names.add(arg.arg)
            for arg in (args.vararg, args.kwarg):
                if arg is not None:
                    names.add(arg.arg)

        def _visit(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    names.add(child.name)
                    continue
                if isinstance(child, ast.Lambda):
                    continue
                if isinstance(child, ast.Global | ast.Nonlocal):
                    declared_outer.update(child.names)
                elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store | ast.Del):
                    names.add(child.id)
                _visit(child)

        body = fn.body if isinstance(fn, ast.Lambda) else fn
        if isinstance(body, ast.AST):
            _visit(body)
        result = frozenset(names - declared_outer)
        self._locals[fn] = result
        return result

    def used_by_nested_function(self, fn: ast.AST, name: str) -> bool:
        """Whether a function nested in *fn* refers to *fn*'s local *name*."""
        for node in ast.walk(fn):
            if node is fn or not isinstance(node, _FUNCTION_TYPES):
                continue
            if name in self.locals_of(node):
                continue
            if any(isinstance(ref, ast.Name) and ref.id == name for ref in ast.walk(node)):
                return True
        return False

    def outlives_call(self, container: ast.expr, at: ast.AST) -> bool:
        """Whether *container*, written at *at*, is state beyond one call."""
        root = container
        while isinstance(root, ast.Attribute | ast.Subscript):
            root = root.value
        if not isinstance(root, ast.Name):
            return False
        scope = self.scope_of(at)
        if scope is None or isinstance(scope, ast.ClassDef):
            return True
        if isinstance(container, ast.Attribute | ast.Subscript) and root.id in _SELF_NAMES:
            return True
        return root.id not in self.locals_of(scope) or self.used_by_nested_function(scope, root.id)


def _assignments(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


def _status_state(tree: ast.Module, scopes: _Scopes) -> list[tuple[int, str]]:
    """Status-named containers that outlive one call."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        targets, value = _assignments(node)
        if value is None or not _is_container(value):
            continue
        for target in targets:
            if isinstance(target, ast.Attribute):
                name, held = target.attr, True
            elif isinstance(target, ast.Name):
                name = target.id
                scope = scopes.scope_of(node)
                held = (
                    scope is None
                    or isinstance(scope, ast.ClassDef)
                    or scopes.used_by_nested_function(scope, name)
                )
            else:
                continue
            if held and STATUS_NAME_RE.search(name):
                found.append(
                    (node.lineno, f"`{_dotted(target)}` keeps session status outside the book")
                )
    return found


def _status_writes(tree: ast.Module, scopes: _Scopes) -> list[tuple[int, str]]:
    """Status values stored into containers that outlive one call."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        targets, value = _assignments(node)
        if value is not None and _is_status_value(value):
            for target in targets:
                if not isinstance(target, ast.Subscript):
                    continue
                # A literal key is a record's field, not a per-session cache.
                if isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                    continue
                if scopes.outlives_call(target.value, node):
                    found.append((node.lineno, f"`{_dotted(target)}` stores a session status"))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _call_stores_status(node)
            and scopes.outlives_call(node.func.value, node)
        ):
            found.append((node.lineno, f"`{_dotted(node.func)}()` stores a session status"))
    return found


def _call_stores_status(call: ast.Call) -> bool:
    """``x.setdefault(key, <status>)`` or ``x.update({key: <status>})``."""
    method = call.func.attr if isinstance(call.func, ast.Attribute) else None
    args = call.args
    if method == "setdefault":
        return len(args) == 2 and _is_status_value(args[1])
    if method == "update":
        return (
            len(args) == 1
            and isinstance(args[0], ast.Dict)
            and any(_is_status_value(item) for item in args[0].values)
        )
    return False


def _is_book(node: ast.expr) -> bool:
    return "status_book" in _dotted(node)


def _book_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """Book writes, private reaches and extra books outside the owners."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node) == "SessionStatusBook":
            found.append((node.lineno, "a second `SessionStatusBook` is a second source of truth"))
        if not isinstance(node, ast.Attribute) or not _is_book(node.value):
            continue
        if node.attr.startswith("_"):
            found.append((node.lineno, f"`{_dotted(node)}` reaches into the status book"))
        elif node.attr not in BOOK_READERS:
            found.append((node.lineno, f"`{_dotted(node)}` writes the status book"))
    return found


def violations(source: str, rel_path: str) -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` for every violation in *source*.

    Pure detection over source text, so it is directly unit-testable; *rel_path*
    (repo-relative, e.g. ``"omnigent/runner/app.py"``) selects which checks
    apply. :func:`scan` layers inline suppression on top.
    """
    if rel_path == RECORDER_MODULE:
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return []
    found: list[tuple[int, str]] = []
    if rel_path.startswith(SCANNED_ROOTS):
        scopes = _Scopes(tree)
        found.extend(_status_state(tree, scopes))
        found.extend(_status_writes(tree, scopes))
    if rel_path.startswith(BOOK_SCAN_ROOT) and rel_path not in BOOK_OWNERS:
        found.extend(_book_writes(tree))
    return sorted(set(found))


def unsuppressed(source: str, rel_path: str) -> list[tuple[int, str]]:
    """:func:`violations` minus those carrying an inline disable for this rule."""
    disabled = disabled_rules_by_line(source)
    return [
        (line, message)
        for line, message in violations(source, rel_path)
        if RULE_NAME not in disabled.get(line, frozenset())
    ]


def scan(path: Path) -> list[Hit]:
    """Return violations in *path*, honoring inline disables."""
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    return [
        Hit(path, line, message) for line, message in unsuppressed(source, _repo_relative(path))
    ]


def _iter_scannable_paths() -> list[Path]:
    """Return every tracked ``.py`` file under ``omnigent/``."""
    output = subprocess.check_output(["git", "ls-files", "-z", BOOK_SCAN_ROOT])
    return [Path(raw) for raw in output.decode().split("\0") if raw.endswith(".py")]


# Rule identity + fix guidance, consumed by the dev/lint/custom_lint.py runner.
RULE_NAME = "session-status-single-source"
HINT = (
    "Session status has one store: SessionStatusBook (omnigent/runner/session_status.py). "
    "Record a new edge through an existing recorder (_publish_event, the registry watcher's "
    "_publish_status, note_external_session_status, _record_control_idle) or add a "
    "StatusSource; read it only through the book's reader API. A recorded `running` is a "
    "claim that can stay stale forever, so a busy/keep-alive decision must also have "
    "first-hand evidence (a live turn, pane output, a harness probe, a pending prompt). "
    "The runner idle watchdog's native-turn hold is the one exception: a recorded "
    "running/waiting holds it until a ceiling after the last evidence of work, and a "
    "reported dialog (blocked_on) or an open prompt park until "
    "OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S after it opened. "
    "If the container does not hold session status (a lock map, the runner's own turn "
    "tasks, the reaper's clock), exempt it with "
    "`# custom-lint: disable=session-status-single-source -- <what it holds>` on its line "
    "(or `disable-next` on the line above)."
)


def check() -> list[str]:
    """Rule entry point for the custom-lint runner: full-surface scan → messages."""
    return [
        f"{_repo_relative(hit.path)}:{hit.line}: {hit.message}"
        for path in _iter_scannable_paths()
        for hit in scan(path)
    ]


def main(argv: list[str] | None = None) -> int:
    """Scan the given paths (or the full surface) and reject status copies."""
    args = argv if argv is not None else sys.argv
    explicit = [Path(a) for a in args[1:]]
    if explicit:
        messages = [
            f"{_repo_relative(hit.path)}:{hit.line}: {hit.message}"
            for path in explicit
            for hit in scan(path)
        ]
    else:
        messages = check()
    if not messages:
        return 0
    for message in messages:
        sys.stdout.write(f"{message}\n")
    sys.stdout.write(f"\n{HINT}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
