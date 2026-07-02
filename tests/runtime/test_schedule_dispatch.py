"""Unit tests for the scheduler fire callback (B2).

``build_inprocess_fire`` turns a fired loop schedule into a user message POSTed
in-process to ``/v1/sessions/{id}/events``. These tests drive the callback
against a tiny capture app via the real :class:`httpx.ASGITransport`, asserting
the request shape, attribution header, the monitor/empty no-ops, and that an
offline runner (a >=400 status) is swallowed so the cron loop survives.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities.schedule import Schedule
from omnigent.runtime.schedule_dispatch import build_inprocess_fire


def _loop(**over: Any) -> Schedule:
    """A ready-to-fire loop schedule, overridable per test."""
    base: dict[str, Any] = {
        "id": "sch_1",
        "conversation_id": "conv_1",
        "name": "weekly",
        "kind": "loop",
        "prompt": "run the weekly report",
        "enabled": True,
        "status": "idle",
        "created_at": 0,
        "cron": "0 22 * * FRI",
        "created_by_user_id": "alice@example.com",
    }
    base.update(over)
    return Schedule(**base)


def _capture_app(status_code: int = 200) -> tuple[FastAPI, dict[str, Any]]:
    """A minimal app exposing the events route; records the one request it gets."""
    app = FastAPI()
    captured: dict[str, Any] = {}

    @app.post("/v1/sessions/{session_id}/events")
    async def events(session_id: str, request: Request) -> JSONResponse:
        captured["session_id"] = session_id
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.json()
        return JSONResponse({"queued": True}, status_code=status_code)

    return app, captured


async def test_fire_posts_user_message_with_attribution() -> None:
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop())

    assert captured["session_id"] == "conv_1"
    assert captured["body"] == {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "run the weekly report"}],
        },
    }
    # Attributed to the schedule's creator via the trusted identity header.
    assert captured["headers"]["x-forwarded-email"] == "alice@example.com"


async def test_fire_omits_identity_header_without_a_creator() -> None:
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop(created_by_user_id=None))

    assert captured["session_id"] == "conv_1"
    assert "x-forwarded-email" not in captured["headers"]


async def test_fire_omits_header_for_reserved_identity() -> None:
    # The reserved "local" sentinel must NOT be sent as an explicit identity
    # header — header-auth 401s on it, accepting it only as the absent-header
    # fallback. Omitting the header lets that fallback resolve the identity.
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(
        app,
        identity_header="X-Forwarded-Email",
        reserved_identities=frozenset({"local"}),
    )

    await fire(_loop(created_by_user_id="local"))

    assert captured["session_id"] == "conv_1"
    assert "x-forwarded-email" not in captured["headers"]


async def test_fire_sends_service_identity_when_configured() -> None:
    # With the per-boot secret wired, a fire carries BOTH attribution forms:
    # the identity header (header-auth) and the service identity (every mode).
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(
        app, identity_header="X-Forwarded-Email", service_token="boot-token"
    )

    await fire(_loop())

    assert captured["headers"]["x-forwarded-email"] == "alice@example.com"
    assert captured["headers"]["x-omnigent-service-token"] == "boot-token"
    assert captured["headers"]["x-omnigent-acting-user"] == "alice@example.com"


async def test_fire_omits_service_identity_for_reserved_identity() -> None:
    # The reserved "local" sentinel resolves via the absent-header fallback —
    # neither the identity header nor the service identity is sent for it.
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(
        app,
        identity_header="X-Forwarded-Email",
        reserved_identities=frozenset({"local"}),
        service_token="boot-token",
    )

    await fire(_loop(created_by_user_id="local"))

    assert "x-omnigent-service-token" not in captured["headers"]
    assert "x-omnigent-acting-user" not in captured["headers"]


def _auth_enforcing_app() -> tuple[FastAPI, dict[str, Any]]:
    """A capture app that REQUIRES authentication via the real ``get_user_id``
    with a cookie/OIDC-style provider (identity headers ignored, no session →
    None). Mirrors why fires used to 401 on such deployments."""
    from omnigent.server.routes._auth_helpers import get_user_id

    class _CookieModeProvider:
        def get_user_id(self, request: Request) -> str | None:
            del request
            return None

    app = FastAPI()
    captured: dict[str, Any] = {}
    provider = _CookieModeProvider()

    @app.post("/v1/sessions/{session_id}/events")
    async def events(session_id: str, request: Request) -> JSONResponse:
        user = get_user_id(request, provider)  # type: ignore[arg-type]
        if user is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        captured["session_id"] = session_id
        captured["user"] = user
        return JSONResponse({"queued": True}, status_code=200)

    return app, captured


async def test_fire_authenticates_on_cookie_mode_via_service_identity() -> None:
    # End-to-end S1 proof: a cookie/OIDC-style app (ignores identity headers)
    # accepts the fire as the loop's creator when the service secret is wired…
    app, captured = _auth_enforcing_app()
    app.state.service_identity_token = "boot-token"
    fire = build_inprocess_fire(
        app, identity_header="X-Forwarded-Email", service_token="boot-token"
    )

    fired = await fire(_loop())

    assert fired == "conv_1"  # dispatched → last_fired_at/last_run_id stamp
    assert captured["user"] == "alice@example.com"


async def test_fire_without_service_token_still_401s_on_cookie_mode() -> None:
    # …and without it the fire is rejected (logged, returns None → no stamp).
    app, captured = _auth_enforcing_app()
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    fired = await fire(_loop())

    assert fired is None
    assert captured == {}


async def test_fire_is_a_noop_for_monitors_and_empty_prompts() -> None:
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop(kind="monitor", cron=None, command="tail -f log"))
    await fire(_loop(prompt=""))

    # Monitors stream on the host side; an empty prompt has nothing to send.
    assert captured == {}


async def test_fire_swallows_offline_runner() -> None:
    # 503 RUNNER_UNAVAILABLE (and 404/409/410) must not propagate — the loop
    # has to keep its cadence when a session has no live runner.
    app, _ = _capture_app(503)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop())  # must return without raising


async def test_fire_swallows_unexpected_error_status() -> None:
    app, _ = _capture_app(400)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop())  # logged, not raised


# ── Global loops → fresh runs ─────────────────────────


class _FakeAgent:
    def __init__(self, agent_id: str) -> None:
        self.id = agent_id


class _FakeAgentStore:
    def __init__(self, agents: dict[str, _FakeAgent]) -> None:
        self._agents = agents

    def get_by_name(self, name: str) -> _FakeAgent | None:
        return self._agents.get(name)


class _FakeConv:
    def __init__(self, conv_id: str) -> None:
        self.id = conv_id


class _FakeConversationStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_conversation(self, **kwargs: Any) -> _FakeConv:
        self.created.append(kwargs)
        return _FakeConv("conv_fresh")


class _FakeRunnerSession:
    def __init__(self, owner: str | None) -> None:
        self.owner = owner


class _FakeRegistry:
    def __init__(self, online: list[str], owners: dict[str, str | None] | None = None) -> None:
        self._online = online
        self._owners = owners or {}

    def online_runner_ids(self) -> list[str]:
        return list(self._online)

    def get(self, rid: str) -> _FakeRunnerSession | None:
        if rid not in self._online:
            return None
        return _FakeRunnerSession(self._owners.get(rid))


class _FakePermissionStore:
    def __init__(self) -> None:
        self.grants: list[tuple[str, str, int]] = []
        self.ensured: list[str] = []

    def ensure_user(self, user_id: str, *, is_admin: bool = False) -> None:
        self.ensured.append(user_id)

    def grant(self, user_id: str, conversation_id: str, level: int) -> None:
        self.grants.append((user_id, conversation_id, level))


def _global_fire(
    app: FastAPI,
    *,
    agents: dict[str, _FakeAgent] | None = None,
    online: list[str] | None = None,
    owners: dict[str, str | None] | None = None,
) -> tuple[Any, _FakeConversationStore, _FakePermissionStore]:
    """Build a fire callback wired for global loops + return its stores."""
    conv_store = _FakeConversationStore()
    perm_store = _FakePermissionStore()
    fire = build_inprocess_fire(
        app,
        identity_header="X-Forwarded-Email",
        conversation_store=conv_store,
        agent_store=_FakeAgentStore(agents if agents is not None else {}),
        tunnel_registry=_FakeRegistry(online if online is not None else [], owners),
        permission_store=perm_store,
    )
    return fire, conv_store, perm_store


async def test_global_loop_spawns_fresh_conversation() -> None:
    app, captured = _capture_app(200)
    fire, conv_store, perm_store = _global_fire(
        app, agents={"reporter": _FakeAgent("agt_1")}, online=["rnr_1"]
    )

    await fire(_loop(conversation_id=None, agent_name="reporter"))

    # Spawned a fresh conversation for the agent + bound the online runner...
    assert conv_store.created and conv_store.created[0]["agent_id"] == "agt_1"
    assert conv_store.created[0]["runner_id"] == "rnr_1"
    # ...ensured the creator row exists BEFORE granting (grant FK-references it)...
    assert perm_store.ensured == ["alice@example.com"]
    # ...granted the loop's creator ownership so the run is visible (LEVEL_OWNER=4)...
    assert perm_store.grants == [("alice@example.com", "conv_fresh", 4)]
    # ...then dispatched the prompt INTO that freshly-spawned conversation.
    assert captured["session_id"] == "conv_fresh"
    assert captured["body"]["data"]["content"][0]["text"] == "run the weekly report"


async def test_global_loop_binds_runner_owned_by_creator() -> None:
    # A runner owned by the loop's creator is usable — bind it.
    app, captured = _capture_app(200)
    fire, conv_store, _ = _global_fire(
        app,
        agents={"reporter": _FakeAgent("agt_1")},
        online=["rnr_alice"],
        owners={"rnr_alice": "alice@example.com"},
    )

    await fire(_loop(conversation_id=None, agent_name="reporter"))

    assert conv_store.created and conv_store.created[0]["runner_id"] == "rnr_alice"
    assert captured["session_id"] == "conv_fresh"


async def test_global_loop_skips_runner_owned_by_another_user() -> None:
    # Never execute the creator's prompt on another user's host: a runner owned
    # by someone else is NOT usable, so with no other runner online we soft-skip.
    app, captured = _capture_app(200)
    fire, conv_store, perm_store = _global_fire(
        app,
        agents={"reporter": _FakeAgent("agt_1")},
        online=["rnr_bob"],
        owners={"rnr_bob": "bob@example.com"},
    )

    await fire(_loop(conversation_id=None, agent_name="reporter"))

    assert conv_store.created == []
    assert perm_store.grants == []
    assert captured == {}


async def test_global_loop_skips_when_no_runner_online() -> None:
    app, captured = _capture_app(200)
    fire, conv_store, _ = _global_fire(app, agents={"reporter": _FakeAgent("agt_1")}, online=[])

    await fire(_loop(conversation_id=None, agent_name="reporter"))

    # A fresh run needs a live host — no runner → no spawn, no dispatch.
    assert conv_store.created == []
    assert captured == {}


async def test_global_loop_skips_when_agent_missing() -> None:
    app, captured = _capture_app(200)
    fire, conv_store, _ = _global_fire(app, agents={}, online=["rnr_1"])

    await fire(_loop(conversation_id=None, agent_name="ghost"))

    assert conv_store.created == []
    assert captured == {}


async def test_global_loop_skips_when_spawn_deps_unwired() -> None:
    # No conversation_store/agent_store/registry passed → a global loop can't
    # spawn; it soft-skips instead of erroring.
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop(conversation_id=None, agent_name="reporter"))

    assert captured == {}
