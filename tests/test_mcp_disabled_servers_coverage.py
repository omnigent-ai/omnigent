"""Require exactly one hook per supported executor; adapters assert launch consumption."""

import ast
import contextlib
import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from _pytest.junitxml import LogXML

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


@pytest.mark.parametrize(
    "file_state,expected_state",
    [
        ("ordinary", "parsed"),
        ("at-limit", "parsed"),
        ("oversize", "oversized"),
        ("file-link", "symlink"),
        ("malformed", "unreadable_or_invalid"),
        ("missing-envelope", "parsed"),
        ("nonobject-envelope", "parsed"),
        ("nondict-servers", "parsed"),
        ("claude", "not_applicable"),
    ],
)
def test_native_failure_metadata_is_bounded_and_excludes_private_values(
    tmp_path, monkeypatch, file_state, expected_state
):
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
    startup = {
        "servers": {
            "omnigent": {"status": "failed", "error": private},
            private: {"status": private},
        }
    }
    if file_state == "missing-envelope":
        startup = startup["servers"]
    elif file_state == "nonobject-envelope":
        startup = [startup]
    elif file_state == "nondict-servers":
        startup = {"servers": [startup["servers"]]}
    (bridge / "mcp_startup.json").write_text(json.dumps(startup))
    expected_startup_state = (
        "unreadable_or_invalid"
        if file_state in {"missing-envelope", "nonobject-envelope", "nondict-servers"}
        else expected_state
    )
    for filename in ("tool_relay.json", "mcp_startup.json"):
        path = bridge / filename
        if file_state in {"at-limit", "oversize"}:
            size = 524288 if file_state == "at-limit" else 524289
            path.write_bytes(path.read_bytes().ljust(size, b" "))
            assert path.stat().st_size == size
        elif file_state == "file-link":
            target = tmp_path / filename
            path.rename(target)
            path.symlink_to(target)
        elif file_state == "malformed":
            path.write_text(private)
    known = {
        "type": "namespace",
        "name": "mcp__omnigent",
        "namespace": "omnigent",
        "server_label": "on_fixture",
        "defer_loading": True,
        "tools": [
            {
                "type": "function",
                "name": "sys_session_get_info",
                "description": private,
                "tools": [
                    {
                        "name": "on_fixture",
                        "tools": [{"name": private, "description": private}],
                    }
                ],
            }
        ],
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

    harness = "claude" if file_state == "claude" else "codex"
    with monkeypatch.context() as scoped:
        if harness == "claude":
            bridge_open = MagicMock(side_effect=AssertionError("unexpected Codex bridge read"))
            scoped.setattr("tests.e2e.test_mcp_disabled_servers.os.open", bridge_open)
        metadata = _mcp_failure_metadata(requests, home, "session", harness)
    if harness == "claude":
        bridge_open.assert_not_called()

    assert private not in json.dumps(metadata)
    assert metadata["requests_omitted"] == 2
    assert metadata["descriptor_slots_used"] == 64
    assert [row["ordinal"] for row in metadata["requests"]] == list(range(9, 1, -1))
    projected = metadata["requests"][0]["tools"]
    assert projected["omitted"] > 0
    assert projected["entries"][0]["name"]["known"] == "mcp__omnigent"
    assert (
        projected["entries"][0]["tools"]["entries"][0]["name"]["known"] == "sys_session_get_info"
    )
    nested = projected["entries"][0]["tools"]["entries"][0]["tools"]
    assert nested["count"] == 1
    assert nested["entries"][0]["name"]["known"] == "on_fixture"
    assert nested["entries"][0]["tools"] == {
        "shape": "list",
        "count": 1,
        "entries": [],
        "omitted": 1,
    }
    assert projected["entries"][0]["deferred"]["defer_loading"] is True
    assert projected["entries"][1]["name"]["known"] is None
    assert all(row["tools"]["entries"] == [] for row in metadata["requests"][1:])
    assert metadata["relay"]["state"] == expected_state
    assert metadata["startup_observer_cache"]["state"] == expected_startup_state
    assert (
        metadata["startup_observer_cache"]["individual_success_not_implied_by_settlement"] is True
    )
    if expected_state == "parsed":
        assert metadata["relay"]["count"] == 130
        assert metadata["relay"]["omitted"] == 2
        assert metadata["relay"]["controls_in_inspected_prefix"]["sys_session_get_info"] is True
    else:
        assert metadata["relay"]["tools_shape"] == "NoneType"
        assert metadata["relay"]["count"] is None
        assert metadata["relay"]["omitted"] == 0
        assert metadata["relay"]["controls_in_inspected_prefix"] == {
            "sys_session_get_info": False,
            "sys_session_create": False,
            "sys_terminal_list": False,
        }
    if expected_startup_state == "parsed":
        assert metadata["startup_observer_cache"]["servers"] == {"omnigent": "failed"}
    else:
        assert metadata["startup_observer_cache"]["servers"] == {}


@pytest.mark.parametrize(
    ("outcome", "control"),
    [
        ("timeout", "switch"),
        ("post-success", "switch"),
        ("poll-error", "switch"),
        ("diagnostic-error", "switch"),
        ("timeout", "strict"),
    ],
)
def test_native_failure_traceback_and_junit_exclude_request_bodies(
    tmp_path, monkeypatch, outcome, control
):
    from tests.e2e import test_mcp_disabled_servers as scenario

    fixture_request = MagicMock()
    fixture_request.getfixturevalue.side_effect = {
        "mcp_enabled_server": "http://enabled.example.test",
        "mcp_flagless_server": "http://flagless.example.test",
    }.__getitem__
    selected_server = scenario.mcp_journey_server.__wrapped__(fixture_request, control)
    assert selected_server == (
        "http://enabled.example.test" if control == "switch" else "http://flagless.example.test"
    )

    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value.status_code = 200
    client.get.return_value.json.return_value = {
        "mcp_servers": [
            {"name": name, "harness": "codex"} for name in ("on_fixture", "off_fixture")
        ]
    }
    host = SimpleNamespace(home=tmp_path / "host/home", host_id="host")
    monkeypatch.setattr(scenario.shutil, "which", lambda name: "mock-native")
    monkeypatch.setattr(scenario.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(
        scenario, "isolated_host", lambda *args, **kwargs: contextlib.nullcontext(host)
    )
    monkeypatch.setattr(scenario, "configure_mock_llm", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        scenario, "create_native_session", lambda *args, **kwargs: {"session_id": "session"}
    )
    monkeypatch.setattr(scenario, "send_user_text", lambda *args, **kwargs: None)
    raised = []

    def get_requests(*args, **kwargs):
        body = {
            "input": "-".join(("private", "request", "sentinel")),
            "tools": [
                {
                    "name": "mcp__on_fixture",
                    "description": "-".join(("private", "request", "sentinel")),
                }
            ],
        }
        if outcome == "poll-error":
            try:
                raise ValueError("synthetic request decoding error")
            except ValueError as cause:
                error = RuntimeError("synthetic polling error")
                raised.append(error)
                raise error from cause
        if outcome == "post-success":
            body["tools"].extend([{"name": "mcp__omnigent"}, {"name": "mcp__off_fixture"}])
        return [body]

    def poll(predicate, **kwargs):
        if kwargs["timeout_s"] == 30:
            assert predicate()
            return
        assert kwargs == {"timeout_s": 120}
        if predicate():
            return
        error = AssertionError("synthetic polling deadline")
        raised.append(error)
        raise error

    def fail_diagnostics(requests, *args):
        raise ValueError("synthetic diagnostic error")

    monkeypatch.setattr(scenario, "get_mock_requests", get_requests)
    monkeypatch.setattr(scenario, "poll_until", poll)
    if outcome == "diagnostic-error":
        monkeypatch.setattr(scenario, "_mcp_failure_metadata", fail_diagnostics)
    with pytest.raises((AssertionError, RuntimeError, ValueError)) as captured:
        scenario.test_disabled_server_absent_from_real_native_session(
            selected_server,
            "http://provider.example.test",
            tmp_path,
            "codex",
            control,
        )
    representation = captured.getrepr(style="long", showlocals=True)
    formatted = str(representation)
    report = pytest.TestReport(
        nodeid="synthetic.py::test_native_failure",
        location=("synthetic.py", 0, "test_native_failure"),
        keywords={},
        outcome="failed",
        longrepr=representation,
        when="call",
    )
    xml_path = tmp_path / "failure.xml"
    xml = LogXML(str(xml_path), prefix=None)
    xml.pytest_sessionstart()
    xml.pytest_runtest_logreport(report)
    xml.pytest_sessionfinish()
    from xml.etree import ElementTree

    failure = ElementTree.parse(xml_path).getroot().find(".//failure")
    assert failure is not None and failure.text
    assert "-".join(("private", "request", "sentinel")) not in formatted
    assert "-".join(("private", "request", "sentinel")) not in failure.text
    if raised:
        assert captured.value is raised[0]
    else:
        assert captured.type is AssertionError
        assert (
            captured.traceback[-1].name == "test_disabled_server_absent_from_real_native_session"
        )
        assert captured.value.__cause__ is None and captured.value.__context__ is None
    if outcome == "timeout":
        metadata_path = tmp_path / "mcp-failure-metadata.json"
        assert metadata_path.is_file()
        metadata_text = metadata_path.read_text()
        metadata = json.loads(metadata_text)
        assert metadata["requests_omitted"] == 0
        assert metadata["requests"][0]["tools"]["entries"][0]["name"]["known"] == "mcp__on_fixture"
        assert captured.value.__notes__ == [f"MCP controls never advertised: {metadata}"]
        assert "-".join(("private", "request", "sentinel")) not in metadata_text
    client.delete.assert_called_once_with("/v1/sessions/session")
