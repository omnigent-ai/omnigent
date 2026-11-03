"""A Claude message refused by a launcher sign-in waits for the sign-in instead of failing."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.diagnostics import sign_in_next_step
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, sign_in_watch, subagent_work
from omnigent.runtime import pending_inputs
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

_SIGN_IN_STEP = sign_in_next_step("Claude Code")
# The held notice keeps the sign-in step but not the resend: the message goes out on its own.
_HELD_SIGN_IN_STEP = (
    "Open the sign-in link and sign in. Claude Code continues on its own once the "
    "sign-in completes."
)
# The bridge's step when another session's sign-in holds the launcher's lock.
_ANOTHER_SESSION_SIGN_IN_STEP = (
    "Finish the sign-in that another session opened, in your browser or in "
    "that session's terminal. Claude Code continues on its own once it "
    "completes; then send your message again."
)
_HELD_ANOTHER_SESSION_SIGN_IN_STEP = (
    "Finish the sign-in that another session opened, in your browser or in "
    "that session's terminal. Claude Code continues on its own once it completes."
)
_SIGN_IN_SCREEN = (
    "If the browser does not open automatically, please open the following URL:\n\n"
    "\thttps://example.okta.test/oauth2/v1/authorize?client_id=0oa1&state=T4IU\n\n"
)
_READY_SCREEN = "─" * 16 + "\n❯ \n" + "─" * 16 + "\n"
_STABLE_ID = "0123456789abcdef0123456789abcdef"


_OWN_SIGN_IN_MESSAGE = (
    "Claude Code is waiting for a sign-in in this session's terminal, "
    "so the message was not delivered."
)
_ANOTHER_SESSION_SIGN_IN_MESSAGE = (
    "Claude Code is waiting for a sign-in that another session started, "
    "so the message was not delivered."
)


def _sign_in_refusal(*, undelivered: bool = True, message: str = _OWN_SIGN_IN_MESSAGE) -> str:
    error: dict[str, Any] = {
        "code": "databricks_sign_in_pending",
        "message": message,
        "title": "Claude Code can't start until you sign in to Databricks",
        "remediation": _SIGN_IN_STEP,
    }
    if undelivered:
        error["undelivered"] = True
    return _sse({"type": "response.failed", "response": {"status": "failed"}, "error": error})


_REFUSED = [_sse({"type": "response.created", "response": {"id": "resp_1"}}), _sign_in_refusal()]
_DELIVERED = [
    _sse({"type": "response.created", "response": {"id": "resp_2"}}),
    _sse({"type": "response.completed", "response": {"id": "resp_2"}}),
]
_FAILED_OTHERWISE = [
    _sse({"type": "response.created", "response": {"id": "resp_3"}}),
    _sse(
        {
            "type": "response.failed",
            "response": {"status": "failed"},
            "error": {"code": "native_turn_error", "message": "Claude Code stopped responding."},
        }
    ),
]


class _TurnScriptHarness(_ScriptedHarnessClient):
    """Stream each turn's own scripted frames; the last script repeats."""

    def __init__(self, scripts: list[list[str]]) -> None:
        super().__init__(scripts[0])
        self._scripts = scripts

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        turn = len(self.posted_bodies)
        self._sse_frames = self._scripts[min(turn, len(self._scripts) - 1)]
        return super().stream(method, url, json=copy.deepcopy(json), timeout=timeout)


class _TakeoverScript(list[str]):
    """The refused turn's frames; streaming them runs ``take_over`` before frame ``at``."""

    def __init__(self, at: int) -> None:
        super().__init__(_REFUSED)
        self._at = at
        self.take_over: Callable[[], None] = lambda: None

    def __iter__(self) -> Iterator[str]:
        for index, frame in enumerate(super().__iter__()):
            if index == self._at:
                self.take_over()
            yield frame
        if self._at == len(self):
            self.take_over()


class _PausedStreamHarness(_TurnScriptHarness):
    """Keep turn ``pause_turn``'s stream open after its last frame until ``resume`` is set."""

    def __init__(self, scripts: list[list[str]], *, pause_turn: int = 1) -> None:
        super().__init__(scripts)
        self._pause_turn = pause_turn
        self.frames_sent = asyncio.Event()
        self.resume = asyncio.Event()

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        inner = super().stream(method, url, json=json, timeout=timeout)
        if len(self.posted_bodies) != self._pause_turn:
            return inner
        harness = self

        class _Paused:
            status_code = 200

            async def __aenter__(self) -> Any:
                handle = await inner.__aenter__()

                class _Handle:
                    status_code = 200

                    async def aiter_text(self) -> AsyncIterator[str]:
                        async for frame in handle.aiter_text():
                            yield frame
                        # Every frame was read and handled; the stream just hasn't ended.
                        harness.frames_sent.set()
                        await harness.resume.wait()

                return _Handle()

            async def __aexit__(self, *exc: Any) -> None:
                await inner.__aexit__(*exc)

        return _Paused()


class _BrokenStreamHarness(_TurnScriptHarness):
    """Break the first turn's stream with ``httpx.ReadError`` once all its frames are read."""

    def __init__(self, scripts: list[list[str]]) -> None:
        super().__init__(scripts)
        self.before_break: Callable[[], None] = lambda: None

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        inner = super().stream(method, url, json=json, timeout=timeout)
        if len(self.posted_bodies) != 1:
            return inner
        harness = self

        class _Broken:
            status_code = 200

            async def __aenter__(self) -> Any:
                handle = await inner.__aenter__()

                class _Handle:
                    status_code = 200

                    async def aiter_text(self) -> AsyncIterator[str]:
                        async for frame in handle.aiter_text():
                            yield frame
                        harness.before_break()
                        raise httpx.ReadError("synthetic harness disconnect")

                return _Handle()

            async def __aexit__(self, *exc: Any) -> None:
                await inner.__aexit__(*exc)

        return _Broken()


class _RecordingServerClient(NullServerClient):
    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []
        # Awaited inside each POST, e.g. to keep a notice in flight for a while.
        self.while_posting: Callable[[Any], Awaitable[None]] | None = None

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.posts.append((url, kwargs.get("json")))
        if self.while_posting is not None:
            await self.while_posting(kwargs.get("json"))
        return await super().post(url, **kwargs)

    def notices(self, code: str) -> list[dict[str, Any]]:
        return [
            body["data"]["item_data"] for _url, body in self.posts if _notice_code(body) == code
        ]


def _notice_code(body: Any) -> str | None:
    if isinstance(body, dict) and body.get("type") == "external_conversation_item":
        return body["data"]["item_data"].get("code")
    return None


def _build_app(
    scripts: list[list[str]],
    *,
    terminal_registry: TerminalRegistry | None = None,
    harness: _TurnScriptHarness | None = None,
) -> tuple[FastAPI, _TurnScriptHarness, _RecordingServerClient]:
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    harness = harness or _TurnScriptHarness(scripts)
    server = _RecordingServerClient()
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=server,  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    return app, harness, server


def _claude_pane(
    sid: str, tmp_path: Path, read: Callable[..., Awaitable[dict[str, object]]]
) -> TerminalRegistry:
    """A terminal registry holding the session's Claude pane, read through ``read``."""
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.read = read  # type: ignore[method-assign]
    registry = TerminalRegistry(conversation_link_base_url="http://127.0.0.1:8000")
    registry._by_conversation.setdefault(sid, {})[(instance.name, instance.session_key)] = instance
    return registry


async def _create_session(client: httpx.AsyncClient, sid: str) -> None:
    created = await client.post(
        "/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex}
    )
    assert created.status_code == 201


def _message(text: str) -> dict[str, Any]:
    return {"type": "message", "content": [{"type": "input_text", "text": text}]}


def _sent_text(body: dict[str, Any]) -> str:
    return body["content"][-1]["content"][0]["text"]


def _published(app: FastAPI, sid: str) -> list[dict[str, Any]]:
    queue = app.state.session_event_queues.get(sid)
    events: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if event is not None:
            events.append(event)
    return events


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _quiet_pane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def skip_terminal_launch(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def bridge_id(**_: Any) -> str:
        return "label-owned-bridge"

    monkeypatch.setattr(runner_app, "_launch_native_terminal", skip_terminal_launch)
    monkeypatch.setattr(runner_app, "_claude_native_bridge_id_for_session", bridge_id)
    monkeypatch.setattr(runner_app, "_CLAUDE_PENDING_PROMPT_POLL_S", 0.01)
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "claude-native")
    monkeypatch.setattr(claude_native_bridge, "has_pending_user_prompt", lambda _: False)
    # The pane watcher is driven by hand below: it never reads a pane on its own.
    monkeypatch.setattr(sign_in_watch, "_SIGN_IN_WATCH_INTERVAL_S", 3600.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal_message",
    [_OWN_SIGN_IN_MESSAGE, _ANOTHER_SESSION_SIGN_IN_MESSAGE],
    ids=["own-prompt", "another-session-lock"],
)
async def test_sign_in_refusal_holds_the_message_and_sends_it_after_sign_in(
    refusal_message: str,
) -> None:
    """The refused message is neither failed nor lost; it goes out once the sign-in is done."""
    refused = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sign_in_refusal(message=refusal_message),
    ]
    app, harness, server = _build_app([refused, _DELIVERED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        first = await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        assert first.status_code == 202
        await _until(
            lambda: (
                sid in app.state.sign_in_holds
                and bool(server.notices("databricks_sign_in_pending"))
            )
        )

        published = _published(app, sid)
        assert not [e for e in published if e.get("type") == "response.failed"]
        assert not [
            e for e in published if e.get("type") == "session.status" and e["status"] == "failed"
        ]
        assert len(harness.posted_bodies) == 1
        assert [m["content"][0]["text"] for m in app.state.session_message_buffers[sid]] == [
            "first"
        ]
        (notice,) = server.notices("databricks_sign_in_pending")
        assert notice["level"] == "info"
        assert notice["remediation"] == _HELD_SIGN_IN_STEP
        assert "will be sent once you sign in" in notice["title"]

        # A message sent meanwhile queues behind the held one.
        second = await client.post(f"/v1/sessions/{sid}/events", json=_message("second"))
        assert second.status_code == 202
        assert (
            second.json()["detail"] == "Message buffered until the Databricks sign-in completes."
        )
        await asyncio.sleep(0.05)  # custom-lint: disable=sleep-then-assert -- no send in window
        assert len(harness.posted_bodies) == 1

        # The sign-in watcher saw the agent ready.
        hold = app.state.sign_in_holds[sid]
        hold.waiting = False
        await _until(
            lambda: (
                len(harness.posted_bodies) == 3 and not app.state.session_message_buffers.get(sid)
            )
        )

    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first", "second"]
    # The resent message is recorded once in the turn history.
    resent_history = [item for item in harness.posted_bodies[1]["content"] if "content" in item]
    assert [item["content"][0]["text"] for item in resent_history].count("first") == 1
    assert sid not in app.state.sign_in_holds


@pytest.mark.asyncio
async def test_sign_in_refusal_still_fails_once_the_wait_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sign-in that never completes ends in today's failure with its card, not a loop."""
    monkeypatch.setattr(runner_app, "_SIGN_IN_HOLD_MAX_S", 0.05)
    app, harness, _server = _build_app([_REFUSED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await _until(lambda: len(harness.posted_bodies) == 2 and sid not in app.state.active_turns)
        await asyncio.sleep(0.05)

    failed = [
        e
        for e in _published(app, sid)
        if e.get("type") == "session.status" and e.get("status") == "failed"
    ]
    assert len(failed) == 1
    assert failed[0]["error"]["code"] == "databricks_sign_in_pending"
    assert len(harness.posted_bodies) == 2
    assert not app.state.session_message_buffers.get(sid)
    assert sid not in app.state.sign_in_holds


@pytest.mark.asyncio
async def test_pane_that_keeps_asking_after_it_looked_ready_fails_instead_of_looping() -> None:
    """Each release resends the message; past the repeat cap the refusal stands."""
    app, harness, server = _build_app([_REFUSED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        for attempt in range(1, runner_app._SIGN_IN_HOLD_MAX_REPEATS + 1):
            await _until(
                lambda attempt=attempt: (
                    len(harness.posted_bodies) == attempt
                    and sid in app.state.sign_in_holds
                    and app.state.sign_in_holds[sid].waiting
                )
            )
            app.state.sign_in_holds[sid].waiting = False
        await _until(
            lambda: (
                len(harness.posted_bodies) == runner_app._SIGN_IN_HOLD_MAX_REPEATS + 1
                and sid not in app.state.active_turns
            )
        )
        await asyncio.sleep(0.05)

    failed = [
        e
        for e in _published(app, sid)
        if e.get("type") == "session.status" and e.get("status") == "failed"
    ]
    assert len(failed) == 1
    # One "waiting for sign-in" notice for the whole wait, not one per refusal.
    assert len(server.notices("databricks_sign_in_pending")) == 1


@pytest.mark.asyncio
async def test_refusal_without_undelivered_evidence_fails_as_before() -> None:
    """Only a message that provably never reached the pane is held."""
    app, harness, _server = _build_app([[_REFUSED[0], _sign_in_refusal(undelivered=False)]])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await _until(lambda: len(harness.posted_bodies) == 1 and sid not in app.state.active_turns)
        await asyncio.sleep(0.05)

    statuses = [e for e in _published(app, sid) if e.get("type") == "session.status"]
    assert [e["status"] for e in statuses] == ["failed"]
    assert sid not in app.state.sign_in_holds
    assert not app.state.session_message_buffers.get(sid)


@pytest.mark.parametrize(
    ("remediation", "held_remediation"),
    [
        (_SIGN_IN_STEP, _HELD_SIGN_IN_STEP),
        (_ANOTHER_SESSION_SIGN_IN_STEP, _HELD_ANOTHER_SESSION_SIGN_IN_STEP),
    ],
    ids=["own-prompt", "another-session-lock"],
)
def test_hold_notice_keeps_the_sign_in_step_without_asking_for_a_resend(
    remediation: str, held_remediation: str
) -> None:
    """The neutral notice points at the sign-in link; the held message is sent on its own."""
    server = _RecordingServerClient()
    watch = sign_in_watch.build_sign_in_watch(
        _background_tasks=set(),
        _session_harness_name=lambda _conv: "claude-native",
        _claim_sign_in_release=lambda _conv: False,
        _release_sign_in_hold=lambda _conv: None,
        _sign_in_watchers={},
        resource_registry=None,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )
    error = json.loads(_sign_in_refusal().split("data: ", 1)[1])["error"]
    asyncio.run(watch.post_sign_in_hold_notice("conv_1", {**error, "remediation": remediation}))
    (notice,) = server.notices("databricks_sign_in_pending")
    assert notice == {
        "source": "harness",
        "code": "databricks_sign_in_pending",
        "title": "Your message will be sent once you sign in to Databricks",
        "message": (
            "Claude Code is waiting for a sign-in in this session's terminal. "
            "You don't need to send your message again."
        ),
        "remediation": held_remediation,
        "level": "info",
    }
    # The refusal itself still asks for a resend; only the held notice drops it.
    assert _SIGN_IN_STEP.endswith("then send your message again.")


def test_sign_in_wait_ends_while_the_server_still_has_the_message() -> None:
    """A refusal that outlasts the wait must still find its queued message on the server."""
    # Leave the resend and its refusal time to reach the server before the entry expires.
    assert runner_app._SIGN_IN_HOLD_MAX_S <= pending_inputs._TTL_S - 60


@pytest.mark.asyncio
async def test_wait_is_shortened_by_the_time_the_server_already_queued_the_message() -> None:
    """A message whose delivery started late still settles before the server forgets it."""
    app, _harness, _server = _build_app([_REFUSED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(
            f"/v1/sessions/{sid}/events",
            json={**_message("first"), "input_enqueued_at_ms": int((time.time() - 100) * 1000)},
        )
        await _until(lambda: sid in app.state.sign_in_holds)
        remaining = app.state.sign_in_holds[sid].deadline - time.monotonic()

    assert runner_app._SIGN_IN_HOLD_MAX_S - 110 < remaining <= runner_app._SIGN_IN_HOLD_MAX_S - 100


@pytest.mark.asyncio
async def test_message_the_server_queued_longer_than_the_wait_fails_at_once() -> None:
    """With no time left to wait, the refusal stands while the server can still record it."""
    app, harness, server = _build_app([_REFUSED])
    sid = uuid.uuid4().hex
    queued_at_ms = int((time.time() - runner_app._SIGN_IN_HOLD_MAX_S - 1) * 1000)
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(
            f"/v1/sessions/{sid}/events",
            json={**_message("first"), "input_enqueued_at_ms": queued_at_ms},
        )
        await _until(lambda: len(harness.posted_bodies) == 1 and sid not in app.state.active_turns)
        await asyncio.sleep(0.05)

    failed = [
        e
        for e in _published(app, sid)
        if e.get("type") == "session.status" and e.get("status") == "failed"
    ]
    assert [e["error"]["code"] for e in failed] == ["databricks_sign_in_pending"]
    assert sid not in app.state.sign_in_holds
    assert not server.notices("databricks_sign_in_pending")


@pytest.mark.asyncio
async def test_messages_queued_during_a_wait_that_ran_out_fail_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spent wait doesn't start over for the queue behind it, so none outlives the server."""
    monkeypatch.setattr(runner_app, "_SIGN_IN_HOLD_MAX_S", 0.3)
    app, harness, server = _build_app([_REFUSED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await _until(lambda: sid in app.state.sign_in_holds)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("second"))
        await _until(
            lambda: (
                len(harness.posted_bodies) >= 3
                and sid not in app.state.sign_in_holds
                and sid not in app.state.active_turns
                and not app.state.session_message_buffers.get(sid)
            )
        )
        await asyncio.sleep(0.05)

    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first", "second"]
    failed = [
        e
        for e in _published(app, sid)
        if e.get("type") == "session.status" and e.get("status") == "failed"
    ]
    assert [e["error"]["code"] for e in failed] == ["databricks_sign_in_pending"] * 2
    assert len(server.notices("databricks_sign_in_pending")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("take_over_at", [1, 2], ids=["before-refusal", "before-stream-end"])
async def test_refusal_on_a_stream_a_newer_turn_took_over_is_reported_not_held(
    take_over_at: int,
) -> None:
    """Only the stream that owns the live turn may hold; an older one reports its failure."""
    script = _TakeoverScript(take_over_at)
    app, harness, server = _build_app([script])
    sid = uuid.uuid4().hex
    newer = asyncio.get_running_loop().create_task(asyncio.Event().wait())
    refused_turns: list[asyncio.Task[None]] = []

    def take_over() -> None:
        refused_turns.append(app.state.active_turns[sid])
        app.state.active_turns[sid] = newer
        app.state.live_response_id[sid] = "resp_newer"

    script.take_over = take_over
    try:
        async with _runner_client(app) as client:
            await _create_session(client, sid)
            await client.post(
                f"/v1/sessions/{sid}/events",
                json={**_message("first"), "stable_id": _STABLE_ID},
            )
            await _until(lambda: bool(refused_turns) and refused_turns[0].done())
            await asyncio.sleep(0.05)
    finally:
        newer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await newer
        app.state.active_turns.pop(sid, None)

    (failed,) = [e for e in _published(app, sid) if e.get("type") == "response.failed"]
    assert failed["error"]["code"] == "databricks_sign_in_pending"
    assert failed["input_stable_id"] == _STABLE_ID
    assert sid not in app.state.sign_in_holds
    assert not app.state.session_message_buffers.get(sid)
    assert len(harness.posted_bodies) == 1
    assert not server.notices("databricks_sign_in_pending")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read-raises", "watch-cancelled"])
async def test_a_failing_sign_in_watch_still_releases_the_held_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    """An unreadable pane or a cancelled watch never leaves the held message paused."""
    monkeypatch.setattr(sign_in_watch, "_SIGN_IN_WATCH_INTERVAL_S", 0.01)
    sid = uuid.uuid4().hex
    reads = 0

    async def read(scrollback: int = 0, *, join_wrapped: bool = False) -> dict[str, object]:
        nonlocal reads
        reads += 1
        if failure == "watch-cancelled":
            return {"screen": _SIGN_IN_SCREEN}
        if reads == 1:
            raise RuntimeError("synthetic pane read failure")
        return {"screen": _READY_SCREEN}

    app, harness, server = _build_app(
        [_REFUSED, _DELIVERED], terminal_registry=_claude_pane(sid, tmp_path, read)
    )
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        if failure == "watch-cancelled":
            # The pane still asks for the sign-in, so only the cancel can end this watch.
            await _until(lambda: sid in app.state.sign_in_holds and reads > 0)
            (watch,) = [t for t in asyncio.all_tasks() if t.get_name() == f"sign-in-watch-{sid}"]
            watch.cancel()
        await _until(lambda: len(harness.posted_bodies) == 2 and sid not in app.state.active_turns)

    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first"]
    assert sid not in app.state.sign_in_holds
    if failure == "read-raises":
        assert reads >= 2
        (ready,) = server.notices("databricks_sign_in_completed")
        assert ready["message"] == "Claude Code is ready. Sending your message now."


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_ends", ["later", "during-notice"])
async def test_ready_notice_lands_before_the_held_message_is_resent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wait_ends: str
) -> None:
    """The queue stays paused while the "signed in" notice posts, even past the wait's end."""
    monkeypatch.setattr(sign_in_watch, "_SIGN_IN_WATCH_INTERVAL_S", 0.01)
    sid = uuid.uuid4().hex

    async def read(scrollback: int = 0, *, join_wrapped: bool = False) -> dict[str, object]:
        return {"screen": _READY_SCREEN}

    app, harness, server = _build_app(
        [_REFUSED, _DELIVERED], terminal_registry=_claude_pane(sid, tmp_path, read)
    )
    resent_during_notice: list[int] = []

    async def slow_ready_notice(body: Any) -> None:
        if _notice_code(body) == "databricks_sign_in_completed":
            if wait_ends == "during-notice":
                # Expire the wait only once the watcher has claimed the release.
                app.state.sign_in_holds[sid].deadline = 0.0
            await asyncio.sleep(0.3)
            resent_during_notice.append(len(harness.posted_bodies) - 1)

    server.while_posting = slow_ready_notice
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await _until(
            lambda: (
                bool(resent_during_notice)
                and len(harness.posted_bodies) == 2
                and sid not in app.state.active_turns
            )
        )

    assert resent_during_notice == [0]
    (ready,) = server.notices("databricks_sign_in_completed")
    assert ready["message"] == "Claude Code is ready. Sending your message now."
    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first"]


@pytest.mark.asyncio
async def test_message_resent_when_its_wait_ran_out_gets_no_resend_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A message delivered once the wait ran out isn't followed by "send your message again"."""
    monkeypatch.setattr(runner_app, "_SIGN_IN_HOLD_MAX_S", 0.05)
    monkeypatch.setattr(sign_in_watch, "_SIGN_IN_WATCH_INTERVAL_S", 0.3)
    sid = uuid.uuid4().hex

    async def read(scrollback: int = 0, *, join_wrapped: bool = False) -> dict[str, object]:
        return {"screen": _READY_SCREEN}

    app, harness, server = _build_app(
        [_REFUSED, _DELIVERED], terminal_registry=_claude_pane(sid, tmp_path, read)
    )
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await _until(lambda: len(harness.posted_bodies) == 2 and sid not in app.state.active_turns)
        # Past the watcher's next poll of the now-ready pane.
        await asyncio.sleep(0.5)

    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first"]
    assert not server.notices("databricks_sign_in_completed")
    assert sid not in app.state.sign_in_holds


@pytest.mark.asyncio
async def test_held_message_is_sent_after_a_failed_bridge_lookup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bridge lookup that fails once the hold lifts is retried instead of ending the queue."""
    app, harness, _server = _build_app([_REFUSED, _DELIVERED])
    sid = uuid.uuid4().hex
    failures = 2

    async def flaky_bridge_id(**_: Any) -> str:
        nonlocal failures
        if sid in app.state.sign_in_holds and failures:
            failures -= 1
            raise httpx.ConnectError("synthetic labels lookup failure")
        return "label-owned-bridge"

    monkeypatch.setattr(runner_app, "_claude_native_bridge_id_for_session", flaky_bridge_id)
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await _create_session(client, sid)
            await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
            await _until(lambda: sid in app.state.sign_in_holds)
            app.state.sign_in_holds[sid].waiting = False
            await _until(
                lambda: len(harness.posted_bodies) == 2 and sid not in app.state.active_turns
            )

    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first"]
    assert failures == 0
    retried = [
        r for r in caplog.records if "Failed to resolve the Claude bridge" in r.getMessage()
    ]
    assert len(retried) == 1


@pytest.mark.asyncio
async def test_held_message_is_sent_when_the_queue_drains_prompt_check_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pane check that fails in the queue drain, after the waiter's own check worked, retries."""
    app, harness, _server = _build_app([_REFUSED, _DELIVERED])
    sid = uuid.uuid4().hex
    checks_after_release = 0

    def flaky_pending_prompt(_bridge_dir: Path) -> bool:
        nonlocal checks_after_release
        hold = app.state.sign_in_holds.get(sid)
        if hold is not None and not hold.waiting:
            checks_after_release += 1
            # The waiter checks the pane first; the drain's own check is the second.
            if checks_after_release == 2:
                raise OSError("synthetic tmux capture failure")
        return False

    monkeypatch.setattr(claude_native_bridge, "has_pending_user_prompt", flaky_pending_prompt)
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            await _create_session(client, sid)
            await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
            await _until(lambda: sid in app.state.sign_in_holds)
            app.state.sign_in_holds[sid].waiting = False
            await _until(
                lambda: len(harness.posted_bodies) == 2 and sid not in app.state.active_turns
            )

    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first"]
    assert checks_after_release >= 3
    assert any(
        "Failed to check the Claude prompt before sending" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resend", [_DELIVERED, _FAILED_OTHERWISE], ids=["delivered", "failed-otherwise"]
)
async def test_resend_that_is_not_refused_ends_the_wait_even_with_messages_still_queued(
    resend: list[str],
) -> None:
    """Once the held message ends any other way, a later refusal waits afresh with a notice."""
    app, harness, server = _build_app([_REFUSED, resend, _REFUSED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await _until(lambda: sid in app.state.sign_in_holds)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("second"))
        app.state.sign_in_holds[sid].waiting = False
        await _until(
            lambda: (
                len(harness.posted_bodies) == 3
                and sid in app.state.sign_in_holds
                and app.state.sign_in_holds[sid].waiting
            )
        )
        await asyncio.sleep(0.05)

    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first", "second"]
    assert app.state.sign_in_holds[sid].holds == 1
    assert len(server.notices("databricks_sign_in_pending")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "control"),
    [("interrupt", "inject_interrupt"), ("stop_session", "kill_session")],
)
async def test_interrupt_or_stop_during_the_hold_drops_the_held_message(
    action: str, control: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control sent while the message waits on the sign-in keeps it from ever being sent."""
    monkeypatch.setattr(claude_native_bridge, control, lambda *_a, **_k: None)
    app, harness, _server = _build_app([_REFUSED, _DELIVERED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await _until(lambda: sid in app.state.sign_in_holds)
        hold = app.state.sign_in_holds[sid]
        response = await client.post(f"/v1/sessions/{sid}/events", json={"type": action})
        assert response.status_code == 204
        # The sign-in completes after the control.
        hold.waiting = False
        await asyncio.sleep(0.1)  # custom-lint: disable=sleep-then-assert -- no send in window

    assert len(harness.posted_bodies) == 1
    assert sid not in app.state.sign_in_holds
    assert not app.state.session_message_buffers.get(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "control"),
    [("interrupt", "inject_interrupt"), ("stop_session", "kill_session")],
)
async def test_interrupt_or_stop_before_the_refused_stream_ends_is_never_undone(
    action: str, control: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control that lands after the refusal but before the stream ends drops the message."""
    monkeypatch.setattr(claude_native_bridge, control, lambda *_a, **_k: None)
    harness = _PausedStreamHarness([_REFUSED, _DELIVERED])
    app, _harness, server = _build_app([], harness=harness)
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        async with asyncio.timeout(5):
            await harness.frames_sent.wait()
        response = await client.post(f"/v1/sessions/{sid}/events", json={"type": action})
        assert response.status_code == 204
        harness.resume.set()
        await _until(lambda: sid not in app.state.active_turns)
        # A sign-in that completes afterwards finds nothing to send.
        if (hold := app.state.sign_in_holds.get(sid)) is not None:
            hold.waiting = False
        await asyncio.sleep(0.1)

    assert len(harness.posted_bodies) == 1
    assert sid not in app.state.sign_in_holds
    assert not app.state.session_message_buffers.get(sid)
    assert not server.notices("databricks_sign_in_pending")
    failures = [e for e in _published(app, sid) if e.get("type") == "response.failed"]
    if action == "stop_session":
        # Stopping the session ends the turn as cancelled, with no failure card.
        assert failures == []
        return
    # The refusal is reported as it was before messages were held.
    (failed,) = failures
    assert failed["error"]["code"] == "databricks_sign_in_pending"


@pytest.mark.asyncio
async def test_child_agent_dispatch_refused_by_a_sign_in_fails_at_once() -> None:
    """A sub-agent dispatch is never held: its launch bookkeeping fails it as before."""
    app, harness, server = _build_app([_REFUSED, _DELIVERED])
    parent, sid = uuid.uuid4().hex, uuid.uuid4().hex
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent, child_session_id=sid, agent="worker", title="child"
    )
    try:
        async with _runner_client(app) as client:
            await _create_session(client, sid)
            await client.post(f"/v1/sessions/{sid}/events", json=_message("child task"))
            await _until(lambda: sid not in app.state.active_turns and entry.status == "failed")
            await asyncio.sleep(0.05)
    finally:
        subagent_work.unregister_subagent_work(sid)

    assert len(harness.posted_bodies) == 1
    assert sid not in app.state.sign_in_holds
    assert not app.state.session_message_buffers.get(sid)
    assert not server.notices("databricks_sign_in_pending")
    (failed,) = [e for e in _published(app, sid) if e.get("type") == "response.failed"]
    assert failed["error"]["code"] == "databricks_sign_in_pending"


@pytest.mark.asyncio
async def test_ready_notice_during_the_resend_an_expired_wait_released_asks_for_no_resend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pane looks ready while the expired wait's resend is still running: no manual resend."""
    monkeypatch.setattr(runner_app, "_SIGN_IN_HOLD_MAX_S", 0.05)
    monkeypatch.setattr(sign_in_watch, "_SIGN_IN_WATCH_INTERVAL_S", 0.02)
    sid = uuid.uuid4().hex
    harness = _PausedStreamHarness([_REFUSED, _DELIVERED], pause_turn=2)

    async def read(scrollback: int = 0, *, join_wrapped: bool = False) -> dict[str, object]:
        # Still signing in until the expired wait has resent the message.
        return {"screen": _READY_SCREEN if harness.frames_sent.is_set() else _SIGN_IN_SCREEN}

    app, _harness, server = _build_app(
        [], terminal_registry=_claude_pane(sid, tmp_path, read), harness=harness
    )
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        # The resend is under way and the watcher sees the agent ready before it ends.
        await _until(lambda: bool(server.notices("databricks_sign_in_completed")))
        harness.resume.set()
        await _until(lambda: len(harness.posted_bodies) == 2 and sid not in app.state.active_turns)

    (ready,) = server.notices("databricks_sign_in_completed")
    assert ready["message"] == "Claude Code is ready. Sending your message now."
    assert [_sent_text(body) for body in harness.posted_bodies] == ["first", "first"]
    assert sid not in app.state.sign_in_holds


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_end", ["read-error", "required-terminal-exit"])
async def test_sign_in_refusal_is_reported_when_the_stream_breaks_after_it(
    stream_end: str,
) -> None:
    """A refusal kept for the wait still reaches the server as undelivered if the stream breaks."""
    harness = _BrokenStreamHarness([_REFUSED, _DELIVERED])
    app, _harness, server = _build_app([], harness=harness)
    sid = uuid.uuid4().hex
    if stream_end == "required-terminal-exit":

        def terminal_exited() -> None:
            app.state.required_terminal_exit_errors[sid] = {
                "code": "required_terminal_exited",
                "message": "Claude Code exited.",
            }

        harness.before_break = terminal_exited
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(
            f"/v1/sessions/{sid}/events", json={**_message("first"), "stable_id": _STABLE_ID}
        )
        await _until(lambda: sid not in app.state.active_turns)
        await asyncio.sleep(0.05)

    failures = [e for e in _published(app, sid) if e.get("type") == "response.failed"]
    refusal, broken = failures
    assert refusal["error"]["code"] == "databricks_sign_in_pending"
    assert refusal["error"]["undelivered"] is True
    assert refusal["input_stable_id"] == _STABLE_ID
    assert broken["error"]["code"] == (
        "required_terminal_exited"
        if stream_end == "required-terminal-exit"
        else "connection_error"
    )
    assert len(harness.posted_bodies) == 1
    assert sid not in app.state.sign_in_holds
    assert not app.state.session_message_buffers.get(sid)
    assert not server.notices("databricks_sign_in_pending")


@pytest.mark.asyncio
async def test_sign_in_refusal_is_reported_when_a_resync_cancels_the_turn() -> None:
    """A respawn or model-switch resync that ends the refused turn still reports it undelivered."""
    harness = _PausedStreamHarness([_REFUSED, _DELIVERED])
    app, _harness, server = _build_app([], harness=harness)
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        await _create_session(client, sid)
        await client.post(
            f"/v1/sessions/{sid}/events", json={**_message("first"), "stable_id": _STABLE_ID}
        )
        async with asyncio.timeout(5):
            await harness.frames_sent.wait()
        await app.state.resync_turn_state(
            sid, "harness_respawn_model_switch", owner_response_id="resp_1"
        )
        await _until(lambda: sid not in app.state.active_turns)
        await asyncio.sleep(0.05)

    (refusal,) = [e for e in _published(app, sid) if e.get("type") == "response.failed"]
    assert refusal["error"]["code"] == "databricks_sign_in_pending"
    assert refusal["error"]["undelivered"] is True
    assert refusal["input_stable_id"] == _STABLE_ID
    assert len(harness.posted_bodies) == 1
    assert sid not in app.state.sign_in_holds
    assert not app.state.session_message_buffers.get(sid)
    assert not server.notices("databricks_sign_in_pending")
