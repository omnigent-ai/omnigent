"""Vertical integration tests: omnigent-slack ↔ Omnigent HTTP server.

These drive the REAL ``OmnigentClient`` (real ``httpx``) against a fake Omnigent
server (:class:`FakeOmnigentServer`, a ``respx`` router that owns the API
contract), and assert BOTH sides of the seam:

  1. the bot issued spec-correct HTTP requests (method, path, bearer, body); and
  2. driven by each server response/status, the bot called the right Slack
     method with the right content (a login modal, a streamed answer, a re-login
     DM).

Slack and Bolt are mocked: tests call ``service.handle_*`` / ``setup._handle_*``
directly (as the unit tests do) with a :class:`RecordingSlackClient`. This keeps
the focus on the omnigent-slack ↔ server logic rather than Slack transport.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import respx
from cryptography.fernet import Fernet
from fakes import FakeOmnigentServer, RecordingSlackClient, sse_delta, sse_status
from omnigent_slack.auth_manager import AuthManager
from omnigent_slack.models import UserConfig
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.setup import SetupFlow
from omnigent_slack.store import SQLiteStore
from omnigent_slack.tokens import EncryptedTokenStore

_SERVER = "http://omnigent.test"


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    return store


async def _token_store(tmp_path: Path) -> EncryptedTokenStore:
    token_store = EncryptedTokenStore(tmp_path / "tok.sqlite3", Fernet.generate_key().decode())
    await token_store.initialize()
    return token_store


async def _configure_user(
    store: SQLiteStore, team_id: str, user_id: str, *, agent_id: str = "ag_1"
) -> None:
    await store.upsert_user_config(
        team_id,
        user_id,
        UserConfig(
            agent_id=agent_id,
            agent_name="debby",
            workspace="/home/bot/work",
            host_id="h1",
        ),
    )


# Generous ceiling for the waits below. These are event-driven (they await the
# actual turn task), so a healthy run returns near-instantly and never spends
# this budget; it only bounds a genuine hang. Kept well above any real turn so a
# loaded CI runner doesn't spuriously time out (see the isolation note in the
# module docstring's history).
_WAIT_TIMEOUT_S = 10.0


async def _wait_for_turns(service: SlackOmnigentService, timeout: float = _WAIT_TIMEOUT_S) -> None:
    """Wait until the service's spawned turn tasks have finished.

    A turn runs as a background task; ``shutdown`` would CANCEL it, so tests that
    assert on a turn's outcome must let it complete first. Event-driven: awaits
    the tracked tasks directly (no polling), so it returns the instant the turn
    ends and the ``timeout`` only bounds a real hang. Turn tasks never propagate
    (``_run_turn_tracked`` swallows exceptions), so gather won't raise.
    """
    tasks = list(service._turn_tasks)
    if not tasks:
        return
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)


async def _wait_for_stream_stop(
    client: RecordingSlackClient,
    service: SlackOmnigentService,
    timeout: float = _WAIT_TIMEOUT_S,
) -> None:
    """Wait for the turn to finish, then assert it delivered a stream.

    The stream is stopped from inside the turn task, so awaiting the task
    (event-driven) is a stronger, non-polling signal than watching for the
    side effect."""
    await _wait_for_turns(service, timeout)
    assert client.streams and client.stream.stopped, "turn finished without stopping a stream"


# ── Scenario 1: /omnigent against an auth-required server → login link ─────────


@respx.mock
async def test_omnigent_command_auth_wall_shows_login_link(tmp_path: Path) -> None:
    """``/omnigent`` on an auth-enabled server: the bot probes the server, hits the
    auth wall on the agent listing, starts the device grant, and shows the
    verification link in the setup modal — no DM."""
    server = FakeOmnigentServer(_SERVER)
    server.auth_required = True
    server.install(respx.mock)

    store = await _store(tmp_path)
    token_store = await _token_store(tmp_path)
    pool = OmnigentClientPool()
    auth = AuthManager(token_store)
    pool.set_auth_resolver(auth.resolve_auth)
    setup = SetupFlow(store=store, pool=pool, server_url=_SERVER, auth_manager=auth)
    client = RecordingSlackClient()

    try:
        await setup._handle_config_command(
            ack=_noop_ack,
            command={"team_id": "T1", "user_id": "U1", "trigger_id": "tg1", "text": ""},
            client=client,
        )
    finally:
        await auth.shutdown()
        await pool.aclose_all()

    # Server side: the bot probed /health and the auth-gated /v1/agents, then
    # started the device grant when that 401'd.
    assert "/health" in server.paths("GET")
    server.assert_request("GET", "/v1/agents")
    server.assert_request("POST", "/oauth/device/authorize")

    # Slack side: a modal opened, then updated to the login-waiting screen
    # carrying the verification code — and no DM was sent.
    assert client.opened_views, "expected the connecting modal to open"
    assert server.user_code in client.last_view_text()
    assert client.posts == []


# ── Scenario 2: /omnigent happy path → agent/host picker ───────────────────────


@respx.mock
async def test_omnigent_command_happy_path_shows_picker(tmp_path: Path) -> None:
    """``/omnigent`` with a valid token: validate() succeeds and the modal advances
    to the agent/host/workspace picker built from the server's data."""
    server = FakeOmnigentServer(_SERVER)
    server.agents = [{"id": "ag_1", "name": "debby"}]
    server.hosts = [{"host_id": "h1", "name": "Host One", "status": "online"}]
    server.install(respx.mock)

    store = await _store(tmp_path)
    token_store = await _token_store(tmp_path)
    await token_store.put("T1", "U1", _SERVER, access_token="tok-abc", refresh_token="ref-abc")
    pool = OmnigentClientPool()
    auth = AuthManager(token_store)
    pool.set_auth_resolver(auth.resolve_auth)
    setup = SetupFlow(store=store, pool=pool, server_url=_SERVER, auth_manager=auth)
    client = RecordingSlackClient()

    try:
        await setup._begin_setup(client, team_id="T1", user_id="U1", view_id="V1")
    finally:
        await auth.shutdown()
        await pool.aclose_all()

    # Server side: the listing endpoints were hit AND carried the delegated bearer.
    server.assert_bearer("GET", "/v1/agents", "tok-abc")
    server.assert_bearer("GET", "/v1/hosts", "tok-abc")

    # Slack side: the modal advanced to the select screen (not a login/failure one).
    assert client.updated_views, "expected the modal to advance"
    view = client.updated_views[-1]["view"]
    assert view.get("callback_id") == "omnigent_setup_select"
    # No login link and no DM — the token was accepted.
    assert server.user_code not in client.last_view_text()
    assert client.posts == []


# ── Scenario 3: app_mention → full turn (create/launch/submit/stream) ──────────


@respx.mock
async def test_app_mention_runs_full_turn_and_streams_answer(tmp_path: Path) -> None:
    """An @-mention on a new thread drives the whole turn HTTP contract in order
    and streams the answer back into Slack."""
    server = FakeOmnigentServer(_SERVER)
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> review this"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_stream_stop(client, service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # Server side: the ordered turn contract.
    server.assert_request("POST", "/v1/sessions", json_contains={"agent_id": "ag_1"})
    launch = server.assert_request("POST", "/v1/hosts/h1/runners")
    assert launch[3] == {"session_id": "conv_1", "workspace": "/home/bot/work"}
    assert f"/v1/runners/{server.runner_id}/status" in server.paths("GET")
    server.assert_request("POST", f"/v1/sessions/{server.session_id}/events")
    assert f"/v1/sessions/{server.session_id}/stream" in server.paths("GET")

    # Slack side: the SSE answer streamed into the reply.
    assert "Here is the answer." in client.streamed_text


# ── Scenario 4: auth wall mid-turn → re-login DM ───────────────────────────────


@respx.mock
async def test_auth_wall_mid_turn_prompts_relogin(tmp_path: Path) -> None:
    """When the session already exists and the server returns an auth wall on the
    stream, the bot reacts by prompting the user to re-login rather than posting a
    generic failure."""
    server = FakeOmnigentServer(_SERVER)
    server.auth_required = True  # /health ok, but the stream 401s
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    # Pre-map the thread to an existing session so the turn skips create/launch
    # and goes straight to streaming — where the auth wall is hit.
    from omnigent_slack.models import ThreadKey

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(
        key, "conv_1", "Slack: t", owner_user_id="U1", host_id="h1", workspace="/home/bot/work"
    )
    pool = OmnigentClientPool()
    setup = _RecordingSetup()
    service = SlackOmnigentService(store=store, pool=pool, setup=setup, server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> continue"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_turns(service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # Server side: the existing session was reused (no create/launch) and the
    # stream was attempted against the auth-walled server.
    assert f"/v1/sessions/{server.session_id}/stream" in server.paths("GET")
    assert "/v1/sessions" not in server.paths("POST")
    # Slack side: the bot prompted a re-login rather than a generic failure.
    assert setup.relogin_prompted, "expected prompt_relogin to be called"


# ── Scenario 5: runner unavailable → launch + retry, then stream ───────────────


@respx.mock
async def test_runner_unavailable_triggers_launch_and_retry(tmp_path: Path) -> None:
    """A pre-existing session whose bound runner is gone: the first submit returns
    503 runner_unavailable, so the client launches a fresh runner and retries the
    turn — the answer still streams."""
    server = FakeOmnigentServer(_SERVER)
    server.first_submit_runner_unavailable = True
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    from omnigent_slack.models import ThreadKey

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(
        key, "conv_1", "t", owner_user_id="U1", host_id="h1", workspace="/home/bot/work"
    )
    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> retry"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_stream_stop(client, service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # Server side: the 503 on the first submit drove a runner launch, then a retry.
    assert server.paths("POST").count(f"/v1/sessions/{server.session_id}/events") == 2
    assert "/v1/hosts/h1/runners" in server.paths("POST")
    # Slack side: the answer streamed despite the mid-turn runner miss.
    assert "Here is the answer." in client.streamed_text


# ── Scenario 6: host unavailable on launch → host guidance, no orphan ──────────


@respx.mock
async def test_host_unavailable_on_launch_shows_guidance(tmp_path: Path) -> None:
    """A new session is created, but launching its runner 409s (host offline): the
    bot surfaces the host-unavailable guidance rather than a generic failure."""
    server = FakeOmnigentServer(_SERVER)
    server.launch_status = 409
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_turns(service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # Server side: create happened, launch was attempted (and 409'd), no stream.
    server.assert_request("POST", "/v1/sessions")
    server.assert_request("POST", "/v1/hosts/h1/runners")
    assert not any("/stream" in p for p in server.paths("GET"))
    # Slack side: the host-unavailable guidance surfaced (mentions bringing a host
    # online), not a stream.
    posted = " ".join(p.get("text", "") for p in client.posts) + (
        client.stream.stop_text or "" if client.streams else ""
    )
    assert "host" in posted.lower()


# ── Scenario 7: harness not configured (412) → curated message surfaces ────────


@respx.mock
async def test_harness_not_configured_surfaces_curated_message(tmp_path: Path) -> None:
    """A 412 harness_not_configured carries curated, actionable guidance the bot is
    allowed to surface verbatim (unlike a raw server body)."""
    server = FakeOmnigentServer(_SERVER)
    server.harness_not_configured_message = "Run `omnigent setup` on host h1 to install claude."
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    from omnigent_slack.models import ThreadKey

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(
        key, "conv_1", "t", owner_user_id="U1", host_id="h1", workspace="/home/bot/work"
    )
    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_turns(service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # Slack side: the curated 412 message surfaced (not the generic failure).
    surfaced = " ".join(p.get("text", "") for p in client.posts)
    if client.streams:
        surfaced += client.stream.stop_text or ""
    assert "omnigent setup" in surfaced.lower()


# ── Scenario 8: mid-stream drop → reconnect + delta de-dup ─────────────────────


@respx.mock
async def test_mid_stream_drop_reconnects_without_double_render(tmp_path: Path) -> None:
    """The proxy severs the stream mid-answer; the client reconnects WITHOUT
    re-submitting and de-dups the server's cumulative replay so text isn't doubled."""
    server = FakeOmnigentServer(_SERVER)
    server.stream_legs = [
        # First leg: a running edge + partial answer, then a mid-tail drop.
        (sse_status("running", "r1") + sse_delta("Running tests", "m1") + "<DROP>"),
        # Reconnect leg: server replays the cumulative text, then finishes.
        (
            sse_delta("Running tests", "m1")
            + sse_delta(" all pass.", "m1")
            + sse_status("idle", "r1")
        ),
    ]
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    from omnigent_slack.models import ThreadKey

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(
        key, "conv_1", "t", owner_user_id="U1", host_id="h1", workspace="/home/bot/work"
    )
    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> run"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_stream_stop(client, service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # Server side: two stream connections, message submitted exactly once.
    assert server.paths("GET").count(f"/v1/sessions/{server.session_id}/stream") == 2
    assert server.paths("POST").count(f"/v1/sessions/{server.session_id}/events") == 1
    # Slack side: the replayed text is de-duped — "Running tests all pass.", not
    # "Running testsRunning tests all pass.".
    assert client.streamed_text == "Running tests all pass."


# ── Scenario 9: no delta → recover the committed answer from the items probe ──


@respx.mock
async def test_no_delta_turn_recovers_committed_answer(tmp_path: Path) -> None:
    """A turn that streams no answer text (only status edges) must fall back to the
    server's newest assistant message rather than posting the empty-turn notice."""
    server = FakeOmnigentServer(_SERVER)
    # A stream that produces a delta but no text, ending on idle — the reply has
    # nothing, so the fallback fetches the committed item.
    server.sse_body = sse_status("running", "r1") + sse_status("idle", "r1")
    server.latest_items = [
        {
            "id": "item_new",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Recovered answer."}],
        }
    ]
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_user(store, "T1", "U1")
    from omnigent_slack.models import ThreadKey

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(
        key, "conv_1", "t", owner_user_id="U1", host_id="h1", workspace="/home/bot/work"
    )
    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_stream_stop(client, service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # Server side: the items endpoint was probed for the fallback.
    assert f"/v1/sessions/{server.session_id}/items" in server.paths("GET")
    # Slack side: the recovered committed answer was delivered.
    assert "Recovered answer." in client.streamed_text


# ── minimal setup/ack doubles for the service path ────────────────────────────


async def _noop_ack(**kwargs: object) -> None:
    return None


class _NoopSetup:
    """SetupFlow stand-in for turns where the user is already configured."""

    async def prompt_unconfigured(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("configured user should not be prompted to set up")

    async def prompt_relogin(self, *args: object, **kwargs: object) -> bool:
        return True


class _RecordingSetup(_NoopSetup):
    def __init__(self) -> None:
        self.relogin_prompted: list[dict[str, object]] = []

    async def prompt_relogin(self, client: object, user_id: str, **kwargs: object) -> bool:
        self.relogin_prompted.append({"user_id": user_id, **kwargs})
        return True
