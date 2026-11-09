"""Require exactly one hook per supported executor; adapters assert launch consumption."""

import ast
from collections import Counter
from pathlib import Path

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
        for node in ast.walk(ast.parse(path.read_text())):
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
            assert isinstance(executor, ast.Constant), str(path)
            calls[executor.value] += 1
    assert calls == Counter(dict.fromkeys(listed, 1))
