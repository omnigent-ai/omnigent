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

if TYPE_CHECKING:
    import httpx  # used in type annotations only; runtime import is lazy in fetch_runner_models

_logger = logging.getLogger(__name__)

# Custom-method path (Google API convention) appended to the external
# router's base URL, e.g. ``<base_url>/routes:select``.
ROUTES_SELECT_PATH = "routes:select"

# ── Model lists per harness family ──────────────────────────────────────────
#
# Ordered cheapest → most powerful within each family.

MODEL_LISTS: dict[str, list[str]] = {
    "claude": [
        "databricks-claude-haiku-4-5",
        "databricks-claude-sonnet-4-6",
        "databricks-claude-opus-4-8",
    ],
    "gpt": [
        "databricks-gpt-5-4-nano",
        "databricks-gpt-5-4-mini",
        "databricks-gpt-5-4",
        "databricks-gpt-5-5",
    ],
    # pi is multi-model: Claude and GPT both available.
    "pi": [
        "databricks-gpt-5-4-nano",
        "databricks-claude-haiku-4-5",
        "databricks-gpt-5-4-mini",
        "databricks-claude-sonnet-4-6",
        "databricks-gpt-5-4",
        "databricks-claude-opus-4-8",
        "databricks-gpt-5-5",
    ],
}

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


def infer_models(harness: str | None) -> list[str] | None:
    """Return available models for *harness*, or ``None`` if unroutable."""
    if harness is None:
        return None
    family = _HARNESS_FAMILY.get(harness)
    if family is None:
        return None
    return MODEL_LISTS.get(family)


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


async def fetch_runner_models(
    session_id: str,
    runner_client: httpx.AsyncClient,
) -> dict[str, list[str]] | None:
    """Fetch live model availability from the runner's ``/v1/sessions/{id}/models`` endpoint.

    Converts the ``sys_list_models``-shaped catalog into the harness →
    model-id-list format expected by :class:`RoutingClient`.  Falls back
    to ``None`` on any HTTP/parse failure so callers can use the static
    :func:`infer_models` table instead.

    :param session_id: Session/conversation identifier.
    :param runner_client: Async HTTP client pointed at the runner.
    :returns: ``{harness: [model_id, ...]}`` ordered cheapest → most
        powerful, or ``None`` when the endpoint is unavailable or the
        response cannot be parsed.
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

    result: dict[str, list[str]] = {}
    for worker_name, row in workers.items():
        if not isinstance(row, dict):
            continue
        models_raw = row.get("models", [])
        if not isinstance(models_raw, list):
            continue
        ids = [m["id"] for m in models_raw if isinstance(m, dict) and isinstance(m.get("id"), str)]
        if ids:
            result[worker_name] = ids
    return result or None


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

Model tiers (cheapest → most capable within each family):
- Claude: haiku < sonnet < opus
- GPT: *-nano < *-mini < base (e.g. gpt-5-4-nano < gpt-5-4-mini < gpt-5-4 < gpt-5-5)

Trade-off guidance — classify the task and pick the corresponding model:

  SIMPLE   → cheapest available model (haiku for Claude; nano for GPT)
             Examples: greetings, quick lookups, one-line fixes, trivial Q&A.

  MODERATE → mid-range model (sonnet for Claude; mini for GPT)
             Examples: single-file edits, debugging a known issue, brief explanations.

  COMPLEX  → most capable model (opus for Claude; newest base GPT)
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
    return _JUDGE_SYSTEM_TEMPLATE.format(harness_menu="\n".join(sections))


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

# Routing-strategy name recorded in the ``SelectRouteRequest`` the built-in
# judge constructs. External routers carry the operator's ``router_name``;
# the local judge has no remote strategy, so it labels its own.
_LLM_ROUTER_NAME = "llm"


def _build_route_options(
    available_models: dict[str, list[str]],
    to_router_id: Any = None,  # type: ignore[explicit-any]  # Callable[[str], str] | None
) -> tuple[list[Any], dict[str, str]]:  # type: ignore[explicit-any]  # list[pb.RouteOption]
    """Build proto ``route_options`` from a harness → models catalog.

    :param available_models: Mapping of harness → catalog model ids.
    :param to_router_id: Optional transform applied to each id before it is
        sent (e.g. stripping a provider prefix). Identity when ``None``.
    :returns: ``(route_options, router_id -> local_id map)``. The map
        recovers the exact catalog id from the router's (transformed) answer.
    """
    from omnigent.api.routing.v1 import routing_pb2 as pb

    options: list[Any] = []  # type: ignore[explicit-any]
    router_to_local: dict[str, str] = {}
    for harness, models in available_models.items():
        for model in models:
            router_id = to_router_id(model) if to_router_id is not None else model
            router_to_local[router_id] = model
            options.append(pb.RouteOption(model=router_id, harness=harness))
    return options, router_to_local


def _result_from_response(
    response: Any,  # type: ignore[explicit-any]  # pb.SelectRouteResponse
    router_to_local: dict[str, str],
) -> RoutingResult | None:
    """Map the first ``route_selection`` back to a :class:`RoutingResult`.

    Recovers the local catalog id via *router_to_local*, falling back to the
    returned id if the router answered with something we did not send.
    """
    if not response.route_selection:
        return None
    selected = response.route_selection[0].route_option
    if not selected.model:
        return None
    return RoutingResult(
        model=router_to_local.get(selected.model, selected.model),
        rationale=response.rationale,
        harness=selected.harness or None,
    )


class _ProtoRoutingClient:
    """Shared skeleton for routers speaking the ``routing.v1`` proto.

    Both concrete clients follow the same shape — build a
    ``SelectRouteRequest`` from the catalog, obtain a ``SelectRouteResponse``
    (via :meth:`_select`), and map it to a :class:`RoutingResult`. Subclasses
    supply only the decision step (an HTTP call, or a local LLM judge) and,
    optionally, an id transform.
    """

    _router_name: str = ""

    def _to_router_id(self, model: str) -> str:
        """Transform a catalog id into the router's vocabulary. Identity by default."""
        return model

    async def _select(
        self,
        request: Any,  # type: ignore[explicit-any]  # pb.SelectRouteRequest
    ) -> Any | None:  # type: ignore[explicit-any]  # pb.SelectRouteResponse | None
        """Decide a route for *request*. Returns ``None`` to skip routing."""
        raise NotImplementedError

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> RoutingResult | None:
        from omnigent.api.routing.v1 import routing_pb2 as pb

        options, router_to_local = _build_route_options(available_models, self._to_router_id)
        if not options:
            return None
        request = pb.SelectRouteRequest(
            route_options=options,
            task=pb.Task(prompt=message[:4000]),
            route_selector=pb.RouteSelector(router_name=self._router_name),
        )
        response = await self._select(request)
        if response is None:
            return None
        return _result_from_response(response, router_to_local)


class LLMRoutingClient(_ProtoRoutingClient):
    """Built-in routing client: a local LLM judge that answers the same proto.

    Implements the ``routes:select`` contract locally — it builds a
    ``SelectRouteResponse`` by prompting the server-level ``PolicyLLMClient``
    and validating the verdict against the request's ``route_options``
    (clamping a hallucinated model to the cheapest offered, re-resolving the
    harness). Same request/response shape as :class:`ExternalRoutingClient`.
    """

    _router_name = _LLM_ROUTER_NAME

    def __init__(self, llm_client: Any) -> None:  # type: ignore[explicit-any]
        self._llm = llm_client

    async def _select(
        self,
        request: Any,  # type: ignore[explicit-any]  # pb.SelectRouteRequest
    ) -> Any | None:  # type: ignore[explicit-any]  # pb.SelectRouteResponse | None
        from omnigent.api.routing.v1 import routing_pb2 as pb

        # Regroup the request's route_options into the harness → models shape
        # the rubric + clamping logic works on (insertion order preserved).
        grouped: dict[str, list[str]] = {}
        for opt in request.route_options:
            grouped.setdefault(opt.harness, []).append(opt.model)
        flat = _flatten_models(grouped)
        rubric = _build_rubric(grouped)
        _logger.info("LLMRoutingClient: available_models=%s", grouped)
        try:
            response = await self._llm.create(
                instructions=rubric,
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": request.task.prompt}],
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
                _logger.info("LLMRoutingClient: clamping unknown model %r to %s", model, flat[0])
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
            or chosen_harness not in grouped
            or model not in grouped[chosen_harness]
        ):
            if isinstance(chosen_harness, str) and chosen_harness in grouped:
                _logger.info(
                    "LLMRoutingClient: harness %r does not contain model %r; re-resolving",
                    chosen_harness,
                    model,
                )
            chosen_harness = next((h for h, models in grouped.items() if model in models), None)

        out = pb.SelectRouteResponse(rationale=str(rationale))
        selection = out.route_selection.add()
        selection.route_option.model = model
        if chosen_harness:
            selection.route_option.harness = chosen_harness
        return out


def _bearer_auth(token: str) -> Any:  # type: ignore[explicit-any]  # returns httpx.Auth
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


class ExternalRoutingClient(_ProtoRoutingClient):
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
        auth: Any = None,  # type: ignore[explicit-any]  # httpx.Auth, imported lazily
        model_prefix: str = "",
        request_timeout: float = 20.0,
    ) -> None:
        """
        :param base_url: Routing service base, e.g.
            ``"https://host/ai-gateway/routing/v1"``.
            ``/routes:select`` is appended.
        :param router_name: Router strategy name, e.g. ``"task_v0"``.
        :param auth: Optional httpx auth (a Databricks bearer for the
            router's host). ``None`` for an unauthenticated endpoint.
        :param model_prefix: Optional prefix this deployment's catalog
            attaches to model ids that the router does NOT expect (e.g.
            ``"databricks-"`` when serving-endpoint names are
            ``databricks-claude-opus-4-8`` but the router keys on
            ``claude-opus-4-8``). Stripped from ids sent to the router and
            restored on the router's answer via the bare -> local map.
            Empty (default) sends catalog ids verbatim — no provider assumed.
        :param request_timeout: Per-call timeout in seconds; routing
            runs once per turn so a slow router can't stall forever.
        """
        self._url = base_url.rstrip("/") + "/" + ROUTES_SELECT_PATH
        self._router_name = router_name
        self._auth = auth
        self._model_prefix = model_prefix
        self._request_timeout = request_timeout

    def _to_router_id(self, model: str) -> str:
        """Strip the configured ``model_prefix`` for the router's vocabulary.

        A no-op when ``model_prefix`` is empty or absent from *model*.
        """
        if self._model_prefix and model.startswith(self._model_prefix):
            return model[len(self._model_prefix) :]
        return model

    async def _select(
        self,
        request: Any,  # type: ignore[explicit-any]  # pb.SelectRouteRequest
    ) -> Any | None:  # type: ignore[explicit-any]  # pb.SelectRouteResponse | None
        import httpx
        from google.protobuf import json_format

        from omnigent.api.routing.v1 import routing_pb2 as pb

        # snake_case wire format — the router uses the proto field names.
        body = json_format.MessageToDict(request, preserving_proto_field_name=True)
        _logger.info("ExternalRoutingClient: POST %s body=%s", self._url, body)
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as http:
                resp = await http.post(
                    self._url,
                    headers={"Content-Type": "application/json"},
                    json=body,
                    auth=self._auth,
                )
        except httpx.HTTPError as exc:
            # Transport-level failure (connect/timeout/DNS): no response body.
            _logger.warning("ExternalRoutingClient: routes:select request failed: %s", exc)
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
            return None
        try:
            return json_format.ParseDict(resp.json(), pb.SelectRouteResponse())
        except (ValueError, json_format.ParseError):
            _logger.warning(
                "ExternalRoutingClient: could not parse routes:select response: %s",
                resp.text[:2000],
            )
            return None


# ── Public API ──────────────────────────────────────────────────────────────


async def route_turn(
    harness: str | None,
    user_message: str,
    *,
    session_id: str | None = None,
    runner_client: httpx.AsyncClient | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Pick the best model for a turn via :attr:`RuntimeCaps.routing_client`.

    When *session_id* and *runner_client* are provided, fetches live model
    availability from the runner's ``/v1/sessions/{id}/models`` endpoint.
    Falls back to the static :func:`infer_models` lookup table if the runner
    is unreachable or returns no data.
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
        models = infer_models(harness)
        if models is None:
            return None, None
        available = {harness or "": models}

    result = await _caps.routing_client.route(user_message, available)
    if result is None:
        return None, None

    _logger.info(
        "smart_routing: model=%s rationale=%s",
        result.model,
        result.rationale,
    )
    return result.model, {"model": result.model, "rationale": result.rationale}
