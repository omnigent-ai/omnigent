"""Server-side intelligent model routing.

Infers available models from the session's harness type and delegates
the routing decision to the :class:`RoutingClient` on
:attr:`RuntimeCaps.routing_client`.  The default implementation
(:class:`LLMRoutingClient`) calls the server-level LLM with a prompt
that describes each model's capabilities directly — no tier abstraction.
Managed deployments can swap in a different implementation via
``RuntimeCaps``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from omnigent.model_metadata import ModelCostTier, ModelIntent, ModelWireAPI

if TYPE_CHECKING:
    import httpx  # used in type annotations only; runtime import is lazy in fetch_runner_models
    from databricks.sdk.config import Config

_logger = logging.getLogger(__name__)

# Custom-method path (Google API convention) appended to the external
# router's base URL, e.g. ``<base_url>/routes:select``.
ROUTES_SELECT_PATH = "routes:select"

# ── RoutingClient protocol ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingResult:
    """The routing client's recommendation.

    :param model: Model id to use, e.g. ``"databricks-claude-opus-4-8"``.
    :param rationale: One-sentence explanation from the judge.
    :param harness: The harness the judge selected, e.g. ``"claude-sdk"``.
        ``None`` when the routing client does not distinguish harnesses (e.g.
        single-harness calls or custom implementations that omit it).
    """

    model: str
    rationale: str
    harness: str | None = None


class RoutingClient(Protocol):
    """Protocol for pluggable model routing implementations."""

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


async def _fetch_runner_catalog(
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
    """Default routing client using the server-level PolicyLLMClient."""

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> RoutingResult | None:
        flat = _flatten_models(available_models)
        rubric = _build_rubric(available_models)
        _logger.info("LLMRoutingClient: available_models=%s", dict(available_models))
        try:
            response = await self._llm.create(
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
            )
            text = response.output[0].content[0].text
            _logger.info("LLMRoutingClient: raw response: %s", text[:500])
            verdict = json.loads(text)
        except Exception:  # noqa: BLE001  # fail-open
            _logger.warning("LLMRoutingClient: judge call failed", exc_info=True)
            return None

        model = verdict.get("model")
        rationale = verdict.get("rationale", "")
        if not model or not isinstance(model, str):
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


class ExternalRoutingClient:
    """Routing client backed by an external ``routes:select`` service.

    Calls an external routing service (the Databricks AI-Gateway router,
    or any endpoint speaking the ``omnigent.api.routing.v1`` proto)
    instead of running a local judge. The candidate models come from
    ``available_models`` (the same live catalog the built-in judge sees),
    so no catalog plumbing changes. A failure or empty selection returns
    ``None`` so the turn proceeds on the agent's default model.
    """

    def __init__(
        self,
        *,
        base_url: str,
        router_name: str,
        auth: httpx.Auth | None = None,
        databricks_profile: str | None = None,
        model_prefixes: list[str] | None = None,
        request_timeout: float = 20.0,
    ) -> None:
        """
        :param base_url: Routing service base, e.g.
            ``"https://host/ai-gateway/routing/v1"``.
            ``/routes:select`` is appended.
        :param router_name: Router strategy name, e.g. ``"task_v0"``.
        :param auth: Optional static httpx auth (e.g. a bearer built from an
            explicit ``api_key``). ``None`` for an unauthenticated endpoint or
            when *databricks_profile* supplies per-call OAuth instead.
        :param databricks_profile: Optional Databricks CLI profile. When set
            (and *auth* is ``None``), a fresh bearer is minted per :meth:`route`
            call via the databricks-sdk ``Config`` — which refreshes OAuth
            tokens transparently, so a long-lived server never sends a stale
            token (the 401 an at-startup captured token hits after ~1h).
        :param model_prefixes: Optional prefixes this deployment's catalog
            attaches to model ids that the router does NOT expect. The
            first matching prefix is stripped from ids sent to the router
            and restored on its answer via the (harness, bare-id) -> local
            map. Examples: ``"databricks-"`` when serving-endpoint names are
            ``databricks-claude-opus-4-8`` but the router keys on
            ``claude-opus-4-8``; ``"system.ai."`` for Unity Catalog
            foundation-model ids like ``system.ai.claude-opus-4-8``. Empty
            or omitted (default) sends catalog ids verbatim — no provider
            assumed.
        :param request_timeout: Per-call timeout in seconds; routing
            runs once per turn so a slow router can't stall forever.
        """
        self._url = base_url.rstrip("/") + "/" + ROUTES_SELECT_PATH
        self._router_name = router_name
        self._auth = auth
        self._databricks_profile = databricks_profile
        # Cached SDK Config for the profile (created lazily), reused across
        # calls; its authenticate() refreshes the OAuth token as needed.
        self._sdk_config: Config | None = None
        self._model_prefixes = model_prefixes or []
        self._request_timeout = request_timeout
        # Human-readable reason the most recent route() returned None, for the
        # caller to surface in the UI (a bare None hides the actual cause, e.g.
        # a 401 or the task_v0 required-model-set error). Set on every failure
        # path, cleared on success.
        self.last_error: str | None = None

    def _resolve_auth(self) -> httpx.Auth | None:
        """Return the auth for a request, refreshing an OAuth token per call.

        A static *auth* (explicit api_key) is returned as-is. When only a
        Databricks profile is configured, mint a fresh bearer from the cached
        SDK ``Config`` so token expiry never surfaces as a router 401.
        """
        if self._auth is not None:
            return self._auth
        if self._databricks_profile is None:
            return None
        try:
            if self._sdk_config is None:
                from databricks.sdk.config import Config

                self._sdk_config = Config(profile=self._databricks_profile)
            headers = self._sdk_config.authenticate()
        except Exception:  # noqa: BLE001 — auth failure degrades to unauthenticated
            _logger.warning(
                "ExternalRoutingClient: could not resolve auth for profile %r",
                self._databricks_profile,
                exc_info=True,
            )
            return None
        token = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not token:
            return None
        return _bearer_auth(token)

    def _to_router_id(self, model: str) -> str:
        """Strip the first matching ``model_prefixes`` entry for the router.

        A no-op when no configured prefix matches *model* (or none is set).
        """
        for prefix in self._model_prefixes:
            if prefix and model.startswith(prefix):
                return model[len(prefix) :]
        return model

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> RoutingResult | None:
        import httpx
        from google.protobuf import json_format

        from omnigent.api.routing.v1 import routing_pb2 as pb

        # Send router-vocabulary ids (model_prefixes stripped) and keep a
        # (harness, router-id) -> local-id map to recover the exact catalog id
        # from the answer. Harness is part of the key because one bare id can
        # be served under different harnesses (Databricks-authed PI vs a Codex
        # subscription) that must map back to distinct local ids.
        options: list[pb.RouteOption] = []
        router_to_local: dict[tuple[str, str], str] = {}
        for harness, models in available_models.items():
            for model in models:
                router_id = self._to_router_id(model)
                router_to_local[(harness, router_id)] = model
                options.append(pb.RouteOption(model=router_id, harness=harness))
        if not options:
            return None
        request = pb.SelectRouteRequest(
            route_options=options,
            task=pb.Task(prompt=message[:4000]),
            route_selector=pb.RouteSelector(router_name=self._router_name),
        )
        # snake_case wire format — the router uses the proto field names.
        body = json_format.MessageToDict(request, preserving_proto_field_name=True)
        _logger.info("ExternalRoutingClient: available_models=%s", dict(available_models))
        _logger.info("ExternalRoutingClient: POST %s body=%s", self._url, body)
        # Resolve auth per call (SDK token refresh is a blocking HTTP call, so
        # run it off the event loop) — keeps a long-lived server from sending a
        # token that has expired since startup.
        import asyncio

        auth = await asyncio.to_thread(self._resolve_auth)
        self.last_error = None
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as http:
                resp = await http.post(
                    self._url,
                    headers={"Content-Type": "application/json"},
                    json=body,
                    auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                )
        except httpx.HTTPError as exc:
            # Transport-level failure (connect/timeout/DNS): no response body.
            _logger.warning("ExternalRoutingClient: routes:select request failed: %s", exc)
            self.last_error = f"router request failed: {exc}"
            return None
        if resp.status_code >= 400:
            # Log the response body — the gateway puts the actual reason there
            # (e.g. task_v0's required-model-set error), which the bare status
            # code from raise_for_status() omits.
            _logger.warning(
                "ExternalRoutingClient: routes:select returned %s: %s",
                resp.status_code,
                resp.text[:2000],
            )
            self.last_error = (
                f"router returned HTTP {resp.status_code}: {_router_error_detail(resp.text)}"
            )
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
        # Map the router's pick back to the local catalog id, rejecting an
        # out-of-set model (falls back to an id-only match when the router
        # omits the harness).
        local_model = router_to_local.get((selected.harness, selected.model))
        if local_model is None:
            local_model = next(
                (
                    local
                    for (_harness, router_id), local in router_to_local.items()
                    if router_id == selected.model
                ),
                None,
            )
        if local_model is None:
            _logger.warning(
                "ExternalRoutingClient: router returned model %r (harness %r) "
                "not in the candidate set; ignoring",
                selected.model,
                selected.harness,
            )
            self.last_error = f"router picked model {selected.model!r} outside the candidate set"
            return None
        return RoutingResult(
            model=local_model,
            rationale=out.rationale,
            harness=selected.harness or None,
        )


# ── Public API ──────────────────────────────────────────────────────────────

# SDK harnesses offered as candidates when the user picks "auto" harness.
# Native harnesses are excluded: they require CLI binaries that may not be
# installed, and they bake the model at terminal launch rather than per-turn.
#
# Order matters: it is the insertion order of the candidate set sent to the
# router AND the tiebreak order when a model is served by multiple harnesses
# (both the external router's id-only fallback and our own model-ownership
# fallback pick the FIRST harness owning the model). codex precedes pi so GPT
# models default to codex — which uses the Responses API and handles gpt-5.5+
# reasoning models with tools, whereas pi's openai-completions path 400s on
# them. claude-sdk owns Claude models; pi is the last-resort multi-model home.
_AUTO_ROUTING_HARNESSES: tuple[str, ...] = ("claude-sdk", "codex", "pi")

# The live runner catalog (fetch_runner_models) keys rows by WORKER name — the
# sub-agent names declared in the parent spec (e.g. "claude_code") plus "self"
# for the session's own harness — NOT by harness id. Map the common worker
# names back to their harness id so a child session's catalog still yields
# routable candidates. Unknown worker names are ignored.
_WORKER_NAME_TO_HARNESS: dict[str, str] = {
    "claude_code": "claude-sdk",
    "claude-sdk": "claude-sdk",
    "codex": "codex",
    "pi": "pi",
}


def _redirect_incompatible_pick(
    harness: str | None,
    model: str,
    harness_catalog: dict[str, list[_RunnerModel]],
) -> str | None:
    """Redirect a router verdict off a harness that can't serve *model*.

    Pi speaks Anthropic Messages for Claude-family models. When the runner
    catalog explicitly reports that the selected endpoint does not accept that
    wire, move the verdict to ``claude-sdk``. Unknown metadata from an older
    runner is not treated as incompatible.

    :param harness: The router's chosen harness id (may be ``None``).
    :param model: The router's chosen model id.
    :param harness_catalog: Catalog metadata keyed by normalized harness id.
    :returns: A replacement harness id, or the original *harness* when the
        pick is already compatible.
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
    ):
        return "claude-sdk"
    return harness


async def route_session_harness(
    user_message: str,
    *,
    session_id: str | None = None,
    catalog_session_id: str | None = None,
    runner_client: httpx.AsyncClient | None = None,
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
    :returns: ``(harness, model, verdict, error)`` — on success ``error`` is
        ``None``; on failure ``harness``, ``model``, and ``verdict`` are ``None``
        and ``error`` carries a human-readable reason shown in the UI.
    """
    if not user_message:
        return None, None, None, None
    try:
        from omnigent.runtime._globals import _caps
    except ImportError:
        return None, None, None, "Intelligent routing is not available."

    if _caps is None or _caps.routing_client is None:
        return None, None, None, "Intelligent routing is not configured on this server."

    # Fetch the live catalog. Its rows are keyed by worker name (sub-agent
    # names + "self"), so normalize those to harness ids before matching
    # against _AUTO_ROUTING_HARNESSES. Prefer catalog_session_id (the parent
    # for a sub-agent) so the candidate set is the full spawnable-worker map,
    # independent of whether the routed session is top-level or a sub-agent.
    _catalog_sid = catalog_session_id or session_id
    live_catalog: dict[str, list[_RunnerModel]] | None = None
    if _catalog_sid and runner_client is not None:
        live_catalog = await _fetch_runner_catalog(_catalog_sid, runner_client)

    # NOTE: we do NOT filter incompatible (harness, model) pairs out of the
    # candidate set here. The external router (task_v0) enforces a required
    # model set and 400s if any required model is missing, so dropping e.g.
    # gpt-5-6-luna would break the whole request. Instead we send the full set
    # and correct an incompatible verdict afterward via
    # _redirect_incompatible_pick.
    harness_models: dict[str, list[str]] = {}
    harness_catalog: dict[str, list[_RunnerModel]] = {}
    if live_catalog:
        for worker_name, worker_catalog in live_catalog.items():
            harness = _WORKER_NAME_TO_HARNESS.get(worker_name)
            if harness is None or harness not in _AUTO_ROUTING_HARNESSES:
                continue
            if worker_catalog:
                # First worker wins for a given harness id (dedupe).
                harness_catalog.setdefault(harness, worker_catalog)
                harness_models.setdefault(harness, [entry.id for entry in worker_catalog])

    if not harness_models:
        return None, None, None, "No discovered routable harnesses are available on this runner."

    try:
        result = await _caps.routing_client.route(user_message, harness_models)
    except Exception as exc:  # routing failures must not block session creation
        _logger.exception("smart_routing: route_session_harness failed")
        return None, None, None, f"Routing call failed: {exc}"

    if result is None:
        # Surface the client's specific failure reason (e.g. HTTP 401 with the
        # gateway's message) when it exposes one; otherwise a generic note.
        detail = getattr(_caps.routing_client, "last_error", None)
        reason = (
            f"Routing unavailable: {detail}"
            if detail
            else "The router returned no verdict; using default harness."
        )
        return None, None, None, reason

    # Use the router's harness pick only when it names one of our candidates
    # AND the chosen model is in that harness's list (avoids mismatches).
    if result.harness in harness_models and result.model in harness_models[result.harness]:
        chosen_harness = result.harness
    else:
        if result.harness and result.harness not in harness_models:
            _logger.debug(
                "smart_routing: router harness %r not in candidate set; "
                "falling back to model-ownership lookup",
                result.harness,
            )
        elif result.harness and result.model not in harness_models.get(result.harness, []):
            _logger.debug(
                "smart_routing: router harness %r does not own model %r; "
                "falling back to model-ownership lookup",
                result.harness,
                result.model,
            )
        chosen_harness = None
        for h, models in harness_models.items():
            if result.model in models:
                chosen_harness = h
                break

    # Redirect a catalog-proven wire mismatch after routing so the complete
    # candidate set still reaches external routers that require it.
    _redirected = _redirect_incompatible_pick(chosen_harness, result.model, harness_catalog)
    if _redirected != chosen_harness:
        _logger.info(
            "smart_routing: redirecting incompatible pick harness=%s model=%s -> harness=%s",
            chosen_harness,
            result.model,
            _redirected,
        )
        chosen_harness = _redirected

    _logger.info(
        "smart_routing: auto-harness harness=%s model=%s rationale=%s",
        chosen_harness,
        result.model,
        result.rationale,
    )
    return (
        chosen_harness,
        result.model,
        {"model": result.model, "rationale": result.rationale},
        None,
    )


async def route_turn(
    _harness: str | None,
    user_message: str,
    *,
    session_id: str | None = None,
    runner_client: httpx.AsyncClient | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Pick the best model for a turn via :attr:`RuntimeCaps.routing_client`.

    Fetches live model availability from the runner's
    ``/v1/sessions/{id}/models`` endpoint. Routing is skipped when discovery
    is unavailable so the harness can use its provider-resolved default.
    """
    try:
        from omnigent.runtime._globals import _caps
    except ImportError:
        return None, None

    if _caps is None or _caps.routing_client is None:
        return None, None

    # Prefer live runner catalog — but only the "self" worker entry.
    # The catalog includes sub-agent workers (claude_code, pi, codex…);
    # for brain-turn routing we only want the models this session's own
    # harness can run, not the sub-agents' model lists.
    available: dict[str, list[str]] | None = None
    if session_id and runner_client is not None:
        catalog = await fetch_runner_models(session_id, runner_client)
        if catalog and "self" in catalog:
            available = {"self": catalog["self"]}
    if not available:
        return None, None

    result = await _caps.routing_client.route(user_message, available)
    if result is None:
        return None, None

    _logger.info(
        "smart_routing: model=%s rationale=%s",
        result.model,
        result.rationale,
    )
    return result.model, {"model": result.model, "rationale": result.rationale}
