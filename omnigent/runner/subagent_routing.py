"""Native-subagent routing: loopback endpoint + server-side policy.

Harness ``PreToolUse`` hooks (Claude ``Task``, Codex ``spawn_agent``) call
this before a native subagent spawn to ask which model the spawn should
run on. The gate is **advisory**: every transport failure, unreachable
endpoint, unparseable verdict or hook timeout allows the spawn unchanged,
because a spawn that dies on routing infrastructure is worse than a spawn
on the inherited model. Two halves live here:

* **Runner side** — a loopback HTTP relay (:func:`start_subagent_router`)
  on ``127.0.0.1:0``, bearer-token authenticated, advertised to hook
  scripts via ``subagent_router.json`` in the session bridge dir. Same
  rendezvous as ``tool_relay.json``.
* **Server side** — the policy (:func:`resolve_subagent_route`), which
  runs where ``RuntimeCaps.routing_client`` lives and persists every
  decision as a ``routing_decision`` transcript item.

The two halves are joined by the server relay route
:data:`SERVER_ROUTE_PATH` (registered in
``omnigent/server/routes/sessions/routes_hooks.py``); the runner's
handler forwards to it with :func:`make_server_relay_resolver`.

**Timeout budget.** Four hops wait on each other, so each one's budget is
strictly larger than the hop it waits on — otherwise an inner hop's
fail-open branch can never run. Canonical values, outermost first:

1. harness hook timeout — 40s (``claude_native_bridge`` PreToolUse entry,
   codex's spawn hook)
2. hook script HTTP request — :data:`HOOK_REQUEST_TIMEOUT_S` (30s, defined
   as ``REQUEST_TIMEOUT_S`` in ``omnigent.inner.hook_scripts.subagent_router``,
   which stays stdlib-only and cannot import this module)
3. runner loopback relay wait — :data:`RELAY_TIMEOUT_S` (20s)
4. server relay hop — :data:`SERVER_HOP_TIMEOUT_S` (15s)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import httpx

    from omnigent.entities.conversation import RoutingDecisionData

from omnigent.telemetry import record_routing_decision

_logger = logging.getLogger(__name__)

#: Bridge-dir file that advertises the loopback endpoint to hook scripts.
ADVERTISEMENT_FILE = "subagent_router.json"

#: Path served by the loopback relay, per the frozen endpoint contract.
ROUTE_PATH_TEMPLATE = "/v1/sessions/{session_id}/route-subagent"

#: Server relay route the runner-side handler forwards to (the server
#: process is the only one holding ``RuntimeCaps.routing_client``).
SERVER_ROUTE_PATH = "/v1/sessions/{session_id}/hooks/route-subagent"

#: Seconds the runner's loopback relay waits for a verdict. Hop 3 of the
#: timeout budget in the module docstring.
RELAY_TIMEOUT_S = 20.0

#: Seconds the runner waits on the server relay route. Hop 4 (innermost).
SERVER_HOP_TIMEOUT_S = 15.0

#: Hop 2 of the budget, restated for the docstring cross-reference. The hook
#: script owns the value; it cannot import this module (stdlib-only).
HOOK_REQUEST_TIMEOUT_S = 30.0

#: Conversation label carrying the routing decision behind a session's
#: ``model_override``, so the child-sessions API can join the two without
#: a new column or a transcript scan.
ROUTING_DECISION_LABEL_KEY = "omnigent.routing.decision_id"

#: Conversation label marking a session created in auto-harness mode. The
#: ``harness_override`` sentinel is replaced the moment first-message routing
#: resolves a harness, so this label is the durable record that the router
#: may still move this session's subagents across harness families.
AUTO_HARNESS_LABEL_KEY = "omnigent.routing.auto_harness"

_SCOPE = "native_subagent"
_PROMPT_CAP = 4000
#: Agent-authored, so bounded before it is stored or echoed anywhere.
_TASK_NAME_CAP = 200
#: Ledger entries kept per session; a long-running session can spawn
#: without limit, so the oldest verdicts are dropped.
_RELAYED_CAP = 500

# Harnesses whose spawn hooks read a router advertisement, per family. Every
# other harness gets no router env vars at all.
_CLAUDE_HOOK_HARNESSES = frozenset({"claude-sdk", "claude_sdk", "claude-native"})
_CODEX_HOOK_HARNESSES = frozenset({"codex", "codex-native"})

# Cross-harness counterpart used to offer the router a second family, and
# to name the redirect target when it picks from that family.
_COUNTERPART_HARNESS: dict[str, str] = {
    "claude-sdk": "codex",
    "claude-native": "codex-native",
    "codex": "claude-sdk",
    "codex-native": "claude-native",
}

Resolver = Callable[[str, "SubagentRouteRequest"], Awaitable["SubagentRouteDecision"]]


# ── Enablement gate ────────────────────────────────────────────────────────


def routing_enabled(
    cost_control_mode: str | None,
    *,
    parent_cost_control_mode: str | None = None,
    caps: Any = None,
) -> bool:
    """Report whether smart routing is on for one session.

    The shared gate for main-agent routing, so "routing is on" means the
    same thing everywhere the server decides to route. Two conditions:
    the per-session toggle (its own, or its parent's for a spawned child
    — the server routes children of a routed parent), and, where a
    ``RuntimeCaps`` is in reach, a configured routing client. Subagent
    spawns read :func:`subagent_routing_enabled`, which layers the
    per-session subagent override on top of this.

    :param cost_control_mode: The session's ``cost_control_mode_override``,
        e.g. ``"on"``.
    :param parent_cost_control_mode: The parent session's value for a
        spawned child. ``None`` for a top-level session.
    :param caps: ``RuntimeCaps``-shaped object whose ``routing_client``
        must be configured. ``None`` skips that check — the runner
        process holds no routing client (the server relay owns the
        policy), so runner-side callers pass nothing.
    :returns: ``True`` when routing applies to this session.
    """
    if cost_control_mode != "on" and parent_cost_control_mode != "on":
        return False
    if caps is None:
        return True
    return getattr(caps, "routing_client", None) is not None


def subagent_routing_enabled(
    subagent_routing_override: str | None,
    *,
    cost_control_mode: str | None,
    parent_cost_control_mode: str | None = None,
) -> bool:
    """Report whether subagent spawns are routed for one session.

    Read per spawn (not at launch) so the setting can be flipped at any
    point in a session and take effect on the next spawn. ``"on"`` /
    ``"off"`` win outright; unset inherits the session's main routing
    state, so a session started on Smart Routing routes its
    subagents and one started on a manual model does not.

    :param subagent_routing_override: The session's
        ``subagent_routing_override`` — ``"on"``, ``"off"``, or ``None``
        to inherit.
    :param cost_control_mode: The session's ``cost_control_mode_override``.
    :param parent_cost_control_mode: The parent session's value for a
        spawned child. ``None`` for a top-level session.
    :returns: ``True`` when subagent spawns should be routed.
    """
    if subagent_routing_override == "on":
        return True
    if subagent_routing_override == "off":
        return False
    return routing_enabled(
        cost_control_mode,
        parent_cost_control_mode=parent_cost_control_mode,
    )


# ── Wire types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubagentRouteRequest:
    """One native-subagent spawn awaiting a routing verdict.

    :param harness: Requesting harness id, e.g. ``"claude-native"``.
    :param task_name: Subagent type / task name from the spawn payload,
        e.g. ``"code-reviewer"``.
    :param prompt: Raw task text. ``None`` on codex, whose spawn message
        is encrypted in hook payloads.
    :param fork: ``True`` when the spawn is a fork of the parent session.
    :param parent_model: Model the parent session runs on, e.g.
        ``"databricks-claude-sonnet-4-6"``.
    """

    harness: str
    task_name: str = ""
    prompt: str | None = None
    fork: bool = False
    parent_model: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SubagentRouteRequest:
        """Parse a request body into a :class:`SubagentRouteRequest`.

        :param payload: Decoded JSON object from the hook script.
        :returns: Parsed request.
        :raises ValueError: If ``harness`` is missing or not a string.
        """
        harness = payload.get("harness")
        if not isinstance(harness, str) or not harness.strip():
            raise ValueError("route-subagent body requires a non-empty 'harness' string")
        task_name = payload.get("task_name")
        prompt = payload.get("prompt")
        parent_model = payload.get("parent_model")
        return cls(
            harness=harness.strip(),
            task_name=task_name[:_TASK_NAME_CAP] if isinstance(task_name, str) else "",
            prompt=prompt if isinstance(prompt, str) and prompt else None,
            fork=bool(payload.get("fork")),
            parent_model=parent_model if isinstance(parent_model, str) and parent_model else None,
        )


@dataclass(frozen=True)
class SubagentRouteDecision:
    """The verdict a hook script enforces on a spawn.

    ``model`` is always a servable catalog id (e.g.
    ``"databricks-claude-sonnet-5"``) — never a harness's own tool
    vocabulary. Translating it is the harness hook's job: Claude Code's
    Agent/Task ``model`` parameter, for instance, only accepts the tier
    aliases ``sonnet``/``opus``/``haiku``/``fable`` and rejects a catalog id
    outright, so its hook inverse-maps before rewriting ``updatedInput``.

    :param action: ``"allow"`` (spawn unchanged), ``"rewrite"`` (same
        harness, injected model), ``"redirect"`` (cross-harness — deny
        and tell the model to use ``sys_session_send``) or ``"deny"``.
    :param model: Servable model id; set for rewrite/redirect.
    :param harness: Target harness; set for redirect.
    :param raw_model: Router-vocabulary pick before resolution.
    :param rationale: One-line explanation, surfaced to the model and UI.
    :param decision_id: Identity shared by the response, the transcript
        item and telemetry.
    """

    action: Literal["allow", "rewrite", "redirect", "deny"]
    rationale: str
    model: str | None = None
    harness: str | None = None
    raw_model: str | None = None
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the frozen response shape.

        :returns: JSON-ready response body.
        """
        return {
            "action": self.action,
            "model": self.model,
            "harness": self.harness,
            "raw_model": self.raw_model,
            "rationale": self.rationale,
            "decision_id": self.decision_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SubagentRouteDecision:
        """Parse a response body (used by the runner-side relay).

        :param payload: Decoded JSON response from the server route.
        :returns: Parsed decision; unknown actions degrade to ``allow``.
        """
        action = payload.get("action")
        if action not in ("allow", "rewrite", "redirect", "deny"):
            action = "allow"
        rationale = payload.get("rationale")
        decision_id = payload.get("decision_id")
        return cls(
            action=action,
            rationale=rationale if isinstance(rationale, str) else "",
            model=_opt_str(payload.get("model")),
            harness=_opt_str(payload.get("harness")),
            raw_model=_opt_str(payload.get("raw_model")),
            decision_id=decision_id if isinstance(decision_id, str) else str(uuid.uuid4()),
        )


def decision_record(
    req: SubagentRouteRequest,
    decision: SubagentRouteDecision,
) -> RoutingDecisionData:
    """Build the transcript payload for *decision*.

    :param req: The spawn that was routed.
    :param decision: The verdict returned to the hook.
    :returns: Item data ready for :func:`persist_subagent_decision`.
    """
    from omnigent.entities.conversation import RoutingDecisionData

    model = decision.model or req.parent_model or "unrouted"
    return RoutingDecisionData(
        model=model,
        applied=decision.action in ("rewrite", "redirect"),
        rationale=decision.rationale,
        decision_id=decision.decision_id,
        harness=decision.harness or req.harness,
        raw_model=decision.raw_model,
        agent=req.task_name or None,
        scope=_SCOPE,
    )


# ── Policy ─────────────────────────────────────────────────────────────────


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _harness_family(harness: str) -> str | None:
    from omnigent.server.smart_routing import _HARNESS_FAMILY

    return _HARNESS_FAMILY.get(harness)


def harness_family(harness: str | None) -> str | None:
    """Return the model family *harness* belongs to.

    The single family lookup shared by the hook path (native subagent
    spawns) and the omnigent child-session path, so "in-family" means the
    same thing for both.

    :param harness: Harness id, e.g. ``"codex-native"``. ``None`` or the
        unresolved ``"auto"`` sentinel carry no family.
    :returns: ``"claude"``, ``"gpt"``, ``"pi"``, or ``None`` when the
        harness is unknown / multi-family.
    """
    if harness is None or harness == "auto":
        return None
    return _harness_family(harness)


def auto_harness_session(conv: Any, parent: Any = None) -> bool:
    """Report whether a session may cross harness families.

    True only for a session started in Smart Routing (auto) harness mode,
    or a child of one: those are the sessions whose harness the router
    owns. Everyone else is pinned to the family they started on, so a
    codex session never gets Claude children and vice versa.

    :param conv: Conversation row for the session, or ``None``.
    :param parent: Conversation row for its parent, when known. ``None``
        for a top-level session or when the parent was not loaded.
    :returns: ``True`` when cross-family picks are allowed.
    """
    for row in (conv, parent):
        if row is None:
            continue
        if getattr(row, "labels", None) and row.labels.get(AUTO_HARNESS_LABEL_KEY) == "1":
            return True
        if getattr(row, "harness_override", None) == "auto":
            return True
    return False


def model_in_family(family: str | None, model: str) -> bool:
    """Report whether *model* can run on a harness in *family*.

    The ``"gpt"`` family means codex-compatible, not literally GPT: it
    admits the GLM and Kimi ids codex serves over the same Responses wire.

    :param family: Family from :func:`harness_family`. ``None`` (unknown
        or multi-family harness) accepts every model.
    :param model: Model id, e.g. ``"databricks-gpt-5-5"``.
    :returns: ``True`` when the pairing is servable.
    """
    if family is None or family == "pi":
        return True
    from omnigent.model_catalog import model_family_token

    token = model_family_token(model)
    return token == "claude" if family == "claude" else token == "openai"


def candidate_models(
    harness: str,
    *,
    cross_harness: bool = False,
    catalog: Mapping[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Build the harness → models map offered to the router.

    In-family by default: a Claude Code session must not be told to move a
    subagent to Codex, so only the requesting harness's models are offered
    and the router's scenario stays that one family. Auto-harness sessions
    already let the router pick the family, so they also get the
    cross-harness counterpart — the only case where a ``redirect`` verdict
    can fire.

    :param harness: Requesting harness id, e.g. ``"codex-native"``.
    :param cross_harness: ``True`` to also offer the counterpart family.
    :param catalog: Live per-session model catalog keyed by worker name
        (:func:`omnigent.server.smart_routing.fetch_runner_models`). Preferred
        over the static table so a model generation the workspace serves today
        isn't treated as unservable; the static table fills any harness the
        catalog has no row for.
    :returns: Harness → model ids, cheapest first, empty entries dropped.
    """
    from omnigent.server.smart_routing import catalog_models_for_harness, infer_models

    offered = (harness, _COUNTERPART_HARNESS.get(harness)) if cross_harness else (harness,)
    result: dict[str, list[str]] = {}
    for candidate in offered:
        if candidate is None or candidate in result:
            continue
        models = catalog_models_for_harness(
            catalog, candidate, allow_self=candidate == harness
        ) or infer_models(candidate)
        # A catalog row can hold models the harness cannot speak (a codex
        # ``"self"`` row lists Claude ids too), which earn a hard
        # ``model_family_mismatch`` at dispatch.
        family = harness_family(candidate)
        models = [m for m in models if model_in_family(family, m)]
        if models:
            result[candidate] = list(models)
    return result


def _routing_task(req: SubagentRouteRequest) -> str | None:
    """Return the task text the router should score.

    :param req: Spawn request.
    :returns: Task text, or ``None`` when the spawn carries no signal of
        its own to score.
    """
    if req.prompt:
        return req.prompt[:_PROMPT_CAP]
    # Codex encrypts the spawn message, so ``task_name`` is the only signal.
    if req.task_name:
        return req.task_name[:_PROMPT_CAP]
    return None


def _unavailable_decision(reason: str) -> SubagentRouteDecision:
    """Allow the spawn unchanged and say why nothing was routed."""
    return SubagentRouteDecision(
        action="allow",
        rationale=f"Routing unavailable ({reason}); spawn allowed unchanged",
    )


def _target_harness(
    req_harness: str, picked_harness: str | None, picked_family: str | None
) -> str:
    """Name the harness a picked model should run on."""
    if picked_family is not None and picked_family == _harness_family(req_harness):
        return req_harness
    counterpart = _COUNTERPART_HARNESS.get(req_harness)
    if counterpart is not None and _harness_family(counterpart) == picked_family:
        return counterpart
    return picked_harness or counterpart or req_harness


async def resolve_subagent_route(
    session_id: str,
    req: SubagentRouteRequest,
    *,
    caps: Any = None,
    available_models: dict[str, list[str]] | None = None,
    catalog: Mapping[str, list[str]] | None = None,
    cross_harness: bool = False,
    persist: Callable[[RoutingDecisionData], Awaitable[None]] | None = None,
) -> SubagentRouteDecision:
    """Decide what happens to one native-subagent spawn.

    :param session_id: Parent session/conversation identifier.
    :param req: The spawn awaiting a verdict.
    :param caps: ``RuntimeCaps``-shaped object. ``None`` reads the
        process-global caps.
    :param available_models: Candidate harness → models map. ``None``
        derives it from the requesting harness.
    :param catalog: Live per-session model catalog, preferred over the
        static table when deriving candidates. Ignored when
        *available_models* is given.
    :param cross_harness: ``True`` when the session may move a subagent to
        the counterpart harness family (auto-harness sessions only).
        Ignored when *available_models* is given.
    :param persist: Coroutine that records the decision in the
        transcript. ``None`` skips persistence (unit tests, dry runs).
    :returns: The verdict the hook script enforces.
    """
    if caps is None:
        from omnigent.runtime import get_caps

        caps = get_caps()

    decision = await _decide(session_id, req, caps, available_models, catalog, cross_harness)
    # ``task_name`` is deliberately not reported: it is free-form text the
    # calling agent wrote, so it stays in the transcript only.
    record_routing_decision(
        session_id,
        scope=_SCOPE,
        harness=decision.harness or req.harness,
        action=decision.action,
        applied=decision.action in ("rewrite", "redirect"),
        model=decision.model,
        raw_model=decision.raw_model,
        # The spawn hook does not report the model the calling agent asked
        # for, so an override cannot be detected on this path.
        overrode_agent_model=False,
        decision_id=decision.decision_id,
    )
    if persist is not None:
        try:
            await persist(decision_record(req, decision))
        except Exception:
            _logger.exception("route-subagent: decision persist failed for session=%s", session_id)
    return decision


async def _decide(
    session_id: str,
    req: SubagentRouteRequest,
    caps: Any,
    available_models: dict[str, list[str]] | None,
    catalog: Mapping[str, list[str]] | None,
    cross_harness: bool,
) -> SubagentRouteDecision:
    if req.fork:
        return SubagentRouteDecision(
            action="allow",
            rationale="Fork keeps the parent's model; forks are not routed",
            model=req.parent_model,
        )

    task = _routing_task(req)
    if task is None:
        # Codex encrypts the spawn message, so an unnamed codex subagent
        # carries nothing to score. Calling the router with an empty task
        # earns an "HTTP 400: task.prompt is required" that reads on the chip
        # as an outage; this is a deliberate verdict, so say so. Naming the
        # parent model also keeps the SubagentStart audit from flagging it.
        return SubagentRouteDecision(
            action="allow",
            rationale=(
                "No routable signal (encrypted prompt, no task name); "
                "subagent inherits the session model"
            ),
            model=req.parent_model,
        )

    client = getattr(caps, "routing_client", None)
    if client is None:
        return _unavailable_decision("no routing client configured")

    candidates = (
        available_models
        if available_models is not None
        else candidate_models(req.harness, cross_harness=cross_harness, catalog=catalog)
    )
    if not candidates:
        return _unavailable_decision(f"no candidate models for harness {req.harness}")

    try:
        result = await client.route(task, candidates)
    except Exception:  # noqa: BLE001 — router outages are a normal path here
        _logger.warning(
            "route-subagent: router call failed for session=%s", session_id, exc_info=True
        )
        result = None
    if result is None or not getattr(result, "model", None):
        detail = _opt_str(getattr(client, "last_error", None)) or "router returned no verdict"
        return _unavailable_decision(detail)

    return _decision_from_result(req, result, candidates)


def _decision_from_result(
    req: SubagentRouteRequest,
    result: Any,
    candidates: Mapping[str, list[str]],
) -> SubagentRouteDecision:
    model = result.model
    rationale = getattr(result, "rationale", "") or ""
    # Only report a raw pick that actually differs from the resolved model, so
    # the telemetry field means "the router asked for something else". A
    # prefix-only spelling difference is the same arm, not a substitution.
    from omnigent.server.smart_routing import _bare_id

    raw = _opt_str(getattr(result, "raw_model", None))
    raw_model = raw if raw and _bare_id(raw) != _bare_id(model) else None
    offered = {m for models in candidates.values() for m in models}
    if offered and model not in offered:
        # Never let an unoffered pick through: "didn't spawn" beats "wrong model".
        return SubagentRouteDecision(
            action="deny",
            rationale=f"Router picked {model}, which this harness cannot run",
            raw_model=raw_model or model,
        )

    picked_harness = _opt_str(getattr(result, "harness", None))
    family = _harness_family(picked_harness) if picked_harness else None
    if family is None:
        for harness_id, models in candidates.items():
            if model in models:
                family = _harness_family(harness_id)
                break
    target = _target_harness(req.harness, picked_harness, family)

    if target == req.harness:
        if req.parent_model is not None and model == req.parent_model:
            return SubagentRouteDecision(
                action="allow",
                rationale=rationale or "Router kept the parent model",
                model=model,
                raw_model=raw_model,
            )
        return SubagentRouteDecision(
            action="rewrite",
            rationale=rationale or f"Router selected {model}",
            model=model,
            raw_model=raw_model,
        )
    return SubagentRouteDecision(
        action="redirect",
        rationale=rationale or f"Router selected {target}/{model}",
        model=model,
        harness=target,
        raw_model=raw_model,
    )


# ── Transcript persistence ─────────────────────────────────────────────────


async def persist_subagent_decision(
    session_id: str,
    conversation_store: Any,
    record: RoutingDecisionData,
) -> None:
    """Persist and publish *record* as a ``routing_decision`` item.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store exposing ``append``.
    :param record: Decision payload.
    """
    from omnigent.entities.conversation import NewConversationItem
    from omnigent.runtime import session_stream

    item_data = record.model_dump()
    item = NewConversationItem(
        type="routing_decision",
        response_id=f"routing_{uuid.uuid4().hex}",
        data=record,
    )
    try:
        persisted = await asyncio.to_thread(conversation_store.append, session_id, [item])
        persisted_id: str | None = persisted[0].id if persisted else None
    except Exception:
        _logger.exception(
            "route-subagent: routing_decision persist failed for session=%s", session_id
        )
        persisted_id = None

    session_stream.publish(
        session_id,
        {
            "type": "response.output_item.done",
            "item": {"id": persisted_id, "type": "routing_decision", **item_data},
        },
    )


def store_persister(
    session_id: str,
    conversation_store: Any,
) -> Callable[[RoutingDecisionData], Awaitable[None]]:
    """Bind :func:`persist_subagent_decision` to a session and store.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store exposing ``append``.
    :returns: Coroutine function accepting a record.
    """

    async def _persist(record: RoutingDecisionData) -> None:
        await persist_subagent_decision(session_id, conversation_store, record)

    return _persist


# ── Runner-side loopback relay ─────────────────────────────────────────────


def write_advertisement(
    bridge_dir: Path,
    *,
    url: str,
    token: str,
    session_id: str | None = None,
) -> Path:
    """Advertise the endpoint to hook scripts.

    :param bridge_dir: Session bridge directory.
    :param url: Endpoint base URL, e.g. ``"http://127.0.0.1:53421"``.
    :param token: Bearer token hook scripts must present.
    :param session_id: Session the endpoint serves. Included so a hook
        with no session env var of its own still knows which session to
        route for.
    :returns: Path of the written ``subagent_router.json``.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / ADVERTISEMENT_FILE
    payload: dict[str, Any] = {
        "url": url,
        "token": token,
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    if session_id is not None:
        payload["session_id"] = session_id
    # The file carries a bearer token, so it is never world-readable — not even
    # for the instant between the write and a follow-up chmod. A unique temp
    # name also keeps two concurrent re-advertisements from clobbering each
    # other's partial file.
    fd, tmp_name = tempfile.mkstemp(dir=bridge_dir, prefix=f"{ADVERTISEMENT_FILE}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


@dataclass
class SubagentRouter:
    """Handle for the running loopback router.

    :param bridge_dir: Bridge dir holding the advertisement file.
    :param url: Base URL the hook scripts POST to.
    :param token: Bearer token hook scripts present.
    :param httpd: The backing HTTP server.
    """

    bridge_dir: Path
    url: str
    token: str
    httpd: ThreadingHTTPServer
    advertised_dirs: set[Path] = field(default_factory=set)
    _closed: bool = False

    def advertise(self, bridge_dir: Path, session_id: str | None = None) -> Path:
        """Write this router's advertisement into *bridge_dir* and track it.

        :param bridge_dir: Directory to advertise in.
        :param session_id: Session the endpoint serves.
        :returns: Path of the written advertisement.
        """
        path = write_advertisement(
            bridge_dir, url=self.url, token=self.token, session_id=session_id
        )
        self.advertised_dirs.add(bridge_dir)
        return path

    def close(self) -> None:
        """Stop the server and remove the advertisements it still owns.

        Idempotent and safe to call from any thread, so a session-close path
        can call it without knowing whether an earlier one already did.
        Sessions that fork/clear/resume keep the same bridge dir, so a newer
        router may have overwritten an advertisement with its own url;
        unlinking unconditionally would kill that live router's routing. Only
        files still naming this router's url are removed.
        """
        if self._closed:
            return
        self._closed = True
        self.httpd.shutdown()
        self.httpd.server_close()
        for bridge_dir in (*self.advertised_dirs, self.bridge_dir):
            path = bridge_dir / ADVERTISEMENT_FILE
            try:
                advertised = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(advertised, dict) and advertised.get("url") == self.url:
                with contextlib.suppress(OSError):
                    path.unlink()


def start_subagent_router(
    *,
    bridge_dir: Path,
    session_id: str,
    resolver: Resolver,
    loop: asyncio.AbstractEventLoop,
    request_timeout_s: float = RELAY_TIMEOUT_S,
) -> SubagentRouter:
    """Start the loopback router and advertise it in *bridge_dir*.

    :param bridge_dir: Session bridge directory.
    :param session_id: Session this router serves; requests for any
        other session id are rejected.
    :param resolver: Coroutine function returning a decision.
    :param loop: Event loop that owns *resolver*.
    :param request_timeout_s: Seconds to wait for a verdict before allowing
        the spawn unchanged. Hop 3 of the module's timeout budget.
    :returns: Started router handle; call :meth:`SubagentRouter.close`
        when the session ends.
    """
    token = secrets.token_urlsafe(32)
    handler_cls = _handler_factory(session_id, token, resolver, loop, request_timeout_s)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    # Handler threads must not hold up ``close()``: the default joins every
    # in-flight request (up to the relay timeout), which stalls session
    # teardown and widens the window for a relaunch to race the shutdown.
    httpd.daemon_threads = True
    host, port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{port}"
    router = SubagentRouter(bridge_dir=bridge_dir, url=url, token=token, httpd=httpd)
    router.advertise(bridge_dir, session_id)
    threading.Thread(
        target=httpd.serve_forever,
        name="omnigent-subagent-router",
        daemon=True,
    ).start()
    return router


def _handler_factory(
    session_id: str,
    token: str,
    resolver: Resolver,
    loop: asyncio.AbstractEventLoop,
    request_timeout_s: float,
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class for one session's router."""
    expected_path = ROUTE_PATH_TEMPLATE.format(session_id=session_id)
    # Compared as bytes: ``compare_digest`` raises TypeError on a non-ASCII
    # str, and http.server decodes headers as latin-1, so a request with a
    # non-ASCII Authorization byte would crash the handler instead of 401ing.
    expected_auth = f"Bearer {token}".encode()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            presented = (self.headers.get("Authorization") or "").encode("latin-1", "replace")
            if not secrets.compare_digest(presented, expected_auth):
                self._reject(401, "unauthorized")
                return
            if self.path.rstrip("/") != expected_path:
                self._reject(404, "not found")
                return
            try:
                payload = json.loads(self._read_body() or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                req = SubagentRouteRequest.from_payload(payload)
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                future = asyncio.run_coroutine_threadsafe(resolver(session_id, req), loop)
                decision = future.result(timeout=request_timeout_s)
            except Exception:  # noqa: BLE001 — never wedge the spawn path
                _logger.warning(
                    "route-subagent: resolver failed for session=%s", session_id, exc_info=True
                )
                decision = _unavailable_decision("routing endpoint failed")
            self._send(200, decision.to_payload())

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _reject(self, status: int, error: str) -> None:
            # Keep-alive is on (HTTP/1.1), so an undrained body would be parsed
            # as the next request's start line. Drain, then close the connection.
            with contextlib.suppress(OSError, ValueError):
                self._read_body()
            self.close_connection = True
            self._send(status, {"error": error}, close=True)

        def _send(self, status: int, body: dict[str, Any], *, close: bool = False) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            _logger.debug("subagent-router: " + format, *args)

    return _Handler


def make_server_relay_resolver(
    server_client: Any,
    *,
    timeout_s: float = SERVER_HOP_TIMEOUT_S,
) -> Resolver:
    """Build a resolver that forwards to the server's relay route.

    :param server_client: Async HTTP client pointed at the AP server.
    :param timeout_s: Per-request timeout in seconds. Hop 4 (innermost) of
        the module's timeout budget.
    :returns: Resolver for :func:`start_subagent_router`.
    """

    async def _resolve(session_id: str, req: SubagentRouteRequest) -> SubagentRouteDecision:
        body = {
            "harness": req.harness,
            "task_name": req.task_name,
            "prompt": req.prompt,
            "fork": req.fork,
            "parent_model": req.parent_model,
        }
        try:
            resp = await server_client.post(
                SERVER_ROUTE_PATH.format(session_id=session_id),
                json=body,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:  # noqa: BLE001 — server hop failures are expected
            _logger.warning(
                "route-subagent: server relay failed for session=%s", session_id, exc_info=True
            )
            return _unavailable_decision("routing server unreachable")
        if not isinstance(payload, dict):
            return _unavailable_decision("unreadable verdict from routing server")
        return SubagentRouteDecision.from_payload(payload)

    return _resolve


# ── Per-session router lifecycle (runner side) ─────────────────────────────

_session_routers: dict[str, SubagentRouter] = {}
# Models the relay actually handed back, per session — the ledger the
# codex ``SubagentStart`` audit is reconciled against.
_relayed: dict[str, list[dict[str, Any]]] = {}
_lifecycle_lock = threading.Lock()


def relayed_decisions(session_id: str) -> tuple[dict[str, Any], ...]:
    """Return the verdicts this runner relayed for *session_id*.

    :param session_id: Session/conversation identifier.
    :returns: Records ``{decision_id, action, model, harness, task_name}``,
        oldest first.
    """
    with _lifecycle_lock:
        return tuple(_relayed.get(session_id, ()))


def routed_models(session_id: str) -> frozenset[str]:
    """Return every model the router approved for *session_id*.

    Session-wide view of the ledger; the codex spawn audit reconciles
    against :func:`relayed_decisions` instead, so it can join per spawn.

    :param session_id: Session/conversation identifier.
    :returns: Approved model ids; empty when nothing was routed.
    """
    return frozenset(
        str(record["model"])
        for record in relayed_decisions(session_id)
        if record.get("model") and record.get("action") in ("rewrite", "allow")
    )


def _recording_resolver(resolver: Resolver) -> Resolver:
    """Wrap *resolver* so each verdict lands in the reconciliation ledger."""

    async def _resolve(session_id: str, req: SubagentRouteRequest) -> SubagentRouteDecision:
        decision = await resolver(session_id, req)
        with _lifecycle_lock:
            ledger = _relayed.setdefault(session_id, [])
            ledger.append(
                {
                    "decision_id": decision.decision_id,
                    "action": decision.action,
                    "model": decision.model,
                    "harness": decision.harness or req.harness,
                    "task_name": req.task_name[:_TASK_NAME_CAP],
                }
            )
            if len(ledger) > _RELAYED_CAP:
                del ledger[: len(ledger) - _RELAYED_CAP]
        return decision

    return _resolve


def ensure_session_router(
    session_id: str,
    *,
    bridge_dir: Path,
    server_client: httpx.AsyncClient,
    loop: asyncio.AbstractEventLoop | None = None,
) -> SubagentRouter:
    """Start (once) the loopback router serving *session_id*.

    Idempotent: a second call for a session already served returns the
    running router after re-asserting its advertisement, so a resumed or
    re-launched terminal finds the file the hook scripts read. The whole
    start runs under ``_lifecycle_lock`` — releasing it between the lookup
    and the insert let two concurrent launches each bind a socket, and the
    loser was never closed.

    :param session_id: Session/conversation identifier.
    :param bridge_dir: Directory the advertisement is written into — the
        same directory the harness's hooks are pointed at.
    :param server_client: Runner→server client used to relay verdicts.
    :param loop: Event loop owning the relay. ``None`` uses the running
        loop.
    :returns: The running router handle.
    """
    resolver_loop = loop if loop is not None else asyncio.get_running_loop()
    with _lifecycle_lock:
        existing = _session_routers.get(session_id)
        if existing is not None:
            existing.advertise(bridge_dir, session_id)
            return existing
        router = start_subagent_router(
            bridge_dir=bridge_dir,
            session_id=session_id,
            resolver=_recording_resolver(make_server_relay_resolver(server_client)),
            loop=resolver_loop,
        )
        _session_routers[session_id] = router
    _logger.info(
        "subagent router started: session=%s url=%s dir=%s", session_id, router.url, bridge_dir
    )
    return router


def ensure_session_router_quietly(
    session_id: str,
    *,
    bridge_dir: Path | None = None,
    server_client: httpx.AsyncClient | None,
    harness: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    caps: Any = None,
) -> SubagentRouter | None:
    """Start the session's router, or return ``None`` instead of failing.

    The launch-site wrapper: a session with no server client has nowhere to
    relay verdicts to, and neither a router that cannot bind a socket nor a
    bridge root that fails its ownership check may take the harness launch
    down with it. Only the harnesses whose spawn hooks read the
    advertisement get a router at all — every other harness (pi, copilot,
    goose, …) is handed no router env vars, so starting one for it would
    only risk failing its session init.

    :param session_id: Session/conversation identifier.
    :param bridge_dir: Directory the advertisement is written into.
        ``None`` resolves the session's private router dir, inside the
        failure guard.
    :param server_client: Runner→server client. ``None`` skips the router.
    :param harness: Harness being launched; gates the start and is logged
        on failure.
    :param loop: Event loop owning the relay. ``None`` uses the running
        loop.
    :param caps: Accepted and ignored; kept so launch sites can pass the
        ``RuntimeCaps`` they already hold.
    :returns: The running router handle, or ``None`` when it could not
        start.
    """
    del caps
    if server_client is None:
        return None
    if harness not in _CLAUDE_HOOK_HARNESSES and harness not in _CODEX_HOOK_HARNESSES:
        return None
    try:
        return ensure_session_router(
            session_id,
            bridge_dir=bridge_dir
            if bridge_dir is not None
            else router_dir_for_session(session_id),
            server_client=server_client,
            loop=loop,
        )
    except (OSError, RuntimeError):
        # ``router_dir_for_session`` raises RuntimeError when the shared
        # bridge root fails its ownership check — never a reason to fail
        # session creation for a harness that may not even use routing.
        _logger.warning(
            "subagent router could not start for session=%s harness=%s",
            session_id,
            harness,
            exc_info=True,
        )
        return None


def shutdown_session_router(session_id: str, router: SubagentRouter | None = None) -> None:
    """Stop the router serving *session_id* and forget its state.

    Cheap and safe to call for a session that never had a router, and safe
    to call twice — :meth:`SubagentRouter.close` is idempotent — so every
    teardown path can call it unconditionally.

    :param session_id: Session/conversation identifier.
    :param router: The handle the caller started. When given, the teardown
        is identity-scoped: a delayed teardown from a previous launch of
        the same session must not close the router a re-create has since
        installed, which would leave routing silently dead behind a
        still-valid advertisement. ``None`` tears down whatever is
        registered (session-delete paths that hold no handle).
    """
    with _lifecycle_lock:
        current = _session_routers.get(session_id)
        if router is not None and current is not router:
            return
        _session_routers.pop(session_id, None)
        _relayed.pop(session_id, None)
    if current is None:
        return
    router = current
    with contextlib.suppress(Exception):
        router.close()
    _prune_router_dirs(router)


def _prune_router_dirs(router: SubagentRouter) -> None:
    """Remove the router's own (now empty) advertisement dirs.

    Only dirs under the subagent-router root: the native harnesses' bridge
    dirs are advertised in too and belong to the bridge, not to this router.
    """
    from omnigent.claude_native_bridge import subagent_router_bridge_root

    root = subagent_router_bridge_root()
    for bridge_dir in (*router.advertised_dirs, router.bridge_dir):
        # Strictly below the root: the root itself is shared by every session.
        if bridge_dir != root and bridge_dir.is_relative_to(root):
            with contextlib.suppress(OSError):
                bridge_dir.rmdir()


def router_dir_for_session(session_id: str) -> Path:
    """Return the advertisement directory for a session with no bridge dir.

    The SDK harnesses (claude-agent-sdk, codex app-server) have no bridge
    directory of their own, so the router gets a private owner-only one
    beside the native bridges. Created through the shared bridge-dir
    hardening: the advertisement holds a bearer token, and
    ``mkdir(mode=0o700, parents=True)`` would trust a pre-created
    world-writable or symlinked ancestor.

    :param session_id: Session/conversation identifier.
    :returns: Created directory, mode ``0o700``.
    :raises RuntimeError: If an ancestor fails the ownership check.
    """
    from omnigent.claude_native_bridge import ensure_secure_dir, subagent_router_bridge_root

    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    path = subagent_router_bridge_root() / digest
    ensure_secure_dir(path)
    return path


def session_router_env(session_id: str, harness: str | None = None) -> dict[str, str]:
    """Return the router env for a session, or ``{}`` when it has no router.

    Read at harness-spawn time so a session whose router was started at
    session init hands every harness process the same rendezvous.

    :param session_id: Session/conversation identifier.
    :param harness: Harness being launched, so only its own vars are set.
    :returns: Env-var overrides, empty when the session has no router.
    """
    with _lifecycle_lock:
        router = _session_routers.get(session_id)
    if router is None:
        return {}
    return router_env(session_id, router.bridge_dir, harness=harness)


def router_env(session_id: str, router_dir: Path, harness: str | None = None) -> dict[str, str]:
    """Build the env that points a harness process at the router.

    Each harness family reads the advertisement out of a directory named in
    its own env var: the claude-agent-sdk executor registers its in-process
    hook from it, and the codex executor uses it as the switch that turns
    generated routing ``hooks.json`` on. Only *harness*'s own vars are set —
    a codex executor spawned beneath a claude session would otherwise see
    the codex vars carrying the parent's session id and route as it.

    :param session_id: Session/conversation identifier.
    :param router_dir: Directory holding ``subagent_router.json``.
    :param harness: Harness being launched. ``None`` sets both families'
        vars, for callers that cannot name the harness.
    :returns: Env-var overrides for the harness process; empty for a
        harness with no routing hooks.
    """
    from omnigent.inner.codex_executor import (
        CODEX_ROUTER_DIR_ENV_VAR,
        CODEX_ROUTER_SESSION_ID_ENV_VAR,
    )
    from omnigent.inner.hook_scripts.subagent_router import (
        ROUTER_DIR_ENV_VAR,
        SESSION_ID_ENV_VAR,
    )

    claude_env = {ROUTER_DIR_ENV_VAR: str(router_dir), SESSION_ID_ENV_VAR: session_id}
    codex_env = {
        CODEX_ROUTER_DIR_ENV_VAR: str(router_dir),
        CODEX_ROUTER_SESSION_ID_ENV_VAR: session_id,
    }
    if harness is None:
        return {**claude_env, **codex_env}
    if harness in _CLAUDE_HOOK_HARNESSES:
        return claude_env
    if harness in _CODEX_HOOK_HARNESSES:
        return codex_env
    return {}
