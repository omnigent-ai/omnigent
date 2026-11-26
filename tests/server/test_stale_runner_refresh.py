"""Which sessions a project-text edit relaunches when their runner can't carry it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.project_instructions_notice import CAP_PROJECT_INSTRUCTION_REFRESH
from omnigent.server import stale_runner_refresh
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.routes._sessions import orchestration, tool_reload
from omnigent.server.stale_runner_refresh import TextChange, recycle_stale_sessions

PROJECT = SimpleNamespace(
    id="p1", user_id="owner@example.com", main_session_id="lead", retired_main_session_ids=[]
)


def _conv(session_id, *, project="p1", native=True, identity=None, archived=False):
    labels = {PROJECT_IDENTITY_LABEL_KEY: identity} if identity else {}
    return SimpleNamespace(
        id=session_id,
        project_id=project,
        archived=archived,
        host_id="host-a",
        labels=labels,
        native=native,
    )


class _Runners:
    def __init__(self, runners):
        self._runners = runners

    def online_runner_ids(self):
        return list(self._runners)

    def get(self, runner_id):
        caps = self._runners[runner_id]["caps"]
        return SimpleNamespace(hello=SimpleNamespace(capabilities=caps))


class _Conversations:
    def __init__(self, runners):
        self._by_id = {c.id: c for r in runners.values() for c in r["sessions"]}
        self._runners = runners

    def list_runner_session_statuses(self, runner_id, *, after, limit):
        rows = [(c.id, "idle") for c in self._runners[runner_id]["sessions"]]
        start = next((i + 1 for i, (sid, _) in enumerate(rows) if sid == after), 0)
        return rows[start : start + limit]

    def get_conversation(self, session_id):
        return self._by_id.get(session_id)


@pytest.fixture
def scheduled(monkeypatch):
    calls: list[str] = []

    async def reloadable(conv, store):
        if conv.id == "shares-parent-runner":
            raise OmnigentError("shares its parent's runner", code=ErrorCode.INVALID_INPUT)

    monkeypatch.setattr(
        tool_reload,
        "schedule_tool_reload",
        lambda request, sid, *, user_id, **kwargs: calls.append((sid, user_id)),
    )
    monkeypatch.setattr(tool_reload, "tool_reload_pending", lambda sid: sid == "pending")
    monkeypatch.setattr(tool_reload, "require_reloadable", reloadable)
    monkeypatch.setattr(orchestration, "_is_native_terminal_session", lambda conv: conv.native)
    monkeypatch.setattr(
        "omnigent.server.project_orchestrator_rollover._owner_request",
        lambda app, key, template=None: SimpleNamespace(app=app),
    )
    return calls


class _Permissions:
    def list_for_sessions(self, session_ids):
        owner = {"worker": "member@example.com"}
        return {
            sid: [SimpleNamespace(user_id=owner[sid], level=LEVEL_OWNER)]
            for sid in session_ids
            if sid in owner
        }


def _app(runners):
    state = SimpleNamespace(tunnel_registry=_Runners(runners), permission_store=_Permissions())
    return SimpleNamespace(state=state), _Conversations(runners)


RUNNERS = {
    "old": {
        "caps": [],
        "sessions": [
            _conv("lead"),
            _conv("reviewer", identity="rev"),
            _conv("worker"),
            _conv("sdk", native=False),
            _conv("elsewhere", project="p2"),
            _conv("gone", archived=True),
            _conv("pending"),
            _conv("shares-parent-runner"),
        ],
    },
    "new": {"caps": [CAP_PROJECT_INSTRUCTION_REFRESH], "sessions": [_conv("fresh")]},
}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (TextChange(), ["lead", "reviewer", "worker"]),
        (TextChange(identity_id="rev"), ["reviewer"]),
        (TextChange(lead=True), ["lead"]),
    ],
    ids=["mandate", "persona", "lead-charter"],
)
async def test_edits_relaunch_only_native_sessions_on_old_runners(scheduled, change, expected):
    app, conversations = _app(RUNNERS)
    assert await recycle_stale_sessions(app, PROJECT, change, conversations=conversations) == (
        expected
    )
    assert [sid for sid, _user in scheduled] == expected
    # A member's own session relaunches as the member; the rest as the owner.
    owners = dict(scheduled)
    assert owners.get("worker", "member@example.com") == "member@example.com"
    assert owners.get("lead", "owner@example.com") == "owner@example.com"


def test_a_runner_reporting_the_refresh_is_left_alone():
    registry = _Runners(RUNNERS)
    assert stale_runner_refresh.runner_lacks_refresh(registry, "old")
    assert not stale_runner_refresh.runner_lacks_refresh(registry, "new")
    assert not stale_runner_refresh.runner_lacks_refresh(None, "old")


async def test_every_page_of_a_runner_is_read(scheduled, monkeypatch):
    monkeypatch.setattr(stale_runner_refresh, "_RUNNER_PAGE", 2)
    app, conversations = _app(RUNNERS)
    scheduled_ids = await recycle_stale_sessions(
        app, PROJECT, TextChange(), conversations=conversations
    )
    assert scheduled_ids == ["lead", "reviewer", "worker"]
