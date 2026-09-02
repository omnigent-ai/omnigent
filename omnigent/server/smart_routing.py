"""Server-side intelligent model routing.

Infers available models from the session's harness type and delegates
the routing decision to a :class:`RoutingClient`.  Which one is picked
per call by :func:`omnigent.server.routing_backend.select_router`: the
external :class:`ExternalRoutingClient` when the call's harnesses are
AI-Gateway-backed, else the built-in :class:`LLMRoutingClient`, which
calls the server-level LLM with a prompt that describes each model's
capabilities directly — no tier abstraction.  Managed deployments can
swap in a different implementation via ``RuntimeCaps``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from omnigent.model_fallbacks import (
    SMART_ROUTING_CLAUDE_LADDER,
    SMART_ROUTING_CURRENT_GENERATION_GPT,
    SMART_ROUTING_FAMILY_FALLBACKS,
    SMART_ROUTING_GPT_LADDER,
    SMART_ROUTING_PI_EXCLUDED,
    SMART_ROUTING_PI_LADDER,
    SMART_ROUTING_TASK_V1_CLAUDE_ARMS,
    SMART_ROUTING_TASK_V1_CODEX_ARMS,
)
from omnigent.model_metadata import ModelCostTier, ModelIntent, ModelWireAPI

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    import httpx  # used in type annotations only; runtime import is lazy in fetch_runner_models
    from databricks.sdk.config import Config

    from omnigent.reasoning_effort import ModelEffortCaps

_logger = logging.getLogger(__name__)

# Custom-method path (Google API convention) appended to the external
# router's base URL, e.g. ``<base_url>/routes:select``.
ROUTES_SELECT_PATH = "routes:select"

#: Per-call budget for ONE routing request — the external ``routes:select``
#: POST and the built-in judge's own LLM call alike. The innermost number in
#: every routing timeout ladder, so a wedged router degrades to "not routed"
#: before any hop above it gives up on the hook that is holding a prompt or a
#: spawn.
#:
#: Sized from the observed round trip: healthy ``routes:select`` answers land
#: in ~1.4–3s, and the slowest sample on record (~6.7s) was a gateway 500, not
#: a verdict. Nine seconds covers the healthy case with wide headroom while
#: still capping the hazard paths — a first-message hook and a subagent spawn
#: gate — well under the 30–45s a wedged server used to cost.
#:
#: This budget covers the CALL only. The hops above it must also absorb the
#: candidate/catalog preparation that runs before it (~3s measured on a first
#: message), which is why each of them sits several seconds higher rather than
#: one second above this value.
#:
#: One attempt, no retry: on an interactive path a second try only doubles the
#: stall, and falling open onto the harness's own model is the better answer.
ROUTING_REQUEST_TIMEOUT_S = 9.0

# ── Model lists per harness family ──────────────────────────────────────────
#
# Ordered cheapest → most powerful within each family. Live per-session catalogs
# win wherever one is in reach (:func:`fetch_runner_models`); this table is the
# fallback, so a stale entry pushes a router pick through the substitution path.
# The ids themselves are owned records in :mod:`omnigent.model_fallbacks`.

MODEL_LISTS: dict[str, list[str]] = {
    "claude": list(SMART_ROUTING_CLAUDE_LADDER),
    "gpt": list(SMART_ROUTING_GPT_LADDER),
    # pi is multi-model: Claude and GPT both available.
    "pi": list(SMART_ROUTING_PI_LADDER),
}

# The router's own current arms, offered as candidates so a routed arm resolves
# to its own endpoint instead of substituting down a generation (a routed
# gpt-5-6-sol landing on gpt-5-5). Kept out of the curated ordering above,
# which is a cost/capability spread the arms do not belong to.
#
# ``glm-5-2`` is an arm too, and no discovery listing carries it (see
# :data:`_SERVABLE_ALIASES`), so this table is the only place a glm pick can be
# offered from — without it a routed glm substitutes down to the luna fallback.
# It resolves through :func:`apply_servable_alias` to ``system.ai.glm-5-2``.
#
# A workspace-probed fact, so ``routing.current_generation_models`` overrides it
# (:class:`RoutingSettings`) rather than a managed deployment forking this module.
_CURRENT_GENERATION_MODELS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"gpt": SMART_ROUTING_CURRENT_GENERATION_GPT}
)

_HARNESS_FAMILY: dict[str, str] = {
    "claude-sdk": "claude",
    "claude_sdk": "claude",
    "claude-native": "claude",
    "pi": "pi",
    "codex": "gpt",
    "codex-native": "gpt",
    "openai-agents": "gpt",
    "openai-agents-sdk": "gpt",
    "agents_sdk": "gpt",
}


def infer_models(
    harness: str | None,
    *,
    current_generation: Mapping[str, Sequence[str]] | None = None,
) -> list[str] | None:
    """Return available models for *harness*, or ``None`` if unroutable.

    The curated ordering plus the current generation's endpoints
    (:data:`_CURRENT_GENERATION_MODELS`), so a routed current arm resolves to
    its own endpoint rather than substituting down a generation.

    :param current_generation: Family → current-generation endpoints; ``None``
        reads this deployment's configured table.
    """
    if harness is None:
        return None
    family = _HARNESS_FAMILY.get(harness)
    if family is None:
        return None
    curated = MODEL_LISTS.get(family)
    if curated is None:
        return None
    arms = current_generation_models(current_generation)
    return [*curated, *arms.get(family, ())]


def models_in_family(harness: str | None, models: Iterable[str]) -> list[str]:
    """Keep only the models *harness*'s family can actually run.

    :param harness: Harness id, e.g. ``"codex-native"``. Unknown or
        multi-model harnesses (and the unresolved ``"auto"`` sentinel)
        impose no constraint.
    :param models: Candidate model ids, cheapest first.
    :returns: The servable subset, order preserved.
    """
    from omnigent.runner.subagent_routing import harness_family, model_in_family

    family = harness_family(harness)
    if family is None:
        return list(models)
    return [model for model in models if model_in_family(family, model)]


def catalog_models_for_harness(
    catalog: Mapping[str, list[str]] | None,
    harness: str,
    *,
    allow_self: bool = False,
) -> list[str] | None:
    """Pull *harness*'s models out of a live runner catalog.

    Catalog rows are keyed by WORKER name (sub-agent names plus ``"self"``),
    not harness id, so match a harness id first, then a worker whose harness
    shares the family, then — only for the session's own harness — the
    ``"self"`` row.

    :param catalog: :func:`fetch_runner_models` result, or ``None``.
    :param harness: Harness id whose models are wanted, e.g.
        ``"claude-native"``.
    :param allow_self: ``True`` when *harness* is the catalog session's own
        harness, so its ``"self"`` row applies.
    :returns: Servable model ids, or ``None`` when the catalog has no row
        for this harness.
    """
    if not catalog:
        return None
    own = catalog.get(harness)
    if own:
        return list(own)
    family = _HARNESS_FAMILY.get(harness)
    for worker, worker_models in catalog.items():
        if not worker_models:
            continue
        worker_harness = _WORKER_NAME_TO_HARNESS.get(worker)
        if worker_harness is None:
            continue
        if worker_harness == harness or (
            family is not None and _HARNESS_FAMILY.get(worker_harness) == family
        ):
            return list(worker_models)
    if allow_self:
        own_models = catalog.get("self")
        if own_models:
            return list(own_models)
    return None


def failure_detail(exc: BaseException) -> str:
    """Describe *exc* for a routing decline the user reads.

    ``str(exc)`` is empty for the exceptions routing hits most — httpx's
    timeouts carry no message — which rendered as a dangling "router request
    failed: " with the cause missing. The class name is the cause in that case:
    ``ReadTimeout`` says what happened, and a timeout is exactly what a
    fail-open budget produces.

    :param exc: The exception a routing call raised.
    :returns: A non-empty description, e.g. ``"ReadTimeout"`` or
        ``"[Errno 61] Connection refused"``.
    """
    text = str(exc).strip()
    return text or type(exc).__name__


# ── RoutingClient protocol ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingResult:
    """The routing client's recommendation.

    :param model: Model id to use, e.g. ``"databricks-claude-opus-4-8"``.
    :param rationale: One-sentence explanation from the judge.
    :param harness: The harness the judge selected, e.g. ``"claude-sdk"``.
        ``None`` when the routing client does not distinguish harnesses (e.g.
        single-harness calls or custom implementations that omit it).
    :param raw_model: The router's pick in its own vocabulary, before it was
        resolved to a servable catalog id (they differ when the router names an
        arm this workspace has no endpoint for). ``None`` when the two are the
        same or the client does not distinguish them.
    """

    model: str
    rationale: str
    harness: str | None = None
    raw_model: str | None = None


class RoutingClient(Protocol):
    """Protocol for pluggable model routing implementations.

    An implementation may also expose a ``last_error`` string with the reason
    its most recent :meth:`route` returned ``None``; callers read it through
    :func:`routing_last_error`, which tolerates its absence.
    """

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> RoutingResult | None:
        """Pick the best model for a session's initial message.

        :param message: The user's first message text.
        :param available_models: Mapping of harness → model ids, each list
            ordered cheapest → most powerful.  A single-harness call passes
            a one-entry dict; multi-agent fan-out passes one entry per
            harness.  Implementations that only need the flat model list can
            call :func:`_flatten_models` to get a deduped ordered sequence.
        :returns: A :class:`RoutingResult`, or ``None`` to skip routing.
        """
        ...


# ── Helpers ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RunnerModel:
    """Model metadata retained from the runner catalog for routing decisions."""

    id: str
    family: str | None
    cost_tier: str | None
    wire_apis: frozenset[ModelWireAPI]


def _catalog_wire_apis(raw: object) -> frozenset[ModelWireAPI]:
    """Parse known wire values while ignoring older or future vocabulary."""
    if not isinstance(raw, list):
        return frozenset()
    wire_apis: set[ModelWireAPI] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        try:
            wire_apis.add(ModelWireAPI(value))
        except ValueError:
            continue
    return frozenset(wire_apis)


#: How long a fetched runner catalog stays servable without a re-fetch.
#:
#: The catalog is the installed CLI's model list: it changes on a relaunch or a
#: provider-config edit, both of which invalidate this cache explicitly
#: (:func:`invalidate_runner_catalog`). The TTL is only the backstop for a
#: change nothing announced, so it is sized for "a long session should not go
#: stale for hours", not for freshness per turn.
RUNNER_CATALOG_TTL_S = 300.0

#: session id → (monotonic deadline, catalog). Process-local, like every other
#: runner-derived overlay cache on the server.
_runner_catalog_cache: dict[str, tuple[float, dict[str, list[_RunnerModel]]]] = {}

#: Single-flight per session, so a burst of turns costs one runner round trip.
_runner_catalog_inflight: dict[str, asyncio.Task[dict[str, list[_RunnerModel]] | None]] = {}


def invalidate_runner_catalog(session_id: str | None = None) -> None:
    """Drop the cached runner catalog for *session_id* (or every session).

    Called from the seam that already invalidates runner-derived snapshot
    overlays, so a relaunched runner, a rebind, or a refreshed snapshot all
    re-read the catalog instead of routing off the previous process's models.

    :param session_id: Session/conversation identifier; ``None`` clears all.
    """
    if session_id is None:
        _runner_catalog_cache.clear()
        return
    _runner_catalog_cache.pop(session_id, None)


async def prefetch_runner_catalog(
    session_id: str,
    runner_client: httpx.AsyncClient,
) -> None:
    """Warm the catalog cache for *session_id* off the turn path.

    Routing's candidate preparation is the runner round trip below, and paying
    it while a user's prompt is held is what made the first message slow. A
    launch-time call fills the cache so the first turn reads it instead.

    :param session_id: Session/conversation identifier.
    :param runner_client: Async HTTP client pointed at the runner.
    """
    await _fetch_runner_catalog(session_id, runner_client)


async def _fetch_runner_catalog(
    session_id: str,
    runner_client: httpx.AsyncClient,
) -> dict[str, list[_RunnerModel]] | None:
    """Return this session's runner catalog, from cache when one is warm.

    A miss runs :func:`_load_runner_catalog` under a per-session single flight,
    so concurrent turns share one round trip. Only a parsed, non-empty catalog
    is cached: an unreachable runner (a booting session) must stay re-fetchable
    rather than pin "no catalog" for the whole TTL.

    :param session_id: Session/conversation identifier.
    :param runner_client: Async HTTP client pointed at the runner.
    :returns: Worker names mapped to ordered model metadata, or ``None``.
    """
    cached = _runner_catalog_cache.get(session_id)
    if cached is not None:
        deadline, catalog = cached
        if time.monotonic() < deadline:
            return catalog
        _runner_catalog_cache.pop(session_id, None)
    inflight = _runner_catalog_inflight.get(session_id)
    if inflight is None:
        inflight = asyncio.ensure_future(_load_runner_catalog(session_id, runner_client))
        _runner_catalog_inflight[session_id] = inflight
        inflight.add_done_callback(
            lambda _task, sid=session_id: _runner_catalog_inflight.pop(sid, None)
        )
    catalog = await asyncio.shield(inflight)
    if catalog:
        _runner_catalog_cache[session_id] = (time.monotonic() + RUNNER_CATALOG_TTL_S, catalog)
    return catalog


async def _load_runner_catalog(
    session_id: str,
    runner_client: httpx.AsyncClient,
) -> dict[str, list[_RunnerModel]] | None:
    """Fetch live model availability from the runner's ``/v1/sessions/{id}/models`` endpoint.

    Parses the ``sys_list_models``-shaped catalog while retaining compatibility
    metadata. Models with provider-relative cost metadata are ordered economy →
    standard → premium; catalog order remains the deterministic tie-breaker.

    :param session_id: Session/conversation identifier.
    :param runner_client: Async HTTP client pointed at the runner.
    :returns: Worker names mapped to ordered model metadata, or ``None`` when
        the endpoint is unavailable or the response cannot be parsed.
    """
    import httpx as _httpx

    try:
        resp = await runner_client.get(f"/v1/sessions/{session_id}/models", timeout=5.0)
        resp.raise_for_status()
        payload = resp.json()
    except (_httpx.HTTPError, ValueError, KeyError):
        _logger.debug(
            "fetch_runner_models: runner request failed for session=%s", session_id, exc_info=True
        )
        return None

    workers: dict[str, Any] = payload.get("workers", {})
    if not workers:
        return None

    result: dict[str, list[_RunnerModel]] = {}
    for worker_name, row in workers.items():
        if not isinstance(row, dict):
            continue
        models_raw = row.get("models", [])
        if not isinstance(models_raw, list):
            continue
        cost_order = {
            ModelCostTier.ECONOMY.value: 0,
            ModelCostTier.STANDARD.value: 1,
            ModelCostTier.PREMIUM.value: 2,
        }
        indexed_models: list[tuple[int, _RunnerModel]] = []
        for index, model in enumerate(models_raw):
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                continue
            indexed_models.append(
                (
                    index,
                    _RunnerModel(
                        id=model["id"],
                        family=model.get("family")
                        if isinstance(model.get("family"), str)
                        else None,
                        cost_tier=(
                            model.get("cost_tier")
                            if isinstance(model.get("cost_tier"), str)
                            else None
                        ),
                        wire_apis=_catalog_wire_apis(model.get("wire_apis")),
                    ),
                )
            )
        ordered_models = sorted(
            indexed_models,
            key=lambda item: (
                cost_order.get(item[1].cost_tier or "", len(cost_order)),
                item[0],
            ),
        )
        models = [model for _, model in ordered_models]
        if models:
            result[worker_name] = models
    return result or None


async def fetch_runner_models(
    session_id: str,
    runner_client: httpx.AsyncClient,
) -> dict[str, list[str]] | None:
    """Return the runner catalog in the id-only routing-client shape."""
    catalog = await _fetch_runner_catalog(session_id, runner_client)
    if catalog is None:
        return None
    return {worker: [model.id for model in models] for worker, models in catalog.items()}


def _flatten_models(available_models: dict[str, list[str]]) -> list[str]:
    """Return a deduped, ordered flat model list from a harness → models map.

    Iterates harness entries in insertion order; within each harness the
    model list is already cheapest → most powerful.  Duplicates (a model
    supported by multiple harnesses) are dropped on second occurrence so
    the first-harness ordering is preserved.
    """
    seen: set[str] = set()
    result: list[str] = []
    for models in available_models.values():
        for m in models:
            if m not in seen:
                seen.add(m)
                result.append(m)
    return result


# ── Default LLM-based implementation ───────────────────────────────────────

_JUDGE_SYSTEM_TEMPLATE = """\
You are a model router for a coding assistant. Given the user's message,
pick the harness and model best suited for the task.

Available harnesses and their models:
{harness_menu}

Harness descriptions:
- claude-sdk / claude-native: Claude Code harness; best for multi-file
  refactors, test writing, and deep reasoning chains.
- codex / codex-native: Codex harness; best for narrow, well-scoped
  code changes.
- pi: Multi-model headless harness; can run both Claude and GPT models;
  best for read-only exploration, review, and cross-vendor verification.

Each harness list is ordered by provider-relative cost when the catalog reports
it, with provider catalog order as the tie-breaker. Classify the task using
stable model intents and choose the corresponding relative position:

  SIMPLE   → {fast_intent}: first available model
             Examples: greetings, quick lookups, one-line fixes, trivial Q&A.

  MODERATE → {balanced_intent}: middle available model
             Examples: single-file edits, debugging a known issue, brief explanations.

  COMPLEX  → {powerful_intent}: last available model
             Examples: multi-file refactors, architecture decisions, security analysis,
             long reasoning chains, tasks requiring high accuracy or broad context.

The rationale field must follow this exact pattern so the explanation is consistent
with the model chosen:
  "This is a [SIMPLE/MODERATE/COMPLEX] task ([brief reason]); \
selected [cheapest/mid-range/most capable] model [model-id]."

Return **strict JSON only**:
{{"harness": "<harness-id>", "model": "<model-id>", "rationale": "<sentence>"}}
"""


def _build_rubric(available_models: dict[str, list[str]]) -> str:
    """Format the judge prompt with the harness → models structure."""
    sections: list[str] = []
    for harness, models in available_models.items():
        model_lines = "\n".join(f"    - {m}" for m in models)
        sections.append(f"  harness: {harness}\n{model_lines}")
    return _JUDGE_SYSTEM_TEMPLATE.format(
        harness_menu="\n".join(sections),
        fast_intent=ModelIntent.FAST.value,
        balanced_intent=ModelIntent.BALANCED.value,
        powerful_intent=ModelIntent.POWERFUL.value,
    )


_VERDICT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "harness": {"type": "string"},
        "model": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["harness", "model", "rationale"],
    "additionalProperties": False,
}


class LLMRoutingClient:
    """Default routing client using the server-level PolicyLLMClient.

    The judge call is capped at :data:`ROUTING_REQUEST_TIMEOUT_S` rather than
    left on the server ``llm:`` block's own ``request_timeout`` (300s by
    default, multiplied by every configured fallback model). A judge that slow
    is not a verdict anybody is still waiting for: the prompt or spawn it
    belongs to has long since needed an answer, so it degrades to "not routed"
    on the same budget the external router gets.
    """

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client
        # Reason the most recent route() returned None; see RoutingClient.
        self.last_error: str | None = None

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> RoutingResult | None:
        import asyncio

        flat = _flatten_models(available_models)
        rubric = _build_rubric(available_models)
        _logger.info(
            "LLMRoutingClient: available_models=%s prompt_chars=%d",
            dict(available_models),
            len(message),
        )
        self.last_error = None
        try:
            # Both bounds are needed: ``timeout`` is the adapter's own per-call
            # HTTP budget, and ``wait_for`` covers everything it does not (a
            # fallback chain, a token refresh, a stalled stream read).
            response = await asyncio.wait_for(
                self._llm.create(
                    instructions=rubric,
                    input=[
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": message[:4000]}],
                        }
                    ],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "routing_verdict",
                            "strict": True,
                            "schema": _VERDICT_SCHEMA,
                        }
                    },
                    timeout=ROUTING_REQUEST_TIMEOUT_S,
                ),
                timeout=ROUTING_REQUEST_TIMEOUT_S,
            )
            text = response.output[0].content[0].text
            # The verdict can echo prompt text in its rationale, so keep it off
            # INFO; the chosen model is logged by the caller either way.
            _logger.debug("LLMRoutingClient: raw response: %s", text[:500])
            verdict = json.loads(text)
        except Exception as exc:  # noqa: BLE001  # fail-open
            _logger.warning("LLMRoutingClient: judge call failed", exc_info=True)
            self.last_error = f"routing judge call failed: {failure_detail(exc)}"
            return None

        model = verdict.get("model")
        rationale = verdict.get("rationale", "")
        if not model or not isinstance(model, str):
            self.last_error = "routing judge returned no model"
            return None

        # Clamp hallucinated models to the cheapest available.
        if model not in flat:
            if flat:
                _logger.info(
                    "LLMRoutingClient: clamping unknown model %r to %s",
                    model,
                    flat[0],
                )
                model = flat[0]
            else:
                self.last_error = "no candidate models were available"
                return None

        # Resolve the harness: use the judge's pick only when it is both a
        # known harness key AND actually contains the chosen model.  If
        # either check fails, fall back to the first harness that owns the
        # (possibly clamped) model.
        chosen_harness = verdict.get("harness")
        if (
            not isinstance(chosen_harness, str)
            or chosen_harness not in available_models
            or model not in available_models[chosen_harness]
        ):
            if isinstance(chosen_harness, str) and chosen_harness in available_models:
                _logger.info(
                    "LLMRoutingClient: harness %r does not contain model %r; re-resolving",
                    chosen_harness,
                    model,
                )
            chosen_harness = next(
                (h for h, models in available_models.items() if model in models),
                None,
            )

        return RoutingResult(model=model, rationale=str(rationale), harness=chosen_harness)


def _bearer_auth(token: str) -> httpx.Auth:
    """Build a static ``Authorization: Bearer <token>`` httpx auth.

    :param token: The bearer token, e.g. a Databricks workspace token.
    :returns: An ``httpx.Auth`` that adds the bearer header to each request.
    """
    import httpx

    class _BearerAuth(httpx.Auth):
        def auth_flow(self, request: httpx.Request):  # type: ignore[no-untyped-def]
            request.headers["Authorization"] = f"Bearer {token}"
            yield request

    return _BearerAuth()


def _config_bearer(
    config: Any,  # type: ignore[explicit-any]  # databricks.sdk.config.Config
    label: str,
) -> Any:  # type: ignore[explicit-any]  # httpx.Auth | None
    """Mint a bearer from a databricks-sdk ``Config``, per call.

    ``authenticate()`` refreshes an expired OAuth token, so calling it on every
    request is what keeps a long-lived server from sending a stale token.

    :param config: A resolved SDK ``Config``.
    :param label: How to name the credential's source in a warning.
    :returns: An httpx auth, or ``None`` when the credential can't be minted
        (auth failure degrades to unauthenticated rather than failing the turn).
    """
    try:
        headers = config.authenticate()
    except Exception:  # noqa: BLE001 — auth failure degrades to unauthenticated
        _logger.warning(
            "ExternalRoutingClient: could not resolve auth from %s", label, exc_info=True
        )
        return None
    token = (dict(headers or {}).get("Authorization") or "").removeprefix("Bearer ").strip()
    if not token:
        return None
    return _bearer_auth(token)


def _same_host(candidate: str | None, url: str) -> bool:
    """Whether *candidate* names the same host as *url*, schemes ignored.

    :param candidate: A workspace host, with or without a scheme.
    :param url: The URL being posted to.
    :returns: ``True`` only when both resolve to a non-empty, equal hostname.
    """
    from urllib.parse import urlsplit

    def _host(value: str) -> str:
        value = value.strip().rstrip("/")
        if "//" not in value:
            value = "//" + value
        return (urlsplit(value).hostname or "").lower()

    if not candidate:
        return False
    return bool(_host(candidate)) and _host(candidate) == _host(url)


def _router_error_detail(body: str) -> str:
    """Extract a clean, short reason from a router error response body.

    The gateway wraps the reason in nested JSON (e.g.
    ``{"error_code": 401, "message": "Credential was not sent ..."}`` or a
    doubly-encoded ``message`` holding another JSON object). Pull out the
    innermost human message when possible; otherwise return a trimmed body.

    :param body: The raw response text.
    :returns: A short reason string suitable for a UI card.
    """
    text = (body or "").strip()
    for _ in range(4):  # unwrap up to a few nested message/error layers
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            break
        if not isinstance(parsed, dict):
            break
        candidate = parsed.get("message")
        if candidate is None:
            candidate = parsed.get("error")
        # A dict ``error`` (e.g. {"error": {"message": ...}}) — descend into it.
        if isinstance(candidate, dict):
            candidate = candidate.get("message")
        if isinstance(candidate, str) and candidate:
            text = candidate.strip()
            continue
        break
    return text[:300]


def router_permanently_disabled(status_code: int, body: str) -> bool:
    """Whether the router's answer reports a condition no retry can clear.

    A workspace without the routing API answers ``routes:select`` with a 404
    saying it is not enabled for the account. That is configuration, not an
    outage: every later call would 404 identically, so the client latches it
    and the deployment's other backend answers instead.

    :param status_code: The response status.
    :param body: The raw response text.
    :returns: ``True`` when the service is disabled for this account.
    """
    if status_code != 404:
        return False
    text = (body or "").lower()
    return "routes:select" in text and "not enabled" in text


# ── Route-options seam ──────────────────────────────────────────────────────
#
# Every assumption about the router's wire contract lives here: the arms it
# expects to be offered, how a pick maps back to a servable catalog id, and
# which harness can run it. Callers never see router vocabulary.

DEFAULT_ROUTER_NAME = "task_v1"

# Catalog ids carry these prefixes; the router keys ids bare. A deployment with
# different ones sets ``routing.model_prefix``. Kept in sync with
# ``claude_model_vocabulary._CATALOG_PREFIXES``, which the hook path uses
# because it cannot read server config.
# The prefix a gateway model ROUTE carries (as opposed to a serving endpoint's
# ``databricks-`` prefix); also how a servable alias below is spelled.
_MODEL_ROUTE_PREFIX = "system.ai."
MODEL_ID_PREFIXES: tuple[str, ...] = ("databricks-", _MODEL_ROUTE_PREFIX)


def strip_catalog_prefix(model: str, prefixes: Sequence[str]) -> str:
    """Strip the first matching *prefixes* entry from a catalog model id.

    A prefix configured without its trailing separator (``system.ai``) leaves
    one behind, so a single leading separator is dropped too.

    :param model: A catalog model id, e.g. ``"system.ai.claude-opus-5"``.
    :param prefixes: Prefixes to try, in order.
    :returns: The id with the prefix removed.
    """
    for prefix in prefixes:
        if prefix and model.startswith(prefix):
            rest = model[len(prefix) :]
            return rest[1:] if rest[:1] in ".-_" else rest
    return model


# task_v1 infers a scenario from WHICH model families appear in route_options
# and then requires that scenario's full arm menu (extra models are tolerated
# and ignored); a partial menu is rejected. Menu ids need not be servable in
# the workspace — the router requires them anyway and may select them.
_TASK_V1_CLAUDE_ARMS: tuple[str, ...] = SMART_ROUTING_TASK_V1_CLAUDE_ARMS
_TASK_V1_CODEX_ARMS: tuple[str, ...] = SMART_ROUTING_TASK_V1_CODEX_ARMS
# The glm arm, named on its own for the servable alias below.
_GLM_ARM = _TASK_V1_CODEX_ARMS[0]

# Scenario → the full arm menu task_v1 requires when that scenario is inferred.
# A router fronting a different catalog has a different menu, so this is the
# DEFAULT rather than the only answer: ``routing.menus`` replaces it wholesale
# (:class:`RoutingSettings`).
TASK_V1_MENUS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "cc": _TASK_V1_CLAUDE_ARMS,
        "codex": _TASK_V1_CODEX_ARMS,
        "both": _TASK_V1_CLAUDE_ARMS + _TASK_V1_CODEX_ARMS,
    }
)

# One fixed fallback per family, used only when the workspace serves no endpoint
# for the picked arm (or the session's harness bars it). Bare ids. The claude
# fallback is the sonnet arm from _TASK_V1_CLAUDE_ARMS (the sonnet pin), never a
# hardcoded id; gpt and glm both land on luna, itself a frozen arm that serves
# codex-side, so a glm fallback never leaves its harness. glm resolves to the
# "gpt" family (model_family_token), so one gpt entry covers it.
_CLAUDE_FAMILY_FALLBACK, _GPT_FAMILY_FALLBACK = SMART_ROUTING_FAMILY_FALLBACKS
_FAMILY_FALLBACK: Mapping[str, str] = MappingProxyType(
    {
        "claude": _CLAUDE_FAMILY_FALLBACK,
        "gpt": _GPT_FAMILY_FALLBACK,
    }
)

# Per-model overrides for the id a resolved arm is actually applied under, when
# the catalog's spelling is not the one the gateway serves. Bare id → exact id
# to apply. Not a general prefix rule: each entry is a probed fact about one
# model.
#
# ``glm-5-2``: probed 2026-08-01 on staging and prod — the gateway serves GLM on
# the Responses API only as the model route ``system.ai.glm-5-2``. The serving
# endpoint ``databricks-glm-5-2`` the catalog offers advertises chat-completions
# only and 400s on ``/codex/v1``, and GLM appears in no discovery listing, so the
# working spelling has to be named here.
# One workspace's probe, so ``routing.servable_aliases`` replaces it for a
# gateway that spells its models differently (:class:`RoutingSettings`).
_SERVABLE_ALIASES: Mapping[str, str] = MappingProxyType(
    {_GLM_ARM: f"{_MODEL_ROUTE_PREFIX}{_GLM_ARM}"}
)

#: The scenario key a menu-less router is offered under: no arms, catalog only.
_NO_MENUS: Mapping[str, tuple[str, ...]] = MappingProxyType({})


def scenario_menus(
    menus: Mapping[str, Sequence[str]] | None = None,
    *,
    router_name: str | None = None,
) -> Mapping[str, Sequence[str]]:
    """Resolve the scenario → arm menus that apply.

    :param menus: An explicit table, e.g. from :class:`RoutingSettings`.
        ``None`` falls back to the built-in default.
    :param router_name: The router the menu is for. Only ``task_v1`` demands an
        arm menu, so any other version defaults to none rather than inheriting
        task_v1's arms; ``None`` skips that gate.
    :returns: Scenario key → the arms that scenario requires.
    """
    if menus is not None:
        return menus
    if router_name is not None and router_name != DEFAULT_ROUTER_NAME:
        return _NO_MENUS
    return TASK_V1_MENUS


def servable_aliases(aliases: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """Resolve the bare-id → served-spelling table that applies.

    :param aliases: An explicit table; ``None`` reads this deployment's.
    """
    return _SERVABLE_ALIASES if aliases is None else aliases


def current_generation_models(
    arms: Mapping[str, Sequence[str]] | None = None,
) -> Mapping[str, Sequence[str]]:
    """Resolve the family → current-generation-endpoints table that applies.

    :param arms: An explicit table; ``None`` reads this deployment's.
    """
    if arms is not None:
        return arms
    return routing_settings().current_generation_models or _CURRENT_GENERATION_MODELS


def apply_servable_alias(
    model: str,
    prefixes: Sequence[str] | None = None,
    *,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """Map a resolved model id onto the spelling this gateway actually serves.

    :param model: A servable catalog id, in either vocabulary.
    :param prefixes: Catalog prefixes to strip before comparing ids.
    :param aliases: Bare id → served spelling; ``None`` reads this
        deployment's configured table.
    :returns: The aliased id, or *model* unchanged when no alias applies.
    """
    table = servable_aliases(
        aliases if aliases is not None else routing_settings().servable_aliases
    )
    return table.get(_bare_id(model, prefixes), model)


def task_v1_claude_arms() -> tuple[str, ...]:
    """Return the Claude-family arms the configured router may select.

    Read by the claude-native launch path, which pins Claude Code's family
    aliases to these arms so the first turn's ``/model`` can reach whatever
    the router picks. Derived from the deployment's own menu, so the pins can't
    drift from it — including when a managed gateway configures other arms.

    :returns: Bare arm ids, e.g. ``("claude-opus-4-8", "claude-sonnet-5")``.
    """
    menus = routing_settings().menus
    if menus is None:
        return _TASK_V1_CLAUDE_ARMS
    return tuple(menus.get("cc", _TASK_V1_CLAUDE_ARMS))


@dataclass(frozen=True)
class RoutingSettings:
    """Deployment routing knobs, parsed from the server ``routing:`` block.

    Hung on :attr:`~omnigent.runtime.caps.RuntimeCaps.routing_settings` so
    every consumer reads one value object instead of re-parsing config.

    The four table fields carry facts probed against ONE gateway's catalog. They
    are overridable so a managed deployment fronting a different menu configures
    it instead of forking this module; ``None`` on any of them keeps the
    module-level constant, which stays the single definition of the default.

    :param router_name: Router strategy to invoke, e.g. ``"task_v1"``.
    :param selection_model: Model the router should use for its own
        extraction call, sent as ``route_selector.config.model``. ``None``
        leaves the router's frozen default in place.
    :param model_prefixes: Prefixes this deployment's catalog attaches to
        model ids that the router keys bare; see :data:`MODEL_ID_PREFIXES`.
    :param menus: Scenario key (``cc`` / ``codex`` / ``both``) → the full arm
        menu the router requires when that scenario is inferred. ``None`` uses
        :data:`TASK_V1_MENUS` for ``task_v1`` and no menu for any other router.
    :param servable_aliases: Bare model id → the exact id this gateway serves
        it as, for models whose catalog spelling is not the served one.
        ``None`` uses :data:`_SERVABLE_ALIASES`.
    :param current_generation_models: Harness family → the router's own current
        arms, offered as candidates so a routed arm resolves to its own
        endpoint. ``None`` uses :data:`_CURRENT_GENERATION_MODELS`.
    :param model_effort_caps: Per-model reasoning-effort ceilings this
        gateway imposes. ``None`` uses
        :data:`~omnigent.reasoning_effort.DEFAULT_MODEL_EFFORT_CAPS`. The
        provider ladders themselves are not configurable — they are the wire
        APIs' own vocabularies, not a deployment fact.
    """

    router_name: str = DEFAULT_ROUTER_NAME
    selection_model: str | None = None
    model_prefixes: tuple[str, ...] = MODEL_ID_PREFIXES
    menus: Mapping[str, Sequence[str]] | None = None
    servable_aliases: Mapping[str, str] | None = None
    current_generation_models: Mapping[str, Sequence[str]] | None = None
    model_effort_caps: ModelEffortCaps | None = None


def _string_list(raw: Any) -> tuple[str, ...] | None:  # type: ignore[explicit-any]  # parsed YAML
    """Read a YAML scalar-or-list of ids into a tuple, or ``None`` if malformed."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _id_table(raw: Any) -> Mapping[str, tuple[str, ...]] | None:  # type: ignore[explicit-any]
    """Read a ``key: [ids]`` block, or ``None`` when it is not a mapping."""
    if not isinstance(raw, dict):
        return None
    table: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        ids = _string_list(value)
        if isinstance(key, str) and key.strip() and ids is not None:
            table[key.strip()] = ids
    return MappingProxyType(table)


def _alias_table(raw: Any) -> Mapping[str, str] | None:  # type: ignore[explicit-any]
    """Read a ``bare-id: served-id`` block, or ``None`` when it is not a mapping."""
    if not isinstance(raw, dict):
        return None
    return MappingProxyType(
        {
            key.strip().replace(".", "-").lower(): value.strip()
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
        }
    )


def parse_routing_tables(
    routing_cfg: Any,  # type: ignore[explicit-any]  # parsed YAML block
) -> dict[str, Any]:  # type: ignore[explicit-any]  # RoutingSettings kwargs
    """Parse the overridable routing tables out of a ``routing:`` block.

    The keys a managed deployment sets to front a different catalog without
    forking this module::

        routing:
          menus:
            cc: [claude-opus-4-8, claude-sonnet-5]
            codex: [glm-5-2, gpt-5-6-sol]
            both: [claude-opus-4-8, claude-sonnet-5, glm-5-2, gpt-5-6-sol]
          servable_aliases:
            glm-5-2: system.ai.glm-5-2
          current_generation_models:
            gpt: [databricks-gpt-5-6-sol]
          effort_caps:
            glm-5-2:
              fallback: medium
              unsupported: [xhigh, max]

    Each key is independent and a malformed one is dropped (routing keeps its
    default rather than failing the boot). An explicit empty mapping is honoured
    — ``menus: {}`` means "this router demands no arm menu".

    :param routing_cfg: The parsed ``routing:`` mapping, or ``None``.
    :returns: Keyword arguments for :class:`RoutingSettings`; every value is
        ``None`` when the block names none of these keys.
    """
    from omnigent.reasoning_effort import ModelEffortCaps

    cfg = routing_cfg if isinstance(routing_cfg, dict) else {}
    caps: ModelEffortCaps | None = None
    raw_caps = cfg.get("effort_caps")
    if isinstance(raw_caps, dict):
        fallback: dict[str, str] = {}
        unsupported: dict[str, frozenset[str]] = {}
        for model, entry in raw_caps.items():
            if not isinstance(model, str) or not isinstance(entry, dict):
                continue
            key = model.strip().replace(".", "-").lower()
            value = entry.get("fallback")
            if isinstance(value, str) and value.strip():
                fallback[key] = value.strip()
            barred = _string_list(entry.get("unsupported"))
            if barred:
                unsupported[key] = frozenset(barred)
        caps = ModelEffortCaps(
            fallback=MappingProxyType(fallback), unsupported=MappingProxyType(unsupported)
        )
    return {
        "menus": _id_table(cfg.get("menus")),
        "servable_aliases": _alias_table(cfg.get("servable_aliases")),
        "current_generation_models": _id_table(cfg.get("current_generation_models")),
        "model_effort_caps": caps,
    }


@dataclass(frozen=True)
class RouteOptionSpec:
    """One candidate destination to offer the router, in router vocabulary."""

    model: str
    harness: str | None = None


@dataclass(frozen=True)
class RoutePick:
    """The router's raw selection, exactly as it came off the wire."""

    model: str
    harness: str | None = None


@dataclass(frozen=True)
class ResolvedRoute:
    """A router pick translated into something this deployment can run.

    :param model: Servable catalog id to apply.
    :param harness: Harness derived from the picked arm's family.
    :param raw_model: The router's pick verbatim, kept for the decision
        payload so the UI can show what the router actually said.
    """

    model: str
    harness: str | None
    raw_model: str


# Harness family that serves several vendors and so never wins family matching
# outright — only used when no single-vendor harness fits.
_MULTI_MODEL_FAMILY = "pi"

# (harness, model) pairs the harness's own gateway 400s on: under pi, Claude
# models on its ``eager_input_streaming`` field and gpt-5.5/5.6 reasoning models
# on its openai-completions default ``reasoning_effort``. Probed against a
# live gateway; tests/e2e/routing/ exercises the exclusions.
_HARNESS_EXCLUDED_MODELS: dict[str, tuple[str, ...]] = {"pi": SMART_ROUTING_PI_EXCLUDED}


def _configured_prefixes(prefixes: Sequence[str] | None) -> Sequence[str]:
    """Resolve which catalog prefixes to compare ids with.

    ``None`` reads this deployment's own :class:`RoutingSettings`, so a
    workspace whose catalog uses other prefixes still compares ids correctly.
    """
    return tuple(routing_settings().model_prefixes) if prefixes is None else prefixes


def _bare_id(model: str, prefixes: Sequence[str] | None = None) -> str:
    """Strip a catalog prefix so ids compare across vocabularies.

    Comparison spelling only, never a model id to send anywhere: the ``[1m]``
    long-context suffix is dropped (it names a context window, not another
    arm), dots read as dashes (a picker's ``gpt-5.6-sol`` is the router's
    ``gpt-5-6-sol``) and case is folded.
    """
    bare = strip_catalog_prefix(model, _configured_prefixes(prefixes))
    # Folded last so an upper-case ``[1M]`` is dropped too.
    return bare.replace(".", "-").lower().removesuffix("[1m]")


def _model_family(model: str) -> str:
    """Tag *model* with the harness family that can serve it.

    Defers to the shared token rule so this file, the dispatch gate, and
    subagent candidate filtering never disagree about a family; the
    ``"openai"`` token there is this file's ``"gpt"`` family.
    """
    from omnigent.model_catalog import model_family_token

    bare = _bare_id(model).lower()
    token = model_family_token(bare)
    return "gpt" if token == "openai" else token


def _model_tier(model: str, prefixes: Sequence[str] | None = None) -> str | None:
    """The model-line codename a router arm encodes, e.g. ``opus`` / ``sol``.

    task_v1's frozen arms name a *tier*, not a specific servable id:
    ``claude-opus-4-8`` is the opus tier, ``gpt-5-6-sol`` the sol tier. When
    the workspace serves a different model of the same tier
    (``claude-opus-5`` for an ``claude-opus-4-8`` pick), that model is the arm
    the router meant, so it substitutes ahead of the family default. The tier
    is the id's last alphabetic segment; ``None`` for a bare generation id
    (``gpt-5-5``) whose only alpha segment is the family root and so carries no
    model line.
    """
    segments = _bare_id(model, prefixes).split("-")
    tier = next((s for s in reversed(segments) if s.isalpha()), None)
    return None if tier in ("gpt", "claude") else tier


def _version_key(model: str, prefixes: Sequence[str] | None = None) -> tuple[int, ...]:
    """Numeric version of *model*, higher = newer, to rank within a tier.

    ``claude-opus-5`` → ``(5,)`` and ``claude-opus-4-8`` → ``(4, 8)``, so
    ``(5,) > (4, 8)`` picks opus-5 as the best opus.
    """
    return tuple(int(s) for s in _bare_id(model, prefixes).split("-") if s.isdigit())


def substitute_model(
    model: str,
    candidates: Sequence[str],
    *,
    prefixes: Sequence[str] | None = None,
    barred: Sequence[str] = (),
    aliases: Mapping[str, str] | None = None,
) -> str | None:
    """Find something in *candidates* to run in place of *model*.

    Three steps, honoring what the router actually named. The pick itself
    first, so a workspace that serves it applies it exactly. Otherwise the best
    servable model of the pick's own *tier* — task_v1 arms name a model line
    (``claude-opus-4-8`` is the opus tier), so a workspace that serves a
    different opus (``claude-opus-5``) gets the arm the router meant, not the
    family default. Only when no same-tier model is servable does it fall to
    the family fallback (:data:`_FAMILY_FALLBACK`), then decline. No cost walk:
    an unservable pick never slides down to a cheaper tier.

    :param model: The router's pick, in either vocabulary.
    :param candidates: Servable model ids.
    :param prefixes: Catalog prefixes to strip before comparing ids; ``None``
        uses this deployment's configured ones.
    :param barred: Bare ids to skip, e.g. models the harness's gateway 400s on.
    :param aliases: Served-spelling table for :func:`apply_servable_alias`;
        ``None`` reads this deployment's.
    :returns: A servable candidate id (through :func:`apply_servable_alias`),
        or ``None`` when nothing servable matches the pick, its tier, or its
        family fallback.
    """
    local: dict[str, str] = {}
    for candidate in candidates:
        local.setdefault(_bare_id(candidate, prefixes), candidate)
    skip = {_bare_id(m, prefixes) for m in barred}
    family = _model_family(model)

    # 1. The exact pick, when the workspace serves it.
    bare = _bare_id(model, prefixes)
    if bare not in skip and bare in local:
        return apply_servable_alias(local[bare], prefixes, aliases=aliases)

    # 2. The best servable model of the pick's own tier (opus/sonnet/sol/luna).
    tier = _model_tier(model, prefixes)
    if tier is not None:
        same_tier = [
            orig
            for cand_bare, orig in local.items()
            if cand_bare not in skip
            and _model_family(cand_bare) == family
            and _model_tier(cand_bare, prefixes) == tier
        ]
        if same_tier:
            best = max(same_tier, key=lambda m: _version_key(m, prefixes))
            return apply_servable_alias(best, prefixes, aliases=aliases)

    # 3. The family fallback, then decline.
    fallback = _FAMILY_FALLBACK.get(family)
    if fallback is not None and fallback not in skip and fallback in local:
        return apply_servable_alias(local[fallback], prefixes, aliases=aliases)
    return None


def natural_harness_for_model(
    model: str,
    catalog: Mapping[str, Sequence[str]],
    *,
    harnesses: Sequence[str] = (),
    prefixes: Sequence[str] | None = None,
) -> str | None:
    """Choose which offered harness *model* belongs to, bars ignored.

    Prefers a harness whose catalog row serves the model, then one of the
    model's own family, then the multi-model harness, then whatever is first
    on offer.

    :param model: The picked model id, in either vocabulary.
    :param catalog: Harness → servable model ids.
    :param harnesses: Harnesses on offer, used when *catalog* is empty.
    :param prefixes: Catalog prefixes to strip before comparing ids.
    :returns: A harness id from the offer, or ``None`` when nothing was offered.
    """
    order = list(catalog) or list(harnesses)
    bare = _bare_id(model, prefixes)
    serving = [
        h for h in order if any(_bare_id(m, prefixes) == bare for m in catalog.get(h, ()) or ())
    ]
    candidates = serving or order
    family = _model_family(model)
    chosen = next((h for h in candidates if _HARNESS_FAMILY.get(h) == family), None)
    if chosen is None:
        chosen = next(
            (h for h in candidates if _HARNESS_FAMILY.get(h) == _MULTI_MODEL_FAMILY), None
        )
    if chosen is None:
        chosen = candidates[0] if candidates else None
    return chosen


def harness_for_model(
    model: str,
    catalog: Mapping[str, Sequence[str]],
    *,
    harnesses: Sequence[str] = (),
    prefixes: Sequence[str] | None = None,
) -> str | None:
    """Choose the offered harness that can actually run *model*.

    :func:`natural_harness_for_model`, then :func:`_redirect_incompatible_pick`
    so a harness whose gateway bars the model never wins.

    :returns: A harness id from the offer, or ``None`` when nothing was offered
        or no offered harness can run the model.
    """
    order = list(catalog) or list(harnesses)
    chosen = natural_harness_for_model(model, catalog, harnesses=harnesses, prefixes=prefixes)
    return _redirect_incompatible_pick(chosen, model, harnesses=order, prefixes=prefixes)


def harness_bars_model(
    harness: str | None,
    model: str,
    *,
    prefixes: Sequence[str] | None = None,
) -> bool:
    """Report whether *harness*'s gateway rejects *model*.

    See :data:`_HARNESS_EXCLUDED_MODELS`.
    """
    barred = {_bare_id(m, prefixes) for m in _HARNESS_EXCLUDED_MODELS.get(harness or "", ())}
    return _bare_id(model, prefixes) in barred


def _redirect_incompatible_pick(
    harness: str | None,
    model: str,
    *,
    harnesses: Sequence[str] = (),
    prefixes: Sequence[str] | None = None,
) -> str | None:
    """Redirect a verdict off a harness that can't serve *model*.

    A Claude model on pi moves to ``claude-sdk``; a gpt-5.5/5.6 reasoning model
    on pi moves to ``codex`` (Responses API) — but only when that harness was
    itself on offer, so a child restricted to one family can never escape it.

    :param harness: The chosen harness id (may be ``None``).
    :param model: The chosen model id, in either vocabulary.
    :param harnesses: The harnesses on offer; a replacement outside them is
        declined. Empty means nothing is known to be on offer, so any needed
        redirect declines.
    :param prefixes: Catalog prefixes to strip before comparing ids.
    :returns: *harness* when it already serves the model, a replacement from
        *harnesses*, or ``None`` when no offered harness can run the model.
    """
    if harness is None:
        return None
    if not harness_bars_model(harness, model, prefixes=prefixes):
        return harness
    family = _model_family(model)
    replacement = {"claude": "claude-sdk", "gpt": "codex"}.get(family)
    if replacement is not None and replacement in harnesses:
        return replacement
    return None


class TaskV1RouteOptionSource:
    """Route-options source for the ``task_v1`` generation of the router.

    Offers the scenario menu the router demands (injecting arms the workspace
    has no endpoint for), ignores the echoed harness tag — passthrough and
    untrusted — and maps a pick back to a servable catalog id.
    """

    def __init__(
        self,
        *,
        router_name: str = DEFAULT_ROUTER_NAME,
        model_prefixes: Sequence[str] = MODEL_ID_PREFIXES,
        menus: Mapping[str, Sequence[str]] | None = None,
        servable_aliases: Mapping[str, str] | None = None,
    ) -> None:
        """
        :param router_name: Router strategy name. Only ``task_v1`` demands an
            arm menu; another version is offered exactly the caller's catalog.
        :param model_prefixes: Catalog prefixes to strip on the way out and
            restore on the way back; see :data:`MODEL_ID_PREFIXES`.
        :param menus: Scenario → required arm menu; ``None`` uses
            :data:`TASK_V1_MENUS` for ``task_v1`` and no menu otherwise. An
            explicit table applies whatever *router_name* is, so a managed
            gateway's own router can demand its own arms.
        :param servable_aliases: Bare id → served spelling; ``None`` reads this
            deployment's configured table.
        """
        self._router_name = router_name
        self._model_prefixes = tuple(model_prefixes)
        self._menus = scenario_menus(menus, router_name=router_name)
        self._aliases = servable_aliases

    def to_router_id(self, model: str) -> str:
        """Strip the first matching configured prefix from a catalog id."""
        return strip_catalog_prefix(model, self._model_prefixes)

    def build_route_options(
        self,
        harnesses: Sequence[str],
        catalog: dict[str, list[str]],
    ) -> list[RouteOptionSpec]:
        """Offer the catalog plus whatever arms the router's menu requires.

        :param harnesses: Harnesses the decision may land on; picks the
            scenario (codex-only, claude-only, or mixed).
        :param catalog: Harness → servable model ids.
        :returns: Candidate options in router vocabulary, catalog first.
        """
        # Keyed on the comparison spelling so a catalog's ``gpt-5.6-luna`` and the
        # menu's ``gpt-5-6-luna`` are one option, not two; the arm's own spelling
        # wins the collision because the router matches its menu byte-exactly.
        offered: dict[str, RouteOptionSpec] = {}
        for harness, models in catalog.items():
            for model in models:
                router_id = self.to_router_id(model)
                offered.setdefault(
                    _bare_id(router_id, self._model_prefixes),
                    RouteOptionSpec(model=router_id, harness=harness),
                )
        for arm in self.menu(harnesses or list(catalog)):
            key = _bare_id(arm, self._model_prefixes)
            existing = offered.get(key)
            offered[key] = RouteOptionSpec(
                model=arm,
                harness=existing.harness
                if existing is not None
                else self._tag_harness(arm, harnesses, catalog),
            )
        return list(offered.values())

    def resolve_selection(
        self,
        pick: RoutePick,
        harnesses: Sequence[str],
        catalog: dict[str, list[str]],
    ) -> ResolvedRoute | None:
        """Translate *pick* into a servable (harness, model) pair.

        :param pick: The router's selection as received; its ``harness`` is
            ignored because the router echoes the tag verbatim without ever
            reading it. An id that already carries a catalog prefix is mapped
            back to router vocabulary first, so re-resolving an id this seam
            already resolved is a no-op rather than a miss.
        :param harnesses: Harnesses the decision may land on.
        :param catalog: Harness → servable model ids.
        :returns: A :class:`ResolvedRoute`, or ``None`` when the pick was
            never offered or nothing servable is close enough.
        """
        raw = self.to_router_id((pick.model or "").strip())
        if not raw:
            return None
        harness = harness_for_model(
            raw, catalog, harnesses=harnesses, prefixes=self._model_prefixes
        )
        if harness is None:
            # No offered harness runs the pick itself: keep the harness it would
            # have landed on (a child must not leave its family) and swap the
            # model for one that harness's gateway accepts.
            return self._substitute_within_harness(raw, harnesses, catalog)
        local = self._local_id(raw, harness, catalog)
        if local is None:
            # Only an arm we injected may be substituted; anything else is a
            # model the router was never offered.
            if not self._is_menu_arm(raw, harnesses, catalog):
                return None
            pool = catalog.get(harness or "") or [m for models in catalog.values() for m in models]
            local = substitute_model(
                raw, pool, prefixes=self._model_prefixes, aliases=self._aliases
            )
        if local is None:
            return None
        return ResolvedRoute(
            model=apply_servable_alias(local, self._model_prefixes, aliases=self._aliases),
            harness=harness,
            raw_model=raw,
        )

    def _is_menu_arm(
        self,
        router_id: str,
        harnesses: Sequence[str],
        catalog: dict[str, list[str]],
    ) -> bool:
        """Report whether *router_id* is an arm this seam injected."""
        key = _bare_id(router_id, self._model_prefixes)
        return any(
            _bare_id(arm, self._model_prefixes) == key
            for arm in self.menu(harnesses or list(catalog))
        )

    def _substitute_within_harness(
        self,
        router_id: str,
        harnesses: Sequence[str],
        catalog: dict[str, list[str]],
    ) -> ResolvedRoute | None:
        """Resolve a pick every offered harness bars, without changing harness."""
        harness = natural_harness_for_model(
            router_id, catalog, harnesses=harnesses, prefixes=self._model_prefixes
        )
        if harness is None:
            return None
        pool = catalog.get(harness) or [m for models in catalog.values() for m in models]
        local = substitute_model(
            router_id,
            pool,
            prefixes=self._model_prefixes,
            barred=_HARNESS_EXCLUDED_MODELS.get(harness, ()),
            aliases=self._aliases,
        )
        if local is None:
            return None
        return ResolvedRoute(model=local, harness=harness, raw_model=router_id)

    def menu(self, harnesses: Iterable[str]) -> tuple[str, ...]:
        """Return the arm menu the router requires for *harnesses*."""
        return tuple(self._menus.get(self._scenario(harnesses), ()))

    def _scenario(self, harnesses: Iterable[str]) -> str:
        """Map a harness set to a router scenario key."""
        families = {_HARNESS_FAMILY.get(h) for h in harnesses}
        if families == {"claude"}:
            return "cc"
        if families == {"gpt"}:
            return "codex"
        return "both"

    def _tag_harness(
        self,
        arm: str,
        harnesses: Sequence[str],
        catalog: dict[str, list[str]],
    ) -> str | None:
        """Pick a plausible harness tag for an injected arm.

        The tag is decoration — no router reads it — so a family match is
        enough, falling back to the first harness on offer.
        """
        order = list(catalog) or list(harnesses)
        family = _model_family(arm)
        match = next((h for h in order if _HARNESS_FAMILY.get(h) == family), None)
        if match is not None:
            return match
        return order[0] if order else None

    def _local_id(
        self,
        router_id: str,
        harness: str | None,
        catalog: dict[str, list[str]],
    ) -> str | None:
        """Restore the exact catalog id behind a router id, if it is servable.

        Matched on the comparison spelling, so a catalog row the picker spells
        with dots still answers for the arm's dashed id.
        """
        key = _bare_id(router_id, self._model_prefixes)

        def _matches(model: str) -> bool:
            return _bare_id(self.to_router_id(model), self._model_prefixes) == key

        own = catalog.get(harness or "", [])
        exact = next((m for m in own if _matches(m)), None)
        if exact is not None:
            return exact
        for models in catalog.values():
            for model in models:
                if _matches(model):
                    return model
        return None


def routing_settings(caps: Any = None) -> RoutingSettings:  # type: ignore[explicit-any]
    """Read this deployment's :class:`RoutingSettings`, defaulted.

    The single accessor for routing knobs: every consumer reads the value
    object off the caps instead of re-parsing config.

    :param caps: A :class:`~omnigent.runtime.caps.RuntimeCaps` (or structural
        equivalent) to read from. ``None`` reads the process-global caps.
    :returns: The configured settings, or all-defaults when none are set.
    """
    if caps is None:
        try:
            from omnigent.runtime._globals import _caps
        except ImportError:
            return RoutingSettings()
        caps = _caps
    settings = getattr(caps, "routing_settings", None) if caps is not None else None
    return settings if isinstance(settings, RoutingSettings) else RoutingSettings()


def routing_last_error(client: Any) -> str | None:  # type: ignore[explicit-any]
    """Read the reason *client*'s last :meth:`route` call returned ``None``.

    Defensive: a custom :class:`RoutingClient` may predate the protocol's
    ``last_error`` field, and an empty string is no reason at all.

    :param client: A routing client, or ``None``.
    :returns: The non-empty reason string, or ``None``.
    """
    detail = getattr(client, "last_error", None)
    return detail if isinstance(detail, str) and detail else None


def route_option_source(
    settings: RoutingSettings | None = None,
    *,
    model_prefixes: Sequence[str] = (),
) -> TaskV1RouteOptionSource:
    """Build the route-options source for this deployment.

    :param settings: Routing settings; read off the runtime caps when omitted.
    :param model_prefixes: Catalog prefixes the router does not expect;
        defaults to the settings' own ``model_prefixes`` when empty.
    :returns: The configured source.
    """
    resolved = settings or routing_settings()
    return TaskV1RouteOptionSource(
        router_name=resolved.router_name,
        model_prefixes=tuple(model_prefixes) or resolved.model_prefixes,
        menus=resolved.menus,
        servable_aliases=resolved.servable_aliases,
    )


class ExternalRoutingClient:
    """Routing client backed by an external ``routes:select`` service.

    Calls an external routing service (the Databricks AI-Gateway ``task_v1``
    router, or any endpoint speaking the ``omnigent.api.routing.v1`` proto)
    instead of running a local judge. The candidate models come from
    ``available_models`` (the same live catalog the built-in judge sees),
    so no catalog plumbing changes. This client is the one authority on
    resolving a pick: it returns both the servable ``model`` and the router's
    own ``raw_model``, and callers apply them as-is. A failure or empty
    selection returns ``None`` so the turn proceeds on the agent's default
    model.
    """

    def __init__(
        self,
        *,
        base_url: str,
        router_name: str,
        auth: httpx.Auth | None = None,
        databricks_profile: str | None = None,
        auth_provider: Callable[[], Mapping[str, str] | None] | None = None,
        model_prefixes: Sequence[str] | None = None,
        request_timeout: float = ROUTING_REQUEST_TIMEOUT_S,
        selection_model: str | None = None,
        menus: Mapping[str, Sequence[str]] | None = None,
        servable_aliases: Mapping[str, str] | None = None,
    ) -> None:
        """
        :param base_url: Routing service base, e.g.
            ``"https://host/ai-gateway/routing/v1"``.
            ``/routes:select`` is appended.
        :param router_name: Router strategy name, e.g. ``"task_v1"``.
        :param auth: Optional static httpx auth (e.g. a bearer built from an
            explicit ``api_key``). ``None`` for an unauthenticated endpoint or
            when *databricks_profile* supplies per-call OAuth instead.
        :param databricks_profile: Optional Databricks CLI profile. When set
            (and *auth* is ``None``), a fresh bearer is minted per :meth:`route`
            call via the databricks-sdk ``Config`` — which refreshes OAuth
            tokens transparently, so a long-lived server never sends a stale
            token (the 401 an at-startup captured token hits after ~1h).
        :param auth_provider: Optional callable returning the request headers to
            authenticate ONE :meth:`route` call, e.g.
            ``{"Authorization": "Bearer ..."}``. Evaluated per call and takes
            precedence over every other mode, so one process serving many
            workspaces can carry the calling identity instead of a
            construction-time credential. Returning ``None`` (or an empty
            mapping) means this caller has no credential and the request goes
            out unauthenticated — the provider is the sole authority once set,
            never a first attempt that falls back to *auth*.
        :param model_prefixes: Prefixes this deployment's catalog attaches to
            model ids the router keys bare, stripped on the way out and
            restored on its answer. ``None`` uses :data:`MODEL_ID_PREFIXES`.
        :param request_timeout: Per-call timeout in seconds, defaulting to
            :data:`ROUTING_REQUEST_TIMEOUT_S`. The innermost wait in every
            routing ladder, so a slow router degrades to "not routed" long
            before the hook holding the user's prompt or spawn is killed.
        :param selection_model: Model the router should use for its own
            extraction call, sent as ``route_selector.config.model`` so a
            deployment can pin one it has query access to. ``None`` omits
            ``config`` entirely and leaves the router's frozen default.
        :param menus: Scenario → required arm menu, overriding
            :data:`TASK_V1_MENUS`; see :class:`RoutingSettings`.
        :param servable_aliases: Bare id → served spelling, overriding
            :data:`_SERVABLE_ALIASES`; see :class:`RoutingSettings`.
        """
        self._url = base_url.rstrip("/") + "/" + ROUTES_SELECT_PATH
        self._router_name = router_name
        self._auth = auth
        self._databricks_profile = databricks_profile
        self._auth_provider = auth_provider
        # Cached SDK Config for the profile (created lazily), reused across
        # calls; its authenticate() refreshes the OAuth token as needed.
        self._sdk_config: Config | None = None
        # Same cache for the ambient chain, plus a latch that stops re-probing
        # the SDK on every call once it is known not to apply here.
        self._ambient_config: Config | None = None
        self._ambient_off = False
        self._model_prefixes = (
            list(MODEL_ID_PREFIXES) if model_prefixes is None else list(model_prefixes)
        )
        self._request_timeout = request_timeout
        self._selection_model = selection_model or None
        # All router-contract knowledge (arm menus, harness derivation,
        # unservable-pick substitution) lives behind this seam.
        self._source = TaskV1RouteOptionSource(
            router_name=router_name,
            model_prefixes=self._model_prefixes,
            menus=menus,
            servable_aliases=servable_aliases,
        )
        # Human-readable reason the most recent route() returned None, for the
        # caller to surface in the UI (a bare None hides the actual cause, e.g.
        # a 401 or the router's required-model-set error). Set on every failure
        # path, cleared on success.
        self.last_error: str | None = None
        # Latched once the service reports a condition no retry can clear (the
        # routing API is not enabled for this account). Every later call short-
        # circuits to the stored reason, so a deployment whose workspace has no
        # routing API pays one request, not one per turn.
        self.permanently_unavailable = False
        self._permanent_error: str | None = None

    def _resolve_credentials(self) -> tuple[Any, Mapping[str, str]]:  # type: ignore[explicit-any]
        """Resolve one request's credentials as ``(httpx auth, extra headers)``.

        Evaluated per :meth:`route` call, in precedence order: the injected
        *auth_provider* (per-caller identity), a static *auth* (explicit
        api_key), the Databricks *databricks_profile* (fresh bearer per call so
        token expiry never surfaces as a router 401), then the ambient SDK
        chain. Both slots empty means the request goes out unauthenticated.
        """
        if self._auth_provider is not None:
            return None, self._provider_headers()
        if self._auth is not None:
            return self._auth, {}
        if self._databricks_profile is not None:
            return self._profile_auth(), {}
        return self._ambient_auth(), {}

    def _provider_headers(self) -> Mapping[str, str]:
        """Call the injected provider for this request's auth headers."""
        try:
            headers = self._auth_provider() if self._auth_provider is not None else None
        except Exception:  # noqa: BLE001 — a caller with no credential still routes
            _logger.warning(
                "ExternalRoutingClient: auth_provider raised; sending no credential",
                exc_info=True,
            )
            return {}
        return {k: v for k, v in dict(headers or {}).items() if isinstance(v, str) and v.strip()}

    def _profile_auth(self) -> Any:  # type: ignore[explicit-any]  # httpx.Auth | None
        """Mint a bearer from the configured Databricks CLI profile."""
        if self._sdk_config is None:
            try:
                from databricks.sdk.config import Config

                self._sdk_config = Config(profile=self._databricks_profile)
            except Exception:  # noqa: BLE001 — auth failure degrades to unauthenticated
                _logger.warning(
                    "ExternalRoutingClient: could not resolve auth for profile %r",
                    self._databricks_profile,
                    exc_info=True,
                )
                return None
        return _config_bearer(self._sdk_config, f"profile {self._databricks_profile!r}")

    def _ambient_auth(self) -> Any:  # type: ignore[explicit-any]  # httpx.Auth | None
        """Mint a bearer from the ambient Databricks SDK chain, if it applies.

        A server deployment has no CLI profile and no api_key: its workspace
        credential is the ambient environment (``DATABRICKS_HOST`` plus
        ``DATABRICKS_CLIENT_ID``/``DATABRICKS_CLIENT_SECRET``, or whatever the
        SDK's default chain resolves). Rather than make that a config flag, it
        engages only when the ambient workspace host IS the host this client
        posts to.

        Host matching is the safer guard than an ``auth: ambient`` opt-in: the
        hazard is sending a workspace token to an endpoint that is not that
        workspace, and a config value cannot prevent that (a deployment that
        sets it and later re-points ``base_url`` leaks the token), whereas host
        matching cannot leak regardless of config. It also keeps a plain
        unauthenticated endpoint unauthenticated — a non-matching host never
        attaches a credential — while costing managed no configuration.
        """
        if self._ambient_off:
            return None
        if self._ambient_config is None:
            try:
                from databricks.sdk.config import Config

                config = Config()
            except Exception:  # noqa: BLE001 — no ambient credential is the normal case
                _logger.debug(
                    "ExternalRoutingClient: no ambient Databricks credential", exc_info=True
                )
                self._ambient_off = True
                return None
            if not _same_host(getattr(config, "host", None), self._url):
                _logger.debug(
                    "ExternalRoutingClient: ambient Databricks host %r is not the router host; "
                    "sending no credential",
                    getattr(config, "host", None),
                )
                self._ambient_off = True
                return None
            self._ambient_config = config
        return _config_bearer(self._ambient_config, "the ambient Databricks environment")

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> RoutingResult | None:
        import httpx
        from google.protobuf import json_format

        from omnigent.api.routing.v1 import routing_pb2 as pb

        if self.permanently_unavailable:
            # The account has no routing API; skip the call rather than spend a
            # round trip per turn to be told so again.
            self.last_error = self._permanent_error
            return None
        # The seam turns the catalog into router vocabulary and injects
        # whatever arms the router's scenario menu demands.
        harnesses = list(available_models)
        specs = self._source.build_route_options(harnesses, available_models)
        if not specs:
            return None
        options = [pb.RouteOption(model=s.model, harness=s.harness) for s in specs]
        selector = pb.RouteSelector(router_name=self._router_name)
        if self._selection_model:
            # The router makes its own extraction call; ``config.model`` pins
            # the model it uses so a deployment can name one it can query.
            json_format.ParseDict({"model": self._selection_model}, selector.config)
        request = pb.SelectRouteRequest(
            route_options=options,
            task=pb.Task(prompt=message[:4000]),
            route_selector=selector,
        )
        # snake_case wire format — the router uses the proto field names.
        body = json_format.MessageToDict(request, preserving_proto_field_name=True)
        _logger.info(
            "ExternalRoutingClient: POST %s router=%s options=%s prompt_chars=%d",
            self._url,
            self._router_name,
            [s.model for s in specs],
            len(message),
        )
        # The body carries the user's prompt, so it stays at DEBUG and the
        # prompt itself is replaced with its length.
        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "ExternalRoutingClient: request body=%s",
                {**body, "task": {"prompt": f"<{len(message)} chars>"}},
            )
        # Resolve auth per call (SDK token refresh is a blocking HTTP call, so
        # run it off the event loop) — keeps a long-lived server from sending a
        # token that has expired since startup.
        import asyncio

        auth, auth_headers = await asyncio.to_thread(self._resolve_credentials)
        self.last_error = None
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as http:
                resp = await http.post(
                    self._url,
                    headers={"Content-Type": "application/json", **auth_headers},
                    json=body,
                    auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                )
        except httpx.HTTPError as exc:
            # Transport-level failure (connect/timeout/DNS): no response body,
            # and httpx's timeouts carry no message — name the class instead.
            _logger.warning(
                "ExternalRoutingClient: routes:select request failed: %s", failure_detail(exc)
            )
            self.last_error = f"router request failed: {failure_detail(exc)}"
            return None
        if resp.status_code >= 400:
            # Log the response body — the gateway puts the actual reason there
            # (e.g. the required-model-set error), which the bare status code
            # from raise_for_status() omits.
            _logger.warning(
                "ExternalRoutingClient: routes:select returned %s: %s",
                resp.status_code,
                resp.text[:2000],
            )
            self.last_error = (
                f"router returned HTTP {resp.status_code}: {_router_error_detail(resp.text)}"
            )
            if router_permanently_disabled(resp.status_code, resp.text):
                _logger.warning(
                    "ExternalRoutingClient: %s is not enabled for this account; "
                    "no further routes:select calls will be made in this process",
                    self._url,
                )
                self.permanently_unavailable = True
                self._permanent_error = self.last_error
            return None
        try:
            out = json_format.ParseDict(resp.json(), pb.SelectRouteResponse())
        except (ValueError, json_format.ParseError):
            _logger.warning(
                "ExternalRoutingClient: could not parse routes:select response: %s",
                resp.text[:2000],
            )
            self.last_error = "router returned an unparseable response"
            return None
        if not out.route_selection:
            self.last_error = "router returned no route selection"
            return None
        selected = out.route_selection[0].route_option
        if not selected.model:
            self.last_error = "router returned an empty model"
            return None
        resolved = self._source.resolve_selection(
            RoutePick(model=selected.model, harness=selected.harness or None),
            harnesses,
            available_models,
        )
        if resolved is None:
            _logger.warning(
                "ExternalRoutingClient: router returned model %r (harness %r) "
                "with nothing servable behind it; ignoring",
                selected.model,
                selected.harness,
            )
            self.last_error = f"router picked model {selected.model!r}, which nothing here serves"
            return None
        if resolved.model != resolved.raw_model:
            _logger.info(
                "ExternalRoutingClient: router pick %r is not servable here; using %r",
                resolved.raw_model,
                resolved.model,
            )
        return RoutingResult(
            model=resolved.model,
            rationale=out.rationale,
            harness=resolved.harness,
            raw_model=resolved.raw_model,
        )


# ── Public API ──────────────────────────────────────────────────────────────

# SDK harnesses offered as candidates when the user picks "auto" harness; native
# ones need a CLI binary that may not be installed. Order is the tiebreak when
# several serve the pick: codex precedes pi so GPT models take the Responses
# API, which pi's completions path 400s on for gpt-5.5+ reasoning models.
_AUTO_ROUTING_HARNESSES: tuple[str, ...] = ("claude-sdk", "codex", "pi")

# Native terminal harnesses offered when Smart Routing is the top-level harness.
# Both families are on the menu, so the pick maps onto the matching native
# wrapper agent (claude-native-ui / codex-native-ui). The caller narrows them to
# the host's installed CLIs and resolves their models pre-launch.
AUTO_NATIVE_ROUTING_HARNESSES: tuple[str, ...] = ("claude-native", "codex-native")

# The live runner catalog keys rows by WORKER name — the sub-agent names declared
# in the parent spec (e.g. "claude_code") plus "self" for the session's own
# harness — NOT by harness id. Map the common worker names back so a child
# session's catalog still yields routable candidates; unknown worker names fall
# through to the static infer_models table.
_WORKER_NAME_TO_HARNESS: dict[str, str] = {
    "claude_code": "claude-sdk",
    "claude-sdk": "claude-sdk",
    "codex": "codex",
    "pi": "pi",
}


def _redirect_wire_incompatible_pick(
    harness: str | None,
    model: str,
    harness_catalog: Mapping[str, Sequence[_RunnerModel]],
    *,
    harnesses: Sequence[str] = (),
) -> str | None:
    """Redirect a verdict off a harness the catalog proves cannot serve *model*.

    The catalog-driven companion to :func:`_redirect_incompatible_pick`, which
    only knows the statically-barred pairs. Pi speaks Anthropic Messages for
    Claude-family models, so an endpoint the catalog reports as not accepting
    that wire moves to ``claude-sdk``. Unknown metadata from an older runner is
    not treated as incompatible.

    :param harness: The chosen harness id (may be ``None``).
    :param model: The chosen model id.
    :param harness_catalog: Catalog metadata keyed by normalized harness id.
    :param harnesses: The harnesses on offer; a replacement outside them is
        declined so a family-restricted child cannot escape its family.
    :returns: A replacement harness id, or *harness* when the pick already fits.
    """
    if harness != "pi":
        return harness
    entry = next(
        (candidate for candidate in harness_catalog.get(harness, ()) if candidate.id == model),
        None,
    )
    if (
        entry is not None
        and entry.family == "claude"
        and entry.wire_apis
        and ModelWireAPI.ANTHROPIC_MESSAGES not in entry.wire_apis
        and (not harnesses or "claude-sdk" in harnesses)
    ):
        return "claude-sdk"
    return harness


async def route_session_harness(
    user_message: str,
    *,
    session_id: str | None = None,
    catalog_session_id: str | None = None,
    runner_client: httpx.AsyncClient | None = None,
    allowed_family: str | None = None,
    harness_candidates: Sequence[str] | None = None,
    catalog: Mapping[str, Sequence[str]] | None = None,
    gateway_backed: bool = True,
    allow_static_fallback: bool = True,
) -> tuple[str | None, str | None, dict[str, Any] | None, str | None]:
    """Pick the best harness + model for a new session via the routing client.

    Builds a candidate set from the live runner catalog when *catalog_session_id*
    (defaulting to *session_id*) and *runner_client* are provided. Only harnesses
    in :data:`_AUTO_ROUTING_HARNESSES` are offered as candidates.

    :param user_message: The user's first message text, used to size the task.
    :param session_id: Session being routed (optional).
    :param catalog_session_id: Session whose catalog defines the candidate set.
        For a sub-agent this should be the PARENT session id — the parent's
        catalog enumerates the spawnable workers (claude_code/codex/pi) with
        their full model lists, whereas the child's own leaf catalog only has a
        ``"self"`` row and would force the static fallback. Defaults to
        *session_id* when unset.
    :param runner_client: HTTP client pointed at the runner (optional).
    :param allowed_family: Restrict candidates to this harness family
        (:func:`omnigent.runner.subagent_routing.harness_family`), e.g.
        ``"gpt"`` for a child of a codex session. ``None`` offers every
        auto-routing harness — only sessions in auto-harness mode.
    :param harness_candidates: Replace the default SDK candidate set with an
        explicit one, e.g. :data:`AUTO_NATIVE_ROUTING_HARNESSES` for a session
        that must land on a native terminal. Callers narrow it to what the host
        can actually run; an empty sequence yields the standard "no routable
        harnesses" error. ``None`` uses :data:`_AUTO_ROUTING_HARNESSES`.
    :param catalog: Harness → model ids resolved without a runner, e.g. the
        host's pre-launch model options during a session create. Used when no
        live runner catalog is in reach; the static table then only tops up the
        candidates it could not answer for.
    :param gateway_backed: Whether every candidate harness resolves
        AI-Gateway-backed inference on the target host. ``False`` takes the
        external router out of play (its picks are gateway catalog ids the pane
        cannot reach) and the built-in judge answers instead.
    :param allow_static_fallback: Whether the static :func:`infer_models` table
        may supply or top up candidates. Callers pass ``gateway_backed``: that
        table is all ``databricks-*`` ids, so off the gateway it would offer
        models the pane cannot run. ``False`` declines instead.
    :returns: ``(harness, model, verdict, error)`` — on success ``error`` is
        ``None``; on failure ``harness``, ``model``, and ``verdict`` are ``None``
        and ``error`` carries a human-readable reason shown in the UI.
    """
    if not user_message:
        return None, None, None, None
    try:
        from omnigent.runtime._globals import _caps
    except ImportError:
        return None, None, None, "Smart routing is not available."

    from omnigent.server.routing_backend import (
        backends_from_caps,
        route_with_fallback,
        select_router,
    )

    backends = backends_from_caps(_caps)
    if select_router(backends, gateway_backed=gateway_backed) is None:
        return None, None, None, "Smart routing is not configured on this server."

    # Fetch the live catalog. Its rows are keyed by worker name (sub-agent
    # names + "self"), so normalize those to harness ids before matching
    # against _AUTO_ROUTING_HARNESSES. Prefer catalog_session_id (the parent
    # for a sub-agent) so the candidate set is the full spawnable-worker map,
    # independent of whether the routed session is top-level or a sub-agent.
    _catalog_sid = catalog_session_id or session_id
    live_catalog: dict[str, list[_RunnerModel]] | None = None
    if _catalog_sid and runner_client is not None:
        live_catalog = await _fetch_runner_catalog(_catalog_sid, runner_client)

    # Incompatible (harness, model) pairs stay on the menu — the router 400s on a
    # partial one — and are corrected post-verdict. Only an auto-harness session
    # may leave its family: a child is offered its parent's family alone.
    candidate_harnesses = tuple(
        h
        for h in (
            _AUTO_ROUTING_HARNESSES if harness_candidates is None else tuple(harness_candidates)
        )
        if allowed_family is None or _HARNESS_FAMILY.get(h) == allowed_family
    )
    harness_models: dict[str, list[str]] = {}
    harness_catalog: dict[str, list[_RunnerModel]] = {}
    if live_catalog:
        for worker_name, worker_catalog in live_catalog.items():
            harness = _WORKER_NAME_TO_HARNESS.get(worker_name)
            if harness is None or harness not in candidate_harnesses:
                continue
            in_family = models_in_family(harness, [entry.id for entry in worker_catalog])
            if in_family:
                # First worker wins for a given harness id (dedupe). The catalog
                # rows are kept alongside the ids so the post-verdict wire check
                # can read the endpoint's advertised protocols.
                harness_catalog.setdefault(harness, worker_catalog)
                harness_models.setdefault(harness, in_family)

    # No runner to ask (a create routes before the session exists): take the
    # caller's pre-session catalog, family-filtered like the live one.
    if not harness_models and catalog:
        for h in candidate_harnesses:
            in_family = models_in_family(h, catalog.get(h) or ())
            if in_family:
                harness_models[h] = in_family

    # Fall back to the static table when neither catalog produced routable
    # candidates (e.g. a child session whose catalog only lists "self" under an
    # unrecognized worker name, or the runner was unreachable), and to top up
    # candidates a pre-session catalog could not answer for. Off the gateway
    # that table is unusable: every id in it is a ``databricks-*`` endpoint the
    # pane cannot reach, so offering one would route the session onto a model
    # the launch silently drops. Decline instead — the provider-accurate
    # sources are the pre-session catalog and the runner catalog's "self" row.
    if allow_static_fallback and (
        not harness_models or (catalog and len(harness_models) < len(candidate_harnesses))
    ):
        for h in candidate_harnesses:
            if h in harness_models:
                continue
            models = infer_models(h)
            if models:
                harness_models[h] = models

    if not harness_models:
        return None, None, None, "No routable harnesses are available on this runner."

    try:
        call = await route_with_fallback(
            backends, user_message, harness_models, gateway_backed=gateway_backed
        )
    except Exception as exc:  # routing failures must not block session creation
        _logger.exception("smart_routing: route_session_harness failed")
        return None, None, None, f"Routing call failed: {failure_detail(exc)}"
    if call is None:
        return None, None, None, "Smart routing is not configured on this server."
    result = call.result

    if result is None:
        # Surface the client's specific failure reason (e.g. HTTP 401 with the
        # gateway's message) when it exposes one; otherwise a generic note.
        detail = routing_last_error(call.client)
        reason = (
            f"Routing unavailable: {detail}"
            if detail
            else "The router returned no verdict; using default harness."
        )
        return None, None, None, reason

    # The client owns resolution: it already mapped the router's pick onto a
    # servable id (reporting the pick itself as ``raw_model``). Re-resolving
    # here would second-guess it with a different prefix list and downgrade the
    # pick, so only the harness is filled in when the client named none.
    prefixes = routing_settings().model_prefixes
    chosen_model = result.model
    raw_model = result.raw_model
    offered = tuple(harness_models)
    chosen_harness = (
        _redirect_incompatible_pick(
            result.harness, chosen_model, harnesses=offered, prefixes=prefixes
        )
        if result.harness
        else harness_for_model(chosen_model, harness_models, prefixes=prefixes)
    )
    if chosen_harness is not None and chosen_harness not in harness_models:
        # A harness this call never offered is not a verdict — it is a pick the
        # caller cannot honor. A child confined to one harness would otherwise
        # have another family's harness written to its row and stamped
        # "applied" while its pane keeps running the harness it booted on.
        # A verdict may still spell an offered harness as its WORKER name
        # (``claude_code``), which is the same harness, not an escape.
        from omnigent.harness_aliases import canonicalize_harness

        _spelled = _WORKER_NAME_TO_HARNESS.get(chosen_harness) or canonicalize_harness(
            chosen_harness
        )
        if _spelled in harness_models:
            chosen_harness = _spelled
        else:
            _logger.info(
                "smart_routing: dropping verdict harness=%s; offered=%s",
                chosen_harness,
                offered,
            )
            chosen_harness = None
    if chosen_harness is None and result.harness in harness_models:
        # Every offered harness bars the pick, and the family the caller allowed
        # is not negotiable — swap the model instead of the harness.
        substitute = substitute_model(
            raw_model or chosen_model,
            harness_models[result.harness],
            prefixes=prefixes,
            barred=_HARNESS_EXCLUDED_MODELS.get(result.harness, ()),
        )
        if substitute is not None:
            raw_model = raw_model or chosen_model
            chosen_harness, chosen_model = result.harness, substitute
    if chosen_harness is None:
        return None, None, None, f"No available harness can run {chosen_model}."

    # Then the catalog's own wire metadata, which knows endpoints the static bar
    # list cannot. Applied post-verdict so the complete candidate set still
    # reaches external routers that require it.
    _redirected = _redirect_wire_incompatible_pick(
        chosen_harness, chosen_model, harness_catalog, harnesses=offered
    )
    if _redirected != chosen_harness:
        _logger.info(
            "smart_routing: redirecting wire-incompatible pick harness=%s model=%s -> harness=%s",
            chosen_harness,
            chosen_model,
            _redirected,
        )
        chosen_harness = _redirected

    # The UI shows what the router said, not only what we could run — but only
    # when they are genuinely different models, not the same one spelled bare.
    verdict: dict[str, Any] = {
        "model": chosen_model,
        "rationale": result.rationale,
        "router_source": call.source,
    }
    if raw_model and _bare_id(raw_model, prefixes) != _bare_id(chosen_model, prefixes):
        verdict["raw_model"] = raw_model

    _logger.info(
        "smart_routing: auto-harness harness=%s model=%s",
        chosen_harness,
        chosen_model,
    )
    # The rationale paraphrases the user's prompt, so it stays off INFO.
    _logger.debug("smart_routing: auto-harness rationale=%s", result.rationale)
    return chosen_harness, chosen_model, verdict, None


async def route_turn(
    harness: str | None,
    user_message: str,
    *,
    session_id: str | None = None,
    runner_client: httpx.AsyncClient | None = None,
    catalog: Sequence[str] | None = None,
    gateway_backed: bool = True,
    allow_static_fallback: bool = True,
) -> tuple[str | None, dict[str, Any] | None]:
    """Pick the best model for a turn via the deployment's routing backends.

    When *session_id* and *runner_client* are provided, fetches live model
    availability from the runner's ``/v1/sessions/{id}/models`` endpoint.
    Falls back to the static :func:`infer_models` lookup table if the runner
    is unreachable or returns no data.

    :param catalog: The models this session can actually be switched onto,
        when the caller knows them exactly - a native terminal's own picker
        vocabulary. Authoritative: its gateway serves more models than the
        running CLI can be switched to, and offering those routes the turn
        onto a model the switch would silently drop.
    :param gateway_backed: Whether this session's harness resolves
        AI-Gateway-backed inference on its host. ``False`` takes the external
        router out of play and the built-in judge answers instead.
    :param allow_static_fallback: Whether the static :func:`infer_models` table
        may supply candidates. Callers pass ``gateway_backed``: that table is
        all ``databricks-*`` ids, so off the gateway it would route the turn
        onto a model the pane cannot switch to. ``False`` declines instead.
    """
    try:
        from omnigent.runtime._globals import _caps
    except ImportError:
        return None, None

    from omnigent.server.routing_backend import (
        backends_from_caps,
        route_with_fallback,
        select_router,
    )

    backends = backends_from_caps(_caps)
    if select_router(backends, gateway_backed=gateway_backed) is None:
        _logger.info(
            "smart_routing: route_turn skipped for session=%s: no routing client configured",
            session_id,
        )
        return None, None

    _logger.info("smart_routing: routing turn session=%s harness=%s", session_id, harness)
    # Candidate preparation used to be invisible in the logs, so a slow first
    # message read as a slow router. Time the two phases separately: this is
    # the number the timeout ladder above the hook has to cover.
    _prep_started = time.monotonic()
    _catalog_fetched = False
    # Prefer the live runner catalog, but only its "self" row — the sub-agent
    # workers' models are not this session's. Key the map by harness id, not the
    # "self" label, so the seam infers the right single-harness scenario.
    available: dict[str, list[str]] | None = None
    if catalog:
        in_vocabulary = models_in_family(harness, catalog)
        if in_vocabulary:
            available = {harness or "self": in_vocabulary}
    if available is None and session_id and runner_client is not None:
        _catalog_fetched = True
        runner_catalog = await fetch_runner_models(session_id, runner_client)
        if runner_catalog and "self" in runner_catalog:
            # A native terminal's own catalog can list models from other
            # families (its gateway serves them all); offering those would
            # route the session onto a model its harness cannot run.
            in_family = models_in_family(harness, runner_catalog["self"])
            if in_family:
                available = {harness or "self": in_family}
    if not available:
        # The static table is every ``databricks-*`` id, so off the gateway it
        # names models this pane cannot be switched to. Decline the turn rather
        # than route it onto an unreachable endpoint.
        models = infer_models(harness) if allow_static_fallback else None
        if models is None:
            _logger.info(
                "smart_routing: route_turn skipped for session=%s: "
                "no candidate models for harness=%s",
                session_id,
                harness,
            )
            return None, None
        available = {harness or "": models}

    prefixes = routing_settings().model_prefixes
    # A turn cannot change the harness, so a model its gateway bars is not a
    # candidate at all. The seam still injects whatever arms the router's menu
    # requires, so dropping these rows never makes that menu partial.
    if _HARNESS_EXCLUDED_MODELS.get(harness or ""):
        available = {
            name: runnable
            for name, models in available.items()
            if (
                runnable := [
                    m for m in models if not harness_bars_model(harness, m, prefixes=prefixes)
                ]
            )
        }
        if not available:
            _logger.info(
                "smart_routing: route_turn skipped for session=%s: "
                "harness=%s bars every candidate model",
                session_id,
                harness,
            )
            return None, None

    _prep_s = time.monotonic() - _prep_started
    _route_started = time.monotonic()
    call = await route_with_fallback(
        backends, user_message, available, gateway_backed=gateway_backed
    )
    _logger.info(
        "smart_routing: session=%s prep=%.3fs router=%.3fs catalog_fetch=%s candidates=%d",
        session_id,
        _prep_s,
        time.monotonic() - _route_started,
        _catalog_fetched,
        sum(len(models) for models in available.values()),
    )
    if call is None or call.result is None:
        return None, None
    result = call.result

    # An injected arm can still come back barred (the menu is offered whole), and
    # a turn cannot change harness — so swap the model for one this gateway
    # accepts. Nothing servable means no routing: the turn keeps its model.
    model = result.model
    raw_model = result.raw_model
    if harness_bars_model(harness, model, prefixes=prefixes):
        candidates = available.get(harness or "", [])
        substitute = substitute_model(
            raw_model or model,
            candidates,
            prefixes=prefixes,
            barred=_HARNESS_EXCLUDED_MODELS.get(harness or "", ()),
        )
        if substitute is None:
            _logger.info(
                "smart_routing: route_turn skipped for session=%s: harness=%s cannot run %s",
                session_id,
                harness,
                model,
            )
            return None, None
        raw_model = raw_model or model
        model = substitute

    _logger.info("smart_routing: session=%s model=%s", session_id, model)
    # The rationale paraphrases the user's prompt, so it stays off INFO.
    _logger.debug("smart_routing: session=%s rationale=%s", session_id, result.rationale)
    verdict: dict[str, Any] = {
        "model": model,
        "rationale": result.rationale,
        "router_source": call.source,
    }
    if raw_model and _bare_id(raw_model, prefixes) != _bare_id(model, prefixes):
        verdict["raw_model"] = raw_model
    return model, verdict


async def route_turn_or_decline(
    harness: str | None,
    user_message: str,
    **kwargs: Any,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Call :func:`route_turn`, converting a raised failure into a decline.

    The fail-open boundary for the message/turn path. :func:`route_turn`
    itself lets a routing failure raise — its own client calls, the credential
    resolution behind them, a malformed body — and on the ``POST /events`` path
    that surfaced as a 500: the user's message was persisted and then abandoned
    rather than simply running unrouted. Nothing about routing is worth a
    dropped turn, so every failure lands here instead as ``error``, and the
    caller shows it on a declined routing card while the turn proceeds on the
    session's own model.

    Mirrors :func:`route_session_harness`'s ``(…, error)`` shape, so both
    create-time and turn-time routing report an outage the same way.

    :param harness: The session's harness, e.g. ``"claude-native"``.
    :param user_message: The turn's text to route on.
    :param kwargs: Forwarded to :func:`route_turn` verbatim.
    :returns: ``(model, verdict, error)`` — ``error`` is ``None`` whenever
        :func:`route_turn` answered at all, including its own "no verdict"
        answers, which are ordinary declines rather than failures.
    """
    try:
        model, verdict = await route_turn(harness, user_message, **kwargs)
    except Exception as exc:  # a routing outage must never drop a turn
        _logger.exception("smart_routing: route_turn failed; the turn runs unrouted")
        return None, None, f"Routing call failed: {failure_detail(exc)}"
    return model, verdict, None
