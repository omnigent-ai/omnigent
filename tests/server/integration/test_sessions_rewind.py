"""Integration tests for ``POST /v1/sessions/{id}/rewind``.

Rewinding forks a claude-sdk or claude-native session with only the history
before a chosen prompt, binds the fork to the source's host, workspace and live
runner, and leaves the source a notice plus a ``session.superseded`` redirect.
Real stores; the runner is a registered tunnel whose HTTP side is a mock
transport (shared with the claude-sdk ``/clear`` tests).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from omnigent.entities import USER_SESSION_TITLE_MAX_CHARS, MessageData, NewConversationItem
from omnigent.server.auth import LEVEL_EDIT
from omnigent.server.managed_hosts import REQUESTED_HOST_ID_LABEL_KEY, managed_repo_labels
from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests.server.integration._session_continuation import (
    _EDITOR,
    _HOST_ID,
    _OWNER,
    _RUNNER_ID,
    LiveRunner,
    bind_to_host,
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
    ("harness", "item_index", "prompt", "kept", "source"),
    [
        ("claude-sdk", 2, "Q2", ["Q1", "A1"], "placed"),
        ("claude-native", 2, "Q2", ["Q1", "A1"], "placed"),
        # The session's override, not the agent's declared harness, runs it.
        ("override:claude-native", 2, "Q2", ["Q1", "A1"], "placed"),
        # Nothing precedes the first prompt, so the fork starts like a /clear.
        ("claude-sdk", 0, "Q1", [], "placed"),
        # A fork's stored "Fork of <title>" can run past the limit a caller's
        # own title has; the rewound chat keeps it all the same.
        ("claude-sdk", 2, "Q2", ["Q1", "A1"], "fork_of_a_long_title"),
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
    source: str,
) -> None:
    """Rewinding to a prompt forks just before it, on the source's runner and host.

    A rewound Claude Code native session rebuilds its transcript from the
    kept items, whether the agent declares that harness or the session
    overrides it. The rewound chat keeps the source's stored title.
    """
    store = SqlAlchemyConversationStore(db_uri)
    if harness.startswith("override:"):
        source_id = await create_placed_session(client, store)
        store.update_conversation(source_id, harness_override=harness.removeprefix("override:"))
    else:
        resolve_harness_as(monkeypatch, harness)
        source_id = await create_placed_session(client, store)
    title = "Parser work"
    if source == "fork_of_a_long_title":
        long_title = "Parser work " + "x" * (USER_SESSION_TITLE_MAX_CHARS - 12)
        renamed = await client.patch(f"/v1/sessions/{source_id}", json={"title": long_title})
        assert renamed.status_code == 200, renamed.text
        forked = await client.post(f"/v1/sessions/{source_id}/fork", json={})
        assert forked.status_code == 201, forked.text
        source_id = forked.json()["id"]
        bind_to_host(store, source_id, "/work/repo", "feature/parser")
        store.replace_runner_id(source_id, _RUNNER_ID)
        title = f"Fork of {long_title}"
        assert len(title) > USER_SESSION_TITLE_MAX_CHARS
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
    assert fork.title == title
    initialized = [(path, body.get("session_id")) for path, body in live_runner.posts]
    assert ("/v1/sessions", new_id) in initialized
    assert message_texts(store, source_id)[:4] == ["Q1", "A1", "Q2", "A2"]
    notice = message_texts(store, source_id)[-1]
    assert "/rewind" in notice and f"(/c/{new_id})" in notice
    assert live_runner.superseded == [(source_id, new_id, "rewind")]
    # Listed in the owner's other tabs once placed.
    assert new_id in live_runner.announced


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


@pytest.mark.parametrize(
    ("harness", "rewound_to", "status"),
    [
        ("override:claude-native", "the_attachment_prompt", 200),
        ("override:claude-native", "a_later_prompt", 200),
        ("claude-sdk", "the_attachment_prompt", 200),
        ("claude-sdk", "a_later_prompt", 400),
    ],
)
async def test_rewind_checks_kept_filesystem_attachments_against_the_session_harness(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    rewound_to: str,
    status: int,
) -> None:
    """Only kept files a native harness must open are checked, against the session's harness.

    The agent declares a non-native harness. Rewinding to the prompt that
    attached the file keeps none of it, so any harness passes. Rewinding past
    it keeps the file: a session overridden to Claude Code native can open it,
    an SDK session cannot.
    """
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, turns=0)
    if harness.startswith("override:"):
        store.update_conversation(source_id, harness_override=harness.removeprefix("override:"))
    else:
        resolve_harness_as(monkeypatch, harness)
    # Recorded directly: the test host predates uploading such files.
    report = SqlAlchemyFileStore(db_uri).create(
        filename="report.xlsx", bytes=16, session_id=source_id
    )
    for turn, extra in ((1, [{"type": "input_file", "file_id": report.id}]), (2, [])):
        store.append(
            source_id,
            [
                NewConversationItem(
                    type="message",
                    response_id=f"resp_{turn}",
                    data=MessageData(
                        role="user",
                        content=[*extra, {"type": "input_text", "text": f"Q{turn}"}],
                    ),
                ),
                NewConversationItem(
                    type="message",
                    response_id=f"resp_{turn}",
                    data=MessageData(
                        role="assistant",
                        content=[{"type": "output_text", "text": f"A{turn}"}],
                        agent="test-agent",
                    ),
                ),
            ],
        )
    items = item_ids(store, source_id)
    item_index = 0 if rewound_to == "the_attachment_prompt" else 2

    resp = await client.post(
        f"/v1/sessions/{source_id}/rewind", json={"item_id": items[item_index]}
    )

    assert resp.status_code == status, resp.text
    if status != 200:
        assert "report.xlsx" in resp.text
        return
    kept = [] if rewound_to == "the_attachment_prompt" else ["Q1", "A1"]
    assert [
        part["text"]
        for item in store.list_items(resp.json()["session_id"]).data
        if isinstance(item.data, MessageData)
        for part in item.data.content
        if "text" in part
    ] == kept


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

    listed_files: list[str] = []
    list_files = SqlAlchemyFileStore.list

    def record_listing(
        self: SqlAlchemyFileStore, session_id: str, *args: Any, **kwargs: Any
    ) -> Any:
        listed_files.append(session_id)
        return list_files(self, session_id, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyFileStore, "list", record_listing)

    resp = await client.post(f"/v1/sessions/{source_id}/rewind", json={"item_id": prompt.id})

    assert resp.status_code == 200, resp.text
    new_id, attachments = resp.json()["session_id"], resp.json()["attachments"]
    # The fork's copies come from the map the fork made, not a listing of its files.
    assert new_id not in listed_files
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
    get_file = SqlAlchemyFileStore.get

    def get_only_from_the_source(
        self: SqlAlchemyFileStore, file_id: str, session_id: str | None = None
    ) -> Any:
        if session_id is not None and session_id != source_id:
            raise RuntimeError("file store unavailable")
        return get_file(self, file_id, session_id=session_id)

    monkeypatch.setattr(SqlAlchemyFileStore, "get", get_only_from_the_source)

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
        # Well-formed ids: another session's prompt, and one no item has.
        ("claude-sdk", 2, "foreign_prompt", 400, "needs one of this session's prompts"),
        ("claude-sdk", 2, "unknown_item", 400, "needs one of this session's prompts"),
        ("claude-sdk", 2, "editor", 403, ""),
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
    """Unsupported harnesses, items that aren't this session's prompts and non-owners are refused.

    Refusals shared with ``/clear`` are in ``test_sessions_continuation.py``.
    """
    resolve_harness_as(monkeypatch, harness)
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(auth_client, store, user=_OWNER)
    items = item_ids(store, source_id)
    caller = _OWNER
    if placement == "editor":
        granted = await auth_client.put(
            f"/v1/sessions/{source_id}/permissions",
            json={"user_id": _EDITOR, "level": LEVEL_EDIT},
            headers={"X-Forwarded-Email": _OWNER},
        )
        assert granted.status_code < 300, granted.text
        caller = _EDITOR
    item_id = items[item_index] if item_index is not None else "msg_bogus"
    if placement == "foreign_prompt":
        other_id = await create_placed_session(auth_client, store, user=_OWNER)
        item_id = item_ids(store, other_id)[2]
    elif placement == "unknown_item":
        item_id = uuid.uuid4().hex
    headers = {"X-Forwarded-Email": caller}
    before = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])

    resp = await auth_client.post(
        f"/v1/sessions/{source_id}/rewind",
        json={"item_id": item_id},
        headers=headers,
    )

    assert resp.status_code == status, resp.text
    assert message in resp.text
    after = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])
    assert after == before
    assert auth_live_runner.superseded == []
