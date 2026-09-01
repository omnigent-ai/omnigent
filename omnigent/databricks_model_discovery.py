"""Live Databricks model discovery for native coding harnesses."""

from __future__ import annotations

import logging
import re
import warnings
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

_logger = logging.getLogger(__name__)

CLAUDE_MODEL_FAMILIES: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

_MODEL_SERVICES_PATH = "/api/2.1/unity-catalog/model-services"
_ANTHROPIC_MODELS_PATH = "/ai-gateway/anthropic/v1/models"
_MODEL_SERVICE_PREFIX = "model-services/"
_SYSTEM_MODEL_PREFIX = "system.ai."
_MODEL_SERVICES_MAX_RESULTS = 1000
_MODEL_SERVICES_PARENT = "schemas/system.ai"
_PAGE_SIZE = 100
_MAX_PAGES = 100
_HTTP_TIMEOUT_S = 10.0


#: Catalog spellings the same endpoint can be served under. Ordered by
#: preference: a workspace exposing both keeps the ``databricks-`` id, so every
#: consumer (routing candidates, the model picker, the launch alias pins) names
#: a model the same way no matter which listing answered.
_CATALOG_SPELLINGS: tuple[str, ...] = ("databricks-", _SYSTEM_MODEL_PREFIX)

# DATABRICKS-PATCH(codex-live-model-discovery)
#: ``gpt-5-6-sol`` → ``("gpt", "5", "6", "sol")``. Mirrors
#: ``codex_model_vocabulary._GPT_ID_RE`` without reaching into its privates.
_GPT_VERSIONED_ID_RE = re.compile(r"^(gpt|codex)-(\d+)-(\d+)(?:-([a-z0-9]+))?$")


def _bare_model_id(model_id: str) -> str:
    """Strip the catalog spelling so ids compare across vocabularies."""
    lowered = model_id.lower()
    for prefix in _CATALOG_SPELLINGS:
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def _natural_model_key(model_id: str) -> tuple[tuple[int, str | int], ...]:
    """Return a comparison key that orders numeric model versions naturally.

    Keyed on the bare id so the catalog spelling never outranks the version.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", _bare_model_id(model_id))
        if part
    )


def _prefer_databricks_spelling(model_ids: Iterable[str]) -> list[str]:
    """Collapse duplicate spellings of one model onto the preferred one.

    :param model_ids: Catalog ids from one or more listings, possibly naming
        the same endpoint under two spellings.
    :returns: One id per model, sorted, with ``databricks-`` winning ties.
    """
    best: dict[str, str] = {}
    for model_id in model_ids:
        bare = _bare_model_id(model_id)
        current = best.get(bare)
        if current is None or _spelling_rank(model_id) < _spelling_rank(current):
            best[bare] = model_id
    return sorted(best.values())


def _spelling_rank(model_id: str) -> int:
    """Rank a catalog spelling; lower wins."""
    lowered = model_id.lower()
    for rank, prefix in enumerate(_CATALOG_SPELLINGS):
        if lowered.startswith(prefix):
            return rank
    return len(_CATALOG_SPELLINGS)


def _claude_family_of(model_id: str, *, marker: str) -> str | None:
    """Return the Claude family *model_id* belongs to, if any."""
    _, separator, suffix = model_id.lower().partition(marker)
    if not separator:
        return None
    segments = suffix.split("-")
    return next((family for family in CLAUDE_MODEL_FAMILIES if family in segments), None)


def _models_by_claude_family(model_ids: list[str], *, marker: str) -> dict[str, str]:
    """Select the newest model id for every Claude family in *model_ids*."""
    result: dict[str, str] = {}
    for family in CLAUDE_MODEL_FAMILIES:
        candidates = [
            model_id
            for model_id in model_ids
            if _claude_family_of(model_id, marker=marker) == family
        ]
        if candidates:
            result[family] = max(candidates, key=_natural_model_key)
    return result


def _all_claude_models(model_ids: list[str], *, marker: str) -> tuple[str, ...]:
    """Keep every Claude-family id in *model_ids*, newest first per family."""
    claude_ids = [
        model_id
        for model_id in model_ids
        if _claude_family_of(model_id, marker=marker) is not None
    ]
    return tuple(sorted(claude_ids, key=_natural_model_key, reverse=True))


def preferred_served_claude_model(
    served_ids: Iterable[str], *, preferred_model_id: str
) -> str | None:
    """Choose a served Claude id closest to *preferred_model_id*.

    Equivalent ``databricks-`` and ``system.ai.`` spellings identify the same
    endpoint. Otherwise, the newest served model in the requested family wins.
    Cross-family fallback is the caller's job (tier precedence lives there),
    so no same-family match returns ``None``.

    :param served_ids: Model ids an authoritative workspace listing returned.
    :param preferred_model_id: The implicit catalog default to match.
    :returns: A served id, or ``None`` when no compatible Claude id is known.
    """
    preferred_family = _claude_family_of(preferred_model_id, marker="claude-")
    if preferred_family is None:
        return None
    claude_ids = _prefer_databricks_spelling(
        model_id
        for model_id in served_ids
        if _claude_family_of(model_id, marker="claude-") is not None
    )
    if not claude_ids:
        return None
    preferred_bare_id = _bare_model_id(preferred_model_id)
    equivalent = next(
        (model_id for model_id in claude_ids if _bare_model_id(model_id) == preferred_bare_id),
        None,
    )
    if equivalent is not None:
        return equivalent
    return _models_by_claude_family(claude_ids, marker="claude-").get(preferred_family)


def _list_model_service_ids(
    client: httpx.Client,
    workspace_url: str,
    headers: dict[str, str],
) -> list[str]:
    """List Databricks-managed ``system.ai`` model-service identifiers."""
    model_ids: list[str] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(_MAX_PAGES):
        params: dict[str, str] = {
            "max_results": str(_MODEL_SERVICES_MAX_RESULTS),
            "parent": _MODEL_SERVICES_PARENT,
        }
        if page_token is not None:
            params["page_token"] = page_token
        response = client.get(
            f"{workspace_url.rstrip('/')}{_MODEL_SERVICES_PATH}",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        payload: object = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Databricks model-services response must be an object")
        services = payload.get("model_services")
        if isinstance(services, list):
            for service in services:
                raw_name = service.get("name") if isinstance(service, dict) else None
                if not isinstance(raw_name, str):
                    continue
                model_id = raw_name.strip()
                if model_id.startswith(_MODEL_SERVICE_PREFIX):
                    model_id = model_id[len(_MODEL_SERVICE_PREFIX) :]
                if model_id.startswith(_SYSTEM_MODEL_PREFIX):
                    model_ids.append(model_id)
        raw_next = payload.get("next_page_token")
        if not isinstance(raw_next, str) or not raw_next:
            break
        if raw_next in seen_tokens:
            raise ValueError("Databricks model-services pagination repeated a page token")
        seen_tokens.add(raw_next)
        page_token = raw_next
    else:
        # Exhausted the page budget with a next-page token still pending: the
        # listing is partial, so the newest model of a family could be missed.
        _logger.warning(
            "Databricks model-services listing truncated after %d pages; "
            "discovery may miss newer models",
            _MAX_PAGES,
        )
    return sorted(set(model_ids))


def _list_anthropic_gateway_ids(
    client: httpx.Client,
    workspace_url: str,
    headers: dict[str, str],
) -> list[str]:
    """List legacy Databricks Anthropic-gateway model identifiers."""
    response = client.get(
        f"{workspace_url.rstrip('/')}{_ANTHROPIC_MODELS_PATH}",
        headers=headers,
    )
    response.raise_for_status()
    payload: object = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Databricks Anthropic models response must be an object")
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [
        model_id
        for item in data
        if isinstance(item, dict)
        and isinstance((model_id := item.get("id")), str)
        and model_id
        and not model_id.endswith("-anthropic")
    ]


@dataclass(frozen=True)
class DatabricksClaudeCatalog:
    """Every Claude endpoint a workspace serves, plus the family picks.

    :param families: Family alias → newest routable id, e.g.
        ``{"opus": "system.ai.claude-opus-5"}``. What the launch env pins
        each Claude Code alias to.
    :param model_ids: Every Claude-family id the workspace serves, newest
        first, e.g. ``("system.ai.claude-opus-5",
        "system.ai.claude-opus-4-8")``. A superset of ``families``: an
        older generation is still servable and still routable, it just
        does not own an alias.
    """

    families: dict[str, str]
    model_ids: tuple[str, ...]


def discover_databricks_claude_catalog(
    workspace_url: str,
    token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> DatabricksClaudeCatalog:
    """Discover every Claude endpoint a Databricks workspace serves.

    Both listings are consulted, because a workspace can serve the same
    endpoint under both spellings (``system.ai.claude-opus-5`` from Unity
    Catalog model services, ``databricks-claude-opus-5`` from the Anthropic AI
    Gateway) and answering with whichever listing happened to succeed makes the
    catalog nondeterministic. Duplicates collapse onto the ``databricks-``
    spelling so every consumer names a model the same way.

    The gateway listing is therefore issued even when Unity Catalog already
    named Claude models — short-circuiting on the UC hit would cost one HTTP
    round trip less per launch, but UC only ever spells ids ``system.ai.``, so
    the spelling a consumer sees would depend on whether the (transiently
    failing) UC call answered.

    :param workspace_url: Workspace origin, e.g. ``"https://example.com"``.
    :param token: Workspace bearer token.
    :param transport: Optional HTTP transport used by tests.
    :returns: The workspace's Claude catalog. Empty ``families`` with empty
        ``model_ids`` is authoritative: the model-services listing answered
        successfully and no Claude models are exposed.
    :raises httpx.HTTPError: When the primary listing fails and the fallback
        cannot compensate (it fails too, or exposes no Claude models).
    :raises ValueError: Same contract for malformed responses.
    """
    headers = {"Authorization": f"Bearer {token}"}
    primary_error: Exception | None = None
    gateway_error: Exception | None = None
    model_service_ids: list[str] = []
    gateway_ids: list[str] = []
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        try:
            model_service_ids = _list_model_service_ids(client, workspace_url, headers)
        except (httpx.HTTPError, ValueError) as exc:
            primary_error = exc
        try:
            gateway_ids = _list_anthropic_gateway_ids(client, workspace_url, headers)
        except (httpx.HTTPError, ValueError) as exc:
            gateway_error = exc

    if primary_error is not None and gateway_error is not None:
        raise gateway_error from primary_error

    merged = _prefer_databricks_spelling([*model_service_ids, *gateway_ids])
    models = _models_by_claude_family(merged, marker="claude-")
    if models:
        return DatabricksClaudeCatalog(
            families=models,
            model_ids=_all_claude_models(merged, marker="claude-"),
        )
    if primary_error is not None:
        # Neither listing named a Claude model and the authoritative one failed
        # — an empty result here is NOT authoritative (e.g. a transient UC 503
        # plus an unused legacy gateway). Surface the primary failure so callers
        # fall back to cached models instead of treating the workspace as having
        # none.
        raise primary_error
    # A successful permission-aware UC listing is authoritative even when the
    # compatibility endpoint is not enabled.
    return DatabricksClaudeCatalog(families={}, model_ids=())


def discover_databricks_claude_models(
    workspace_url: str,
    token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, str]:
    """Discover the live Claude family mapping for a Databricks workspace.

    .. deprecated:: 0.8.0
        Use :func:`discover_databricks_claude_catalog` and read its
        ``families``, which also carries every servable id. Removed in
        ``v0.10.0``.

    :param workspace_url: Workspace origin, e.g. ``"https://example.com"``.
    :param token: Workspace bearer token.
    :param transport: Optional HTTP transport used by tests.
    :returns: Family aliases mapped to routable model ids. An empty mapping is
        authoritative: the listing answered and no Claude models are exposed.
    :raises httpx.HTTPError: Same contract as the catalog lookup.
    :raises ValueError: Same contract as the catalog lookup.
    """
    warnings.warn(
        "discover_databricks_claude_models() is deprecated and will be removed in "
        "v0.10.0; call discover_databricks_claude_catalog() and read .families.",
        DeprecationWarning,
        stacklevel=2,
    )
    return discover_databricks_claude_catalog(
        workspace_url,
        token,
        transport=transport,
    ).families


# DATABRICKS-PATCH(codex-live-model-discovery)
def discover_databricks_codex_models(
    workspace_url: str,
    token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, ...]:
    """Discover every codex-compatible model a Databricks workspace serves.

    Unity Catalog model services is the only listing that reports what the
    codex Responses route will serve, so ids are ``system.ai.`` by
    construction — unlike the Claude catalog above, which also has a legacy
    gateway listing to merge in.

    :param workspace_url: Workspace origin, e.g. ``"https://example.com"``.
    :param token: Workspace bearer token.
    :param transport: Optional HTTP transport used by tests.
    :returns: Codex-servable model ids, best default first, e.g.
        ``("system.ai.gpt-5-6-sol", "system.ai.gpt-5-5")``. An empty tuple is
        authoritative: the listing answered and exposes no codex model.
    :raises httpx.HTTPError: When the listing cannot be read.
    :raises ValueError: When the listing is malformed.
    """
    from omnigent.model_override import is_codex_compatible_model

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        model_ids = _list_model_service_ids(client, workspace_url, headers)
    codex_ids = [model_id for model_id in model_ids if is_codex_compatible_model(model_id)]
    return tuple(sorted(codex_ids, key=_codex_preference_rank, reverse=True))


def _codex_preference_rank(model_id: str) -> tuple[int, int, int, int, str]:
    """Order codex-servable ids so the best launch default sorts first.

    The listing says what a workspace *can* serve, not which to start on, and a
    name sort has no opinion either (it ranks ``kimi-k3`` over every GPT).
    Tiers, highest first: the owned curated codex catalog in its declared
    cheapest-safe-first order; then versioned ``gpt``/``codex`` ids, newest
    generation first and untiered ahead of a same-generation tier; then the
    rest by name, for determinism.

    :param model_id: A servable id, e.g. ``"system.ai.gpt-5-6-sol"``.
    :returns: A sort key; compare descending.
    """
    from omnigent.codex_model_vocabulary import comparable_model_id
    from omnigent.model_fallbacks import static_model_fallback
    from omnigent.onboarding.provider_config import SUBSCRIPTION_KIND

    bare = comparable_model_id(model_id)
    curated = static_model_fallback(SUBSCRIPTION_KIND, "codex")
    order = [comparable_model_id(m) for m in (curated.model_ids if curated else ())]
    if bare in order:
        # Negated so the earliest curated entry sorts highest under reverse=True.
        return (2, -order.index(bare), 0, 0, "")
    match = _GPT_VERSIONED_ID_RE.match(bare)
    if match is None:
        return (0, 0, 0, 0, bare)
    _family, major, minor, tier = match.groups()
    return (1, int(major), int(minor), 0 if tier else 1, tier or "")


def select_servable_model(requested: str, servable: Iterable[str]) -> str | None:
    """Resolve *requested* against the ids a workspace actually serves.

    Compared on the bare id, so a request naming the legacy ``databricks-``
    spelling resolves to the ``system.ai.`` id serving that same model — only
    the served spelling is routable.

    :param requested: Model id in either vocabulary, e.g.
        ``"databricks-gpt-5-6-luna"``.
    :param servable: Ids the workspace serves, e.g. the result of
        :func:`discover_databricks_codex_models`.
    :returns: The servable id for *requested*, or ``None`` when the workspace
        serves no such model.
    """
    wanted = _bare_model_id(requested)
    for model_id in servable:
        if _bare_model_id(model_id) == wanted:
            return model_id
    return None
