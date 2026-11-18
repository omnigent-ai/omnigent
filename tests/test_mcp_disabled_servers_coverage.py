"""Require exactly one hook per supported executor; adapters assert launch consumption."""

import ast
import hashlib
import json
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


def test_native_failure_metadata_is_bounded_and_excludes_private_values(tmp_path):
    from tests.e2e.test_mcp_disabled_servers import _mcp_failure_metadata

    home = tmp_path / "home"
    bridge = home / ".omnigent/codex-native" / hashlib.sha256(b"session").hexdigest()[:32]
    bridge.mkdir(parents=True)
    private = "private-value-must-not-appear"
    (bridge / "tool_relay.json").write_text(
        json.dumps(
            {
                "token": private,
                "url": private,
                "tools": [
                    {
                        "name": "sys_session_get_info",
                        "description": private,
                        "parameters": {"key": private},
                    },
                    *({"name": private} for _ in range(129)),
                ],
            }
        )
    )
    (bridge / "mcp_startup.json").write_text(
        json.dumps(
            {
                "omnigent": {"status": "failed", "error": private},
                private: {"status": private},
            }
        )
    )
    known = {
        "type": "namespace",
        "name": "mcp__omnigent",
        "namespace": "omnigent",
        "server_label": "on_fixture",
        "defer_loading": True,
        "tools": [{"type": "function", "name": "sys_session_get_info", "description": private}],
    }
    unknown = {
        "type": private,
        "name": private,
        "namespace": private,
        "server_label": private,
        "deferred": private,
        "description": private,
        "function": {"name": private, "parameters": {"key": private}},
    }
    requests = [{"tools": [known, unknown, *([unknown] * 100)], "input": private}] * 10

    metadata = _mcp_failure_metadata(requests, home, "session")

    assert private not in json.dumps(metadata)
    assert metadata["requests_omitted"] == 2
    assert metadata["descriptor_slots_used"] == 64
    projected = metadata["requests"][0]["tools"]
    assert projected["omitted"] > 0
    assert projected["entries"][0]["name"]["known"] == "mcp__omnigent"
    assert (
        projected["entries"][0]["tools"]["entries"][0]["name"]["known"] == "sys_session_get_info"
    )
    assert projected["entries"][0]["deferred"]["defer_loading"] is True
    assert projected["entries"][1]["name"]["known"] is None
    assert all(row["tools"]["entries"] == [] for row in metadata["requests"][1:])
    assert metadata["relay"]["state"] == "parsed"
    assert metadata["relay"]["count"] == 130
    assert metadata["relay"]["omitted"] == 2
    assert metadata["relay"]["controls_in_inspected_prefix"]["sys_session_get_info"] is True
    assert metadata["startup_observer_cache"]["servers"] == {"omnigent": "failed"}
