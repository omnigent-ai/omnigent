"""Forum scheduling, persistence, and cancellation contracts."""

import uuid

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.server.forum.service import explicit_stop
from omnigent.server.forum.state import ForumState, Participant
from omnigent.server.forum.store import ForumStore


@pytest.fixture(autouse=True)
def enable_forums(monkeypatch):
    monkeypatch.setenv("OMNIGENT_FEATURES", "forums")


def state(count=3, maximum=100):
    return ForumState(
        [
            Participant(uuid.uuid4().hex, "codex", "same-model", f"Agent {i + 1}")
            for i in range(count)
        ],
        max_responses=maximum,
    )


def message(s, text="Solve the task"):
    s.post({"id": uuid.uuid4().hex, "author": "alice", "role": "user", "text": text})


def finish(s, now=0, error=None):
    index = s.select(now)
    assert index is not None
    token = uuid.uuid4().hex
    s.begin(index, token)
    s.finish(token, f"Response from {index}", now, error)
    return index


def test_order_wrap_and_limit():
    s = state(maximum=5)
    assert s.select(0) is None
    message(s)
    assert [finish(s) for _ in range(5)] == [0, 1, 2, 0, 1]
    assert s.completed == 5
    assert s.status == "limit"
    assert s.select(0) is None


def test_guidance_is_deferred_and_stop_fences_late_completion():
    s = state()
    message(s)
    s.begin(0, "old")
    message(s, "Consider edge cases")
    assert s.active_token == "old"
    s.stop()
    message(s, "Continue please")
    assert not s.finish("old", "late", 0)
    assert s.completed == 0
    assert s.select(0) == 1
    assert len(s.posts) == 3


def test_restart_after_limit_resets_allowance_and_preserves_position():
    s = state(maximum=1)
    message(s)
    finish(s)
    message(s, "More review")
    assert s.completed == 0
    assert s.status == "running"
    assert finish(s) == 1
    assert s.status == "limit"


def test_failure_pause_recovery_does_not_reset_count():
    s = state(2)
    message(s)
    finish(s)
    finish(s, error="offline")
    assert s.completed == 1
    assert s.select(0) is None
    assert s.status == "paused"
    assert s.select(100) == 1
    assert s.completed == 1
    s.stop()
    assert s.select(1000) is None


def test_healthy_agents_continue_while_one_cools_down():
    s = state()
    message(s)
    finish(s, error="offline")
    assert s.select(0) is None
    finish(s, now=2, error="offline")
    finish(s, now=6, error="offline")
    assert s.select(6) == 1
    assert s.completed == 0


def test_duplicate_message_does_not_restart_twice():
    s = state()
    post = {"id": "same", "author": "alice", "role": "user", "text": "go"}
    s.post(post)
    finish(s)
    s.post(post)
    assert s.completed == 1
    assert len([p for p in s.posts if p["id"] == "same"]) == 1


@pytest.mark.parametrize(
    "text",
    ["that is enough", "Thank you all!", "Stop", "Please stop the discussion", "We're done."],
)
def test_stop_intent(text):
    assert explicit_stop(text)


@pytest.mark.parametrize(
    "text",
    [
        "Don't stop",
        "Thank you all, now test it",
        'Print "thank you all"',
        "Stop using global variables",
    ],
)
def test_guidance_is_not_stop(text):
    assert not explicit_stop(text)


def test_store_is_durable_tenant_scoped_and_leased(db_uri):
    store = ForumStore(db_uri)
    forum_id = uuid.uuid4().hex
    store.create(forum_id, state())
    assert store.mutate(forum_id, lambda s: message(s), owner="worker-a")
    assert store.mutate(forum_id, lambda s: s.stop(), owner="worker-b") is None
    assert ForumStore(db_uri).get(forum_id).status == "running"
    with workspace_scope(99):
        assert store.get(forum_id) is None
        assert store.ids() == []
    store.mutate(forum_id, lambda s: s.stop())
    assert store.get(forum_id).status == "stopped"
    store.release(forum_id, "worker-a")
    assert store.mutate(forum_id, lambda s: None, owner="worker-b") is not None


@pytest.mark.asyncio
async def test_worker_delivers_shared_history_and_stops_at_limit(db_uri):
    import asyncio

    from omnigent.runtime import session_stream
    from omnigent.server.forum.service import ForumService

    store = ForumStore(db_uri)
    forum_id = uuid.uuid4().hex
    s = state(2, maximum=3)
    message(s)
    store.create(forum_id, s)
    service = ForumService(store)
    received = []
    items = {}

    async def history(session_id):
        return items.get(session_id, [])

    async def send(session_id, body):
        if body["type"] == "message":
            received.append((session_id, body["data"]["content"][0]["text"]))
            items.setdefault(session_id, []).append(
                {
                    "id": uuid.uuid4().hex,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"Answer {len(received)}"}],
                }
            )
            session_stream.publish(session_id, {"type": "response.completed"})
        return {"queued": True}

    service.wake(forum_id, send, history)
    try:
        async with asyncio.timeout(10):
            while store.get(forum_id).status != "limit":
                await asyncio.sleep(0.02)
        assert [sid for sid, _ in received] == [
            s.participants[0].session_id,
            s.participants[1].session_id,
            s.participants[0].session_id,
        ]
        assert "Answer 1" in received[1][1]
        assert "Answer 2" in received[2][1]
        assert store.get(forum_id).completed == 3
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_worker_interrupts_inflight_turn_and_ignores_late_output(db_uri):
    import asyncio

    from omnigent.runtime import session_stream
    from omnigent.server.forum.service import ForumService

    store = ForumStore(db_uri)
    forum_id = uuid.uuid4().hex
    s = state(2)
    message(s)
    store.create(forum_id, s)
    service = ForumService(store)
    started, interrupted = asyncio.Event(), asyncio.Event()

    async def history(_session_id):
        return []

    async def send(session_id, body):
        if body["type"] == "message":
            started.set()
        elif body["type"] == "interrupt":
            interrupted.set()
            session_stream.publish(session_id, {"type": "response.completed"})
        return {"queued": True}

    service.wake(forum_id, send, history)
    try:
        await asyncio.wait_for(started.wait(), 5)
        store.mutate(forum_id, lambda s: s.stop())
        await asyncio.wait_for(interrupted.wait(), 5)
        await asyncio.sleep(0.05)
        assert store.get(forum_id).status == "stopped"
        assert store.get(forum_id).completed == 0
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_forum_api_guidance_stop_restart_and_title(app, db_uri, monkeypatch):
    import httpx

    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    conversations = SqlAlchemyConversationStore(db_uri)
    parent = conversations.create_conversation()
    store = ForumStore(db_uri)
    store.create(parent.id, state())
    monkeypatch.setattr(app.state.forum_service, "wake", lambda *_args: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.get(f"/v1/forums/{parent.id}")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "blank"
        result = await client.post(
            f"/v1/forums/{parent.id}/messages",
            json={"text": "Fix the complex parser", "display_name": " Hossein "},
        )
        assert result.status_code == 200, result.text
        assert result.json()["posts"][-1]["display_name"] == "Hossein"
        assert store.get(parent.id).posts[-1]["display_name"] == "Hossein"
        assert result.json()["title"] == "Fix the complex parser"
        assert result.json()["status"] == "running"
        result = await client.post(f"/v1/forums/{parent.id}/stop")
        assert result.status_code == 200, result.text
        result = await client.post(
            f"/v1/forums/{parent.id}/messages", json={"text": "Continue with tests"}
        )
        assert result.status_code == 200, result.text
        assert result.json()["completed"] == 0
        assert result.json()["status"] == "running"
        result = await client.post(
            f"/v1/forums/{parent.id}/messages", json={"text": "thank you all"}
        )
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "stopped"
        listing = await client.get("/v1/forums")
        assert listing.status_code == 200, listing.text
        assert any(row["id"] == parent.id for row in listing.json()["data"])
    await app.state.background_title_coordinator.shutdown()


@pytest.mark.asyncio
async def test_forum_sharing_matches_session_permissions(db_uri, tmp_path, monkeypatch):
    from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from tests.server.routes.test_sessions_sharing_mode import _client, _seed_owned_session

    app, forum_id = _seed_owned_session(db_uri, tmp_path)
    store = ForumStore(db_uri)
    store.create(forum_id, state())
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user, level in [("reader@test", LEVEL_READ), ("editor@test", LEVEL_EDIT)]:
        permissions.ensure_user(user)
        permissions.grant(user, forum_id, level)
    monkeypatch.setattr(app.state.forum_service, "wake", lambda *_args: None)
    async with _client(app, "stranger@test") as client:
        assert (await client.get(f"/v1/forums/{forum_id}")).status_code == 404
        assert (await client.get("/v1/forums")).json()["data"] == []
    async with _client(app, "reader@test") as client:
        assert (
            await client.post(f"/v1/forums/{forum_id}/rename", json={"title": "No"})
        ).status_code == 403
        assert (await client.get(f"/v1/forums/{forum_id}")).json()[
            "permission_level"
        ] == LEVEL_READ
        assert (
            await client.post(f"/v1/forums/{forum_id}/messages", json={"text": "go"})
        ).status_code == 403
        assert (await client.post(f"/v1/forums/{forum_id}/stop")).status_code == 403
    for user in ["owner@sharing.test", "editor@test"]:
        async with _client(app, user) as client:
            response = await client.post(
                f"/v1/forums/{forum_id}/messages", json={"text": "thank you all"}
            )
            assert response.status_code == 200, response.text
            assert response.json()["posts"][-1]["author"] == user
    assert len(store.get(forum_id).posts) == 2
    async with _client(app, "reader@test") as client:
        assert (
            await client.post(f"/v1/forums/{forum_id}/pause", json={"paused": True})
        ).status_code == 403
    store.mutate(forum_id, lambda s: setattr(s, "status", "running"))
    async with _client(app, "editor@test") as client:
        result = await client.post(f"/v1/forums/{forum_id}/pause", json={"paused": True})
        assert result.status_code == 200
        assert result.json()["status"] == "suspended"
        result = await client.post(f"/v1/forums/{forum_id}/pause", json={"paused": False})
        assert result.status_code == 200
        assert result.json()["status"] == "running"
    async with _client(app, "reader@test") as client:
        result = await client.get("/v1/forums?session_ids_only=true")
        assert result.status_code == 200
        assert result.json()["data"] == [
            forum_id,
            *(p.session_id for p in store.get(forum_id).participants),
        ]
    async with _client(app, "stranger@test") as client:
        assert (await client.get("/v1/forums?session_ids_only=true")).json()["data"] == []
    participant_id = store.get(forum_id).participants[0].session_id
    async with _client(app, "reader@test") as client:
        assert (
            await client.post(
                f"/v1/forums/{forum_id}/participants/{participant_id}/pause",
                json={"paused": True},
            )
        ).status_code == 403
    async with _client(app, "editor@test") as client:
        for paused in [True, False]:
            result = await client.post(
                f"/v1/forums/{forum_id}/participants/{participant_id}/pause",
                json={"paused": paused},
            )
            assert result.status_code == 200, result.text
            assert result.json()["participants"][0]["paused"] is paused
    async with _client(app, "editor@test") as client:
        assert (await client.post(f"/v1/forums/{forum_id}/delete")).status_code == 403
    async with _client(app, "owner@sharing.test") as client:
        result = await client.post(f"/v1/forums/{forum_id}/delete")
        assert result.status_code == 200, result.text
        assert store.get(forum_id) is None
        assert (await client.get("/v1/forums")).json()["data"] == []
    await app.state.background_title_coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["ready", "creation_failure", "unconfigured", "unknown_model", "offline"]
)
async def test_creation_uses_shared_workspace_duplicates_and_cleans_up(
    db_uri, monkeypatch, scenario
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import httpx
    from fastapi import FastAPI, HTTPException, Request

    import omnigent.server.routes.forums as routes
    from omnigent.server.schemas import SessionCreateRequest
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    conversations = SqlAlchemyConversationStore(db_uri)
    host = SimpleNamespace(configured_harnesses={"codex-native": scenario != "unconfigured"})
    monkeypatch.setattr(routes, "resolve_host_owner", lambda **_kw: host)
    monkeypatch.setattr(
        routes,
        "validate_session_agent",
        AsyncMock(
            return_value=SimpleNamespace(
                id="agent", bundle_location="bundle", operator_authored=False
            )
        ),
    )
    models = AsyncMock(return_value={"models": [{"id": "model"}]})
    monkeypatch.setattr(routes, "request_host_model_options", models)
    cache = Mock()
    cache.load.return_value.spec.executor.harness_kind = "codex-native"
    registry = Mock()
    if scenario == "offline":
        registry.get.return_value = None
    router, service = routes.create_forums_router(
        conversations,
        Mock(),
        auth_provider=None,
        permission_store=None,
        agent_cache=cache,
        host_store=Mock(),
        host_registry=registry,
    )
    app = FastAPI()
    created, deleted = [], []

    @app.post("/v1/sessions")
    async def create(request: Request):
        body = SessionCreateRequest.model_validate(await request.json())
        if scenario == "creation_failure" and len(created) == 2:
            raise HTTPException(503, "Host went offline")
        conv = conversations.create_conversation(workspace=body.workspace)
        created.append((conv.id, body))
        return conv

    @app.delete("/v1/sessions/{session_id}")
    async def delete(request: Request, session_id: str, delete_branch: bool = False):
        assert not delete_branch
        deleted.append(session_id)
        await conversations.delete_conversation(session_id)

    app.include_router(router, prefix="/v1")
    body = {
        "host_id": "host",
        "workspace": "/shared",
        "participants": [
            {
                "agent_id": "agent",
                "harness": "codex-native",
                "model": "model",
                "display_name": " Reviewer ",
            },
            {
                "agent_id": "agent",
                "harness": "codex-native",
                "model": "model",
                "display_name": " ",
            },
        ],
    }
    if scenario == "unknown_model":
        body["participants"][0]["model"] = "not-in-catalog"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post("/v1/forums", json=body)
    if scenario in {"unconfigured", "unknown_model", "offline"}:
        assert result.status_code == (409 if scenario == "offline" else 422), result.text
        assert not created
        assert service.store.ids() == []
    elif scenario == "creation_failure":
        assert result.status_code == 503
        assert deleted == [row[0] for row in reversed(created)]
        assert service.store.ids() == []
    else:
        assert result.status_code == 201, result.text
        forum = result.json()
        assert forum["status"] == "blank"
        assert forum["max_responses"] == 100
        assert [p["name"] for p in forum["participants"]] == [
            "1. Reviewer",
            "2. codex-native / model",
        ]
        assert service.store.get(forum["id"]).participants[0].name == "1. Reviewer"
        assert len({p["session_id"] for p in forum["participants"]}) == 2
        assert {row[1].workspace for row in created} == {"/shared"}
        assert all(row[1].parent_session_id == forum["id"] for row in created[1:])
        assert all(row[1].model_override == "model" for row in created)
        assert models.await_count == 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.post(f"/v1/forums/{forum['id']}/delete")
        assert result.status_code == 200, result.text
        assert deleted == [p["session_id"] for p in forum["participants"]] + [forum["id"]]
        assert service.store.get(forum["id"]) is None
    await service.shutdown()


@pytest.mark.asyncio
async def test_native_turn_waits_for_authoritative_completion(db_uri):
    import asyncio

    from omnigent.runtime import session_stream
    from omnigent.server.forum.service import ForumService

    service = ForumService(ForumStore(db_uri))
    session_id = uuid.uuid4().hex
    started = asyncio.Event()
    items = []

    async def history(_sid):
        return list(items)

    async def send(_sid, _body):
        started.set()
        session_stream.publish(session_id, {"type": "session.status", "status": "running"})
        session_stream.publish(session_id, {"type": "response.completed"})

    turn = asyncio.create_task(service._turn(session_id, "question", send, history, native=True))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.02)
        assert not turn.done()
        items.append(
            {
                "id": "answer",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Final answer"}],
            }
        )
        session_stream.publish(session_id, {"type": "forum.turn.finished", "status": "idle"})
        assert await asyncio.wait_for(turn, 2) == "Final answer"
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)


@pytest.mark.asyncio
async def test_losing_worker_lease_does_not_interrupt_replacement(db_uri, monkeypatch):
    import asyncio

    from omnigent.server.forum.service import ForumService

    store = ForumStore(db_uri)
    forum_id = uuid.uuid4().hex
    s = state(2)
    message(s)
    store.create(forum_id, s)
    service = ForumService(store)
    started = asyncio.Event()
    sent = []
    lost = False
    mutate = store.mutate

    def fenced(*args, **kwargs):
        return None if lost else mutate(*args, **kwargs)

    monkeypatch.setattr(store, "mutate", fenced)

    async def history(_session):
        return []

    async def send(_session, body):
        sent.append(body["type"])
        started.set()

    service.wake(forum_id, send, history)
    try:
        await asyncio.wait_for(started.wait(), 5)
        lost = True
        async with asyncio.timeout(5):
            while service._tasks:
                await asyncio.sleep(0.02)
        assert sent == ["message"]
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "restart"), [("CONTINUE", False), ("STOP", False), ("STOP", True)]
)
async def test_guidance_is_visible_while_intent_classification_runs(
    app, db_uri, monkeypatch, verdict, restart
):
    import asyncio

    import httpx

    from omnigent.server.background_session_titles import RunnerBackgroundTitleGenerator
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    conversations = SqlAlchemyConversationStore(db_uri)
    parent = conversations.create_conversation()
    store = ForumStore(db_uri)
    initial = state()
    message(initial)
    store.create(parent.id, initial)
    entered, release = asyncio.Event(), asyncio.Event()

    async def classify(_self, _request):
        entered.set()
        await release.wait()
        return verdict

    monkeypatch.setattr(RunnerBackgroundTitleGenerator, "__call__", classify)
    monkeypatch.setattr(app.state.forum_service, "wake", lambda *_args: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        post = asyncio.create_task(
            client.post(
                f"/v1/forums/{parent.id}/messages", json={"text": "Consider boundary cases"}
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert store.get(parent.id).posts[-1]["text"] == "Consider boundary cases"
            assert not post.done()
            if restart:
                store.mutate(parent.id, lambda s: (s.stop(), message(s, "New discussion run")))
            release.set()
            result = await asyncio.wait_for(post, 5)
            assert result.status_code == 200, result.text
            assert result.json()["status"] == (
                "stopped" if verdict == "STOP" and not restart else "running"
            )
        finally:
            release.set()
            post.cancel()
            await asyncio.gather(post, return_exceptions=True)


@pytest.mark.asyncio
async def test_forum_deletion_failure_stays_fenced_and_can_retry(app, db_uri, monkeypatch):
    import httpx

    from omnigent.server.routes import forums as routes
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    conversations = SqlAlchemyConversationStore(db_uri)
    parent = conversations.create_conversation()
    store = ForumStore(db_uri)
    initial = state()
    initial.status = "running"
    initial.active_index = 0
    initial.active_token = "in-flight"
    store.create(parent.id, initial)
    real_endpoint = routes._session_endpoint

    async def fail_delete(**_kwargs):
        from fastapi import HTTPException

        raise HTTPException(503, "Cleanup unavailable")

    monkeypatch.setattr(
        routes,
        "_session_endpoint",
        lambda request, path, method: (
            fail_delete if method == "DELETE" else real_endpoint(request, path, method)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(f"/v1/forums/{parent.id}/delete")
        assert result.status_code == 503
        saved = store.get(parent.id)
        assert saved.status == "deleting"
        assert saved.active_token is None
        assert saved.select(0) is None
        result = await client.post(f"/v1/forums/{parent.id}/messages", json={"text": "Restart"})
        assert result.status_code == 409
        monkeypatch.setattr(routes, "_session_endpoint", real_endpoint)
        result = await client.post(f"/v1/forums/{parent.id}/delete")
        assert result.status_code == 200, result.text
        assert store.get(parent.id) is None


def test_quarantined_agent_is_skipped_and_late_output_is_rejected():
    s = state()
    s.participants.append(Participant(uuid.uuid4().hex, "codex", "model", "Third"))
    s.status = "running"
    s.begin(0, "old-turn")
    assert s.set_participant_paused(0, True) == s.participants[0].session_id
    assert s.select(100) == 1
    assert not s.finish("old-turn", "late output", 100)
    assert s.completed == 0
    restored = ForumState.from_dict(s.to_dict())
    assert restored.participants[0].paused
    restored.next_index = 0
    assert restored.select(100) == 1
    restored.set_participant_paused(0, False)
    assert restored.select(100) == 0


def test_quarantine_waits_for_two_and_resume_preserves_explicit_stop():
    s = state()
    s.participants = s.participants[:2]
    s.status = "running"
    s.completed = 1
    s.set_participant_paused(0, True)
    assert s.status == "paused"
    assert s.select(100) is None
    s.set_participant_paused(0, False)
    assert s.select(100) == 0
    assert s.completed == 1
    s.stop()
    s.set_participant_paused(0, True)
    s.set_participant_paused(0, False)
    assert s.status == "stopped"
    assert s.select(100) is None


def test_forum_pause_holds_guidance_and_resumes_interrupted_turn():
    s = state()
    s.status = "running"
    s.completed = 7
    s.begin(1, "interrupted")
    assert s.pause() == s.participants[1].session_id
    assert not s.finish("interrupted", "late", 100)
    message(s, "Consider another approach")
    s.set_participant_paused(2, True)
    s.set_participant_paused(2, False)
    assert s.status == "suspended"
    assert s.select(100) is None
    restored = ForumState.from_dict(s.to_dict())
    assert restored.status == "suspended"
    restored.resume()
    assert restored.select(100) == 1
    assert restored.completed == 7
    restored.stop()
    restored.resume()
    assert restored.status == "stopped"


def vote_turn(s, vote="DONE", token=None):
    index = s.select(100)
    assert index is not None
    token = token or uuid.uuid4().hex
    s.begin(index, token)
    assert s.finish(token, f"My review.\nFORUM_VOTE: {token} {vote}", 100)
    return token


def test_consensus_stops_unanimously_and_restarts_with_fresh_allowance():
    s = state(maximum=3)
    message(s)
    for _ in range(3):
        vote_turn(s)
    assert s.status == "stopped"
    assert s.stop_reason == "consensus"
    assert s.completed == 3
    assert s.select(100) is None
    assert all(p["text"] == "My review." for p in s.posts[1:])
    assert s.posts[-1]["completion_vote"] == "DONE"
    message(s, "Please also check edge cases")
    assert s.completed == 0
    assert s.stop_reason is None
    assert s.completion_votes == []
    assert s.select(100) == 0


def test_consensus_continue_requires_every_agent_to_agree_again():
    s = state()
    message(s)
    vote_turn(s)
    vote_turn(s, "CONTINUE")
    assert not s.completion_votes
    vote_turn(s)
    vote_turn(s)
    assert s.status == "running"
    vote_turn(s)
    assert s.stop_reason == "consensus"


def test_consensus_guidance_fences_old_context_vote():
    s = state(count=2)
    message(s)
    vote_turn(s)
    s.begin(1, "stale-context")
    message(s, "New requirement")
    s.finish("stale-context", "Review\nFORUM_VOTE: stale-context DONE", 100)
    assert s.status == "running"
    assert not s.completion_votes
    vote_turn(s)
    assert s.status == "running"
    vote_turn(s)
    assert s.stop_reason == "consensus"


def test_consensus_quarantine_excludes_only_manual_pauses_and_resets_votes():
    s = state()
    message(s)
    vote_turn(s)
    s.set_participant_paused(2, True)
    assert not s.completion_votes
    vote_turn(s)
    vote_turn(s)
    assert s.stop_reason == "consensus"
    s = state()
    message(s)
    s.participants[2].retry_at = 200
    for _ in range(4):
        vote_turn(s)
    assert s.status == "running"
    s.set_participant_paused(0, True)
    s.set_participant_paused(1, True)
    assert s.select(100) is None
    assert not s.completion_votes


def test_consensus_persistence_errors_interrupts_and_duplicate_completions():
    s = state()
    message(s)
    token = vote_turn(s)
    s = ForumState.from_dict(s.to_dict())
    assert len(s.completion_votes) == 1
    assert not s.finish(token, f"Again\nFORUM_VOTE: {token} DONE", 100)
    s.begin(1, "failed")
    s.finish("failed", "", 100, "unavailable")
    assert not s.completion_votes
    s.participants[1].retry_at = 0
    vote_turn(s)
    s.pause()
    assert not s.completion_votes
    s.resume()
    vote_turn(s)
    s.stop()
    assert not s.completion_votes


@pytest.mark.parametrize(
    "text",
    [
        "done",
        "Review\nFORUM_VOTE: wrong DONE",
        "Review\n> FORUM_VOTE: token DONE",
        "Review\n```\nFORUM_VOTE: token DONE",
        "Review\nFORUM_VOTE: token DONE\nMore text",
        "Review\nFORUM_VOTE: token DONE\nFORUM_VOTE: token CONTINUE",
        "FORUM_VOTE: token DONE",
    ],
)
def test_invalid_completion_vote_never_grants_consent(text):
    from omnigent.server.forum.state import parse_completion_vote

    assert parse_completion_vote(text, "token") == (text, "CONTINUE")


def test_consensus_protocol_is_in_turn_prompt():
    from omnigent.runtime.prompt import forum_turn_prompt

    prompt = forum_turn_prompt("Reviewer", [], turn_token="unique-turn")
    assert "FORUM_VOTE: unique-turn DONE" in prompt
    assert "FORUM_VOTE: unique-turn CONTINUE" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_worker_stops_by_consensus_before_response_limit(db_uri, native):
    import asyncio

    from omnigent.runtime import session_stream
    from omnigent.server.forum.service import ForumService

    store = ForumStore(db_uri)
    forum_id = uuid.uuid4().hex
    s = state(2, maximum=100)
    if native:
        for participant in s.participants:
            participant.harness = "codex-native"
    message(s)
    store.create(forum_id, s)
    service = ForumService(store)
    items = {}
    prompts = []

    async def history(session_id):
        return items.get(session_id, [])

    async def send(session_id, body):
        if body["type"] == "message":
            prompt = body["data"]["content"][0]["text"]
            prompts.append(prompt)
            token = store.get(forum_id).active_token
            assert f"FORUM_VOTE: {token} DONE" in prompt
            items.setdefault(session_id, []).append(
                {
                    "id": uuid.uuid4().hex,
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": f"Reviewed.\nFORUM_VOTE: {token} DONE"}
                    ],
                }
            )
            event = (
                {"type": "forum.turn.finished", "status": "idle"}
                if native
                else {"type": "response.completed"}
            )
            if native:
                session_stream.publish(session_id, {"type": "session.status", "status": "running"})
            session_stream.publish(session_id, event)
        return {"queued": True}

    service.wake(forum_id, send, history)
    try:
        async with asyncio.timeout(10):
            while store.get(forum_id).status != "stopped":
                await asyncio.sleep(0.02)
        assert store.get(forum_id).stop_reason == "consensus"
        assert store.get(forum_id).completed == 2
        assert len(prompts) == 2
        assert "Reviewed." in prompts[1]
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_forum_rename_persists_and_survives_first_message(app, db_uri, monkeypatch):
    import httpx

    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    conversations = SqlAlchemyConversationStore(db_uri)
    parent = conversations.create_conversation()
    store = ForumStore(db_uri)
    store.create(parent.id, state())
    monkeypatch.setattr(app.state.forum_service, "wake", lambda *_args: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/v1/forums/{parent.id}"
        assert (await client.post(path + "/rename", json={"title": " "})).status_code == 422
        result = await client.post(path + "/rename", json={"title": " My review "})
        assert result.status_code == 200
        assert result.json()["title"] == "My review"
        result = await client.post(path + "/messages", json={"text": "Review this code"})
        assert result.json()["title"] == "My review"
        assert (await client.get(path)).json()["title"] == "My review"
        assert conversations.get_conversation(parent.id).title == "My review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/forums"),
        ("POST", "/v1/forums"),
        ("GET", "/v1/forums/missing"),
        ("POST", "/v1/forums/missing/messages"),
        ("POST", "/v1/forums/missing/stop"),
        ("POST", "/v1/forums/missing/pause"),
        ("POST", "/v1/forums/missing/rename"),
        ("POST", "/v1/forums/missing/delete"),
        ("POST", "/v1/forums/missing/participants/missing/pause"),
    ],
)
async def test_disabled_forums_reject_all_endpoints(db_uri, method, path):
    import httpx
    from fastapi import FastAPI

    from omnigent.server.feature_flags import FeatureFlags
    from omnigent.server.routes.forums import create_forums_router
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    disabled = FastAPI()
    router, service = create_forums_router(
        SqlAlchemyConversationStore(db_uri),
        None,
        auth_provider=None,
        permission_store=None,
        agent_cache=None,
        host_store=None,
        host_registry=None,
        feature_flags=FeatureFlags(),
    )
    disabled.include_router(router, prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=disabled), base_url="http://test"
    ) as client:
        result = await client.request(method, path)
        assert result.status_code == 404, result.text
    assert not service._tasks
