"""Unit tests for the claude-native permission mirror.

The mirror is the safety net for a permission prompt the
``PermissionRequest`` hook failed to route to the web UI: it parses Claude's
own prompt out of the pane and raises a card only once the server confirms
none is pending, so the hook keeps ownership whenever it worked.

``_REAL_PANE`` is a verbatim ``capture-pane -p`` of Claude Code v2.1.257 in an
80x24 pane (the geometry omnigent launches), captured while it gated a Bash
call its own analyzer flagged — the shape that stalled a session for ~3h.
"""

from __future__ import annotations

import asyncio

import pytest

import omnigent.claude_native_permissions as cp
from omnigent.claude_native_bridge import (
    read_recent_permission_traces,
    record_permission_trace,
)

_RULE = "─" * 80

_REAL_PANE = (
    "\n"
    "\n"
    "❯ Run exactly this bash command and show its output: for d in a b; do echo $d;\n"
    "  done\n"
    "\n"
    "  Looping over a and b echoing each\n"
    "  ⎿  $ for d in a b; do echo $d; done\n"
    "\n"
    f"{_RULE}\n"
    " Bash command\n"
    ' Tip: auto mode handles these prompts for you — choose "switch to auto mode"\n'
    " below\n"
    "\n"
    "   for d in a b; do echo $d; done\n"
    "   Loop over a and b echoing each\n"
    "\n"
    " Contains simple_expansion\n"
    "\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, and switch to auto mode · auto mode handles these prompts for you\n"
    "   3. No\n"
    "\n"
    " Esc to cancel · Tab to amend\n"
)

# An edit prompt: the rider option sits between Yes and No, and the No row
# carries a trailing clause.
_EDIT_PANE = (
    f"{_RULE}\n"
    " Edit file\n"
    "\n"
    "   omnigent/cli.py\n"
    "\n"
    " Do you want to make this edit to cli.py?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, and allow all edits during this session\n"
    "   3. No, and tell Claude what to do differently\n"
    "\n"
    " Esc to cancel · Tab to amend\n"
)

# Plan review: every option carries a rider, so there is no plain "Yes" to
# answer with. Left to the hook, which renders the full plan.
_PLAN_PANE = (
    f"{_RULE}\n"
    " Ready to code?\n"
    "\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes, and auto-accept edits\n"
    "   2. Yes, and manually approve edits\n"
    "   3. No, keep planning\n"
    "\n"
    " Esc to cancel\n"
)


def test_parses_the_real_prompt_and_reads_its_digits() -> None:
    prompt = cp.parse_claude_permission_prompt(_REAL_PANE)
    assert prompt is not None
    assert prompt.title == "Bash command"
    assert prompt.question == "Do you want to proceed?"
    assert prompt.accept_key == "1"
    assert prompt.decline_key == "3"
    assert "for d in a b; do echo $d; done" in prompt.preview
    # The tip advice and its wrapped continuation are not part of the preview.
    assert "auto mode handles these prompts" not in prompt.preview
    assert "below" not in prompt.preview
    # Only the prompt block feeds the preview, not the transcript above the rule.
    assert "Looping over a and b" not in prompt.preview


def test_rider_option_is_never_mistaken_for_plain_yes() -> None:
    prompt = cp.parse_claude_permission_prompt(_EDIT_PANE)
    assert prompt is not None
    assert prompt.title == "Edit file"
    # "Yes, and allow all edits during this session" must not win accept, and
    # the trailing clause on the No row must not stop it winning decline.
    assert (prompt.accept_key, prompt.decline_key) == ("1", "3")


def test_shapes_without_a_plain_yes_are_left_to_the_hook() -> None:
    assert cp.parse_claude_permission_prompt(_PLAN_PANE) is None


def test_question_without_live_options_is_not_a_prompt() -> None:
    # Claude quoting the question in its own prose must not raise a card.
    assert (
        cp.parse_claude_permission_prompt(
            "● I asked: Do you want to proceed? but you never answered.\n"
        )
        is None
    )
    # Options above the question (a stale frame) are not an answerable prompt.
    assert (
        cp.parse_claude_permission_prompt(" ❯ 1. Yes\n   3. No\n Do you want to proceed?\n")
        is None
    )
    assert cp.parse_claude_permission_prompt("") is None


def test_an_option_list_without_the_prompt_footer_is_prose() -> None:
    # Claude enumerating choices in its own message must not raise a card —
    # the stray digit a spurious verdict sends would land in the composer.
    quoted = (
        "● You have three options. Do you want to proceed?\n  1. Yes\n  3. No\n● Tell me which.\n"
    )
    assert cp.parse_claude_permission_prompt(quoted) is None
    # The same block under Claude's real footer is a live prompt.
    assert cp.parse_claude_permission_prompt(quoted + " Esc to cancel\n") is not None


def test_signature_ignores_the_selection_caret_but_tracks_the_call() -> None:
    moved = _REAL_PANE.replace(" ❯ 1. Yes", "   1. Yes").replace("   3. No", " ❯ 3. No")
    first = cp.parse_claude_permission_prompt(_REAL_PANE)
    second = cp.parse_claude_permission_prompt(moved)
    assert first is not None and second is not None
    # Moving the caret is a re-render of the same prompt, not a new one.
    assert first.signature == second.signature
    other = cp.parse_claude_permission_prompt(_REAL_PANE.replace("echo $d", "echo $DIFFERENT"))
    assert other is not None
    assert other.signature != first.signature


def test_elicitation_id_is_keyed_by_signature() -> None:
    prompt = cp.parse_claude_permission_prompt(_REAL_PANE)
    assert prompt is not None
    assert cp.claude_permission_elicitation_id("conv_x", prompt.signature) == (
        f"elicit_claude_pane_conv_x_{prompt.signature}"
    )


class _Resp:
    def __init__(self, status: int = 200, content: bytes = b'{"action":"accept"}', payload=None):
        self.status_code = status
        self.content = content
        self._payload = payload if payload is not None else {"action": "accept"}
        self.text = content.decode() if isinstance(content, bytes) else str(content)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp: _Resp, snapshot: _Resp | None = None) -> None:
        self._resp = resp
        self._snapshot = snapshot
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    async def post(self, url, json=None, **_kwargs):
        self.posts.append((url, json or {}))
        return self._resp

    async def get(self, url, **_kwargs):
        self.gets.append(url)
        assert self._snapshot is not None, "unexpected snapshot read"
        return self._snapshot


def _prompt() -> cp.ClaudePermissionPrompt:
    parsed = cp.parse_claude_permission_prompt(_REAL_PANE)
    assert parsed is not None
    return parsed


async def test_accept_verdict_sends_the_yes_digit(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(cp, "send_claude_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(cp, "_prompt_still_shown", lambda _bd, _sig: True)
    client = _FakeClient(_Resp(payload={"action": "accept"}))
    await cp._run_one_approval(
        client, session_id="c", bridge_dir=tmp_path, prompt=_prompt(), elicitation_id="e1"
    )
    assert sent == [("1",)]
    url, body = client.posts[0]
    assert url.endswith("/hooks/native-permission-request")
    assert body["agent"] == "Claude Code"
    assert body["elicitation_id"] == "e1"
    # A decline is answered with the No digit, so the turn must not also be
    # interrupted — Claude continues from its own denial.
    assert body["interrupt_on_decline"] is False


async def test_decline_verdict_sends_the_no_digit(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(cp, "send_claude_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(cp, "_prompt_still_shown", lambda _bd, _sig: True)
    await cp._run_one_approval(
        _FakeClient(_Resp(payload={"action": "decline"})),
        session_id="c",
        bridge_dir=tmp_path,
        prompt=_prompt(),
        elicitation_id="e1",
    )
    assert sent == [("3",)]


async def test_no_keystroke_when_the_prompt_already_went_away(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(cp, "send_claude_pane_keys", lambda _bd, *keys: sent.append(keys))
    # Answered in the terminal while the web verdict was in flight: a digit now
    # would be typed into the composer as a message.
    monkeypatch.setattr(cp, "_prompt_still_shown", lambda _bd, _sig: False)
    await cp._run_one_approval(
        _FakeClient(_Resp(payload={"action": "accept"})),
        session_id="c",
        bridge_dir=tmp_path,
        prompt=_prompt(),
        elicitation_id="e1",
    )
    assert sent == []


async def test_empty_2xx_and_error_send_nothing(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(cp, "send_claude_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(cp, "_prompt_still_shown", lambda _bd, _sig: True)
    for resp in (_Resp(content=b""), _Resp(status=500, content=b"boom")):
        await cp._run_one_approval(
            _FakeClient(resp),
            session_id="c",
            bridge_dir=tmp_path,
            prompt=_prompt(),
            elicitation_id="e1",
        )
    assert sent == []


async def test_external_elicitation_resolved_targets_events(tmp_path) -> None:
    client = _FakeClient(_Resp(status=200, content=b""))
    await cp._post_external_elicitation_resolved(client, "conv_z", "e9")
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_z/events"
    assert body["type"] == "external_elicitation_resolved"
    assert body["data"]["elicitation_id"] == "e9"


def _drive_supervisor(monkeypatch, tmp_path, *, panes, snapshot, polls):
    """Run the supervisor over a scripted pane sequence; return minted ids."""
    seq = {"i": 0}

    def _capture(_bd):
        i = seq["i"]
        seq["i"] += 1
        return panes[i] if i < len(panes) else panes[-1]

    monkeypatch.setattr(cp, "capture_claude_pane", _capture)
    monkeypatch.setattr(cp, "_prompt_still_shown", lambda _bd, _sig: True)
    created: list[str] = []

    async def _fake_run_one(_client, *, session_id, bridge_dir, prompt, elicitation_id):
        created.append(elicitation_id)

    monkeypatch.setattr(cp, "_run_one_approval", _fake_run_one)

    client = _FakeClient(_Resp(status=200, content=b""), snapshot=snapshot)

    class _Ctx:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(cp, "open_server_client", lambda *_a, **_kw: _Ctx(), raising=False)
    monkeypatch.setattr("omnigent.cli_auth.open_server_client", lambda *_a, **_kw: _Ctx())

    sleeps = {"n": 0}

    async def _sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= polls:
            raise asyncio.CancelledError

    monkeypatch.setattr(cp.asyncio, "sleep", _sleep)
    return created, client


async def test_stands_down_while_the_hook_owns_the_prompt(tmp_path, monkeypatch) -> None:
    # Server reports a parked elicitation → the hook route worked, so the
    # mirror must not raise a second card for the same call.
    created, client = _drive_supervisor(
        monkeypatch,
        tmp_path,
        panes=[_REAL_PANE],
        snapshot=_Resp(payload={"pending_elicitations": [{"elicitation_id": "elicit_claude_x"}]}),
        polls=3,
    )
    with pytest.raises(asyncio.CancelledError):
        await cp.supervise_claude_permission_mirror(
            base_url="http://x",
            headers={},
            session_id="c",
            bridge_dir=tmp_path,
            grace_s=0.0,
            recheck_interval_s=0.0,
        )
    assert created == []
    assert client.gets  # it did ask


async def test_surfaces_the_prompt_when_no_card_is_pending(tmp_path, monkeypatch) -> None:
    created, _client = _drive_supervisor(
        monkeypatch,
        tmp_path,
        panes=[_REAL_PANE],
        snapshot=_Resp(payload={"pending_elicitations": []}),
        polls=3,
    )
    with pytest.raises(asyncio.CancelledError):
        await cp.supervise_claude_permission_mirror(
            base_url="http://x",
            headers={},
            session_id="c",
            bridge_dir=tmp_path,
            grace_s=0.0,
            recheck_interval_s=0.0,
        )
    prompt = _prompt()
    # One card for the episode, keyed to the prompt, not one per poll.
    assert created == [cp.claude_permission_elicitation_id("c", prompt.signature)]


async def test_unreadable_snapshot_does_not_mint_a_card(tmp_path, monkeypatch) -> None:
    # Cannot tell whether a card exists → stand down rather than duplicate.
    created, _client = _drive_supervisor(
        monkeypatch,
        tmp_path,
        panes=[_REAL_PANE],
        snapshot=_Resp(status=503, content=b"nope", payload={}),
        polls=3,
    )
    with pytest.raises(asyncio.CancelledError):
        await cp.supervise_claude_permission_mirror(
            base_url="http://x",
            headers={},
            session_id="c",
            bridge_dir=tmp_path,
            grace_s=0.0,
            recheck_interval_s=0.0,
        )
    assert created == []


async def test_prompt_must_persist_past_the_grace_window(tmp_path, monkeypatch) -> None:
    # A prompt answered before the grace window elapses is never surfaced. The
    # clock is left real here so the window does not pass.
    seq = {"i": 0}
    panes = [_REAL_PANE, None]

    def _capture(_bd):
        i = seq["i"]
        seq["i"] += 1
        return panes[i] if i < len(panes) else None

    monkeypatch.setattr(cp, "capture_claude_pane", _capture)
    created: list[str] = []

    async def _fake_run_one(_client, **_kwargs):
        created.append("x")

    monkeypatch.setattr(cp, "_run_one_approval", _fake_run_one)

    class _Ctx:
        async def __aenter__(self):
            return _FakeClient(_Resp(status=200, content=b""))

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", lambda *_a, **_kw: _Ctx())
    sleeps = {"n": 0}

    async def _sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(cp.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await cp.supervise_claude_permission_mirror(
            base_url="http://x", headers={}, session_id="c", bridge_dir=tmp_path
        )
    assert created == []


def test_permission_trace_round_trips_and_is_capped(tmp_path) -> None:
    record_permission_trace(tmp_path, "posting", tool_name="Bash")
    record_permission_trace(tmp_path, "undelivered", tool_name="Bash")
    entries = read_recent_permission_traces(tmp_path, limit=5)
    assert [entry["outcome"] for entry in entries] == ["posting", "undelivered"]
    assert entries[-1]["tool_name"] == "Bash"
    assert isinstance(entries[-1]["pid"], int)


def test_permission_trace_never_raises_on_an_unwritable_dir(tmp_path) -> None:
    # Tracing must never turn a live permission prompt into a failed hook.
    record_permission_trace(tmp_path / "missing" / "deeper", "undelivered")
    assert read_recent_permission_traces(tmp_path / "missing" / "deeper") == []
