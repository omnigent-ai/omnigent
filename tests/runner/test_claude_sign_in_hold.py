"""A Claude message refused by a launcher sign-in waits for the sign-in instead of failing."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, sign_in_watch
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

_SIGN_IN_LINK = "Open https://example.okta.test/activate and enter code ABCD-EFGH."


def _sign_in_refusal(*, undelivered: bool = True) -> str:
    error: dict[str, Any] = {
        "code": "databricks_sign_in_pending",
        "message": (
            "Claude Code is waiting for a sign-in in this session's terminal, "
            "so the message was not delivered."
        ),
        "title": "Claude Code can't start until you sign in to Databricks",
        "remediation": _SIGN_IN_LINK,
    }
    if undelivered:
        error["undelivered"] = True
    return _sse({"type": "response.failed", "response": {"status": "failed"}, "error": error})


_REFUSED = [_sse({"type": "response.created", "response": {"id": "resp_1"}}), _sign_in_refusal()]
_DELIVERED = [
    _sse({"type": "response.created", "response": {"id": "resp_2"}}),
    _sse({"type": "response.completed", "response": {"id": "resp_2"}}),
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


class _RecordingServerClient(NullServerClient):
    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.posts.append((url, kwargs.get("json")))
        return await super().post(url, **kwargs)

    def notices(self, code: str) -> list[dict[str, Any]]:
        return [
            body["data"]["item_data"]
            for _url, body in self.posts
            if isinstance(body, dict)
            and body.get("type") == "external_conversation_item"
            and body["data"]["item_data"].get("code") == code
        ]


def _build_app(
    scripts: list[list[str]],
) -> tuple[FastAPI, _TurnScriptHarness, _RecordingServerClient]:
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    harness = _TurnScriptHarness(scripts)
    server = _RecordingServerClient()
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=server,  # type: ignore[arg-type]
    )
    return app, harness, server


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
async def test_sign_in_refusal_holds_the_message_and_sends_it_after_sign_in() -> None:
    """The refused message is neither failed nor lost; it goes out once the sign-in is done."""
    app, harness, server = _build_app([_REFUSED, _DELIVERED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex}
        )
        assert created.status_code == 201
        first = await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        assert first.status_code == 202
        await _until(
            lambda: sid in app.state.sign_in_holds and server.notices("databricks_sign_in_pending")
        )

        published = _published(app, sid)
        assert not [e for e in published if e.get("type") == "response.failed"]
        assert not [
            e for e in published if e.get("type") == "session.status" and e["status"] == "failed"
        ]
        assert len(harness.posted_bodies) == 1
        assert [_sent_text(_message_as_body(m)) for m in app.state.session_message_buffers[sid]]
        (notice,) = server.notices("databricks_sign_in_pending")
        assert notice["level"] == "info"
        assert notice["remediation"] == _SIGN_IN_LINK
        assert "will be sent once you sign in" in notice["message"]

        # A message sent meanwhile queues behind the held one.
        second = await client.post(f"/v1/sessions/{sid}/events", json=_message("second"))
        assert second.status_code == 202
        assert (
            second.json()["detail"] == "Message buffered until the Databricks sign-in completes."
        )
        await asyncio.sleep(0.05)
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


def _message_as_body(message: dict[str, Any]) -> dict[str, Any]:
    return {"content": [message]}


@pytest.mark.asyncio
async def test_sign_in_refusal_still_fails_once_the_wait_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sign-in that never completes ends in today's failure with its card, not a loop."""
    monkeypatch.setattr(runner_app, "_SIGN_IN_HOLD_MAX_S", 0.05)
    app, harness, _server = _build_app([_REFUSED])
    sid = uuid.uuid4().hex
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex}
        )
        assert created.status_code == 201
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
        created = await client.post(
            "/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex}
        )
        assert created.status_code == 201
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
        created = await client.post(
            "/v1/sessions", json={"session_id": sid, "agent_id": uuid.uuid4().hex}
        )
        assert created.status_code == 201
        await client.post(f"/v1/sessions/{sid}/events", json=_message("first"))
        await _until(lambda: len(harness.posted_bodies) == 1 and sid not in app.state.active_turns)
        await asyncio.sleep(0.05)

    statuses = [e for e in _published(app, sid) if e.get("type") == "session.status"]
    assert [e["status"] for e in statuses] == ["failed"]
    assert sid not in app.state.sign_in_holds
    assert not app.state.session_message_buffers.get(sid)


def test_hold_notice_names_the_link_to_open() -> None:
    """The neutral notice carries the sign-in address as its next step."""
    server = _RecordingServerClient()
    watch = sign_in_watch.build_sign_in_watch(
        _background_tasks=set(),
        _session_harness_name=lambda _conv: "claude-native",
        _release_sign_in_hold=lambda _conv: False,
        _sign_in_watchers={},
        resource_registry=None,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )
    asyncio.run(
        watch.post_sign_in_hold_notice(
            "conv_1", json.loads(_sign_in_refusal().split("data: ", 1)[1])["error"]
        )
    )
    (notice,) = server.notices("databricks_sign_in_pending")
    assert notice == {
        "source": "harness",
        "code": "databricks_sign_in_pending",
        "title": "Claude Code can't start until you sign in to Databricks",
        "message": (
            "Claude Code is waiting for a sign-in in this session's terminal. "
            "Your message will be sent once you sign in."
        ),
        "remediation": _SIGN_IN_LINK,
        "level": "info",
    }
