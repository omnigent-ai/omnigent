"""Static guards on how a routed model reaches a live TUI.

Every check here is a source-level invariant, not a behavior: the behavior is
covered in ``tests/test_claude_native_bridge.py`` (the injector) and
``tests/server/test_turn_routing.py`` (the hook path).

A routed switch is typed as ``/model <id>``. Claude Code answers it with "Set
model to X **and saved as your default for new sessions**" and rewrites the
``model`` key in the person's own ``~/.claude/settings.json`` — an accepted
trade-off, since the alternative (driving the interactive picker over tmux)
cost ~530 lines of fragile screen-scraping automation. What still has to hold
is that Omnigent itself never writes that file, and that the switch path polls
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
    Guard #18: omnigent reads the user's settings file and never writes it.

    Claude Code rewriting its own ``model`` key on ``/model <id>`` is the
    accepted trade-off. A write from *our* side is not: it would clobber hook
    wiring, permissions and everything else in the file, so every reference
    here must be a read.
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
