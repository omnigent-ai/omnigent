"""Static guards on how a routed model reaches a live TUI.

Every check here is a source-level invariant, not a behavior: the behavior is
covered in ``tests/test_claude_native_bridge.py`` (the picker actuator) and
``tests/server/test_turn_routing.py`` (the hook path). What these guards catch is
the class of regression that passes every behavioral test — a *new* call site
that switches a model the old, damaging way.

The damaging way is ``/model <id>``: Claude Code answers it with "Set model to X
**and saved as your default for new sessions**" and rewrites the ``model`` key in
the user's own ``~/.claude/settings.json``. Omnigent never writes that file, so
the only defence is never emitting the argument form on a routing path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_OMNIGENT = Path(__file__).resolve().parent.parent / "omnigent"

#: Every module that can put a model switch onto a live claude pane. All three
#: routing call sites (``runner/turn_routing.py``, ``inner/claude_native_executor
#: .py``) plus ``runner/app.py``'s web/API model-change endpoint go through
#: ``claude_native_bridge.inject_model_selection``.
_CLAUDE_SWITCH_MODULES = (
    "claude_native_bridge.py",
    "runner/app.py",
    "runner/turn_routing.py",
    "inner/claude_native_executor.py",
    "claude_native_hook.py",
)

#: Harnesses that still type the argument form. Documented exposure, not a
#: routing path: neither bridge is reachable from a routing decision, and the
#: picker port is deliberately deferred. Pinned so it cannot spread quietly.
_DOCUMENTED_ARG_FORM_BRIDGES = {
    "cursor_native_bridge.py": 1,
    "kiro_native_bridge.py": 1,
}

#: Functions on the claude model-switch path. A fixed sleep in any of them is
#: the row-100 defect: the "Switch model?" dialog took 1.861 s to render on a
#: session with history, so a 0.3 s sleep dropped the confirming Enter.
_SWITCH_PATH_FUNCTIONS = (
    "inject_model_selection",
    "_confirm_tui_dialog",
    "_wait_for_model_picker",
    "_wait_for_model_picker_applied",
    "_walk_model_picker_cursor",
)


def _tree(relative: str) -> ast.Module:
    """
    Parse one module under ``omnigent/``.

    :param relative: Path relative to the package root, e.g. ``"runner/app.py"``.
    :returns: The parsed module.
    """
    return ast.parse((_OMNIGENT / relative).read_text(encoding="utf-8"))


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """
    Collect the ``id()`` of every docstring constant in *tree*.

    Docstrings quote the argument form on purpose — the picker helper's own
    docstring explains why it is forbidden — so they are not call sites.

    :param tree: A parsed module.
    :returns: Identities of the string constants used as docstrings.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = node.body[0] if node.body else None
            if (
                isinstance(doc, ast.Expr)
                and isinstance(doc.value, ast.Constant)
                and isinstance(doc.value.value, str)
            ):
                ids.add(id(doc.value))
    return ids


def _arg_form_lines(relative: str) -> list[int]:
    """
    Line numbers where *relative* builds a ``/model <arg>`` command.

    Matches a plain string literal and an f-string alike, and ignores
    docstrings and comments (comments never reach the AST). A **bare**
    ``"/model"`` — what the picker submits — has no trailing space and is not a
    match, which is exactly the distinction being guarded.

    :param relative: Path relative to the package root.
    :returns: Sorted line numbers of the offending literals.
    """
    tree = _tree(relative)
    skip = _docstring_nodes(tree)
    # An f-string's own literal head is not a second call site.
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values if isinstance(part, ast.Constant))
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in skip:
            if isinstance(node.value, str) and node.value.startswith("/model "):
                hits.append(node.lineno)
        elif isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if (
                isinstance(head, ast.Constant)
                and isinstance(head.value, str)
                and head.value.startswith("/model ")
            ):
                hits.append(node.lineno)
    return sorted(hits)


@pytest.mark.parametrize("relative", _CLAUDE_SWITCH_MODULES)
def test_no_claude_routing_path_builds_the_model_argument_form(relative: str) -> None:
    """
    Guard #19: the argument form appears on no claude switch path.

    ``runner/app.py`` is in the list because the web/API model-change endpoint
    used to build ``f"/model {resolved_model}"`` — the same defect as the
    routing path, reached by a different door.
    """
    assert _arg_form_lines(relative) == [], (
        f"{relative} builds a '/model <arg>' command. That saves the pick as the "
        "user's global default; switch through "
        "claude_native_bridge.inject_model_selection instead."
    )


@pytest.mark.parametrize(("relative", "expected"), sorted(_DOCUMENTED_ARG_FORM_BRIDGES.items()))
def test_the_remaining_arg_form_exposure_stays_where_it_is_documented(
    relative: str, expected: int
) -> None:
    """
    Guard #20: cursor and kiro keep the argument form, and only they do.

    Decision on record: guard the exposure rather than port the picker now.
    A count change means either a new arg-form site (spreading the hazard) or
    the port landing — both need this test updated deliberately.
    """
    assert len(_arg_form_lines(relative)) == expected


def test_no_other_bridge_grows_an_arg_form_model_switch() -> None:
    """
    Guard #20, the other half: the exposure list is exhaustive.

    Scans every ``*_native_bridge.py`` so a newly added harness cannot inherit
    the hazard without this test naming it.
    """
    offenders = {
        path.name: _arg_form_lines(path.relative_to(_OMNIGENT).as_posix())
        for path in sorted(_OMNIGENT.glob("*_native_bridge.py"))
        if _arg_form_lines(path.relative_to(_OMNIGENT).as_posix())
    }
    assert set(offenders) == set(_DOCUMENTED_ARG_FORM_BRIDGES)


def test_the_users_claude_settings_file_is_only_ever_read() -> None:
    """
    Guard #18: omnigent reads the user's settings file and never writes it.

    The row-95 rewrite was Claude Code's own, provoked by the argument form —
    so a write from *our* side would be a second, independent way to clobber
    someone's global default. Every reference must be a read.
    """
    tree = _tree("claude_native_bridge.py")
    reads: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not (
            isinstance(node.value, ast.Name) and node.value.id == "_USER_CLAUDE_SETTINGS_PATH"
        ):
            continue
        reads.append(node.attr)
    # The definition itself is an assignment, not an attribute access, so every
    # hit here is a use.
    assert reads, "the guard found no usages at all — has the constant been renamed?"
    assert set(reads) <= {"read_text", "read_bytes", "exists", "is_file"}, (
        f"non-read access to the user's settings file: {sorted(set(reads))}"
    )


@pytest.mark.parametrize("name", _SWITCH_PATH_FUNCTIONS)
def test_the_model_switch_path_polls_and_never_sleeps_a_fixed_interval(name: str) -> None:
    """
    Guard #21: no ``time.sleep(<literal>)`` on the switch path.

    A sleep on a named poll interval is fine — that is the poll loop's pacing.
    A sleep on a literal is a guess about how long a TUI takes to render, which
    is what row 100 disproved.
    """
    from omnigent import claude_native_bridge

    tree = _tree("claude_native_bridge.py")
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert name in functions, f"{name} is gone — the switch path moved, update this guard"
    literals: list[float] = []
    for node in ast.walk(functions[name]):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "sleep" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, int | float):
            literals.append(arg.value)
    assert literals == [], f"{name} sleeps a fixed {literals}s instead of polling"
    # And the pacing constant the loops do use is a real, small poll interval.
    assert 0 < claude_native_bridge._CLAUDE_READY_POLL_INTERVAL_S <= 1.0
