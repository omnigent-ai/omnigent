"""Integration tests for the session-scoped ``harness_override`` column.

Mirrors ``test_sessions_model_override.py``: create-time persistence,
snapshot read-back, runner-body forwarding (via a ``_get_runner_client``
capture stub), and fail-loud validation. ``harness_override`` is
create-time only — there is intentionally no PATCH path, since the
harness process spawns on the session's first turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


class _CaptureClient:
    """Runner-client stub that records the POSTed path + body.

    :param captured: Dict the test inspects after the route fires.
    """

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        """Record the path + body and return a fake 202 response."""
        self._captured["path"] = path
        self._captured["body"] = json
        self._captured.setdefault("posts", []).append((path, json))

        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()

    async def get(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError


def _stub_runner_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``_get_runner_client`` to return a capturing stub.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: A dict the test inspects after the runner POST runs;
        contains ``path`` and ``body`` for the last POST plus a ``posts``
        list of every ``(path, body)`` pair, once the route fires.
    """
    from omnigent.server.routes import sessions as sessions_mod

    captured: dict[str, Any] = {}

    async def _stub(*_: Any, **__: Any) -> _CaptureClient:
        return _CaptureClient(captured)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)
    return captured


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_create_with_harness_override_persists_and_snapshot_reflects(
    client: httpx.AsyncClient,
    override: str,
) -> None:
    """Create-time ``harness_override`` makes the snapshot report it.

    The test bundle declares ``executor.config.harness: claude-sdk``;
    after creating with an override the snapshot's
    ``harness`` must match it — what the runner will actually spawn —
    not the spec's declared value. This is the contract the new-chat
    composer and the attach banner rely on.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": override},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body.get("harness") == override, (
        f"Create response harness is {body.get('harness')!r}, expected {override!r}. "
        f"If this is 'claude-sdk', _resolve_harness ignored the persisted "
        f"override and reported the spec default."
    )

    get = await client.get(f"/v1/sessions/{body['id']}")
    assert get.status_code == 200
    assert get.json().get("harness") == override, (
        "GET snapshot lost the harness override — the column did not "
        "persist or _resolve_harness stopped preferring it."
    )


async def test_create_without_harness_override_reports_spec_default(
    client: httpx.AsyncClient,
) -> None:
    """No override → the snapshot reports the spec's declared harness.

    Guards the NULL-means-track-the-spec semantics: an un-overridden
    session must keep following the agent bundle's declared harness.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": []},
    )
    assert resp.status_code == 201, resp.text
    # The test bundle declares claude-sdk (see build_agent_bundle).
    assert resp.json().get("harness") == "claude-sdk"


async def test_create_harness_override_alias_canonicalizes(
    client: httpx.AsyncClient,
) -> None:
    """The ``openai-agents-sdk`` alias persists as canonical ``openai-agents``.

    A raw alias on the row would miss the runner's harness-module
    registry at dispatch (it keys on canonical names).
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "harness_override": "openai-agents-sdk",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json().get("harness") == "openai-agents"


async def test_create_rejects_unknown_harness_override(
    client: httpx.AsyncClient,
) -> None:
    """An unknown harness fails loud at create — no orphan session row."""
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": "bogus"},
    )
    assert resp.status_code == 400, (
        f"Unknown harness_override should 400, got {resp.status_code}: {resp.text}"
    )
    assert "bogus" in resp.text


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_create_rejects_harness_override_for_non_omnigent_agent(
    client: httpx.AsyncClient,
    override: str,
) -> None:
    """Non-omnigent executor types reject the override instead of no-opping.

    Mirrors the CLI's ``--harness`` rule: those executors have no
    ``config.harness``, so accepting the value would silently launch the
    spec's own executor while the snapshot claimed otherwise.
    """
    agent = await create_test_agent(
        client,
        name="sdk-typed-agent",
        # A non-omnigent executor type (claude_sdk rejects the helper's
        # executor.connection, so agents_sdk is the representative here);
        # the helper still injects config.harness, which this executor
        # type simply ignores.
        executor={"type": "agents_sdk", "model": "databricks-gpt-5-4"},
    )
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": override},
    )
    assert resp.status_code == 400, (
        f"harness_override on an agents_sdk-typed agent should 400, got "
        f"{resp.status_code}: {resp.text}"
    )
    assert "agents_sdk" in resp.text


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_runner_first_event_forwards_harness_override(
    client: httpx.AsyncClient,
    override: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create-time override reaches the runner body on the first event.

    The runner reads ``harness_override`` from the message body in
    ``_resolve_harness_config`` / the TurnDispatch builder — if the
    server drops it here, the session silently runs the spec's declared
    harness while the snapshot claims the override.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": override},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    event = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "first turn"}],
            },
        },
    )
    assert event.status_code == 202, event.text

    assert captured.get("body") is not None, (
        "Runner client was never POSTed to — _forward_event_to_runner "
        "did not run. Check the runner-stub wiring."
    )
    assert captured["body"].get("harness_override") == override, (
        f"First-event runner body missing the create-time harness "
        f"override; got {captured['body'].get('harness_override')!r}. The "
        f"create route did not persist harness_override before the first "
        f"turn, or the forwarding site dropped it."
    )


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_create_session_init_carries_harness_override(
    client: httpx.AsyncClient,
    override: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create route's runner notification must carry the override.

    With ``initial_items`` the kickoff turn is forwarded before this
    notification lands. A notification that omits the session-init envelope
    leaves the runner resolving the harness from the spec, so init evicts the
    override harness the kickoff turn is already streaming through.
    """
    from omnigent.server.routes import sessions as sessions_mod

    captured = _stub_runner_client(monkeypatch)

    async def _skip_relay_readiness(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _skip_relay_readiness)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "harness_override": override,
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "kickoff"}],
                    },
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    init_posts = [body for path, body in captured.get("posts", []) if path == "/v1/sessions"]
    assert init_posts, (
        "The create route never notified the runner about the new session — "
        "check the runner-stub wiring."
    )
    snapshot = init_posts[-1].get("session_init", {}).get("snapshot", {})
    assert snapshot.get("harness_override") == override, (
        f"Session-init notification lost the create-time harness override; "
        f"got {init_posts[-1]!r}. The runner then resolves the harness from "
        f"the spec and spawns that one instead."
    )
    assert init_posts[-1].get("session_init", {}).get("suppress_recovery_turn") is True, (
        f"The create-time init must suppress the runner's recovery turn — the "
        f"kickoff was already forwarded, so recovery would run it twice; got "
        f"{init_posts[-1]!r}."
    )


@pytest.mark.parametrize("override", ["pi", "acp:goose"])
async def test_patch_rebind_init_carries_harness_override(
    client: httpx.AsyncClient,
    override: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PATCH rebind's runner notification must carry the override.

    The external-host launch binds a runner after create via
    ``PATCH {runner_id}``; the recovery turn that then executes any seeded
    ``initial_items`` resolves its harness from session init. A bare legacy
    body loses the override, so the kickoff runs on the spec's harness.
    """
    from omnigent.server.routes import sessions as sessions_mod

    captured = _stub_runner_client(monkeypatch)

    async def _skip_relay_readiness(*_: Any, **__: Any) -> None:
        return None

    def _accept_runner_id(_router: Any, raw: str, *, user_id: Any = None) -> str:
        del user_id
        return raw.strip()

    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _skip_relay_readiness)
    monkeypatch.setattr(sessions_mod, "_registered_runner_id", _accept_runner_id)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "harness_override": override},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    captured.pop("posts", None)

    patch = await client.patch(f"/v1/sessions/{sid}", json={"runner_id": "runner_bound_late"})
    assert patch.status_code == 200, patch.text

    init_posts = [body for path, body in captured.get("posts", []) if path == "/v1/sessions"]
    assert init_posts, (
        "The PATCH rebind never notified the runner about the session — "
        "check the runner-stub wiring."
    )
    snapshot = init_posts[-1].get("session_init", {}).get("snapshot", {})
    assert snapshot.get("harness_override") == override, (
        f"Rebind session-init notification lost the harness override; got "
        f"{init_posts[-1]!r}. The runner then resolves the harness from the "
        f"spec, and the kickoff recovery turn runs on the wrong harness."
    )
    assert not init_posts[-1].get("session_init", {}).get("suppress_recovery_turn"), (
        f"The rebind init must keep recovery enabled — on rebind the recovery "
        f"turn is what runs the pending seeded kickoff; got {init_posts[-1]!r}."
    )


async def test_runner_body_omits_harness_override_when_unset(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override → the runner body carries no ``harness_override`` key.

    The runner treats a present key as an explicit override; sending the
    spec default redundantly would defeat NULL-means-track-the-spec when
    bundles update between turns.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": []},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    event = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
    )
    assert event.status_code == 202, event.text
    assert captured.get("body") is not None
    assert "harness_override" not in captured["body"]


async def test_acp_selection_does_not_require_server_configuration(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    agent = await create_test_agent(client)
    response = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "harness_override": "acp:runner-only"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["harness"] == "acp:runner-only"


@pytest.mark.parametrize("override", ["acp:", "acp:Goose", "acp:goose/other", "acp: goose"])
async def test_rejects_malformed_acp_selection(client: httpx.AsyncClient, override: str) -> None:
    agent = await create_test_agent(client)
    response = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "harness_override": override}
    )
    assert response.status_code == 400, response.text
    assert "invalid ACP agent identifier" in response.text


@pytest.mark.parametrize("extra", [0, 1, 100])
@pytest.mark.parametrize("with_other_overrides", [False, True])
@pytest.mark.parametrize("seed_effort", [False, True])
async def test_override_storage_limit_is_checked_before_create(
    client: httpx.AsyncClient, extra: int, with_other_overrides: bool, seed_effort: bool
) -> None:
    import json

    from omnigent.db.db_models import SqlConversation
    from omnigent.runtime import get_conversation_store

    executor = {"type": "omnigent", "config": {"harness": "claude-sdk"}}
    if seed_effort:
        executor["reasoning_effort"] = "high"
    agent = await create_test_agent(client, executor=executor)
    overrides = {"harness_override": "acp:"}
    if seed_effort:
        overrides["reasoning_effort"] = "high"
    if with_other_overrides:
        overrides.update(
            {
                "model_override": "model-" + "a" * 120,
                "reasoning_effort": "high",
                "cost_control_mode_override": "off",
                "subagent_routing_override": "on",
            }
        )
    limit = SqlConversation.__table__.c.session_overrides.type.length
    overhead = len(json.dumps(overrides, separators=(",", ":")))
    overrides["harness_override"] += "a" * (limit - overhead + extra)
    store = get_conversation_store()
    before = {conv.id for conv in store.list_conversations().data}
    requested = dict(overrides)
    if seed_effort:
        requested.pop("reasoning_effort", None)
    response = await client.post("/v1/sessions", json={"agent_id": agent["id"], **requested})
    after = {conv.id for conv in store.list_conversations().data}
    if extra:
        assert after == before, "Oversized input left a persisted session"
        assert response.status_code == 400, response.text
        assert "session overrides" in response.text.lower()
    else:
        assert response.status_code == 201, response.text
        assert after - before == {response.json()["id"]}
        assert response.json()["harness"] == overrides["harness_override"]


async def test_override_growth_rejects_patch_without_mutation(client: httpx.AsyncClient) -> None:
    import json

    from omnigent.db.db_models import SqlConversation
    from omnigent.runtime import get_conversation_store

    agent = await create_test_agent(client)
    limit = SqlConversation.__table__.c.session_overrides.type.length
    harness = "acp:" + "a" * (
        limit - len(json.dumps({"harness_override": "acp:"}, separators=(",", ":")))
    )
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "harness_override": harness}
    )
    assert created.status_code == 201, created.text
    session = created.json()["id"]
    response = await client.patch(
        f"/v1/sessions/{session}",
        json={"model_override": "another-model", "title": "must not persist"},
    )
    assert response.status_code == 400, response.text
    assert "session overrides" in response.text.lower()
    conv = get_conversation_store().get_conversation(session)
    assert conv.harness_override == harness
    assert conv.model_override is None
    assert conv.title != "must not persist"
