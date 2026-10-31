"""Integration tests for ``POST /v1/sessions/{id}/rewind``.

Rewinding forks a claude-sdk or claude-native session with only the history
before a chosen prompt, binds the fork to the source's host, workspace and live
runner, and leaves the source a notice plus a ``session.superseded`` redirect.
Real stores; the runner is a registered tunnel whose HTTP side is a mock
transport (shared with the claude-sdk ``/clear`` tests).
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.routes import sessions as sessions_module
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.integration.test_sessions_clear import (
    _HOST_ID,
    _RUNNER_ID,
    LiveRunner,
    create_placed_session,
    message_texts,
    resolve_harness_as,
)
from tests.server.integration.test_sessions_clear import (
    live_runner as live_runner,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("harness", ["claude-sdk", "claude-native"])
async def test_rewind_continues_on_a_truncated_fork_of_the_same_placement(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    """Rewinding to a prompt forks just before it, on the source's runner and host."""
    resolve_harness_as(monkeypatch, harness)
    store = SqlAlchemyConversationStore(db_uri)
    source_id, items = await create_placed_session(client, store)

    resp = await client.post(f"/v1/sessions/{source_id}/rewind", json={"item_id": items[2]})

    assert resp.status_code == 200, resp.text
    new_id = resp.json()["session_id"]
    assert resp.json()["prompt"] == "Q2"
    assert message_texts(store, new_id) == ["Q1", "A1"]
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
    assert fork.title == "Parser work"
    initialized = [(path, body.get("session_id")) for path, body in live_runner.posts]
    assert ("/v1/sessions", new_id) in initialized
    assert message_texts(store, source_id)[:4] == ["Q1", "A1", "Q2", "A2"]
    notice = message_texts(store, source_id)[-1]
    assert "/rewind" in notice and f"(/c/{new_id})" in notice
    assert live_runner.superseded == [(source_id, new_id, "rewind")]


async def test_rewind_to_the_first_prompt_starts_empty(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing precedes the first prompt, so the fork starts like a /clear."""
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id, items = await create_placed_session(client, store)

    resp = await client.post(f"/v1/sessions/{source_id}/rewind", json={"item_id": items[0]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["prompt"] == "Q1"
    assert message_texts(store, resp.json()["session_id"]) == []


@pytest.mark.parametrize(
    ("harness", "item_index", "placement", "status", "message"),
    [
        ("openai-agents", 2, "runner", 400, "/rewind is not available"),
        ("claude-sdk", 1, "runner", 400, "needs one of this session's prompts"),
        ("claude-sdk", None, "runner", 400, "needs one of this session's prompts"),
        ("claude-sdk", 2, "none", 503, "runner is offline"),
        ("claude-sdk", 2, "running", 400, "Stop the current turn"),
    ],
)
async def test_rewind_refusals_create_no_session(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    item_index: int | None,
    placement: str,
    status: int,
    message: str,
) -> None:
    """Unsupported harnesses, non-prompts, no placement and busy turns are refused."""
    resolve_harness_as(monkeypatch, harness)
    store = SqlAlchemyConversationStore(db_uri)
    source_id, items = await create_placed_session(
        client,
        store,
        runner_id=None if placement == "none" else _RUNNER_ID,
        host=placement != "none",
    )
    if placement == "running":
        monkeypatch.setitem(sessions_module._session_status_cache, source_id, "running")
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await client.post(
        f"/v1/sessions/{source_id}/rewind",
        json={"item_id": items[item_index] if item_index is not None else "msg_bogus"},
    )

    assert resp.status_code == status, resp.text
    assert message in resp.text
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert live_runner.superseded == []
