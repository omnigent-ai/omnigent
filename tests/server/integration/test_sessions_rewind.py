"""Integration tests for ``POST /v1/sessions/{id}/rewind``.

Rewinding forks a claude-sdk or claude-native session with only the history
before a chosen prompt, binds the fork to the source's host, workspace and live
runner, and leaves the source a notice plus a ``session.superseded`` redirect.
Real stores; the runner is a registered tunnel whose HTTP side is a mock
transport (shared with the claude-sdk ``/clear`` tests).
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import pytest

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER
from omnigent.server.managed_hosts import REQUESTED_HOST_ID_LABEL_KEY, managed_repo_labels
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions.orchestration import _mark_dispatch_in_flight
from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration._session_continuation import (
    _EDITOR,
    _HOST_ID,
    _OWNER,
    _RUNNER_ID,
    LiveRunner,
    create_placed_session,
    message_texts,
    resolve_harness_as,
)
from tests.server.integration._session_continuation import (
    auth_live_runner as auth_live_runner,
)
from tests.server.integration._session_continuation import (
    live_runner as live_runner,
)

pytestmark = pytest.mark.asyncio


def item_ids(store: SqlAlchemyConversationStore, session_id: str) -> list[str]:
    """Return *session_id*'s item ids in order (Q1, A1, Q2, A2 when placed)."""
    return [item.id for item in store.list_items(session_id).data]


@pytest.mark.parametrize(
    ("harness", "item_index", "prompt", "kept"),
    [
        ("claude-sdk", 2, "Q2", ["Q1", "A1"]),
        ("claude-native", 2, "Q2", ["Q1", "A1"]),
        # The session's override, not the agent's declared harness, runs it.
        ("override:claude-native", 2, "Q2", ["Q1", "A1"]),
        # Nothing precedes the first prompt, so the fork starts like a /clear.
        ("claude-sdk", 0, "Q1", []),
    ],
)
async def test_rewind_continues_on_a_truncated_fork_of_the_same_placement(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    item_index: int,
    prompt: str,
    kept: list[str],
) -> None:
    """Rewinding to a prompt forks just before it, on the source's runner and host.

    A rewound Claude Code native session rebuilds its transcript from the
    kept items, whether the agent declares that harness or the session
    overrides it.
    """
    store = SqlAlchemyConversationStore(db_uri)
    if harness.startswith("override:"):
        source_id = await create_placed_session(client, store)
        store.update_conversation(source_id, harness_override=harness.removeprefix("override:"))
    else:
        resolve_harness_as(monkeypatch, harness)
        source_id = await create_placed_session(client, store)
    items = item_ids(store, source_id)
    repos = ["https://github.com/acme/api"]
    sandbox_labels = {**managed_repo_labels(repos), REQUESTED_HOST_ID_LABEL_KEY: _HOST_ID}
    store.set_labels(source_id, sandbox_labels)
    store.update_conversation(
        source_id, cost_control_mode_override="on", subagent_routing_override="on"
    )
    listed_files: list[str] = []
    list_files = SqlAlchemyFileStore.list

    def record_listing(
        self: SqlAlchemyFileStore, session_id: str, *args: Any, **kwargs: Any
    ) -> Any:
        listed_files.append(session_id)
        return list_files(self, session_id, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyFileStore, "list", record_listing)

    resp = await client.post(
        f"/v1/sessions/{source_id}/rewind", json={"item_id": items[item_index]}
    )

    assert resp.status_code == 200, resp.text
    new_id = resp.json()["session_id"]
    assert resp.json()["prompt"] == prompt
    # A text-only prompt has no attachments to look up among the fork's files.
    assert (resp.json()["attachments"], new_id in listed_files) == ([], False)
    assert message_texts(store, new_id) == kept
    fork = store.get_conversation(new_id)
    assert fork is not None
    assert (fork.runner_id, fork.host_id, fork.workspace, fork.git_branch) == (
        _RUNNER_ID,
        _HOST_ID,
        "/work/repo",
        "feature/parser",
    )
    assert (fork.model_override, fork.reasoning_effort) == ("claude-opus-4-7", "high")
    assert (fork.cost_control_mode_override, fork.subagent_routing_override) == ("on", "on")
    assert fork.terminal_launch_args == ["--permission-mode", "acceptEdits"]
    assert fork.labels["team"] == "core"
    carries = fork.labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
    assert carries is harness.endswith("claude-native"), fork.labels
    # A relaunch of the shared sandbox still re-clones the repositories.
    assert {key: fork.labels.get(key) for key in sandbox_labels} == sandbox_labels
    assert fork.title == "Parser work"
    initialized = [(path, body.get("session_id")) for path, body in live_runner.posts]
    assert ("/v1/sessions", new_id) in initialized
    assert message_texts(store, source_id)[:4] == ["Q1", "A1", "Q2", "A2"]
    notice = message_texts(store, source_id)[-1]
    assert "/rewind" in notice and f"(/c/{new_id})" in notice
    assert live_runner.superseded == [(source_id, new_id, "rewind")]


async def test_rewind_refuses_a_message_sent_while_it_moves_the_session(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message or second /rewind sent while the rewound session is being set up is refused.

    Rewind frees no client first, so the window is the new session's setup on
    the runner. The message would otherwise run in the original chat while
    viewers follow the rewound one, and a second rewind would fork it again.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    sent: list[httpx.Response] = []

    async def send_from_another_tab() -> None:
        live_runner.on_session_init = None
        sent.append(
            await client.post(
                f"/v1/sessions/{source_id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "Q3"}]},
                },
            )
        )
        sent.append(
            await client.post(
                f"/v1/sessions/{source_id}/rewind",
                json={"item_id": item_ids(store, source_id)[0]},
            )
        )

    live_runner.on_session_init = send_from_another_tab
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(
        f"/v1/sessions/{source_id}/rewind", json={"item_id": item_ids(store, source_id)[2]}
    )

    assert resp.status_code == 200, resp.text
    new_id = resp.json()["session_id"]
    assert [response.status_code for response in sent] == [409, 409], [r.text for r in sent]
    assert "moving to a new session" in sent[0].text
    assert "already moving to a new session" in sent[1].text
    # Only the first rewind made a session.
    assert len((await client.get("/v1/sessions")).json()["data"]) == before + 1
    assert [body for _path, body in live_runner.posts if body.get("type") == "message"] == []
    assert "Q3" not in message_texts(store, source_id)
    assert live_runner.superseded == [(source_id, new_id, "rewind")]


async def test_rewind_returns_the_prompt_attachments_as_the_fork_holds_them(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attachment-only prompt comes back as the fork's copies of its files."""
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    uploads = {}
    for filename, data, content_type in (
        ("notes.txt", b"parser notes", "text/plain"),
        ("diagram.png", b"\x89PNG fake", "image/png"),
        ("deleted.txt", b"gone", "text/plain"),
    ):
        upload = await client.post(
            f"/v1/sessions/{source_id}/resources/files",
            files={"file": (filename, data, content_type)},
        )
        assert upload.status_code == 201, upload.text
        uploads[filename] = upload.json()["id"]
    deleted = await client.delete(
        f"/v1/sessions/{source_id}/resources/files/{uploads.pop('deleted.txt')}"
    )
    assert deleted.status_code == 200, deleted.text
    [prompt] = store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_3",
                data=MessageData(
                    role="user",
                    content=[
                        {"type": "input_image", "file_id": uploads["diagram.png"]},
                        {"type": "input_file", "file_id": uploads["notes.txt"]},
                        {"type": "input_file", "file_id": deleted.json()["id"]},
                    ],
                ),
            )
        ],
    )

    resp = await client.post(f"/v1/sessions/{source_id}/rewind", json={"item_id": prompt.id})

    assert resp.status_code == 200, resp.text
    new_id, attachments = resp.json()["session_id"], resp.json()["attachments"]
    assert resp.json()["prompt"] == ""
    assert [(a["type"], a["filename"]) for a in attachments] == [
        ("input_image", "diagram.png"),
        ("input_file", "notes.txt"),
    ]
    assert not {a["file_id"] for a in attachments} & set(uploads.values())
    contents = [
        (await client.get(f"/v1/sessions/{new_id}/resources/files/{a['file_id']}/content")).content
        for a in attachments
    ]
    assert contents == [b"\x89PNG fake", b"parser notes"]


async def test_rewind_still_returns_the_new_session_when_its_attachments_fail(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rewound session already exists, so a failed attachment lookup drops only them."""
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    upload = await client.post(
        f"/v1/sessions/{source_id}/resources/files",
        files={"file": ("notes.txt", b"parser notes", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    [prompt] = store.append(
        source_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_3",
                data=MessageData(
                    role="user",
                    content=[
                        {"type": "input_file", "file_id": upload.json()["id"]},
                        {"type": "input_text", "text": "Q3"},
                    ],
                ),
            )
        ],
    )
    list_files = SqlAlchemyFileStore.list

    def list_only_the_source(
        self: SqlAlchemyFileStore, session_id: str, *args: Any, **kwargs: Any
    ) -> Any:
        if session_id != source_id:
            raise RuntimeError("file store unavailable")
        return list_files(self, session_id, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyFileStore, "list", list_only_the_source)

    resp = await client.post(f"/v1/sessions/{source_id}/rewind", json={"item_id": prompt.id})

    assert resp.status_code == 200, resp.text
    new_id = resp.json()["session_id"]
    assert (resp.json()["prompt"], resp.json()["attachments"]) == ("Q3", [])
    assert message_texts(store, new_id) == ["Q1", "A1", "Q2", "A2"]
    assert live_runner.superseded == [(source_id, new_id, "rewind")]


@pytest.mark.parametrize(
    ("harness", "item_index", "placement", "status", "message"),
    [
        ("openai-agents", 2, "runner", 400, "/rewind is not available"),
        ("claude-sdk", 1, "runner", 400, "needs one of this session's prompts"),
        ("claude-sdk", None, "runner", 400, "needs one of this session's prompts"),
        ("claude-sdk", 2, "none", 503, "runner is offline"),
        ("claude-sdk", 2, "running", 400, "Stop the current turn"),
        # A message still on its way to the runner counts as a running turn.
        ("claude-sdk", 2, "starting", 400, "Stop the current turn"),
        ("claude-sdk", 2, "editor", 403, ""),
        ("claude-sdk", 0, "side_chat", 400, "isn't available in a side chat"),
    ],
)
async def test_rewind_refusals_create_no_session(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    auth_live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    item_index: int | None,
    placement: str,
    status: int,
    message: str,
) -> None:
    """Unsupported harnesses, non-prompts, no placement, busy turns and non-owners are refused."""
    resolve_harness_as(monkeypatch, harness)
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(
        auth_client,
        store,
        runner_id=None if placement == "none" else _RUNNER_ID,
        host=placement != "none",
        user=_OWNER,
    )
    items = item_ids(store, source_id)
    caller = _OWNER
    if placement == "running":
        monkeypatch.setitem(sessions_module._session_status_cache, source_id, "running")
    elif placement == "editor":
        granted = await auth_client.put(
            f"/v1/sessions/{source_id}/permissions",
            json={"user_id": _EDITOR, "level": LEVEL_EDIT},
            headers={"X-Forwarded-Email": _OWNER},
        )
        assert granted.status_code < 300, granted.text
        caller = _EDITOR
    elif placement == "side_chat":
        # A side chat rides its source's runner and has no host of its own.
        side_chat = store.create_conversation(
            agent_id=store.get_conversation(source_id).agent_id,
            labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source_id},
        )
        store.replace_runner_id(side_chat.id, _RUNNER_ID)
        SqlAlchemyPermissionStore(db_uri).grant(_OWNER, side_chat.id, LEVEL_OWNER)
        store.append(
            side_chat.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="resp_side",
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": "Side question"}]
                    ),
                )
            ],
        )
        source_id = side_chat.id
        items = item_ids(store, source_id)
    headers = {"X-Forwarded-Email": caller}
    before = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])

    with (
        _mark_dispatch_in_flight(source_id)
        if placement == "starting"
        else contextlib.nullcontext()
    ):
        resp = await auth_client.post(
            f"/v1/sessions/{source_id}/rewind",
            json={"item_id": items[item_index] if item_index is not None else "msg_bogus"},
            headers=headers,
        )

    assert resp.status_code == status, resp.text
    assert message in resp.text
    after = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])
    assert after == before
    assert auth_live_runner.superseded == []
