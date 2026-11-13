"""Static guards on how a routed model reaches a live TUI.

Every check here is a source-level invariant, not a behavior: the behavior is
covered in ``tests/harnesses/claude_native/test_claude_native_bridge.py`` (the injector) and
``tests/server/test_turn_routing.py`` (the hook path).

A routed switch is typed as ``/model <id>``. Claude Code answers it with "Set
model to X **and saved as your default for new sessions**" and rewrites the
``model`` key in the person's own ``~/.claude/settings.json``. The dedicated
default-model guard restores that key after an Omnigent switch; its behavior
is covered in ``test_default_model_guard.py``. These static checks keep the
switch path polling the TUI rather than guessing its latency.
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


@pytest.mark.parametrize("name", _SWITCH_PATH_FUNCTIONS)
def test_the_model_switch_path_polls_and_never_sleeps_a_fixed_interval(name: str) -> None:
    """
    Guard #21: no ``time.sleep(<literal>)`` on the switch path.

    A sleep on a named poll interval is fine — that is the poll loop's pacing.
    A sleep on a literal is a guess about how long a TUI takes to render, which
    is what row 100 disproved.
    """
    from omnigent.harnesses.claude_native import bridge as claude_native_bridge

    tree = _tree("harnesses/claude_native/bridge.py")
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
