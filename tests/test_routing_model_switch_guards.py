"""Keep the bounded session-only picker path polling observed UI transitions."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_OMNIGENT = Path(__file__).resolve().parent.parent / "omnigent"

_SWITCH_PATH_FUNCTIONS = (
    ("bridge.py", "inject_model_command"),
    ("model_switch.py", "wait"),
    ("model_switch.py", "command"),
)


def _tree(relative: str) -> ast.Module:
    """
    Parse one module under ``omnigent/``.

    :param relative: Path relative to the package root, e.g. ``"runner/app.py"``.
    :returns: The parsed module.
    """
    return ast.parse((_OMNIGENT / relative).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("module", "name"), _SWITCH_PATH_FUNCTIONS)
def test_the_model_switch_path_polls_and_never_sleeps_a_fixed_interval(
    module: str, name: str
) -> None:
    """
    Guard #21: no ``time.sleep(<literal>)`` on the switch path.

    A sleep on a named poll interval is fine — that is the poll loop's pacing.
    A sleep on a literal is a guess about how long a TUI takes to render, which
    is what row 100 disproved.
    """
    from omnigent.harnesses.claude_native import bridge as claude_native_bridge

    tree = _tree(f"harnesses/claude_native/{module}")
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
