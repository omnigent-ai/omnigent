"""Unit tests for cursor-native elicitation surfacing.

Covers everything a live cursor-agent isn't needed for, with the store + tmux +
HTTP boundaries faked:

* **Transcript detection** — reading pending tool calls out of the chat
  ``store.db`` (incl. binary checkpoint frames), suppressing resolved/auto-run
  calls, and the stable elicitation-id format.
* **Supervisor** — surfacing a settled pending call, the debounce that drops
  auto-approved calls, the TUI-resolved release, and the yolo auto-accept path
  that sends ``y`` without parking a web card — including every way that path
  refuses to type (no gate on screen, dead pane, undelivered keystroke, retry
  budget spent) and falls back to the ordinary card.
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
from collections.abc import Callable
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


def test_iter_embedded_json_recovers_from_enclosing_garbage() -> None:
    """A stray opener whose braces balance AROUND the real object must not hide it.

    Large cursor checkpoint frames contain binary that can form a ``{ … }`` span
    enclosing a real message object while itself being invalid JSON. The scanner
    must keep going (advance by one) and still extract the inner object — not
    jump past the whole failed span (which dropped genuinely-pending tool calls,
    e.g. MCP, in big frames).
    """
    inner = _json.dumps(_pending_tool_call_obj("call_mcp\nfc", "omnigent-list_comments", {"x": 1}))
    # Leading "{"k": … <inner> … bad}" balances at the trailing brace but fails
    # to parse; the genuine object is nested inside it.
    raw = b'{"k": ' + inner.encode("utf-8") + b" trailing-bad}"
    objs = cnp._iter_embedded_json_objects(raw)
    names = [
        p.get("toolName")
        for o in objs
        for p in (o.get("content") or [])
        if isinstance(p, dict) and p.get("type") == "tool-call"
    ]
    assert "omnigent-list_comments" in names


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


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (None, False),
        ([], False),
        (["--approve-mcps"], False),
        (["--auto-review"], False),
        (["--yolo"], True),
        (["--force"], True),
        (["-f"], True),
        (["--yolo", "--approve-mcps", "--model", "grok"], True),
        (["--force=true"], True),
        # An explicit off-value must read as off: this predicate is the safety
        # gate for typing verdicts into someone's terminal, so it fails closed.
        (["--yolo=false"], False),
        (["--force=false"], False),
        (["--yolo=0"], False),
        (["--force=no"], False),
        (["--yolo=OFF"], False),
        (["--yolo=false", "--approve-mcps"], False),
        # A bare ``--`` ends cursor-agent's flags; what follows is prompt text.
        (["--", "-f"], False),
        (["--", "--yolo"], False),
        (["--yolo", "--", "-f"], True),
    ],
)
def test_cursor_launch_args_enable_yolo(args: list[str] | None, expected: bool) -> None:
    """Only the Run Everything CLI flags enable the yolo auto-accept path."""
    assert cnp.cursor_launch_args_enable_yolo(args) is expected


# The block cursor renders for a tool gate. The parenthesised ``(y)`` hint on
# the accept row is what the yolo path requires on screen before it sends
# anything, so the fixtures below carry the real shape rather than a bare "y".
_ACCEPT_PANE = (
    " $  docker pull example in .\n"
    " Run this command?\n"
    " Shell allowlist is empty\n"
    "  → Run (once) (y)\n"
    "    Run Everything (shift+tab)\n"
    "    Skip (esc or n)\n"
)
# A live pane with no gate on screen — the stale-marker case, where a keystroke
# would land in cursor's composer instead of answering anything.
_IDLE_PANE = "  ~/ws\n  Ask me anything…\n"

_SHELL_CALL = CursorPendingToolCall(
    tool_call_id="call_shell\nfc",
    tool_name="Shell",
    args={"command": "docker pull example"},
)


def _install_supervisor_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    pending: list[CursorPendingToolCall],
    pane: str | None,
    deliver_keys: bool = True,
) -> tuple[list[tuple[str, dict]], list[tuple[str, ...]]]:
    """Fake the store, pane and HTTP boundaries the supervisor talks to.

    :param pending: Live list of pending calls; mutate it to resolve a gate.
    :param pane: Pane text ``capture_cursor_pane`` returns, or ``None`` for a
        dead / unadvertised pane.
    :param deliver_keys: Whether the keystroke send reports success (``False``
        models tmux rejecting the send after the capture succeeded).
    :returns: The recorded ``(posts, keys_sent)`` lists.
    """
    posts: list[tuple[str, dict]] = []
    keys_sent: list[tuple[str, ...]] = []

    monkeypatch.setattr(cnp, "_discover_store", lambda *_a, **_k: tmp_path / "store.db")
    (tmp_path / "store.db").write_bytes(b"")
    monkeypatch.setattr(cnp, "read_cursor_pending_tool_calls", lambda _s: list(pending))
    monkeypatch.setattr(cnp, "capture_cursor_pane", lambda _bridge: pane)

    async def _fake_send(_bridge: Path, _session: str, *keys: str) -> bool:
        keys_sent.append(keys)
        return deliver_keys

    monkeypatch.setattr(cnp, "_send_cursor_keys", _fake_send)

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
    return posts, keys_sent


def _start_supervisor(
    tmp_path: Path, *, session_id: str, auto_accept_approvals: bool
) -> asyncio.Task[None]:
    """Start the supervisor with a fast poll and no settle window."""
    return asyncio.create_task(
        cnp.supervise_cursor_transcript_elicitations(
            base_url="http://x",
            headers={},
            session_id=session_id,
            bridge_dir=tmp_path,
            workspace="/ws",
            launch_epoch_ms=0,
            poll_interval_s=0.01,
            settle_s=0.0,
            auto_accept_approvals=auto_accept_approvals,
        )
    )


async def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 1.0) -> bool:
    """Poll *predicate* until it is true or *timeout_s* elapses."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _stop(task: asyncio.Task[None]) -> None:
    """Cancel a supervisor task and swallow the cancellation."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _hook_posts(posts: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """The parked approval-card POSTs among *posts*."""
    return [(u, j) for u, j in posts if "hooks/cursor-permission-request" in u]


async def test_supervise_transcript_yolo_auto_accepts_without_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under yolo, a settled tool gate is accepted in-pane — no web card.

    cursor-agent's Run Everything mode still sometimes leaves a pending marker
    long enough for Omnigent to otherwise mirror an ApprovalCard and stall a
    piloted parent. Auto-accept must send ``y`` and never POST the permission
    hook.
    """
    pending_now = [_SHELL_CALL]
    posts, keys_sent = _install_supervisor_fakes(
        monkeypatch, tmp_path, pending=pending_now, pane=_ACCEPT_PANE
    )

    task = _start_supervisor(tmp_path, session_id="conv_yolo", auto_accept_approvals=True)
    assert await _wait_for(lambda: bool(keys_sent))
    # Call resolves after the keystroke (cursor committed it).
    pending_now.clear()
    await asyncio.sleep(0.05)
    await _stop(task)

    assert keys_sent == [("y",)]
    assert _hook_posts(posts) == []
    assert not any(j.get("type") == "external_elicitation_resolved" for _, j in posts), posts


async def test_supervise_transcript_yolo_caps_retries_then_surfaces_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gate ``y`` never clears is retried a bounded number of times, then mirrored.

    Without a cap the supervisor types ``y`` into the pane every couple of
    seconds for the life of the session and no human ever sees the gate. After
    the budget it must fall back to the ApprovalCard the non-yolo path shows.
    """
    monkeypatch.setattr(cnp, "_YOLO_ACCEPT_RETRY_S", 0.0)
    # Stays pending no matter how many times we accept it.
    pending_now = [_SHELL_CALL]
    posts, keys_sent = _install_supervisor_fakes(
        monkeypatch, tmp_path, pending=pending_now, pane=_ACCEPT_PANE
    )

    task = _start_supervisor(tmp_path, session_id="conv_yolo_cap", auto_accept_approvals=True)
    assert await _wait_for(lambda: bool(_hook_posts(posts)))
    # Give the loop several more polls: the card is parked, so nothing more
    # should be sent and the card must not be re-posted.
    await asyncio.sleep(0.1)
    await _stop(task)

    assert keys_sent == [("y",)] * cnp._YOLO_ACCEPT_MAX_ATTEMPTS
    assert len(_hook_posts(posts)) == 1, posts


async def test_supervise_transcript_yolo_never_types_when_no_prompt_on_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pending marker with no gate rendered is mirrored, never typed at.

    This is the stale-marker case the feature exists for. ``tmux send-keys y``
    against an idle pane types a literal ``y`` into cursor's composer, which
    then prepends itself to whatever the user types next — so the accept only
    fires while cursor is actually advertising its accept key.
    """
    monkeypatch.setattr(cnp, "_YOLO_ACCEPT_RETRY_S", 0.0)
    posts, keys_sent = _install_supervisor_fakes(
        monkeypatch, tmp_path, pending=[_SHELL_CALL], pane=_IDLE_PANE
    )

    task = _start_supervisor(tmp_path, session_id="conv_yolo_idle", auto_accept_approvals=True)
    assert await _wait_for(lambda: bool(_hook_posts(posts)))
    await _stop(task)

    assert keys_sent == []
    assert len(_hook_posts(posts)) == 1, posts


async def test_supervise_transcript_yolo_surfaces_card_when_pane_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dead pane stops the accept immediately instead of spinning on it.

    ``capture_cursor_pane`` returns ``None`` when the tmux target was never
    advertised or the TUI has exited. A keystroke cannot land, and recording it
    as delivered would retry forever against a pane that is gone.
    """
    posts, keys_sent = _install_supervisor_fakes(
        monkeypatch, tmp_path, pending=[_SHELL_CALL], pane=None
    )

    task = _start_supervisor(tmp_path, session_id="conv_yolo_dead", auto_accept_approvals=True)
    assert await _wait_for(lambda: bool(_hook_posts(posts)))
    await _stop(task)

    assert keys_sent == []
    assert len(_hook_posts(posts)) == 1, posts


async def test_supervise_transcript_yolo_surfaces_card_when_keystroke_undelivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A send tmux rejects is not counted as an accept that landed."""
    monkeypatch.setattr(cnp, "_YOLO_ACCEPT_RETRY_S", 0.0)
    posts, keys_sent = _install_supervisor_fakes(
        monkeypatch,
        tmp_path,
        pending=[_SHELL_CALL],
        pane=_ACCEPT_PANE,
        deliver_keys=False,
    )

    task = _start_supervisor(
        tmp_path, session_id="conv_yolo_undelivered", auto_accept_approvals=True
    )
    assert await _wait_for(lambda: bool(_hook_posts(posts)))
    await _stop(task)

    # One rejected send is enough to give up — no point retrying a broken pipe.
    assert keys_sent == [("y",)]
    assert len(_hook_posts(posts)) == 1, posts


async def test_supervise_transcript_without_yolo_never_auto_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the yolo stance a gate is only ever mirrored, never answered.

    Even with cursor's accept prompt on screen, the default launch must not
    send a verdict of its own initiative — the card is the only channel.
    """
    posts, keys_sent = _install_supervisor_fakes(
        monkeypatch, tmp_path, pending=[_SHELL_CALL], pane=_ACCEPT_PANE
    )

    task = _start_supervisor(tmp_path, session_id="conv_plain", auto_accept_approvals=False)
    assert await _wait_for(lambda: bool(_hook_posts(posts)))
    await _stop(task)

    assert keys_sent == []


async def test_supervise_transcript_yolo_still_parks_askquestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AskQuestion still mirrors under yolo — that is deliberate human input."""
    question_call = CursorPendingToolCall(
        tool_call_id="call_q\nfc",
        tool_name="AskQuestion",
        args={
            "title": "Pick one",
            "questions": [
                {
                    "id": "q1",
                    "prompt": "Continue?",
                    "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
                }
            ],
        },
    )
    posts, keys_sent = _install_supervisor_fakes(
        monkeypatch, tmp_path, pending=[question_call], pane=_ACCEPT_PANE
    )

    task = _start_supervisor(tmp_path, session_id="conv_yolo_q", auto_accept_approvals=True)
    assert await _wait_for(lambda: bool(_hook_posts(posts)))
    await _stop(task)

    assert keys_sent == []


@pytest.mark.parametrize(
    ("pane", "expected"),
    [
        (_ACCEPT_PANE, True),
        (_IDLE_PANE, False),
        ("", False),
        # The y/n spelling of the same hint.
        ("  Run this command? (y/n)", True),
        # A ``y`` in prose is not an advertised key.
        ("  yes, you may want to run this", False),
    ],
)
def test_pane_shows_accept_prompt(pane: str, expected: bool) -> None:
    """Only cursor's parenthesised accept hint counts as a gate on screen."""
    assert cnp._pane_shows_accept_prompt(pane) is expected


async def test_send_cursor_keys_reports_undelivered_keystroke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tmux send that raises reports failure rather than a silent success."""

    def _boom(_bridge: Path, _key: str) -> None:
        raise RuntimeError("cursor-native tmux target not advertised")

    monkeypatch.setattr(cnp, "send_cursor_pane_keys", _boom)
    assert await cnp._send_cursor_keys(tmp_path, "conv_dead", "y") is False

    monkeypatch.setattr(cnp, "send_cursor_pane_keys", lambda *_a, **_k: None)
    assert await cnp._send_cursor_keys(tmp_path, "conv_live", "y") is True


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


def test_askquestion_payload_translates_allow_multiple_to_multiselect() -> None:
    """cursor's ``allowMultiple`` flag becomes the web form's ``multiSelect``.

    cursor marks a multi-select question with ``allowMultiple`` (proto
    ``allow_multiple``); the web form renders checkboxes only when its own
    ``multiSelect`` is true, so the flag must survive translation. Anything
    other than a literal ``True`` (absent, false, or a truthy non-bool) stays
    single-select.
    """

    def _payload_for(question_extra: dict[str, object]) -> dict[str, object]:
        args: dict[str, object] = {
            "questions": [
                {
                    "id": "features",
                    "prompt": "Which features should I enable?",
                    "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                    **question_extra,
                }
            ]
        }
        return cnp._askquestion_payload(args)["questions"][0]

    assert _payload_for({"allowMultiple": True})["multiSelect"] is True
    assert _payload_for({"allowMultiple": False})["multiSelect"] is False
    assert _payload_for({})["multiSelect"] is False
    # A stray string is not a multi-select marker.
    assert _payload_for({"allowMultiple": "yes"})["multiSelect"] is False


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


def test_askquestion_keystrokes_toggle_every_option_of_a_multiselect_answer() -> None:
    """A list answer Space-toggles each chosen option before advancing.

    A multi-select answer arrives as a list of labels; the picker must toggle
    every one (Down to each row in ascending order, Space on each) and only
    then press Enter.
    """
    keys = cnp._askquestion_keystrokes(
        _ASKQUESTION_ARGS,
        {
            "demo_topic": [
                "A coding-related question (Recommended)",
                "A fun preference question",
            ],
            "demo_depth": "Brief (Recommended)",
        },
    )
    # Q1: Space on row 0, Down twice to row 2, Space; Enter. Q2: Space, Enter.
    assert keys == ["Space", "Down", "Down", "Space", "Enter", "Space", "Enter"]


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
