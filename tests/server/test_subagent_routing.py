"""Tests for the native-subagent routing endpoint and policy."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.conversation import RoutingDecisionData
from omnigent.inner.hook_scripts.subagent_router import read_router_endpoint
from omnigent.runner.subagent_routing import (
    ADVERTISEMENT_FILE,
    SubagentRouteDecision,
    SubagentRouteRequest,
    candidate_models,
    decision_record,
    ensure_session_router,
    ensure_session_router_quietly,
    harness_family,
    make_server_relay_resolver,
    model_in_family,
    persist_subagent_decision,
    relayed_decisions,
    resolve_subagent_route,
    routed_models,
    router_dir_for_session,
    routing_enabled,
    session_router_env,
    shutdown_session_router,
    start_subagent_router,
    subagent_routing_enabled,
    write_advertisement,
)
from omnigent.server.smart_routing import RoutingResult, RoutingSettings
from tests.server.helpers import FakeCaps, FakeRoutingClient

CLAUDE_MODEL = "databricks-claude-opus-4-8"
GPT_MODEL = "databricks-gpt-5-5"
GLM_MODEL = "databricks-glm-5-2"
# The spelling the gateway actually serves GLM under; see ``_SERVABLE_ALIASES``.
GLM_SERVABLE = "system.ai.glm-5-2"
KIMI_MODEL = "databricks-kimi-k2-6"
PARENT_MODEL = "databricks-claude-sonnet-4-6"

# ── Stubs ───────────────────────────────────────────────────────────


@dataclass
class _PersistedItem:
    id: str


@dataclass
class _FakeStore:
    appended: list[Any] = field(default_factory=list)  # type: ignore[explicit-any]

    def append(self, session_id: str, items: list[Any]) -> list[_PersistedItem]:  # type: ignore[explicit-any]
        del session_id
        self.appended.extend(items)
        return [_PersistedItem(id=f"item_{len(self.appended)}")]


def _request(**overrides: Any) -> SubagentRouteRequest:
    kwargs: dict[str, Any] = {
        "harness": "claude-native",
        "task_name": "code-reviewer",
        "prompt": "review the diff",
        "parent_model": PARENT_MODEL,
    }
    kwargs.update(overrides)
    return SubagentRouteRequest(**kwargs)


# ── Candidate set ───────────────────────────────────────────────────


def test_candidate_models_stays_in_family_by_default() -> None:
    candidates = candidate_models("claude-native")
    assert set(candidates) == {"claude-native"}
    assert CLAUDE_MODEL in candidates["claude-native"]


def test_candidate_models_prefers_the_live_catalog() -> None:
    """A model the workspace serves today must not look unservable."""
    catalog = {"self": ["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"]}
    candidates = candidate_models("claude-native", catalog=catalog)
    assert candidates == {
        "claude-native": ["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"]
    }


def test_candidate_models_falls_back_to_the_static_table_per_harness() -> None:
    catalog = {"self": ["databricks-claude-sonnet-5"]}
    candidates = candidate_models("claude-native", cross_harness=True, catalog=catalog)
    assert candidates["claude-native"] == ["databricks-claude-sonnet-5"]
    # No codex row in the catalog — the static table fills that harness in.
    assert GPT_MODEL in candidates["codex-native"]


def test_candidate_models_ignores_an_empty_catalog() -> None:
    candidates = candidate_models("claude-native", catalog={})
    assert CLAUDE_MODEL in candidates["claude-native"]


def test_candidate_models_applies_the_family_constraint_to_catalog_rows() -> None:
    """A codex ``"self"`` row keeps GLM/Kimi and loses the Claude ids.

    The gateway behind a codex session serves Claude ids too, so the row
    carries models codex cannot speak; offering one earns a hard
    ``model_family_mismatch`` at dispatch. GLM and Kimi serve on the
    Responses wire codex does speak, so they must survive.
    """
    catalog = {"self": [GPT_MODEL, GLM_MODEL, KIMI_MODEL, CLAUDE_MODEL]}
    candidates = candidate_models("codex-native", catalog=catalog)
    assert candidates == {"codex-native": [GPT_MODEL, GLM_SERVABLE, KIMI_MODEL]}
    assert model_in_family(harness_family("codex-native"), GLM_MODEL) is True
    assert model_in_family(harness_family("claude-native"), GLM_MODEL) is False


def test_candidate_models_offers_glm_under_its_servable_alias() -> None:
    """Both catalog spellings collapse to the one the gateway serves.

    The offered id is what a rewrite spawns with, so it has to match the id
    routing resolves the ``glm-5-2`` arm to.
    """
    catalog = {"self": [GPT_MODEL, GLM_MODEL, GLM_SERVABLE]}
    assert candidate_models("codex-native", catalog=catalog) == {
        "codex-native": [GPT_MODEL, GLM_SERVABLE]
    }


def test_candidate_models_drops_a_harness_with_nothing_servable() -> None:
    catalog = {"self": [CLAUDE_MODEL]}
    assert candidate_models("codex-native", catalog=catalog) == {}


def test_candidate_models_offers_both_families_for_auto_sessions() -> None:
    candidates = candidate_models("claude-native", cross_harness=True)
    assert set(candidates) == {"claude-native", "codex-native"}
    assert CLAUDE_MODEL in candidates["claude-native"]
    assert GPT_MODEL in candidates["codex-native"]


# ── Policy ──────────────────────────────────────────────────────────


async def test_same_family_pick_rewrites() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "rewrite"
    assert decision.model == CLAUDE_MODEL
    assert decision.harness is None
    assert decision.raw_model is None
    assert decision.rationale == "deep reasoning"
    assert len(decision.decision_id) == 36


async def test_raw_model_is_omitted_when_it_matches_the_resolved_model() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="r", raw_model=CLAUDE_MODEL)
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.model == CLAUDE_MODEL
    assert decision.raw_model is None


async def test_raw_model_is_preserved_when_the_router_named_another_arm() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="r", raw_model="claude-opus-4-8-thinking")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.model == CLAUDE_MODEL
    assert decision.raw_model == "claude-opus-4-8-thinking"
    assert decision_record(_request(), decision).raw_model == "claude-opus-4-8-thinking"


async def test_live_catalog_pick_is_applied_exactly() -> None:
    """A live-catalog model is offered and applied verbatim — no substitution."""
    client = FakeRoutingClient(
        RoutingResult(model="databricks-claude-sonnet-5", rationale="r", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1",
        _request(),
        caps=FakeCaps(routing_client=client),
        catalog={"self": ["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"]},
    )
    assert client.calls[0][1] == {
        "claude-native": ["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"]
    }
    assert decision.action == "rewrite"
    assert decision.model == "databricks-claude-sonnet-5"


async def test_cross_family_pick_redirects_to_counterpart_harness() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=GPT_MODEL, rationale="narrow change", harness="codex")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client), cross_harness=True
    )
    assert decision.action == "redirect"
    assert decision.model == GPT_MODEL
    assert decision.harness == "codex-native"


async def test_in_family_session_only_offers_its_own_harness() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=GPT_MODEL, rationale="narrow change", harness="codex")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    # Only Claude arms were offered, so a Codex pick is unrunnable — a
    # constrained session can never redirect.
    assert set(client.calls[0][1]) == {"claude-native"}
    assert decision.action == "deny"


async def test_pick_outside_candidate_set_denies() -> None:
    client = FakeRoutingClient(
        RoutingResult(model="databricks-kimi-k2", rationale="unavailable", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "deny"
    assert decision.model is None
    assert "cannot run" in decision.rationale
    # The deny message still names the pick it rejected.
    assert decision.raw_model == "databricks-kimi-k2"


async def test_parent_model_pick_allows_unchanged() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=PARENT_MODEL, rationale="parent model fits", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "allow"
    assert decision.model == PARENT_MODEL


async def test_fork_is_exempt_and_never_calls_router() -> None:
    client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="x"))
    decision = await resolve_subagent_route(
        "conv_1", _request(fork=True), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "allow"
    assert decision.model == PARENT_MODEL
    assert client.calls == []


async def test_no_routable_signal_allows_the_spawn_unchanged() -> None:
    """A spawn with no prompt and no task name is not routed at all.

    Codex encrypts the spawn message, so an unnamed subagent carries nothing
    to score; calling the router with an empty task earns an HTTP 400 that
    reads on the chip as an outage.
    """
    client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="cheap arm fits"))
    decision = await resolve_subagent_route(
        "conv_1",
        _request(prompt=None, task_name=""),
        caps=FakeCaps(routing_client=client),
    )
    assert client.calls == []
    assert decision.action == "allow"
    assert decision.model == PARENT_MODEL
    assert "No routable signal" in decision.rationale


@pytest.mark.parametrize(
    ("client", "reason_fragment"),
    [
        # A router outage allows the spawn unchanged: the gate is advisory.
        (FakeRoutingClient(error=RuntimeError("router down")), "Routing unavailable"),
        # A no-verdict (not an outage) carries the router's own reason so the
        # user sees why nothing was routed.
        (FakeRoutingClient(None, last_error="menu mismatch"), "menu mismatch"),
        # No routing client configured at all is the same advisory outcome.
        (None, "no routing client configured"),
    ],
)
async def test_router_failure_allows_the_spawn_unchanged(
    client: FakeRoutingClient | None,
    reason_fragment: str,
) -> None:
    caps = FakeCaps(routing_client=client, routing_settings=RoutingSettings())
    decision = await resolve_subagent_route("conv_1", _request(), caps=caps)
    assert decision.action == "allow"
    assert reason_fragment in decision.rationale


async def test_identical_spawns_are_each_routed() -> None:
    """Decisions are not cached: a decision_id is an identity, not a key."""
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )
    caps = FakeCaps(routing_client=client)
    first = await resolve_subagent_route("conv_1", _request(), caps=caps)
    second = await resolve_subagent_route("conv_1", _request(), caps=caps)
    assert len(client.calls) == 2
    assert second.decision_id != first.decision_id


async def test_task_name_is_capped_at_parse_time() -> None:
    """``task_name`` is agent-authored, so it is bounded before it is stored."""
    req = SubagentRouteRequest.from_payload({"harness": "codex", "task_name": "x" * 5000})
    assert len(req.task_name) == 200


# ── Decision persistence ────────────────────────────────────────────


async def test_every_decision_is_persisted_with_native_subagent_scope() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )
    records: list[RoutingDecisionData] = []

    async def _persist(record: RoutingDecisionData) -> None:
        records.append(record)

    decision = await resolve_subagent_route(
        "conv_1",
        _request(),
        caps=FakeCaps(routing_client=client),
        persist=_persist,
    )
    assert len(records) == 1
    data = records[0].model_dump()
    assert data["scope"] == "native_subagent"
    assert data["decision_id"] == decision.decision_id
    # The router named the model it resolved to, so there is no raw pick to
    # report separately.
    assert data["raw_model"] is None
    assert data["model"] == CLAUDE_MODEL
    assert data["harness"] == "claude-native"
    assert data["applied"] is True
    assert data["rationale"] == "deep reasoning"


async def test_deny_is_persisted_unapplied() -> None:
    """An unoffered pick is the only deny left, and it persists unapplied."""
    client = FakeRoutingClient(
        RoutingResult(model="databricks-kimi-k2", rationale="unavailable", harness="claude-sdk")
    )
    records: list[RoutingDecisionData] = []

    async def _persist(record: RoutingDecisionData) -> None:
        records.append(record)

    caps = FakeCaps(routing_client=client)
    await resolve_subagent_route("conv_1", _request(), caps=caps, persist=_persist)
    assert records[0].applied is False
    assert records[0].model == PARENT_MODEL


async def test_persist_appends_routing_decision_item() -> None:
    store = _FakeStore()
    record = RoutingDecisionData(
        model=CLAUDE_MODEL,
        applied=True,
        rationale="deep reasoning",
        decision_id="dec_1",
        harness="claude-native",
        raw_model="claude-opus-4-8",
        scope="native_subagent",
    )
    await persist_subagent_decision("conv_1", store, record)
    assert len(store.appended) == 1
    item = store.appended[0]
    assert item.type == "routing_decision"
    assert item.data.model == CLAUDE_MODEL
    assert item.data.rationale == "deep reasoning"
    # The additive fields ride along once the item model carries them.
    if hasattr(item.data, "scope"):
        assert item.data.scope == "native_subagent"
        assert item.data.decision_id == "dec_1"
        assert item.data.raw_model == "claude-opus-4-8"
        assert item.data.harness == "claude-native"


async def test_persist_failure_does_not_break_the_decision() -> None:
    class _BoomStore:
        def append(self, session_id: str, items: list[Any]) -> list[_PersistedItem]:
            raise RuntimeError("db down")

    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )

    async def _persist(record: RoutingDecisionData) -> None:
        await persist_subagent_decision("conv_1", _BoomStore(), record)

    decision = await resolve_subagent_route(
        "conv_1",
        _request(),
        caps=FakeCaps(routing_client=client),
        persist=_persist,
    )
    assert decision.action == "rewrite"


# ── Advertisement + loopback endpoint ───────────────────────────────


def test_advertisement_roundtrip(tmp_path: Path) -> None:
    path = write_advertisement(tmp_path, url="http://127.0.0.1:1234", token="tok")
    assert path.name == ADVERTISEMENT_FILE
    endpoint = read_router_endpoint(tmp_path)
    assert endpoint is not None
    assert (endpoint.url, endpoint.token) == ("http://127.0.0.1:1234", "tok")


def _post(url: str, body: dict[str, Any], token: str | None) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


async def test_loopback_endpoint_serves_decisions_and_checks_token(tmp_path: Path) -> None:
    canned = SubagentRouteDecision(
        action="rewrite", rationale="deep reasoning", model=CLAUDE_MODEL, raw_model=CLAUDE_MODEL
    )
    seen: list[SubagentRouteRequest] = []

    async def _resolver(session_id: str, req: SubagentRouteRequest) -> SubagentRouteDecision:
        del session_id
        seen.append(req)
        return canned

    router = start_subagent_router(
        bridge_dir=tmp_path,
        session_id="conv_1",
        resolver=_resolver,
        loop=asyncio.get_running_loop(),
    )
    try:
        advertised = read_router_endpoint(tmp_path)
        assert advertised is not None
        url = f"{advertised.url}/v1/sessions/conv_1/route-subagent"
        body = {
            "harness": "claude-native",
            "task_name": "code-reviewer",
            "prompt": "review the diff",
            "fork": False,
            "parent_model": PARENT_MODEL,
        }

        status, payload = await asyncio.to_thread(_post, url, body, advertised.token)
        assert status == 200
        assert payload == canned.to_payload()
        assert seen[0].task_name == "code-reviewer"

        status, _ = await asyncio.to_thread(_post, url, body, "wrong-token")
        assert status == 401
        status, _ = await asyncio.to_thread(_post, url, body, None)
        assert status == 401
        assert len(seen) == 1

        status, _ = await asyncio.to_thread(
            _post,
            f"{advertised.url}/v1/sessions/other/route-subagent",
            body,
            advertised.token,
        )
        assert status == 404

        status, _ = await asyncio.to_thread(_post, url, {"task_name": "x"}, advertised.token)
        assert status == 400
    finally:
        router.close()
    assert not (tmp_path / ADVERTISEMENT_FILE).exists()


async def test_server_relay_resolver_forwards_and_parses() -> None:
    posted: list[tuple[str, dict[str, Any]]] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "action": "redirect",
                "model": GPT_MODEL,
                "harness": "codex-native",
                "raw_model": "gpt-5-6-sol",
                "rationale": "narrow change",
                "decision_id": "dec_9",
            }

    class _Client:
        async def post(self, path: str, *, json: dict[str, Any], timeout: float) -> _Resp:
            del timeout
            posted.append((path, json))
            return _Resp()

    resolver = make_server_relay_resolver(_Client())
    decision = await resolver("conv_1", _request())
    assert posted[0][0] == "/v1/sessions/conv_1/hooks/route-subagent"
    assert posted[0][1]["harness"] == "claude-native"
    assert decision.action == "redirect"
    assert decision.harness == "codex-native"
    assert decision.decision_id == "dec_9"


async def test_server_relay_resolver_allows_on_hop_failure() -> None:
    class _DeadClient:
        async def post(self, path: str, *, json: dict[str, Any], timeout: float) -> Any:
            raise RuntimeError("connection refused")

    decision = await make_server_relay_resolver(_DeadClient())("conv_1", _request())
    assert decision.action == "allow"
    assert "unreachable" in decision.rationale


async def test_loopback_endpoint_allows_when_the_resolver_errors(tmp_path: Path) -> None:
    async def _resolver(session_id: str, req: SubagentRouteRequest) -> SubagentRouteDecision:
        raise RuntimeError("boom")

    router = start_subagent_router(
        bridge_dir=tmp_path,
        session_id="conv_1",
        resolver=_resolver,
        loop=asyncio.get_running_loop(),
    )
    try:
        advertised = read_router_endpoint(tmp_path)
        assert advertised is not None
        status, payload = await asyncio.to_thread(
            _post,
            f"{advertised.url}/v1/sessions/conv_1/route-subagent",
            {"harness": "claude-native"},
            advertised.token,
        )
        assert status == 200
        assert payload["action"] == "allow"
    finally:
        router.close()


# ── Enablement gate + session router lifecycle (P7) ─────────────────


@pytest.mark.parametrize(
    ("mode", "parent_mode", "expected"),
    [
        ("on", None, True),
        ("off", None, False),
        (None, None, False),
        # An unset session inherits the parent's mode.
        (None, "on", True),
    ],
)
def test_routing_enabled_reads_the_session_toggle(
    mode: str | None, parent_mode: str | None, expected: bool
) -> None:
    assert routing_enabled(mode, parent_cost_control_mode=parent_mode) is expected


@pytest.mark.parametrize(
    ("routing_client", "expected"),
    [
        # Toggle on but nothing to route with: still off.
        (None, False),
        (object(), True),
    ],
)
def test_routing_enabled_requires_a_client_when_caps_are_given(
    routing_client: object | None, expected: bool
) -> None:
    assert routing_enabled("on", caps=FakeCaps(routing_client=routing_client)) is expected


@pytest.mark.parametrize(
    ("override", "cost_control_mode", "parent_mode", "expected"),
    [
        # No override: subagent routing follows the session's routing state,
        # falling back to the parent's when the session's is unset.
        (None, "on", None, True),
        (None, "off", None, False),
        (None, None, None, False),
        (None, None, "on", True),
        # An explicit override wins over whatever was inherited.
        ("off", "on", None, False),
        ("on", None, None, True),
        ("off", None, "on", False),
    ],
)
def test_subagent_routing_enabled_override_beats_the_inherited_state(
    override: str | None,
    cost_control_mode: str | None,
    parent_mode: str | None,
    expected: bool,
) -> None:
    assert (
        subagent_routing_enabled(
            override,
            cost_control_mode=cost_control_mode,
            parent_cost_control_mode=parent_mode,
        )
        is expected
    )


def test_router_dir_for_session_is_owner_only(tmp_path: Path) -> None:
    path = router_dir_for_session("conv_router_dir")
    assert path.is_dir()
    assert path.stat().st_mode & 0o777 == 0o700


def test_ensure_session_router_is_idempotent_and_advertises_everywhere(
    tmp_path: Path,
) -> None:
    class _DeadClient:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("server down")

    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"

    async def _run() -> None:
        router = ensure_session_router(
            "conv_lifecycle",
            bridge_dir=first_dir,
            server_client=_DeadClient(),
        )
        again = ensure_session_router(
            "conv_lifecycle",
            bridge_dir=second_dir,
            server_client=_DeadClient(),
        )
        assert again is router
        # Same rendezvous advertised in both directories.
        assert read_router_endpoint(first_dir) == read_router_endpoint(second_dir)
        env = session_router_env("conv_lifecycle")
        assert env["OMNIGENT_SUBAGENT_ROUTER_SESSION_ID"] == "conv_lifecycle"
        assert env["OMNIGENT_CODEX_SUBAGENT_ROUTER_DIR"] == str(first_dir)
        shutdown_session_router("conv_lifecycle")
        assert session_router_env("conv_lifecycle") == {}

    asyncio.run(_run())


def test_ensure_session_router_quietly_skips_without_a_server_client(tmp_path: Path) -> None:
    assert (
        ensure_session_router_quietly(
            "conv_no_client",
            bridge_dir=tmp_path,
            server_client=None,
        )
        is None
    )
    assert not (tmp_path / ADVERTISEMENT_FILE).exists()


def test_ensure_session_router_quietly_swallows_a_bind_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner import subagent_routing as module

    def _boom(session_id: str, **kwargs: Any) -> None:
        raise OSError("address in use")

    monkeypatch.setattr(module, "ensure_session_router", _boom)
    assert (
        ensure_session_router_quietly(
            "conv_bind_fail",
            bridge_dir=tmp_path,
            server_client=object(),  # type: ignore[arg-type]
            harness="codex-native",
            caps=FakeCaps(),
        )
        is None
    )


def test_relayed_decisions_start_empty() -> None:
    assert relayed_decisions("conv_never_routed") == ()
    assert routed_models("conv_never_routed") == frozenset()
