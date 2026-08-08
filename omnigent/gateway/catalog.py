"""Codex model catalog for the gateway servlet.

Translates the workspace's Unity Catalog model-services inventory into the
``ModelsResponse`` shape the Codex CLI consumes from a provider ``/models``
endpoint. Pipeline: filter services by declared API surface, convert
``system.ai.*`` ids to the bare slug Codex has native metadata for, then
dress each slug in Codex's own catalog metadata (injected by the host via a
``codex debug models`` probe) so picker rows, effort ladders, and base
instructions stay native.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

import httpx

from omnigent.codex_model_vocabulary import EXTENDED_CATALOG_MODELS

# Codex speaks the Responses API; services declaring this surface are natively
# servable through the codex gateway path (name-matching would wrongly admit
# chat-only models like gpt-oss-*).
CODEX_API_TYPE = "openai/v1/responses"

# Gateway-translated routing arms: chat-only service ids the gateway serves
# through the Responses dialect anyway (the api-types declaration is a floor,
# not a ceiling — probe-verified per arm, reasoning params included). Included
# only when the workspace actually serves them, and never eligible to be the
# catalog default.
_TRANSLATED_ARM_IDS = frozenset(EXTENDED_CATALOG_MODELS.values())

# Arm service id -> the bare slug shown to codex (``system.ai.glm-5-2`` ->
# ``glm-5-2``). The relay maps the bare spelling back before forwarding
# (:func:`normalize_relay_model_body`) because the gateway only resolves
# bare slugs for mainline GPT.
_ARM_BARE_BY_ID = {full: bare for bare, full in EXTENDED_CATALOG_MODELS.items()}


def normalize_relay_model_body(body: bytes) -> bytes:
    """
    Rewrite a relayed request's bare arm model spelling to its service id.

    The one deliberate exception to the byte-faithful relay: codex speaks the
    bare arm slug the catalog shows (``glm-5-2``); the gateway resolves only
    the ``system.ai.*`` spelling. Anything unparsable or unrelated passes
    through untouched.

    :param body: Raw request body bytes.
    :returns: The body with ``model`` translated, or the original bytes.
    """
    try:
        payload = json.loads(body)
        model = payload.get("model")
        full = EXTENDED_CATALOG_MODELS.get(model)
        if full is None and isinstance(model, str) and model.startswith(_DATABRICKS_LOCAL_PREFIX):
            # Localized ids from pre-servlet catalog surfaces
            # (``databricks-glm-5-2``): resolve them too, so sessions pinned
            # with that spelling still run.
            resolved = service_id_for_slug(model)
            full = resolved if resolved != model else None
        if full is None:
            return body
        payload["model"] = full
        return json.dumps(payload).encode("utf-8")
    except Exception:  # noqa: BLE001 — never let normalization break the relay
        return body


_MODEL_SERVICE_PREFIX = "model-services/"
# Localized spelling some catalog surfaces use for gateway-served models
# (``databricks-glm-5-2``); accepted on input, never emitted.
_DATABRICKS_LOCAL_PREFIX = "databricks-"
_SYSTEM_PREFIX = "system.ai."
# Mainline GPT service ids (``gpt-<major>[-<minor>][-<suffix>]``) convert to
# the dotted OpenAI slug (``gpt-5.6-sol``); anything else stays verbatim —
# the gateway resolves both spellings, but only bare slugs carry native
# Codex metadata.
_MAINLINE_SERVICE_RE = re.compile(r"gpt-(\d+)(?:-(\d+))?(-[a-z][a-z0-9-]*)?$")
_MAINLINE_SLUG_RE = re.compile(r"gpt-(\d+)(?:\.(\d+))?(-[a-z][a-z0-9.-]*)?$")

_PAGE_SIZE = 100
_MAX_PAGES = 20

# Efforts allowed on entries synthesized for slugs Codex has no native
# metadata for; keeps unknown models off effort levels the upstream rejects.
_SYNTHETIC_EFFORTS = ("low", "medium", "high")


async def fetch_codex_service_ids(
    client: httpx.AsyncClient,
    workspace_host: str,
    bearer: str,
) -> list[str]:
    """
    List ``system.ai.*`` ids the workspace serves on Codex's API surface.

    Includes services declaring the Responses dialect natively, plus served
    translated arms (:data:`_TRANSLATED_ARM_IDS`) the gateway carries through
    dialect translation.

    :param client: Shared async HTTP client.
    :param workspace_host: Workspace origin, e.g. ``"https://x.databricks.com"``.
    :param bearer: Databricks access token.
    :returns: Sorted, de-duplicated service ids (without the
        ``model-services/`` resource prefix).
    :raises httpx.HTTPError: On listing failures (callers treat the catalog
        as unavailable).
    """
    ids: list[str] = []
    page_token: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, str] = {"page_size": str(_PAGE_SIZE)}
        if page_token:
            params["page_token"] = page_token
        resp = await client.get(
            f"{workspace_host}/api/2.1/unity-catalog/model-services",
            params=params,
            headers={"Authorization": f"Bearer {bearer}"},
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            break
        for svc in payload.get("model_services", []):
            if not isinstance(svc, dict):
                continue
            name = svc.get("name")
            if not isinstance(name, str) or not name:
                continue
            service_id = name.removeprefix(_MODEL_SERVICE_PREFIX)
            api_types = svc.get("supported_api_types")
            declares_responses = isinstance(api_types, list) and CODEX_API_TYPE in api_types
            if declares_responses or service_id in _TRANSLATED_ARM_IDS:
                ids.append(service_id)
        page_token = payload.get("next_page_token") or None
        if not page_token:
            break
    return sorted(set(ids))


def codex_slug(service_id: str) -> str:
    """
    Convert one service id to the slug Codex should see.

    :param service_id: e.g. ``"system.ai.gpt-5-6-sol"``.
    :returns: ``"gpt-5.6-sol"`` for mainline GPT ids; the id verbatim
        otherwise.
    """
    bare_arm = _ARM_BARE_BY_ID.get(service_id)
    if bare_arm is not None:
        return bare_arm
    tail = service_id.removeprefix(_SYSTEM_PREFIX)
    match = _MAINLINE_SERVICE_RE.fullmatch(tail)
    if match is None:
        return service_id
    major, minor, suffix = match.groups()
    version = major if minor is None else f"{major}.{minor}"
    return f"gpt-{version}{suffix or ''}"


def service_id_for_slug(slug: str) -> str:
    """
    Invert :func:`codex_slug`: the ``system.ai.*`` service id a slug routes to.

    Standard picker rows carry both the settable token (``id`` — the slug
    codex accepts) and the wire model it resolves to (``model`` — the service
    id), so UI surfaces can highlight the running model without re-deriving
    spellings.

    Also accepts the ``databricks-``-localized spellings older catalog
    surfaces hand to orchestrators (``databricks-glm-5-2``), so a model id
    picked from any feed resolves to the same service.

    :param slug: Catalog slug, e.g. ``"gpt-5.6-sol"`` or ``"glm-5-2"``.
    :returns: The service id, e.g. ``"system.ai.gpt-5-6-sol"``; verbatim
        (non-mainline, non-arm) ids return unchanged.
    """
    full_arm = EXTENDED_CATALOG_MODELS.get(slug)
    if full_arm is not None:
        return full_arm
    if slug.startswith(_DATABRICKS_LOCAL_PREFIX):
        stripped = slug.removeprefix(_DATABRICKS_LOCAL_PREFIX)
        localized_arm = EXTENDED_CATALOG_MODELS.get(stripped)
        if localized_arm is not None:
            return localized_arm
        if _MAINLINE_SERVICE_RE.fullmatch(stripped):
            return f"{_SYSTEM_PREFIX}{stripped}"
    match = _MAINLINE_SLUG_RE.fullmatch(slug)
    if match is None:
        return slug
    major, minor, suffix = match.groups()
    version = major if minor is None else f"{major}-{minor}"
    return f"{_SYSTEM_PREFIX}gpt-{version}{suffix or ''}"


def _slug_sort_key(slug: str) -> tuple[int, int, int, str]:
    """Order mainline GPT slugs newest-first; non-mainline ids last, alpha."""
    match = _MAINLINE_SLUG_RE.fullmatch(slug)
    if match is None:
        return (1, 0, 0, slug)
    major, minor, suffix = match.groups()
    return (0, -int(major), -(int(minor) if minor else 0), suffix or "")


def newest_mainline_slug(service_ids: list[str]) -> str | None:
    """
    Newest mainline GPT slug served, by parsed version (never alphabetical).

    Mirrors ucode's ``default_model`` rule: only GPT-parseable ids are
    candidates, so a non-GPT id can never be pinned as a launch default.

    :param service_ids: Served ids in either spelling (``system.ai.*`` or
        slugs).
    :returns: e.g. ``"gpt-5.6-luna"``, or ``None`` when nothing parses as
        mainline GPT.
    """
    mainline = [
        slug
        for slug in (codex_slug(service_id) for service_id in service_ids)
        if _MAINLINE_SLUG_RE.fullmatch(slug)
    ]
    if not mainline:
        return None
    return min(mainline, key=_slug_sort_key)


def build_models_response(
    service_ids: list[str],
    native_catalog: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    Build Codex's ``ModelsResponse`` for the servable inventory.

    Known slugs adopt Codex's own catalog entry (native display names,
    descriptions, effort ladders, base instructions); unknown slugs clone the
    first native entry as a template with a clamped effort ladder. Ordering:
    native (OpenAI-priority) order first so the default matches what Codex
    would pick natively when servable, then remaining slugs newest-first.

    :param service_ids: Servable ``system.ai.*`` ids.
    :param native_catalog: ``codex debug models`` output, or ``None``.
    :returns: ``{"models": [...]}`` ready to serve, or ``None`` when nothing
        can be built (no native metadata / empty inventory) — callers fail
        open so Codex keeps its bundled catalog.
    """
    if not isinstance(native_catalog, dict):
        return None
    native_models = [
        m
        for m in native_catalog.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("slug"), str) and m.get("slug")
    ]
    if not native_models:
        return None
    service_by_slug: dict[str, str] = {}
    for service_id in service_ids:
        service_by_slug.setdefault(codex_slug(service_id), service_id)
    if not service_by_slug:
        return None
    native_by_slug = {m["slug"]: m for m in native_models}
    ordered: list[str] = [m["slug"] for m in native_models if m["slug"] in service_by_slug]
    ordered.extend(
        sorted((s for s in service_by_slug if s not in set(ordered)), key=_slug_sort_key)
    )
    template = native_models[0]
    models: list[dict[str, Any]] = []
    for priority, slug in enumerate(ordered):
        native = native_by_slug.get(slug)
        entry = copy.deepcopy(native if native is not None else template)
        entry["slug"] = slug
        entry["priority"] = priority
        entry["visibility"] = "list"
        entry["description"] = f"Databricks AI Gateway ({service_by_slug[slug]})"
        if native is None:
            entry["display_name"] = slug.removeprefix(_SYSTEM_PREFIX)
            entry["supported_reasoning_levels"] = [
                level
                for level in template.get("supported_reasoning_levels", [])
                if isinstance(level, dict) and level.get("effort") in _SYNTHETIC_EFFORTS
            ]
            entry["default_reasoning_level"] = "medium"
            entry["availability_nux"] = None
            entry["upgrade"] = None
            # A cloned ``code_mode_only`` tool mode / ``v2`` multi-agent
            # version declare GPT's trained-in Code Mode grammar, which makes
            # codex withhold the classic JSON tool set entirely — a
            # translated arm cannot speak that grammar and ends up unable to
            # run shell or MCP tools. Nulling both (codex's values for
            # models without Code Mode) restores standard JSON function
            # tools.
            entry["tool_mode"] = None
            entry["multi_agent_version"] = None
            # ``supports_search_tool`` defers the whole tool set behind
            # codex's tool-search mechanism (tools leave the request and are
            # discovered on demand) — another GPT-only protocol an arm
            # cannot drive. Off ⇒ direct tools stay in the request.
            entry["supports_search_tool"] = False
            # ``use_responses_lite`` switches app-server turns onto the lite
            # Responses wire, which omits the JSON ``tools`` array from the
            # request entirely (tool specs travel out-of-band — a hosted
            # OpenAI protocol). GPT models survive on their trained-in
            # grammar; an arm gets no tools at all. The classic wire keeps
            # tools in-body, which arms drive correctly.
            entry["use_responses_lite"] = False
        models.append(entry)
    return {"models": models}


def catalog_etag(payload: bytes) -> str:
    """
    Strong ETag for a serialized catalog.

    :param payload: Serialized ``ModelsResponse`` bytes.
    :returns: Quoted ETag value.
    """
    return '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'


def picker_options(models_response: dict[str, Any]) -> list[dict[str, object]]:
    """
    Web-picker rows for a built catalog (first entry is the default).

    Rows use the standard picker contract — the server's
    ``NativeModelOption`` field names, i.e. the same shape the in-session
    gear already renders from codex ``model/list`` — so both picker surfaces
    share one row schema: ``id`` (the settable slug), ``model`` (the
    ``system.ai.*`` service id it resolves to), the display name, the
    routing description, and the entry's real effort ladder.

    :param models_response: Output of :func:`build_models_response`.
    :returns: Standard picker rows in catalog order; only the first carries
        ``isDefault``.
    """
    options: list[dict[str, object]] = []
    for index, entry in enumerate(models_response.get("models", [])):
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        display = entry.get("display_name")
        row: dict[str, object] = {
            "id": slug,
            "model": service_id_for_slug(slug),
            "displayName": display if isinstance(display, str) and display else slug,
        }
        if index == 0:
            row["isDefault"] = True
        description = entry.get("description")
        if isinstance(description, str) and description:
            row["description"] = description
        default_effort = entry.get("default_reasoning_level")
        if isinstance(default_effort, str) and default_effort:
            row["defaultReasoningEffort"] = default_effort
        efforts: list[dict[str, str]] = []
        for level in entry.get("supported_reasoning_levels", []):
            if not isinstance(level, dict) or not isinstance(level.get("effort"), str):
                continue
            effort_row = {"reasoningEffort": level["effort"]}
            level_description = level.get("description")
            if isinstance(level_description, str) and level_description:
                effort_row["description"] = level_description
            efforts.append(effort_row)
        if efforts:
            row["supportedReasoningEfforts"] = efforts
        options.append(row)
    return options


def routable_models(models_response: dict[str, Any]) -> list[str]:
    """
    Every slug the catalog serves (router-launchable even without a row).

    :param models_response: Output of :func:`build_models_response`.
    :returns: Slug list in catalog order.
    """
    return [
        entry["slug"]
        for entry in models_response.get("models", [])
        if isinstance(entry.get("slug"), str)
    ]


def dumps_catalog(models_response: dict[str, Any]) -> bytes:
    """
    Serialize a catalog deterministically (stable ETags across rebuilds).

    :param models_response: Output of :func:`build_models_response`.
    :returns: UTF-8 JSON bytes.
    """
    return json.dumps(models_response, sort_keys=True).encode("utf-8")
