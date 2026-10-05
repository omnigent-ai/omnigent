import asyncio
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
from omnigent_slack.models import ThreadKey
from omnigent_slack.omnigent import ClientAuth, OmnigentClient, OmnigentError
from omnigent_slack.resume import parse_resume, recap
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.store import SQLiteStore

SERVER = "https://omni.example"
KEY = ThreadKey("T1", "D1", "1.0")
OTHER = ThreadKey("T1", "D1", "2.0")


@pytest.mark.parametrize(
    "target",
    ["conv_abc", "https://omni.example/c/conv_abc", "<https://omni.example/c/conv_abc|session>"],
)
def test_parse(target):
    assert parse_resume(f"resume {target} --force", SERVER + "/c/ID") == ("conv_abc", True)


@pytest.mark.parametrize(
    "text",
    [
        "resume",
        "resume ../secret",
        "resume a --other",
        "resume a extra --force",
        "resume https://wrong.example/c/a",
        "resume https://omni.example/v1/sessions/a",
        "resume https://omni.example/c/a?o=other",
    ],
)
def test_invalid(text):
    with pytest.raises(OmnigentError):
        parse_resume(text, SERVER + "/c/ID")


def test_workspace_url():
    assert parse_resume(
        "resume https://workspace.example/omnigent/c/conv_a?o=1",
        "https://workspace.example/omnigent/c/ID?o=1",
    ) == ("conv_a", False)


def test_recap():
    items = [
        {
            "type": "message",
            "data": {"role": "user", "content": [{"text": "<@everyone>" + "x" * 600}]},
        }
    ] * 10
    result = recap(items)
    assert result.count("User:") == 4
    assert "<@" not in result
    assert len(result) < 2200


@pytest.fixture
async def store(tmp_path):
    value = SQLiteStore(tmp_path / "slack.db")
    await value.initialize()
    return value


async def bind(store, key=KEY, session="conv_a", **kwargs):
    return await store.bind_session(
        key,
        session,
        "title",
        owner_user_id="U1",
        host_id="h1",
        workspace="/repo",
        host_type="external",
        **kwargs,
    )


async def test_binding_conflict_force_idempotency(store):
    assert (await bind(store))[0] == "bound"
    assert await bind(store) == ("same", None)
    assert await bind(store, OTHER) == ("conflict", KEY)
    assert (await bind(store, OTHER, force=True))[0] == "bound"
    assert await store.get_session(KEY) is None
    assert (await store.get_session(OTHER)).workspace == "/repo"
    assert (await bind(store, OTHER, "conv_b", force=True))[0] == "occupied"


async def test_binding_inflight_and_other_owner(store):
    await bind(store)
    await store.set_turn_inflight(KEY, True)
    assert (await bind(store, OTHER, force=True))[0] == "conflict"
    assert (
        await store.bind_session(
            OTHER,
            "conv_a",
            "title",
            owner_user_id="U2",
            host_id=None,
            workspace=None,
            host_type="external",
            force=True,
        )
    )[0] == "unavailable"


async def test_racing_bindings(store):
    results = await asyncio.gather(bind(store), bind(store, OTHER))
    assert sorted(r[0] for r in results) == ["bound", "conflict"]


async def test_migration_preserves_duplicates_and_unique(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE thread_sessions (team_id TEXT, channel_id TEXT, "
            "thread_ts TEXT, omnigent_session_id TEXT, title TEXT, "
            "owner_user_id TEXT, host_id TEXT, workspace TEXT, created_at "
            "INTEGER, updated_at INTEGER, PRIMARY KEY(team_id, channel_id, "
            "thread_ts))"
        )
        db.executemany(
            "INSERT INTO thread_sessions VALUES ('T1', 'D1', ?, 'conv_a', "
            "'title', 'U1', NULL, NULL, 0, ?)",
            [("1.0", 1), ("2.0", 2)],
        )
    value = SQLiteStore(path)
    await value.initialize()
    await value.initialize()
    assert await value.get_session(KEY) is None
    assert await value.get_session(OTHER) is not None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT thread_ts FROM thread_sessions_binding_backup").fetchall() == [
            ("1.0",)
        ]
    with pytest.raises(sqlite3.IntegrityError):
        await value.upsert_session(KEY, "conv_a", "duplicate")


@pytest.fixture
async def environment(store):
    snapshot = {
        "id": "conv_a",
        "permission_level": 3,
        "status": "idle",
        "runner_online": True,
        "host_id": "h1",
        "workspace": "/web",
        "items": [{"type": "message", "role": "assistant", "content": [{"text": "Web answer"}]}],
    }
    state = {"snapshot": snapshot, "status": 200, "recover_status": 200, "requests": []}

    def transport(request):
        state["requests"].append(request)
        assert request.headers.get("authorization") == "Bearer delegated"
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"data": list(reversed(snapshot["items"]))})
        if request.url.path.endswith("/stream"):
            from fakes import sse_delta, sse_status

            return httpx.Response(
                200,
                text=sse_status("running") + sse_delta("Continued from web") + sse_status("idle"),
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == "/v1/hosts":
            return httpx.Response(
                200, json={"hosts": [{"id": "h1", "sandbox_provider": state.get("provider")}]}
            )
        if request.method == "POST":
            if state.get("timeout"):
                raise httpx.ReadTimeout("slow", request=request)
            snapshot["runner_online"] = True
            return httpx.Response(
                state["recover_status"], json={"error": {"code": "runner_unavailable"}}
            )
        return httpx.Response(state["status"], json=snapshot)

    omni = OmnigentClient(SERVER, auth=ClientAuth("delegated", AsyncMock()))
    await omni._client.aclose()
    omni._client = httpx.AsyncClient(base_url=SERVER, transport=httpx.MockTransport(transport))
    pool = AsyncMock()
    pool.get.return_value = omni
    slack = AsyncMock()
    slack.chat_postMessage.return_value = {"ts": "1.0"}
    slack.chat_getPermalink.return_value = {"permalink": "https://slack.test/thread"}
    service = SlackOmnigentService(
        store=store, pool=pool, setup=AsyncMock(), server_url=SERVER, bot_user_id="BOT"
    )
    yield service, slack, state, omni
    await service.shutdown()
    await omni.aclose()


async def resume(environment, key=KEY, text="resume conv_a"):
    service, slack, _, _ = environment
    await service._route_turn(
        key=key, event={"user": "U1"}, text=text, client=slack, in_channel=not key.is_dm
    )
    return "\n".join(c.kwargs["text"] for c in slack.chat_postMessage.call_args_list)


@pytest.mark.parametrize(
    "harness", ["claude-native", "codex-native", "cursor-native", "openai-agents"]
)
async def test_resume_and_repeat(environment, store, harness):
    environment[2]["snapshot"]["harness"] = harness
    text = await resume(environment)
    assert "Web answer" in text and "Open in Omnigent" in text and "Ready" in text
    record = await store.get_session(KEY)
    assert record.session_id == "conv_a" and record.workspace == "/web"
    assert "already connected" in await resume(environment)
    assert all(r.method == "GET" for r in environment[2]["requests"])


@pytest.mark.parametrize("status", [403, 404])
async def test_inaccessible(environment, store, status):
    environment[2]["status"] = status
    assert "Session unavailable" in await resume(environment)
    assert await store.get_session(KEY) is None
    assert all(r.url.path != "/v1/hosts" for r in environment[2]["requests"])


@pytest.mark.parametrize(
    "field,value,expected",
    [("permission_level", 1, "edit access"), ("archived", True, "Unarchive")],
)
async def test_rejected_snapshot(environment, store, field, value, expected):
    environment[2]["snapshot"][field] = value
    assert expected in await resume(environment)
    assert await store.get_session(KEY) is None


async def test_missing_identity(environment, store):
    environment[3]._auth = None
    assert "Sign in" in await resume(environment)
    assert not environment[2]["requests"]
    assert await store.get_session(KEY) is None


async def test_conflict_and_force(environment, store):
    await resume(environment)
    assert "Already connected" in await resume(environment, OTHER)
    assert (await store.get_session(KEY)).session_id == "conv_a"
    assert "Ready" in await resume(environment, OTHER, "resume conv_a --force")
    assert await store.get_session(KEY) is None


@pytest.mark.parametrize("status", ["running", "waiting"])
async def test_busy(environment, store, status):
    environment[2]["snapshot"].update(status=status, runner_online=False)
    assert "busy" in await resume(environment)
    assert (await store.get_session(KEY)).session_id == "conv_a"
    assert all(r.method == "GET" for r in environment[2]["requests"])


async def test_offline_managed(environment, store):
    environment[2]["snapshot"]["runner_online"] = False
    environment[2]["snapshot"]["host_type"] = "managed"
    assert "Reconnecting" in await resume(environment)
    assert (await store.get_session(KEY)).host_type == "managed"
    posts = [r for r in environment[2]["requests"] if r.method == "POST"]
    assert len(posts) == 1 and posts[0].read() == b'{"type":"retry_session"}'


async def test_unavailable_host(environment):
    environment[2]["snapshot"]["runner_online"] = False
    environment[2]["recover_status"] = 503
    assert "host may be offline" in await resume(environment)


async def test_readiness_timeout(environment):
    environment[2]["snapshot"]["runner_online"] = False
    environment[3].recover_session = AsyncMock(side_effect=TimeoutError)
    assert "timed out" in await resume(environment)


async def test_invalid_routing(environment):
    assert "Invalid session ID" in await resume(environment, text="resume ../oops")
    assert not environment[2]["requests"]


@pytest.mark.parametrize("mention", [False, True])
async def test_entry_points(environment, store, mention):
    service, slack, _, _ = environment
    event = {
        "channel": "C1" if mention else "D1",
        "ts": "1.0",
        "user": "U1",
        "text": "<@BOT> resume https://omni.example/c/conv_a",
    }
    handler = service.handle_app_mention if mention else service.handle_message
    await handler(body={"team_id": "T1"}, event=event, client=slack)
    assert (
        await store.get_session(ThreadKey("T1", event["channel"], "1.0"))
    ).session_id == "conv_a"


async def test_slash_root_and_bot_parent(environment, store):
    service, slack, _, _ = environment
    await service.handle_resume_command(
        {"team_id": "T1", "channel_id": "C1", "user_id": "U1", "text": "resume conv_a"}, slack
    )
    assert "thread_ts" not in slack.chat_postMessage.call_args_list[0].kwargs
    key = ThreadKey("T1", "C1", "1.0")
    service._spawn_turn = lambda _turn: None
    await service._route_turn(
        key=key,
        event={"user": "U1", "parent_user_id": "BOT"},
        text="continue",
        client=slack,
        in_channel=True,
    )
    assert key in service._active_threads


async def test_web_session_continues_via_slack_http_boundary(environment, store):
    import json

    from fakes import RecordingSlackClient

    service, _, state, _ = environment
    slack = RecordingSlackClient()
    await service.handle_message(
        body={"team_id": "T1", "event_id": "resume"},
        event={"channel": "D1", "ts": "1.0", "user": "U1", "text": "resume conv_a"},
        client=slack,
    )
    assert (await store.get_session(KEY)).session_id == "conv_a"
    assert not any(request.method == "POST" for request in state["requests"])
    await service.handle_message(
        body={"team_id": "T1", "event_id": "continue"},
        event={
            "channel": "D1",
            "ts": "2.0",
            "thread_ts": "1.0",
            "user": "U1",
            "text": "Continue my web work",
        },
        client=slack,
    )
    await asyncio.wait_for(asyncio.gather(*service._turn_tasks), timeout=5)
    posts = [request for request in state["requests"] if request.method == "POST"]
    assert len(posts) == 1
    assert posts[0].url.path == "/v1/sessions/conv_a/events"
    assert json.loads(posts[0].content)["data"]["content"][0]["text"] == "Continue my web work"
    assert "Continued from web" in slack.streamed_text
    assert not (await store.get_session(KEY)).turn_inflight


async def test_recovery_wait_timeout_uses_real_readiness_loop(environment):
    omni = environment[3]
    omni._runner_launch_timeout_seconds = 0.01
    omni.resume_snapshot = AsyncMock(return_value={"runner_online": False})
    with pytest.raises(TimeoutError):
        await omni.recover_session("conv_a")
    omni.resume_snapshot.assert_awaited()


async def test_force_during_active_old_thread(environment, store):
    await resume(environment)
    environment[0]._active_threads.add(KEY)
    assert "existing Slack thread is busy" in await resume(
        environment, OTHER, "resume conv_a --force"
    )
    assert await store.get_session(OTHER) is None


async def test_recovery_refreshes_assigned_host(environment, store):
    state = environment[2]
    state["snapshot"].update(runner_online=False, host_id=None, workspace=None)
    recovered = {
        **state["snapshot"],
        "runner_online": True,
        "host_id": "new_host",
        "workspace": "/new_repo",
        "host_type": "managed",
    }
    environment[3].recover_session = AsyncMock(return_value=recovered)
    assert "Ready" in await resume(environment)
    record = await store.get_session(KEY)
    assert (record.host_id, record.workspace, record.host_type) == (
        "new_host",
        "/new_repo",
        "managed",
    )
