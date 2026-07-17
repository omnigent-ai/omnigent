"""Helpers for resolving local Databricks profile metadata."""

from __future__ import annotations

import configparser
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

_logger = logging.getLogger(__name__)

_DATABRICKSCFG_PATH = Path.home() / ".databrickscfg"


def normalize_workspace_url(raw: str) -> str:
    """Reduce a Databricks workspace URL to its bare ``scheme://host`` origin.

    Users routinely paste the URL straight from a browser address bar, which
    carries a path and query the workspace host does not — e.g.
    ``https://my-ws.cloud.databricks.com/browse?o=1234567890``. Both the
    ``~/.databrickscfg`` profile host and ``ucode configure --workspaces``
    need the bare origin: the Databricks CLI keys its OAuth token cache by
    host, so a path-laden value resolves to "no access token" and
    ``ucode configure`` then exits non-zero.

    :param raw: A workspace URL, possibly carrying a path/query/fragment
        and/or a trailing slash, e.g.
        ``"https://my-ws.cloud.databricks.com/browse?o=1"``.
    :returns: ``scheme://host`` with no path, query, fragment, or trailing
        slash (e.g. ``"https://my-ws.cloud.databricks.com"``). When *raw* has
        no parseable scheme+host (e.g. a bare ``"host/path"`` with no scheme),
        the input is returned trimmed of surrounding whitespace and a trailing
        slash — matching the prior ``rstrip("/")`` behavior so callers that
        pre-add a scheme never regress.
    """
    parsed = urlparse(raw.strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return raw.strip().rstrip("/")


# The install command surfaced wherever a Databricks flow is gated on the
# `databricks` extra (the add-provider menu, `setup --internal-beta`).
# Matches the README's canonical `uv tool install` path. Dev clones use
# `uv sync --extra databricks` instead, but the tool install is the path
# end users actually took. The repo URL sits on its own line: the slug
# differs per distribution, and inlining it into the hint string would
# make the line's width — and therefore its ruff formatting — depend on
# which slug a checkout carries.
_SOURCE_REPO_URL = "https://github.com/omnigent-ai/omnigent.git"
DATABRICKS_EXTRA_INSTALL_HINT = (
    f'uv tool install --force "omnigent[databricks] @ git+{_SOURCE_REPO_URL}"'
)


def databricks_sdk_installed() -> bool:
    """Return whether ``databricks-sdk`` (the ``databricks`` extra) is present.

    The SDK is not part of the default install — it ships in the
    ``databricks`` (and ``all``) extras. The ``kind: databricks`` provider
    path needs it to mint workspace OAuth tokens at runtime
    (:mod:`omnigent.runtime.credentials.databricks`), so onboarding flows
    gate the Databricks option on this check and surface
    :data:`DATABRICKS_EXTRA_INSTALL_HINT` when it fails.

    Uses :func:`importlib.util.find_spec` so the check never pays the cost
    of actually importing the SDK.

    :returns: ``True`` when ``databricks.sdk`` is importable.
    """
    try:
        return importlib.util.find_spec("databricks.sdk") is not None
    except ModuleNotFoundError:
        # find_spec("databricks.sdk") imports the parent `databricks`
        # namespace package first; when even that is absent it raises
        # instead of returning None.
        return False


# Fallback Claude model for the Databricks AI gateway when neither the spec
# nor the workspace's ucode state names one. Must be a ``databricks-*``
# endpoint name — the gateway rejects Anthropic-direct ids like the CLI's
# own ``opus[1m]`` default.
DATABRICKS_CLAUDE_DEFAULT_MODEL = "databricks-claude-opus-4-8"


# Max context window per Claude model family, matched as substrings against
# endpoint / UC-service names (most specific first, so plain "sonnet-4" only
# catches names the 4-6/4-5 rows did not). Sources: the Anthropic model
# catalog (1M standard on Fable 5 / Opus 4.6+ / Sonnet 4.6+; 200K on Haiku
# 4.5 and older tiers) and the Databricks FMAPI supported-models docs
# (Opus 4.7/4.6 documented at 1M; Opus 4.5/4.1 at 200K), live-verified on a
# workspace gateway (a >200K-token prompt is accepted on an Opus 4.8
# endpoint with no beta header).
_CLAUDE_CONTEXT_WINDOWS: tuple[tuple[str, str], ...] = (
    ("fable-5", "1M"),
    ("opus-4-8", "1M"),
    ("opus-4-7", "1M"),
    ("opus-4-6", "1M"),
    ("opus-4-5", "200K"),
    ("opus-4-1", "200K"),
    ("sonnet-5", "1M"),
    ("sonnet-4-6", "1M"),
    ("sonnet-4-5", "200K"),
    ("sonnet-4", "200K"),
    ("haiku-4-5", "200K"),
)


def claude_context_window(model_name: str) -> str | None:
    """Return the max context window for a Claude endpoint/service name.

    Matches the model family as a substring of *model_name* (hosted
    endpoint names and user-created UC FQNs both usually carry it, e.g.
    ``databricks-claude-opus-4-8`` or ``main.agents.opus-4-8``). A name
    that reveals no known family returns ``None`` — callers show no label
    and must not assume a window.

    :param model_name: Endpoint name or UC model service FQN.
    :returns: ``"1M"``, ``"200K"``, or ``None`` when the family is
        unrecognizable from the name.
    """
    lowered = model_name.lower()
    for family, window in _CLAUDE_CONTEXT_WINDOWS:
        if family in lowered:
            return window
    return None


def _serves_claude_model(endpoint: dict[str, object]) -> bool:
    """Return whether a serving endpoint serves a Claude/Anthropic model.

    Endpoint metadata exposes no supported-API-types field, so the served
    entities' model identity is the discriminator: a Databricks-hosted
    Claude foundation model (``foundation_model`` name/display name), an
    ``external_model`` with the ``anthropic`` provider, or a custom entity
    whose name carries ``claude``.

    :param endpoint: One ``as_dict()``-shaped serving endpoint from the
        list/get API.
    :returns: ``True`` when any served entity is a Claude/Anthropic model.
    """
    config = endpoint.get("config")
    entities = config.get("served_entities", []) if isinstance(config, dict) else []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        foundation = entity.get("foundation_model")
        foundation = foundation if isinstance(foundation, dict) else {}
        external = entity.get("external_model")
        external = external if isinstance(external, dict) else {}
        if str(external.get("provider", "")).lower() == "anthropic":
            return True
        identity = " ".join(
            str(value)
            for value in (
                foundation.get("name"),
                foundation.get("display_name"),
                external.get("name"),
                entity.get("entity_name"),
            )
            if value
        ).lower()
        if "claude" in identity:
            return True
    return False


def list_claude_serving_endpoint_names(profile: str) -> list[str]:
    """Return Claude-serving chat endpoints the caller can query, best-effort.

    Backs the setup flow's model-endpoint picker, whose picks drive the
    Anthropic-surface harnesses (native claude / claude-sdk / pi). The
    workspace AI gateway rejects every other endpoint on that surface
    ("API type 'anthropic/v1/messages' is not supported"), so the list
    keeps only endpoints that:

    - run the ``llm/v1/chat`` task (drops embeddings/completions),
    - serve a Claude/Anthropic model (:func:`_serves_claude_model`), and
    - the caller holds CAN_QUERY / CAN_MANAGE on. The list API alone
      scopes to CAN_VIEW — which cannot query — so a parallel detail
      sweep confirms each survivor's ``permission_level``.

    This covers only classic serving endpoints; custom UC model services
    (Beta securables addressed by ``catalog.schema.name``) are listed by
    the companion :func:`list_claude_model_service_fqns`. Any wholesale
    failure (SDK absent, auth, network) returns ``[]`` — callers degrade
    to free-text entry rather than blocking the flow.

    :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
    :returns: Sorted endpoint names, e.g.
        ``["databricks-claude-opus-4-8", "databricks-claude-sonnet-5"]``.
    """
    try:
        from databricks.sdk import WorkspaceClient

        return _claude_serving_endpoint_names(WorkspaceClient(profile=profile))
    except Exception as exc:  # best-effort listing — the picker degrades to free text
        _logger.debug("Could not list serving endpoints for profile %r: %s", profile, exc)
        return []


def _claude_serving_endpoint_names(client: WorkspaceClient) -> list[str]:
    """Client-taking body of :func:`list_claude_serving_endpoint_names`.

    Split out so :func:`list_claude_endpoints` can run both listings on one
    shared (already-authenticated) client. Raises on wholesale failure —
    the public wrappers own the degrade-to-``[]`` contract.

    :param client: An authenticated SDK workspace client.
    :returns: Sorted Claude-serving chat endpoint names the caller can query.
    """
    from concurrent.futures import ThreadPoolExecutor

    candidates = [
        str(ep.name)
        for ep in client.serving_endpoints.list()
        if ep.name and str(ep.task or "") == "llm/v1/chat" and _serves_claude_model(ep.as_dict())
    ]
    if not candidates:
        return []

    def _can_query(name: str) -> bool:
        try:
            level = client.serving_endpoints.get(name).permission_level
        except Exception as exc:  # dropped from the list, not fatal to the sweep
            _logger.debug("Could not read permission level for %r: %s", name, exc)
            return False
        return level is not None and level.value in ("CAN_QUERY", "CAN_MANAGE")

    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        queryable = list(pool.map(_can_query, candidates))
    return sorted(name for name, ok in zip(candidates, queryable, strict=True) if ok)


def _model_service_serves_claude(service: dict[str, object]) -> bool | None:
    """Return whether a UC model service can answer the Anthropic surface.

    Prefers the service's ``supported_api_types`` (authoritative — the
    gateway names these in its rejection errors), but that field is not
    yet populated on user-created services (observed empty on live
    pay-per-token services that answer ``anthropic/v1/messages`` fine),
    so fall back to the routing destinations' model identity.

    :param service: One raw model-service mapping from the
        ``unity-catalog/model-services`` API.
    :returns: ``True``/``False`` when the payload is decisive, ``None``
        when it carries neither populated ``supported_api_types`` nor
        routing destinations — the *list* response omits destinations, so
        an undecided service needs its single-get detail.
    """
    import json

    api_types = service.get("supported_api_types")
    if isinstance(api_types, list) and api_types:
        return "anthropic/v1/messages" in api_types
    config = service.get("config")
    destinations = config.get("destinations", []) if isinstance(config, dict) else []
    if not destinations:
        return None
    identity = json.dumps(destinations).lower()
    return "claude" in identity or "anthropic" in identity


def list_claude_model_service_fqns(profile: str) -> list[str]:
    """Return custom UC model services that speak the Anthropic surface.

    Model services are Unity Catalog securables (Beta) representing
    governed LLM endpoints, addressed by ``catalog.schema.name`` FQN and
    listed via ``GET /api/2.1/unity-catalog/model-services`` — visibility
    is scoped by Unity Catalog privileges, so the caller only sees
    services they hold grants on. Keeps Claude/Anthropic-capable services
    (:func:`_model_service_serves_claude`; services the list payload
    cannot decide are resolved through a parallel single-get sweep, since
    the list response omits routing destinations) and drops the
    ``system.ai`` schema: those built-ins mirror the workspace's hosted
    ``databricks-*`` serving endpoints, which the picker already lists
    separately. Any wholesale failure returns ``[]`` — callers degrade to
    free-text entry rather than blocking the flow.

    :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
    :returns: Sorted FQNs, e.g.
        ``["main.agents.my-opus-endpoint", "main.agents.my-sonnet-endpoint"]``.
    """
    try:
        from databricks.sdk import WorkspaceClient

        return _claude_model_service_fqns(WorkspaceClient(profile=profile))
    except Exception as exc:  # best-effort listing — the picker degrades to free text
        _logger.debug("Could not list UC model services for profile %r: %s", profile, exc)
        return []


def _claude_model_service_fqns(client: WorkspaceClient) -> list[str]:
    """Client-taking body of :func:`list_claude_model_service_fqns`.

    Split out so :func:`list_claude_endpoints` can run both listings on one
    shared (already-authenticated) client. Raises on wholesale failure —
    the public wrappers own the degrade-to-``[]`` contract.

    :param client: An authenticated SDK workspace client.
    :returns: Sorted Claude-capable custom UC model service FQNs.
    """
    from concurrent.futures import ThreadPoolExecutor

    names: list[str] = []
    undecided: list[str] = []
    page_token: str | None = None
    while True:
        path = "/api/2.1/unity-catalog/model-services"
        if page_token:
            path += f"?page_token={page_token}"
        response = client.api_client.do("GET", path)
        services = response.get("model_services", []) if isinstance(response, dict) else []
        for service in services:
            if not isinstance(service, dict):
                continue
            raw_name = str(service.get("name", ""))
            fqn = raw_name.removeprefix("model-services/")
            if not fqn or fqn.startswith("system.ai."):
                continue
            serves = _model_service_serves_claude(service)
            if serves:
                names.append(fqn)
            elif serves is None:
                undecided.append(fqn)
        page_token = response.get("next_page_token") if isinstance(response, dict) else None
        if not page_token:
            break

    def _detail_serves_claude(fqn: str) -> bool:
        try:
            detail = client.api_client.do("GET", f"/api/2.1/unity-catalog/model-services/{fqn}")
        except Exception as exc:  # dropped from the list, not fatal to the sweep
            _logger.debug("Could not read model service %r: %s", fqn, exc)
            return False
        return isinstance(detail, dict) and bool(_model_service_serves_claude(detail))

    if undecided:
        with ThreadPoolExecutor(max_workers=min(8, len(undecided))) as pool:
            decided = list(pool.map(_detail_serves_claude, undecided))
        names.extend(fqn for fqn, ok in zip(undecided, decided, strict=True) if ok)
    return sorted(names)


def list_claude_endpoints(profile: str) -> tuple[list[str], list[str]]:
    """Return both Claude endpoint listings for *profile*, fetched in parallel.

    The endpoint picker needs the hosted serving endpoints
    (:func:`list_claude_serving_endpoint_names`) **and** the custom UC
    model services (:func:`list_claude_model_service_fqns`); fetching them
    through one shared client (a single OAuth handshake) in parallel
    roughly halves the picker's load time. Each listing degrades to
    ``[]`` independently; an unusable profile/SDK degrades both.

    :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
    :returns: ``(hosted_endpoint_names, custom_service_fqns)``.
    """
    from collections.abc import Callable

    try:
        from databricks.sdk import WorkspaceClient

        client = WorkspaceClient(profile=profile)
    except Exception as exc:
        _logger.debug("Could not build a workspace client for profile %r: %s", profile, exc)
        return [], []
    from concurrent.futures import ThreadPoolExecutor

    def _safe(lister: Callable[[WorkspaceClient], list[str]]) -> list[str]:
        try:
            return lister(client)
        except Exception as exc:
            _logger.debug("Endpoint listing failed for profile %r: %s", profile, exc)
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        hosted = pool.submit(_safe, _claude_serving_endpoint_names)
        custom = pool.submit(_safe, _claude_model_service_fqns)
        return hosted.result(), custom.result()


def list_databricks_profiles() -> list[str]:
    """Return the profile section names declared in ``~/.databrickscfg``.

    Used by ``omnigent setup --no-internal-beta`` to offer the user a pick-list
    when adding a ``kind: databricks`` provider, so they don't have to
    recall the exact profile name.

    :returns: Section names, e.g. ``["oss", "DEFAULT"]``. The ``DEFAULT``
        section is included only when it actually carries keys. Empty when
        the file is missing or unparseable.
    """
    if not _DATABRICKSCFG_PATH.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(_DATABRICKSCFG_PATH)
    except configparser.Error as exc:
        _logger.debug("Could not parse %s: %s", _DATABRICKSCFG_PATH, exc)
        return []
    sections = [s for s in parser.sections() if s != "DEFAULT"]
    if parser.defaults():
        sections.append("DEFAULT")
    return sections


def get_workspace_url_for_profile(profile: str) -> str | None:
    """Return the workspace host for a ``~/.databrickscfg`` profile.

    Reads the INI-style ``~/.databrickscfg`` directly with
    :mod:`configparser`, then falls back to Omnigent' built-in setup
    profile metadata for legacy names.

    :param profile: Profile section name, e.g. ``"<your-profile>"`` or
        ``"DEFAULT"``.
    :returns: The ``host`` value for the profile, stripped of trailing slash,
        or ``None`` when the profile cannot be resolved.
    """
    if _DATABRICKSCFG_PATH.exists():
        cfg = configparser.ConfigParser()
        try:
            cfg.read(_DATABRICKSCFG_PATH)
        except configparser.Error as exc:
            _logger.debug("Could not parse %s: %s", _DATABRICKSCFG_PATH, exc)
        else:
            host = None
            if cfg.has_section(profile):
                try:
                    host = cfg.get(profile, "host")
                except configparser.NoOptionError:
                    host = None
            elif profile.lower() == cfg.default_section.lower():
                host = cfg.defaults().get("host")
            if host:
                return host.rstrip("/")

    try:
        from omnigent.onboarding.internal_beta import DEFAULT_PROFILES
    except ModuleNotFoundError:
        # The internal-beta catalog is intentionally absent from the OSS
        # build; without it there are no bundled-profile fallbacks.
        return None

    for spec in DEFAULT_PROFILES:
        if spec.name == profile:
            return spec.host.rstrip("/")
    return None
