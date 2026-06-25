"""Unit tests for the goose-native approval mirror's pane parser."""

from __future__ import annotations

import asyncio

import pytest

import omnigent.goose_native_permissions as gp
from omnigent.goose_native_permissions import (
    goose_permission_elicitation_id,
    parse_goose_approval_prompt,
)

# cliclack radio with "Always Allow" → Deny is the 3rd item (2 downs from Allow).
_THREE_ITEM = (
    "│ developer__shell\n"
    "│ command: rm -rf /tmp/x\n"
    "◆ Goose would like to call the above tool, do you allow?\n"
    "│ ● Allow          Allow the tool call once\n"
    "│ ○ Always Allow   Always allow the tool call\n"
    "│ ○ Deny           Deny the tool call\n"
    "│ ○ Cancel         Cancel the AI response and tool call\n"
)

# Security-prompt variant: no "Always Allow" → Deny is the 2nd item (1 down).
_TWO_ITEM = (
    "⚠ this command writes files\n"
    "◆ Do you allow this tool call?\n"
    "│ ● Allow   Allow the tool call once\n"
    "│ ○ Deny    Deny the tool call\n"
    "│ ○ Cancel  Cancel the AI response and tool call\n"
)


def test_parses_three_item_prompt_and_deny_index() -> None:
    prompt = parse_goose_approval_prompt(_THREE_ITEM)
    assert prompt is not None
    # Allow(0) → Always Allow(1) → Deny(2): two Down presses.
    assert prompt.deny_down_count == 2
    # Subject is scraped from the tool-request lines above the question.
    assert "developer__shell" in prompt.subject
    assert prompt.block_hash


def test_parses_two_item_prompt_and_deny_index() -> None:
    prompt = parse_goose_approval_prompt(_TWO_ITEM)
    assert prompt is not None
    # Allow(0) → Deny(1): one Down press.
    assert prompt.deny_down_count == 1


def test_requires_question_and_both_items() -> None:
    # Question but no Deny item → not a confirmation block.
    assert parse_goose_approval_prompt("◆ do you allow?\n│ ● Allow\n") is None
    # Items but no question → not live.
    assert parse_goose_approval_prompt("│ ● Allow\n│ ○ Deny\n") is None
    assert parse_goose_approval_prompt("") is None


def test_block_hash_differs_per_tool_and_id_is_deterministic() -> None:
    a = parse_goose_approval_prompt(_THREE_ITEM)
    other = _THREE_ITEM.replace("rm -rf /tmp/x", "cat /etc/passwd")
    b = parse_goose_approval_prompt(other)
    assert a is not None and b is not None
    assert a.block_hash != b.block_hash
    eid = goose_permission_elicitation_id("conv_9", a.block_hash)
    assert eid == goose_permission_elicitation_id("conv_9", a.block_hash)
    assert eid.startswith("elicit_goose_conv_9_")


# --- mirror plumbing (web verdict → cliclack keystrokes; TUI answer → release) -


class _Resp:
    def __init__(self, status: int = 200, content: bytes = b'{"action":"accept"}', payload=None):
        self.status_code = status
        self.content = content
        self._payload = payload if payload is not None else {"action": "accept"}
        self.text = content.decode() if isinstance(content, bytes) else str(content)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, json=None, **_kwargs):
        self.posts.append((url, json or {}))
        return self._resp


def _gprompt(deny_downs: int = 2) -> gp.GooseApprovalPrompt:
    return gp.GooseApprovalPrompt(
        subject="developer__shell rm -rf x",
        message="m",
        preview="developer__shell",
        deny_down_count=deny_downs,
        block_hash="h",
    )


async def test_run_one_approval_accept_presses_enter(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    await gp._run_one_approval(
        _FakeClient(_Resp(payload={"action": "accept"})),
        session_id="c",
        bridge_dir=tmp_path,
        prompt=_gprompt(),
        elicitation_id="e1",
    )
    assert sent == [("Enter",)]  # Allow is the default-highlighted item


async def test_run_one_approval_decline_walks_to_deny(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    await gp._run_one_approval(
        _FakeClient(_Resp(payload={"action": "decline"})),
        session_id="c",
        bridge_dir=tmp_path,
        prompt=_gprompt(deny_downs=2),
        elicitation_id="e1",
    )
    assert sent == [("Down", "Down", "Enter")]  # Allow → Always Allow → Deny


async def test_run_one_approval_empty_and_error_send_nothing(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    await gp._run_one_approval(
        _FakeClient(_Resp(content=b"")),
        session_id="c",
        bridge_dir=tmp_path,
        prompt=_gprompt(),
        elicitation_id="e1",
    )
    await gp._run_one_approval(
        _FakeClient(_Resp(status=503, content=b"down")),
        session_id="c",
        bridge_dir=tmp_path,
        prompt=_gprompt(),
        elicitation_id="e1",
    )
    assert sent == []


async def test_post_external_elicitation_resolved_targets_events(tmp_path) -> None:
    client = _FakeClient(_Resp(status=200, content=b""))
    await gp._post_external_elicitation_resolved(client, "conv_g", "e3")
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_g/events"
    assert body["type"] == "external_elicitation_resolved"


async def test_supervise_raises_one_card_per_episode(tmp_path, monkeypatch) -> None:
    panes = [_THREE_ITEM, _THREE_ITEM, None]
    seq = {"i": 0}

    def _cap(_bd):
        i = seq["i"]
        seq["i"] += 1
        return panes[i] if i < len(panes) else None

    monkeypatch.setattr(gp, "capture_goose_pane", _cap)
    created: list[str] = []

    async def _fake_run_one(_client, *, session_id, bridge_dir, prompt, elicitation_id):
        created.append(elicitation_id)

    monkeypatch.setattr(gp, "_run_one_approval", _fake_run_one)

    sleeps = {"n": 0}

    async def _sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(gp.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await gp.supervise_goose_approval_mirror(
            base_url="http://x", headers={}, session_id="c", bridge_dir=tmp_path
        )
    assert len(created) == 1
