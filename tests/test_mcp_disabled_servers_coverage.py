"""Supported inventory families must cover every executor and exactly one hook each."""

import ast
from collections import Counter
from pathlib import Path

import pytest

from omnigent.harness_aliases import harness_account_key
from omnigent.harness_plugins import valid_harnesses
from omnigent.mcp_disabled_servers import HARNESS_EXECUTORS


def test_every_supported_executor_has_exactly_one_launch_hook():
    listed = {executor for executors in HARNESS_EXECUTORS.values() for executor in executors}
    supported = {
        executor
        for executor in valid_harnesses()
        if harness_account_key(executor) in HARNESS_EXECUTORS
    }
    assert listed == supported
    calls = Counter()
    root = Path(__file__).resolve().parents[1] / "omnigent"
    for path in root.rglob("*.py"):
        calls.update(_used_launch_hooks(ast.parse(path.read_text()), str(path)))
    assert calls == Counter(dict.fromkeys(listed, 1))


def _used_launch_hooks(tree, source="fixture"):
    parents = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    calls = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else None
        )
        if name not in {"apply_claude_settings", "plan_codex_launch"}:
            continue
        executor = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "executor"), None
        )
        assert isinstance(executor, ast.Constant), source
        calls[executor.value] += 1
        assignment = parents[node]
        if isinstance(assignment, ast.Await):
            assignment = parents[assignment]
        assert isinstance(assignment, ast.Assign), f"{source}: discarded hook result"
        assert len(assignment.targets) == 1 and isinstance(assignment.targets[0], ast.Name), source
        result_name = assignment.targets[0].id
        scope = parents[assignment]
        while not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            scope = parents[scope]
        assert any(
            isinstance(use, ast.Name)
            and isinstance(use.ctx, ast.Load)
            and use.id == result_name
            and use.lineno > node.end_lineno
            for use in ast.walk(scope)
        ), f"{source}: unused hook result {result_name}"
    return calls


def test_hook_coverage_counts_attribute_calls():
    tree = ast.parse("""async def launch():
    plan = module.plan_codex_launch(executor="codex")
    settings = apply_claude_settings(executor="claude-sdk")
    return plan.config_overrides, settings
""")
    assert _used_launch_hooks(tree) == {"codex": 1, "claude-sdk": 1}


@pytest.mark.parametrize(
    "statement",
    [
        'module.plan_codex_launch(executor="codex")',
        'await module.plan_codex_launch(executor="codex")',
        'unused = module.plan_codex_launch(executor="codex")',
        'unused = await module.plan_codex_launch(executor="codex")',
    ],
)
def test_hook_coverage_rejects_discarded_results(statement):
    with pytest.raises(AssertionError, match=r"(discarded|unused) hook result"):
        _used_launch_hooks(ast.parse(f"async def launch():\n    {statement}\n"))
