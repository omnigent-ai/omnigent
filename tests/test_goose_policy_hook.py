"""Unit tests for the goose PreToolUse Omnigent-policy hook entrypoint."""

from __future__ import annotations

import io
import json

import omnigent.inner.goose_policy_hook as h
import omnigent.native_policy_hook as nph


class _Resp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _drive(monkeypatch, capsys, *, payload, resp="__none__", raise_exc=False, env=True) -> dict:
    if env:
        monkeypatch.setenv("_OMNIGENT_SERVER_URL", "http://127.0.0.1:6767")
        monkeypatch.setenv("_OMNIGENT_SESSION_ID", "conv_1")
    else:
        monkeypatch.delenv("_OMNIGENT_SERVER_URL", raising=False)
        monkeypatch.delenv("_OMNIGENT_SESSION_ID", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    captured: dict = {}

    def _fake_post(**kwargs):
        captured.update(kwargs)
        if raise_exc:
            raise RuntimeError("boom")
        return None if resp == "__none__" else _Resp(resp)

    monkeypatch.setattr(nph, "post_evaluate_with_retry", _fake_post)
    h.main()
    out = capsys.readouterr().out
    result = json.loads(out) if out.strip() else {}
    result["__captured__"] = captured
    return result


_TOOL = {"event": "PreToolUse", "tool_name": "shell", "tool_input": {"command": "rm -rf /"}}


def test_deny_blocks_and_posts_tool_call(monkeypatch, capsys) -> None:
    out = _drive(
        monkeypatch, capsys, payload=_TOOL, resp={"result": "POLICY_ACTION_DENY", "reason": "no"}
    )
    assert out["decision"] == "block"
    assert "denied by Omnigent policy" in out["reason"]
    body = out["__captured__"]["eval_request"]
    assert body["event"]["type"] == "PHASE_TOOL_CALL"
    assert body["event"]["data"] == {"name": "shell", "arguments": {"command": "rm -rf /"}}


def test_allow_emits_empty(monkeypatch, capsys) -> None:
    out = _drive(monkeypatch, capsys, payload=_TOOL, resp={"result": "POLICY_ACTION_ALLOW"})
    assert "decision" not in out  # empty {} emitted → no block


def test_unspecified_emits_empty(monkeypatch, capsys) -> None:
    out = _drive(monkeypatch, capsys, payload=_TOOL, resp={"result": "POLICY_ACTION_UNSPECIFIED"})
    assert "decision" not in out


def test_ask_fails_closed_to_block(monkeypatch, capsys) -> None:
    # ASK should be collapsed server-side; if it reaches the hook, block.
    out = _drive(monkeypatch, capsys, payload=_TOOL, resp={"result": "POLICY_ACTION_ASK"})
    assert out["decision"] == "block"
    assert "requires approval" in out["reason"]


def test_network_failure_fails_closed(monkeypatch, capsys) -> None:
    out = _drive(monkeypatch, capsys, payload=_TOOL, resp="__none__")  # post returns None
    assert out["decision"] == "block"
    assert "unavailable" in out["reason"]


def test_import_or_unexpected_error_fails_open(monkeypatch, capsys) -> None:
    # An unexpected exception in the POST path fails OPEN (empty) — the call site
    # treats a thrown error as "couldn't evaluate", not "deny".
    out = _drive(monkeypatch, capsys, payload=_TOOL, raise_exc=True)
    assert "decision" not in out


def test_no_env_fails_open(monkeypatch, capsys) -> None:
    out = _drive(monkeypatch, capsys, payload=_TOOL, env=False)
    assert "decision" not in out
    assert out["__captured__"] == {}  # never even POSTed


def test_non_dict_payload_allows(monkeypatch, capsys) -> None:
    out = _drive(monkeypatch, capsys, payload=["not", "a", "dict"], resp={"result": "X"})
    assert "decision" not in out
