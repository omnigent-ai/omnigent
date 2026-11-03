"""Integration tests for ``POST /v1/sessions/{id}/rewind``.

Rewinding forks a claude-sdk or claude-native session with only the history
before a chosen prompt, binds the fork to the source's host, workspace and live
runner, and leaves the source a notice plus a ``session.superseded`` redirect.
Real stores; the runner is a registered tunnel whose HTTP side is a mock
transport (shared with the claude-sdk ``/clear`` tests).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.auth import LEVEL_EDIT
from omnigent.server.managed_hosts import REQUESTED_HOST_ID_LABEL_KEY, managed_repo_labels
from omnigent.server.routes import sessions as sessions_module
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.integration.test_sessions_clear import (
    _EDITOR,
    _HOST_ID,
    _OWNER,
    _RUNNER_ID,
    LiveRunner,
    create_placed_session,
    message_texts,
    resolve_harness_as,
)
from tests.server.integration.test_sessions_clear import (
    auth_live_runner as auth_live_runner,
)
from tests.server.integration.test_sessions_clear import (
    live_runner as live_runner,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("harness", "item_index", "prompt", "kept"),
    [
        ("claude-sdk", 2, "Q2", ["Q1", "A1"]),
        ("claude-native", 2, "Q2", ["Q1", "A1"]),
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
    """Rewinding to a prompt forks just before it, on the source's runner and host."""
    resolve_harness_as(monkeypatch, harness)
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    items = [item.id for item in store.list_items(source_id).data]
    repos = ["https://github.com/acme/api"]
    sandbox_labels = {**managed_repo_labels(repos), REQUESTED_HOST_ID_LABEL_KEY: _HOST_ID}
    store.set_labels(source_id, sandbox_labels)

    resp = await client.post(
        f"/v1/sessions/{source_id}/rewind", json={"item_id": items[item_index]}
    )

    assert resp.status_code == 200, resp.text
    new_id = resp.json()["session_id"]
    assert resp.json()["prompt"] == prompt
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
    assert fork.terminal_launch_args == ["--permission-mode", "acceptEdits"]
    assert fork.labels["team"] == "core"
    # A relaunch of the shared sandbox still re-clones the repositories.
    assert {key: fork.labels.get(key) for key in sandbox_labels} == sandbox_labels
    assert fork.title == "Parser work"
    initialized = [(path, body.get("session_id")) for path, body in live_runner.posts]
    assert ("/v1/sessions", new_id) in initialized
    assert message_texts(store, source_id)[:4] == ["Q1", "A1", "Q2", "A2"]
    notice = message_texts(store, source_id)[-1]
    assert "/rewind" in notice and f"(/c/{new_id})" in notice
    assert live_runner.superseded == [(source_id, new_id, "rewind")]


@pytest.mark.parametrize(
    ("runner_id", "host", "host_live", "runner_leaves"),
    [
        pytest.param("runner_gone", True, True, False, id="on-another-replica"),
        pytest.param(_RUNNER_ID, True, False, True, id="leaves-host-bound"),
        pytest.param(_RUNNER_ID, False, False, True, id="leaves-runner-only"),
    ],
)
async def test_rewind_re_addresses_or_cleans_up_when_the_runner_is_elsewhere(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    runner_id: str,
    host: bool,
    host_live: bool,
    runner_leaves: bool,
) -> None:
    """A runner on another replica re-addresses /rewind before anything is created.

    Rewind frees no client first, so a runner can only leave while the fork is
    made: a host-bound fork then continues on the host, and a runner-only one
    is removed so a retry does not leave a second copy.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, runner_id=runner_id, host=host)
    items = [item.id for item in store.list_items(source_id).data]
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(host_id=_HOST_ID, name="laptop", user_id="local")
    if not host_live:
        host_store.set_offline(_HOST_ID)
    monkeypatch.setattr(app.state, "host_store", host_store)
    if runner_leaves:
        fork_conversation = SqlAlchemyConversationStore.fork_conversation

        def fork_then_disconnect(self: SqlAlchemyConversationStore, *args: Any, **kwargs: Any):
            forked = fork_conversation(self, *args, **kwargs)
            app.state.tunnel_registry.deregister(_RUNNER_ID)
            return forked

        monkeypatch.setattr(SqlAlchemyConversationStore, "fork_conversation", fork_then_disconnect)
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(f"/v1/sessions/{source_id}/rewind", json={"item_id": items[2]})

    if host:
        if host_live:
            assert resp.status_code == 400, resp.text
            assert "wrong_replica" in resp.text
            assert len((await client.get("/v1/sessions")).json()["data"]) == before
            assert live_runner.superseded == []
            return
        assert resp.status_code == 200, resp.text
        new_id = resp.json()["session_id"]
        fork = store.get_conversation(new_id)
        assert fork is not None
        assert (fork.runner_id, fork.host_id, fork.workspace) == (None, _HOST_ID, "/work/repo")
        assert message_texts(store, new_id) == ["Q1", "A1"]
        assert live_runner.superseded == [(source_id, new_id, "rewind")]
        return
    assert resp.status_code == 503, resp.text
    assert "runner is offline" in resp.text
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert live_runner.superseded == []


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


@pytest.mark.parametrize(
    ("harness", "item_index", "placement", "status", "message"),
    [
        ("openai-agents", 2, "runner", 400, "/rewind is not available"),
        ("claude-sdk", 1, "runner", 400, "needs one of this session's prompts"),
        ("claude-sdk", None, "runner", 400, "needs one of this session's prompts"),
        ("claude-sdk", 2, "none", 503, "runner is offline"),
        ("claude-sdk", 2, "running", 400, "Stop the current turn"),
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
    items = [item.id for item in store.list_items(source_id).data]
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
    headers = {"X-Forwarded-Email": caller}
    before = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])

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
