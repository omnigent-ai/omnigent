"""Tests for the agy PreToolUse policy hook."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.antigravity_native import hook as agy_hook
from omnigent.harnesses.antigravity_native.audit import build_audit_evaluation_request
from omnigent.harnesses.antigravity_native.bridge import (
    agy_gemini_dir,
    build_policy_hook_entry,
    write_policy_hook_config,
)
from omnigent.native.native_policy_hook import _EVAL_UNAVAILABLE_REASON

_WRITE_CALL = {
    "name": "write_to_file",
    "args": {
        "TargetFile": "/workspace/probe.txt",
        "CodeContent": "x",
        "toolSummary": "Create file",
        "toolAction": "Creating file",
    },
}


def _governed_bridge(tmp_path: Path) -> Path:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    write_policy_hook_config(
        bridge_dir,
        server_url="http://127.0.0.1:8787/",
        session_id="conv/abc",
        auth_headers={"Authorization": "Bearer server-token"},
    )
    return bridge_dir


def _run_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bridge_dir: Path,
    stdin: str,
    response: tuple[httpx.Response | None, str | None] | None = None,
) -> tuple[str, list[tuple[tuple[object, ...], dict[str, object]]]]:
    """Run the hook with *stdin* and return its stdout and the POSTs it attempted."""
    posts: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_post(*args: object, **kwargs: object) -> tuple[httpx.Response | None, str | None]:
        posts.append((args, kwargs))
        assert response is not None, "the hook must not POST in this scenario"
        return response

    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(agy_hook, "post_evaluate_with_retry", fake_post)
    assert agy_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)]) == 0
    return capsys.readouterr().out, posts


def _verdict(body: object) -> tuple[httpx.Response, None]:
    return httpx.Response(200, json=body, request=httpx.Request("POST", "http://x")), None


@pytest.mark.parametrize(
    ("tool_call", "expected_name", "expected_input"),
    [
        (_WRITE_CALL, "write_to_file", {"TargetFile": "/workspace/probe.txt", "CodeContent": "x"}),
        (
            {
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "github",
                    "ToolName": "create_issue",
                    "Arguments": {"title": "t"},
                    "toolSummary": "Create issue",
                },
            },
            "mcp__github__create_issue",
            {"title": "t"},
        ),
    ],
)
def test_evaluate_policy_posts_policy_tool_call_to_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool_call: dict[str, object],
    expected_name: str,
    expected_input: dict[str, object],
) -> None:
    bridge_dir = _governed_bridge(tmp_path)
    payload = {"toolCall": tool_call, "modelName": "gemini-3.1-pro-low", "stepIdx": 4}

    out, posts = _run_hook(
        monkeypatch,
        capsys,
        bridge_dir,
        json.dumps(payload),
        _verdict({"result": "POLICY_ACTION_ALLOW"}),
    )

    assert out == ""
    [(args, kwargs)] = posts
    url, headers, eval_request, timeout, _label = args
    assert url == "http://127.0.0.1:8787/v1/sessions/conv%2Fabc/policies/evaluate"
    assert headers == {"Authorization": "Bearer server-token"}
    assert eval_request == build_audit_evaluation_request(
        tool_name=expected_name, tool_input=expected_input, model="gemini-3.1-pro-low"
    )
    assert timeout == 86400.0
    assert kwargs["reauth"] is not None


def test_evaluate_policy_prefers_session_tool_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bridge_dir = _governed_bridge(tmp_path)
    (bridge_dir / "tool_relay.json").write_text(
        json.dumps({"url": "http://127.0.0.1:9100", "token": "relay-token", "session_id": "s"}),
        encoding="utf-8",
    )

    _out, posts = _run_hook(
        monkeypatch,
        capsys,
        bridge_dir,
        json.dumps({"toolCall": _WRITE_CALL}),
        _verdict({"result": "POLICY_ACTION_ALLOW"}),
    )

    [(args, kwargs)] = posts
    assert args[0] == "http://127.0.0.1:9100/policies/evaluate"
    assert args[1]["Authorization"] == "Bearer relay-token"
    assert kwargs["reauth"] is None


@pytest.mark.parametrize(
    ("verdict", "expected_reason"),
    [
        ({"result": "POLICY_ACTION_DENY", "reason": "writes are blocked"}, "writes are blocked"),
        ({"result": "POLICY_ACTION_ASK", "reason": "needs approval"}, "needs approval"),
    ],
)
def test_evaluate_policy_blocks_constraining_verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verdict: dict[str, str],
    expected_reason: str,
) -> None:
    out, _posts = _run_hook(
        monkeypatch,
        capsys,
        _governed_bridge(tmp_path),
        json.dumps({"toolCall": _WRITE_CALL}),
        _verdict(verdict),
    )

    decision = json.loads(out)
    assert decision["decision"] == "deny"
    assert expected_reason in decision["reason"]


@pytest.mark.parametrize("action", ["POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"])
def test_evaluate_policy_lets_non_constraining_verdicts_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    out, _posts = _run_hook(
        monkeypatch,
        capsys,
        _governed_bridge(tmp_path),
        json.dumps({"toolCall": _WRITE_CALL}),
        _verdict({"result": action}),
    )

    assert out == ""


def test_evaluate_policy_leaves_relay_enforced_omnigent_tools_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "toolCall": {
            "name": "call_mcp_tool",
            "args": {"ServerName": "omnigent", "ToolName": "sys_os_shell", "Arguments": {}},
        }
    }

    out, posts = _run_hook(monkeypatch, capsys, _governed_bridge(tmp_path), json.dumps(payload))

    assert (out, posts) == ("", [])


@pytest.mark.parametrize(
    ("scenario", "expected_detail"),
    [
        ("unreachable", "connection error: simulated"),
        ("malformed_response", "malformed Omnigent response"),
        ("missing_result", "malformed Omnigent response"),
        ("null_result", "malformed Omnigent response"),
        ("unknown_result", "malformed Omnigent response"),
        ("missing_config", "no Omnigent policy hook config"),
        ("invalid_payload", "unrecognized agy hook payload"),
        ("no_tool_call", "unrecognized agy hook payload"),
    ],
)
def test_evaluate_policy_fails_closed_without_a_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    expected_detail: str,
) -> None:
    bridge_dir = _governed_bridge(tmp_path)
    stdin = json.dumps({"toolCall": _WRITE_CALL})
    malformed_bodies: dict[str, object] = {
        "malformed_response": ["not", "an", "object"],
        "missing_result": {"reason": "no action"},
        "null_result": {"result": None},
        "unknown_result": {"result": "POLICY_ACTION_MAYBE"},
    }
    response: tuple[httpx.Response | None, str | None] | None = None
    if scenario == "unreachable":
        response = (None, "connection error: simulated")
    elif scenario in malformed_bodies:
        response = _verdict(malformed_bodies[scenario])
    elif scenario == "missing_config":
        (bridge_dir / "policy_hook.json").unlink()
    elif scenario == "invalid_payload":
        stdin = "not json"
    else:
        stdin = json.dumps({"conversationId": "c"})

    out, _posts = _run_hook(monkeypatch, capsys, bridge_dir, stdin, response)

    decision = json.loads(out)
    assert decision["decision"] == "deny"
    assert decision["reason"].startswith(_EVAL_UNAVAILABLE_REASON)
    assert expected_detail in decision["reason"]


def test_unknown_subcommand_exits_nonzero_so_agy_blocks(tmp_path: Path) -> None:
    assert agy_hook.main(["bogus", "--bridge-dir", str(tmp_path)]) == 2


@pytest.mark.posix_only
def test_registered_hook_command_runs_from_agy_config_dir(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge dir"
    config_dir = agy_gemini_dir(bridge_dir) / "config"
    config_dir.mkdir(parents=True)
    [matcher] = build_policy_hook_entry(bridge_dir)["PreToolUse"]

    result = subprocess.run(
        ["sh", "-c", matcher["hooks"][0]["command"]],
        cwd=config_dir,
        input=json.dumps({"toolCall": _WRITE_CALL}),
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["decision"] == "deny"
    assert "no Omnigent policy hook config" in decision["reason"]
