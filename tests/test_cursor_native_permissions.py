"""Unit tests for cursor-native elicitation surfacing.

Covers everything a live cursor-agent isn't needed for, with the store + tmux +
HTTP boundaries faked:

* **Transcript detection** — reading pending tool calls out of the chat
  ``store.db`` (incl. binary checkpoint frames), suppressing resolved/auto-run
  calls, and the stable elicitation-id format.
* **Supervisor** — surfacing a settled pending call, the debounce that drops
  auto-approved calls, and the TUI-resolved release.
* **Verdict delivery** — ``_run_one_approval`` (park → verdict → keystroke,
  incl. the reject → reason-prompt → Enter two-step) and ``_run_one_question``
  (AskQuestion form → picker keystrokes).
* **Bridge helpers** — ``capture_cursor_pane`` / ``send_cursor_pane_keys`` with
  the tmux primitives monkeypatched.

The *live* tmux + cursor-agent path (real detect → POST → keystroke end-to-end)
is exercised by ``tests/e2e/test_cursor_native_cli_e2e.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import sqlite3 as _sqlite3
from pathlib import Path

import httpx
import pytest

from omnigent import cursor_native_bridge as cnb
from omnigent import cursor_native_permissions as cnp
from omnigent.cursor_native_permissions import (
    CursorApprovalPrompt,
    CursorPendingToolCall,
    cursor_tool_call_elicitation_id,
    read_cursor_pending_tool_calls,
)


class _QueueClient:
    """Async httpx-client stub: records POSTs, returns queued responses in order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._responses = list(responses)

    async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
        self.posts.append((url, json))
        return self._responses.pop(0)


@pytest.mark.parametrize(
    ("response", "expected_keys"),
    [
        pytest.param(httpx.Response(200, json={"action": "accept"}), ["y"], id="accept->y"),
        # Decline/cancel: the decline key opens cursor's "Reason for rejection"
        # sub-prompt, so a follow-up Enter submits an empty reason to complete it.
        pytest.param(
            httpx.Response(200, json={"action": "decline"}), ["Escape", "Enter"], id="decline->esc"
        ),
        pytest.param(
            httpx.Response(200, json={"action": "cancel"}), ["Escape", "Enter"], id="cancel->esc"
        ),
        pytest.param(httpx.Response(200), [], id="empty-200->no-key"),
        pytest.param(httpx.Response(400, text="nope"), [], id="rejected->no-key"),
        pytest.param(httpx.Response(200, content=b"not-json"), [], id="non-json->no-key"),
        pytest.param(httpx.Response(200, json={"action": "??"}), [], id="unknown-action->no-key"),
    ],
)
@pytest.mark.asyncio
async def test_run_one_approval_posts_then_sends_verdict_keystroke(
    response: httpx.Response,
    expected_keys: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Park a prompt on the server, then drive the TUI with the verdict key(s).

    The hook POST always carries the renderable fields; a keystroke is sent ONLY
    for a concrete accept (``accept_key``) or decline/cancel (``decline_key`` +
    ``Enter`` to submit the empty rejection reason) verdict — an empty 2xx
    (answered in the TUI / timeout), a rejection, a non-JSON body, or an unknown
    action sends nothing.
    """
    prompt = CursorApprovalPrompt(
        operation_type="shell",
        message="Cursor wants to run Shell",
        preview="echo omnigent_probe > out.txt",
        accept_key="y",
        decline_key="Escape",
    )
    sent: list[tuple[Path, tuple[str, ...]]] = []
    monkeypatch.setattr(cnp, "send_cursor_pane_keys", lambda d, *keys: sent.append((d, keys)))
    client = _QueueClient([response])

    await cnp._run_one_approval(
        client,  # type: ignore[arg-type]
        session_id="conv_1",
        bridge_dir=tmp_path,
        prompt=prompt,
        elicitation_id="elic_1",
    )

    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_1/hooks/cursor-permission-request"
    assert body == {
        "elicitation_id": "elic_1",
        "operation_type": "shell",
        "message": prompt.message,
        "content_preview": prompt.preview,
    }
    # Keys are sent one per call (see _send_cursor_keys), so each is its own tuple.
    assert sent == [(tmp_path, (key,)) for key in expected_keys]


@pytest.mark.asyncio
async def test_post_external_elicitation_resolved_shape() -> None:
    """The un-park POST carries the resolved-event type + elicitation id."""
    client = _QueueClient([httpx.Response(200)])
    await cnp._post_external_elicitation_resolved(client, "conv_2", "elic_9")  # type: ignore[arg-type]
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_2/events"
    assert body == {
        "type": "external_elicitation_resolved",
        "data": {"elicitation_id": "elic_9"},
    }


def test_capture_cursor_pane_returns_pane_or_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pane text when the TUI is live; ``None`` when absent or the pane is dead."""
    monkeypatch.setattr(cnb, "read_tmux_info", lambda _d: {"socket_path": "s", "tmux_target": "t"})
    monkeypatch.setattr(cnb, "_session_alive", lambda _s, _t: True)
    monkeypatch.setattr(cnb, "_capture_pane", lambda _s, _t: "PANE-TEXT")
    assert cnb.capture_cursor_pane(tmp_path) == "PANE-TEXT"

    monkeypatch.setattr(cnb, "_session_alive", lambda _s, _t: False)
    assert cnb.capture_cursor_pane(tmp_path) is None  # dead pane

    monkeypatch.setattr(cnb, "read_tmux_info", lambda _d: None)
    assert cnb.capture_cursor_pane(tmp_path) is None  # no tmux target advertised


def test_send_cursor_pane_keys_invokes_tmux_send_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each key is forwarded to ``tmux send-keys -t <target>`` on the pane socket."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        cnb, "read_tmux_info", lambda _d: {"socket_path": "sock", "tmux_target": "main"}
    )
    monkeypatch.setattr(cnb, "_run_tmux", lambda sp, *a: calls.append((sp, a)))

    cnb.send_cursor_pane_keys(tmp_path, "y")
    assert calls == [("sock", ("send-keys", "-t", "main", "y"))]

    cnb.send_cursor_pane_keys(tmp_path, "Escape")
    assert calls[-1] == ("sock", ("send-keys", "-t", "main", "Escape"))


def test_send_cursor_pane_keys_raises_without_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing tmux target is a hard error (the verdict can't be delivered)."""
    monkeypatch.setattr(cnb, "read_tmux_info", lambda _d: None)
    with pytest.raises(RuntimeError):
        cnb.send_cursor_pane_keys(tmp_path, "y")


# ── Transcript-based detector ────────────────────────────────────────────────
#
# These cover the chat-store detection path that replaced pane scraping as the
# primary signal. A pending (approval-gated) tool call is recorded as an
# assistant ``tool-call`` content part carrying
# ``providerOptions.cursor.pendingToolCallStartedAtMs``, embedded inside a
# binary protobuf checkpoint frame; it is "answered" when a ``tool-result`` with
# the same ``toolCallId`` is appended. The fixtures below mirror the real
# store-blob shapes verified against cursor-agent 2026.06.24.


def _write_store(path: Path, blobs: list[bytes]) -> None:
    """Create a minimal cursor-shaped ``store.db`` with the given raw blobs."""
    con = _sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
        con.executemany(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            [(f"blob{i}", data) for i, data in enumerate(blobs)],
        )
        con.commit()
    finally:
        con.close()


def _framed(obj: dict) -> bytes:
    """Wrap a JSON message in fake binary protobuf noise, as cursor checkpoints do.

    The real pending tool-call blob is a protobuf frame with the JSON embedded
    and arbitrary binary (including stray ``{`` / ``"`` bytes) before and after
    it — the case the scanner must survive without aborting the row.
    """
    prefix = b"\n \x16\xa0\x815\x13b\xc6mt2\x90{ noise \xff\x00"  # incl. a stray "{"
    suffix = b"*\x8e\x02\x08\xff\x01 trailing \x00\xfe"
    return prefix + _json.dumps(obj).encode("utf-8") + suffix


def _pending_tool_call_obj(tool_call_id: str, tool_name: str, args: dict) -> dict:
    return {
        "id": "1",
        "role": "assistant",
        "content": [
            {"type": "tool-call", "toolCallId": tool_call_id, "toolName": tool_name, "args": args}
        ],
        "providerOptions": {"cursor": {"pendingToolCallStartedAtMs": 1782373529662}},
    }


def _tool_result_obj(tool_call_id: str) -> dict:
    return {
        "role": "tool",
        "content": [{"type": "tool-result", "toolCallId": tool_call_id, "result": "ok"}],
    }


def test_read_pending_detects_framed_gated_tool_call(tmp_path: Path) -> None:
    """A pending tool-call embedded in a binary frame is detected with its args.

    This is the exact failure the pane parser missed: a file-deletion gate whose
    accept verb ("Delete") is outside the pane regex's allowlist.
    """
    store = tmp_path / "store.db"
    _write_store(
        store,
        [
            b'{"role":"user","content":"<user_query>delete it</user_query>"}',
            _framed(_pending_tool_call_obj("call_abc\nfc_1", "Delete", {"path": "/x/hello.txt"})),
        ],
    )
    calls = read_cursor_pending_tool_calls(store)
    assert len(calls) == 1
    assert calls[0] == CursorPendingToolCall(
        tool_call_id="call_abc\nfc_1", tool_name="Delete", args={"path": "/x/hello.txt"}
    )


def test_read_pending_suppresses_resolved_call(tmp_path: Path) -> None:
    """A pending call whose tool-result has landed is no longer active."""
    store = tmp_path / "store.db"
    _write_store(
        store,
        [
            _framed(_pending_tool_call_obj("call_done\nfc_2", "Read", {"path": "/x/a"})),
            _json.dumps(_tool_result_obj("call_done\nfc_2")).encode("utf-8"),
        ],
    )
    assert read_cursor_pending_tool_calls(store) == []


def test_read_pending_excludes_committed_call(tmp_path: Path) -> None:
    """A marker'd call that ALSO appears committed (no-marker tool-call) is excluded.

    This is the auto-approve case: cursor stamps the pending marker while deciding,
    then finalizes the call to run — writing the same tool-call WITHOUT the marker.
    The committed (no-marker) appearance means cursor is no longer blocked on the
    human, so it must not surface a card even before the tool-result lands.
    """
    store = tmp_path / "store.db"
    committed = {
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": "call_w\nfc",
                "toolName": "Write",
                "args": {"path": "/x"},
            }
        ],
        "providerOptions": {"cursor": {"modelProviderMessageId": "m1"}},
    }
    _write_store(
        store,
        [
            _framed(_pending_tool_call_obj("call_w\nfc", "Write", {"path": "/x"})),
            _json.dumps(committed).encode("utf-8"),
        ],
    )
    assert read_cursor_pending_tool_calls(store) == []


def test_read_pending_ignores_autorun_call_without_marker(tmp_path: Path) -> None:
    """A clean-JSON tool-call lacking the pending marker (auto-ran) is ignored."""
    store = tmp_path / "store.db"
    autorun = {
        "role": "assistant",
        "content": [
            {"type": "tool-call", "toolCallId": "call_auto", "toolName": "Read", "args": {}}
        ],
        "providerOptions": {"cursor": {"modelProviderMessageId": "m1"}},
    }
    _write_store(store, [_json.dumps(autorun).encode("utf-8")])
    assert read_cursor_pending_tool_calls(store) == []


def test_read_pending_detects_multiple_distinct_gated_calls(tmp_path: Path) -> None:
    """Two distinct pending calls are both surfaced; a resolved one is dropped."""
    store = tmp_path / "store.db"
    _write_store(
        store,
        [
            _framed(_pending_tool_call_obj("call_1\nfc", "Delete", {"path": "/a"})),
            _framed(_pending_tool_call_obj("call_2\nfc", "Write", {"path": "/b"})),
            _framed(_pending_tool_call_obj("call_3\nfc", "Read", {"path": "/c"})),
            _json.dumps(_tool_result_obj("call_3\nfc")).encode("utf-8"),
        ],
    )
    names = sorted(c.tool_name for c in read_cursor_pending_tool_calls(store))
    assert names == ["Delete", "Write"]


def test_tool_call_elicitation_id_is_stable_and_scoped(tmp_path: Path) -> None:
    """The id is deterministic per (session, toolCallId) and embeds the session."""
    a = cursor_tool_call_elicitation_id("conv_x", "call_1\nfc")
    b = cursor_tool_call_elicitation_id("conv_x", "call_1\nfc")
    c = cursor_tool_call_elicitation_id("conv_y", "call_1\nfc")
    assert a == b and a != c
    assert a.startswith("elicit_cursor_conv_x_")


async def test_supervise_transcript_parks_new_call_then_releases_on_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """detect pending → POST hook; on resolve (call vanishes) → POST resolved."""
    posts: list[tuple[str, dict]] = []

    pending_now = [
        CursorPendingToolCall(tool_call_id="call_z\nfc", tool_name="Delete", args={"path": "/x"})
    ]

    monkeypatch.setattr(cnp, "_discover_store", lambda *_a, **_k: tmp_path / "store.db")
    (tmp_path / "store.db").write_bytes(b"")  # exists() check
    monkeypatch.setattr(cnp, "read_cursor_pending_tool_calls", lambda _s: list(pending_now))
    # Keystroke + park boundaries faked.
    monkeypatch.setattr(cnp, "send_cursor_pane_keys", lambda *_a, **_k: None)

    class _Resp:
        status_code = 200
        content = b""

        def json(self) -> dict:
            return {}

    release = asyncio.Event()

    class _Client:
        async def post(self, url: str, json: dict | None = None, **_k):
            posts.append((url, json or {}))
            if "hooks/cursor-permission-request" in url:
                # Simulate a parked hook (no web verdict yet): stay open until
                # released, so the call is still "active" when it vanishes from
                # the store — exercising the TUI-answered release path.
                await release.wait()
            return _Resp()

    monkeypatch.setattr(cnp.httpx, "AsyncClient", lambda **_k: _FakeAsyncCM(_Client()))

    task = asyncio.create_task(
        cnp.supervise_cursor_transcript_elicitations(
            base_url="http://x",
            headers={},
            session_id="conv_z",
            bridge_dir=tmp_path,
            workspace="/ws",
            launch_epoch_ms=0,
            poll_interval_s=0.01,
            settle_s=0.0,  # surface immediately; debounce covered separately
        )
    )
    # Let it detect + park the pending call.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if any("hooks/cursor-permission-request" in u for u, _ in posts):
            break
    # Now the call disappears (answered in TUI) → expect a resolved POST.
    pending_now.clear()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if any(j.get("type") == "external_elicitation_resolved" for _, j in posts):
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert any("hooks/cursor-permission-request" in u for u, _ in posts)
    assert any(j.get("type") == "external_elicitation_resolved" for _, j in posts)


async def test_supervise_transcript_debounces_autoapproved_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A call that resolves within the settle window surfaces NO card at all.

    This is the auto-approve case: cursor stamps the pending marker while it
    decides, then auto-approves and executes before a human is ever asked. With
    a settle window, the detector must neither park a hook nor post a resolved
    event — otherwise the web UI flashes a card that flips to "resolved
    elsewhere" for a call no human saw.
    """
    posts: list[tuple[str, dict]] = []

    pending_now = [
        CursorPendingToolCall(tool_call_id="call_auto\nfc", tool_name="Write", args={"path": "/x"})
    ]

    monkeypatch.setattr(cnp, "_discover_store", lambda *_a, **_k: tmp_path / "store.db")
    (tmp_path / "store.db").write_bytes(b"")
    monkeypatch.setattr(cnp, "read_cursor_pending_tool_calls", lambda _s: list(pending_now))
    monkeypatch.setattr(cnp, "send_cursor_pane_keys", lambda *_a, **_k: None)

    class _Resp:
        status_code = 200
        content = b""

        def json(self) -> dict:
            return {}

    class _Client:
        async def post(self, url: str, json: dict | None = None, **_k):
            posts.append((url, json or {}))
            return _Resp()

    monkeypatch.setattr(cnp.httpx, "AsyncClient", lambda **_k: _FakeAsyncCM(_Client()))

    task = asyncio.create_task(
        cnp.supervise_cursor_transcript_elicitations(
            base_url="http://x",
            headers={},
            session_id="conv_a",
            bridge_dir=tmp_path,
            workspace="/ws",
            launch_epoch_ms=0,
            poll_interval_s=0.01,
            settle_s=0.2,  # long enough to span several polls before we resolve
        )
    )
    # Let a few polls run while the call is pending (still inside settle window).
    await asyncio.sleep(0.1)
    # Auto-approved: the call resolves (vanishes) before the settle window ends.
    pending_now.clear()
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert not any("hooks/cursor-permission-request" in u for u, _ in posts), posts
    assert not any(j.get("type") == "external_elicitation_resolved" for _, j in posts), posts


# ── AskQuestion (structured multiple-choice) ─────────────────────────────────
#
# cursor's ``AskQuestion`` tool is NOT an approval gate — it is a multi-question
# picker. It surfaces with the pending marker like any gated call, but must
# render as the web ``AskUserQuestion`` form (not approve/reject) and be answered
# by driving the TUI picker. Args shape verified against cursor-agent 2026.06.24.

_ASKQUESTION_ARGS = {
    "title": "AskQuestion Demo",
    "questions": [
        {
            "id": "demo_topic",
            "prompt": "What kind of example would you like to see?",
            "options": [
                {"id": "coding", "label": "A coding-related question (Recommended)"},
                {"id": "workflow", "label": "A workflow/planning question"},
                {"id": "fun", "label": "A fun preference question"},
            ],
        },
        {
            "id": "demo_depth",
            "prompt": "How detailed should the follow-up be?",
            "options": [
                {"id": "brief", "label": "Brief (Recommended)"},
                {"id": "detailed", "label": "Detailed"},
            ],
        },
    ],
}


def test_is_question_call_distinguishes_askquestion() -> None:
    """``AskQuestion`` routes to the question path; other tools to approval."""
    assert cnp._is_question_call(CursorPendingToolCall("t", "AskQuestion", _ASKQUESTION_ARGS))
    assert not cnp._is_question_call(CursorPendingToolCall("t", "Delete", {"path": "/x"}))
    assert not cnp._is_question_call(CursorPendingToolCall("t", "Shell", {"command": "ls"}))


def test_askquestion_preview_translates_to_web_form_shape() -> None:
    """cursor args → the ``AskUserQuestion(...)`` preview the web UI parses.

    cursor's ``prompt`` becomes ``question``; options keep only ``label``; each
    question ``id`` is preserved (the answer comes back keyed by it).
    """
    preview = cnp._askquestion_preview(_ASKQUESTION_ARGS)
    assert preview.startswith("AskUserQuestion(") and preview.endswith(")")
    payload = _json.loads(preview[len("AskUserQuestion(") : -1])
    assert [q["question"] for q in payload["questions"]] == [
        "What kind of example would you like to see?",
        "How detailed should the follow-up be?",
    ]
    assert [q["id"] for q in payload["questions"]] == ["demo_topic", "demo_depth"]
    assert payload["questions"][0]["options"] == [
        {"label": "A coding-related question (Recommended)"},
        {"label": "A workflow/planning question"},
        {"label": "A fun preference question"},
    ]
    assert all(q["multiSelect"] is False for q in payload["questions"])


def test_askquestion_keystrokes_navigate_to_chosen_options() -> None:
    """Chosen labels map to Down-navigation + Space + Enter per question."""
    # First option of each question (index 0): just Space + Enter.
    keys = cnp._askquestion_keystrokes(
        _ASKQUESTION_ARGS,
        {
            "demo_topic": "A coding-related question (Recommended)",
            "demo_depth": "Brief (Recommended)",
        },
    )
    assert keys == ["Space", "Enter", "Space", "Enter"]

    # Second option of each (index 1): one Down, Space, Enter — per question.
    keys = cnp._askquestion_keystrokes(
        _ASKQUESTION_ARGS,
        {"demo_topic": "A workflow/planning question", "demo_depth": "Detailed"},
    )
    assert keys == ["Down", "Space", "Enter", "Down", "Space", "Enter"]


def test_askquestion_keystrokes_types_into_other_row_for_custom_answer() -> None:
    """A value matching no predefined option targets the trailing Other row."""
    keys = cnp._askquestion_keystrokes(
        _ASKQUESTION_ARGS,
        {"demo_topic": "something custom", "demo_depth": "Detailed"},
    )
    # Q1 has 3 options → Other row at index 3: Down×3 then type the text.
    assert keys == ["Down", "Down", "Down", "something custom", "Enter", "Down", "Space", "Enter"]


async def test_run_one_question_renders_form_then_drives_picker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hook gets an AskUserQuestion preview; the verdict drives the picker."""
    posts: list[tuple[str, dict]] = []
    sent_keys: list[tuple[str, ...]] = []

    monkeypatch.setattr(cnp, "send_cursor_pane_keys", lambda _d, *keys: sent_keys.append(keys))

    class _Resp:
        status_code = 200
        # Web verdict: accept with the user's selected labels, keyed by question id.
        content = b"x"

        def json(self) -> dict:
            return {
                "action": "accept",
                "content": {
                    "demo_topic": "A workflow/planning question",
                    "demo_depth": "Detailed",
                },
            }

    class _Client:
        async def post(self, url: str, json: dict | None = None, **_k):
            posts.append((url, json or {}))
            return _Resp()

    await cnp._run_one_question(
        _Client(),
        session_id="conv_q",
        bridge_dir=tmp_path,
        call=CursorPendingToolCall("tc\nq", "AskQuestion", _ASKQUESTION_ARGS),
        elicitation_id="elicit_cursor_conv_q_abc",
    )

    # 1) The hook payload carries the AskUserQuestion form preview, not raw JSON.
    assert len(posts) == 1
    url, body = posts[0]
    assert "hooks/cursor-permission-request" in url
    assert body["operation_type"] == "question"
    assert body["content_preview"].startswith("AskUserQuestion(")
    # Structured payload (uncapped) is the authoritative source the web renders.
    assert body["ask_user_question"]["questions"][0]["id"] == "demo_topic"
    assert body["ask_user_question"]["questions"][0]["question"] == (
        "What kind of example would you like to see?"
    )
    # 2) The verdict drove the picker to the chosen options (index 1 in each).
    flat = [k for group in sent_keys for k in group]
    assert flat == ["Down", "Space", "Enter", "Down", "Space", "Enter"]


async def test_run_one_question_decline_skips_via_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A declined question sends Escape (skip), not an option selection."""
    sent_keys: list[tuple[str, ...]] = []
    monkeypatch.setattr(cnp, "send_cursor_pane_keys", lambda _d, *keys: sent_keys.append(keys))

    class _Resp:
        status_code = 200
        content = b"x"

        def json(self) -> dict:
            return {"action": "decline"}

    class _Client:
        async def post(self, url: str, json: dict | None = None, **_k):
            return _Resp()

    await cnp._run_one_question(
        _Client(),
        session_id="conv_q",
        bridge_dir=tmp_path,
        call=CursorPendingToolCall("tc", "AskQuestion", _ASKQUESTION_ARGS),
        elicitation_id="e",
    )
    assert [k for group in sent_keys for k in group] == ["Escape"]


class _FakeAsyncCM:
    """Minimal async-context-manager wrapper around a fake client."""

    def __init__(self, client: object) -> None:
        self._client = client

    async def __aenter__(self) -> object:
        return self._client

    async def __aexit__(self, *_exc: object) -> bool:
        return False
