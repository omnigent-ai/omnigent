"""Static guards on how a routed model reaches a live TUI.

Every check here is a source-level invariant, not a behavior: the behavior is
covered in ``tests/harnesses/claude_native/test_claude_native_bridge.py`` (the injector) and
``tests/server/test_turn_routing.py`` (the hook path).

A routed switch is typed as ``/model <id>``. Claude Code answers it with "Set
model to X **and saved as your default for new sessions**" and rewrites the
``model`` key in the person's own ``~/.claude/settings.json``. Driving the
interactive picker instead cost ~530 lines of fragile screen-scraping, so the
bridge puts that one key back after Claude's save. What still has to hold is
that nothing else in Omnigent writes that file, and that the switch path polls
the TUI rather than guessing at its render latency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_OMNIGENT = Path(__file__).resolve().parent.parent / "omnigent"

#: Functions on the claude model-switch path. A fixed sleep in any of them is
#: the row-100 defect: the "Switch model?" dialog took 1.861 s to render on a
#: session with history, so a 0.3 s sleep dropped the confirming Enter.
_SWITCH_PATH_FUNCTIONS = (
    "inject_slash_command",
    "_confirm_tui_dialog",
    # The confirm retry loop lives here, so it inherits the same rule.
    "_confirm_and_verify_dialog_closed",
    "_restore_settings_model",
)


def _tree(relative: str) -> ast.Module:
    """
    Parse one module under ``omnigent/``.

    :param relative: Path relative to the package root, e.g. ``"runner/app.py"``.
    :returns: The parsed module.
    """
    return ast.parse((_OMNIGENT / relative).read_text(encoding="utf-8"))


def test_the_users_claude_settings_file_is_only_ever_read() -> None:
    """
    Guard #18: the bridge's settings readers never write the user's settings file.

    The only write is ``_restore_settings_model`` undoing Claude's own
    ``model`` save. Any other write would clobber hook wiring, permissions
    and everything else in the file, so every reference here must be a read.
    """
    tree = _tree("harnesses/claude_native/bridge.py")
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


_FILE_WRITE_CALLS = frozenset(
    {
        "write_text",
        "write_bytes",
        "open",
        "replace",
        "rename",
        "unlink",
        "chmod",
        "touch",
        "NamedTemporaryFile",
    }
)


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """
    Index a module's functions by name.

    :param tree: Parsed module.
    :returns: Function nodes keyed by name, e.g. ``{"inject_model_command": ...}``.
    """
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _called_names(function: ast.AST) -> set[str]:
    """
    Collect the names a function calls, e.g. ``os.replace(...)`` gives ``"replace"``.

    :param function: Function node.
    :returns: Called function and method names.
    """
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def test_only_the_default_model_restore_writes_the_users_claude_settings() -> None:
    """
    The ``settings.json`` Claude saves ``/model`` to is written by one function.

    Functions that look up the settings path only snapshot the ``model`` key
    or hand the path to ``_restore_settings_model``, the one place allowed to
    rewrite the file.
    """
    functions = _functions(_tree("harnesses/claude_native/bridge.py"))
    path_lookups = {"claude_user_settings_path", "_session_user_settings_path"}
    path_users = {name for name, node in functions.items() if path_lookups & _called_names(node)}
    restore_callers = {
        name
        for name, node in functions.items()
        if "_restore_settings_model" in _called_names(node)
    }

    assert path_users == {"_session_user_settings_path", "inject_model_command"}
    assert restore_callers == {"_restore_default_model"}
    for name in path_users:
        writes = _called_names(functions[name]) & _FILE_WRITE_CALLS
        assert writes == set(), f"{name} writes the user's Claude settings via {sorted(writes)}"


@pytest.mark.parametrize("name", _SWITCH_PATH_FUNCTIONS)
def test_the_model_switch_path_polls_and_never_sleeps_a_fixed_interval(name: str) -> None:
    """
    Guard #21: no ``time.sleep(<literal>)`` on the switch path.

    A sleep on a named poll interval is fine — that is the poll loop's pacing.
    A sleep on a literal is a guess about how long a TUI takes to render, which
    is what row 100 disproved.
    """
    from omnigent.harnesses.claude_native import bridge as claude_native_bridge

    functions = _functions(_tree("harnesses/claude_native/bridge.py"))
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
