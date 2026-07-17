"""Live Databricks model discovery for native coding harnesses."""

from __future__ import annotations

import re
from typing import Any

import httpx

CLAUDE_MODEL_FAMILIES: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

_MODEL_SERVICES_PATH = "/api/2.1/unity-catalog/model-services"
_ANTHROPIC_MODELS_PATH = "/ai-gateway/anthropic/v1/models"
_MODEL_SERVICE_PREFIX = "model-services/"
_SYSTEM_MODEL_PREFIX = "system.ai."
_PAGE_SIZE = 100
_MAX_PAGES = 100
_HTTP_TIMEOUT_S = 10.0


def _natural_model_key(model_id: str) -> tuple[tuple[int, str | int], ...]:
    """Return a comparison key that orders numeric model versions naturally."""
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", model_id.lower())
        if part
    )


def _models_by_claude_family(model_ids: list[str], *, marker: str) -> dict[str, str]:
    """Select the newest model id for every Claude family in *model_ids*."""
    result: dict[str, str] = {}
    for family in CLAUDE_MODEL_FAMILIES:
        candidates = [model_id for model_id in model_ids if f"{marker}{family}-" in model_id]
        if candidates:
            result[family] = max(candidates, key=_natural_model_key)
    return result


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
        params = {"page_size": str(_PAGE_SIZE)}
        if page_token is not None:
            params["page_token"] = page_token
        response = client.get(
            f"{workspace_url.rstrip('/')}{_MODEL_SERVICES_PATH}",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        payload: Any = response.json()
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
    payload: Any = response.json()
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


def discover_databricks_claude_models(
    workspace_url: str,
    token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, str]:
    """Discover the live Claude family mapping for a Databricks workspace.

    Unity Catalog model services are authoritative when they expose Claude
    models. The Anthropic AI Gateway model-list endpoint is the compatibility
    fallback for workspaces that have not moved to model services yet.

    :param workspace_url: Workspace origin, e.g. ``"https://example.com"``.
    :param token: Workspace bearer token.
    :param transport: Optional HTTP transport used by tests.
    :returns: Family aliases mapped to routable model ids.
    :raises httpx.HTTPError: When neither discovery endpoint can be queried.
    :raises ValueError: When both endpoints return malformed responses.
    """
    headers = {"Authorization": f"Bearer {token}"}
    primary_error: Exception | None = None
    with httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT_S) as client:
        try:
            model_service_ids = _list_model_service_ids(client, workspace_url, headers)
        except (httpx.HTTPError, ValueError) as exc:
            primary_error = exc
        else:
            models = _models_by_claude_family(model_service_ids, marker="claude-")
            if models:
                return models

        try:
            gateway_ids = _list_anthropic_gateway_ids(client, workspace_url, headers)
        except (httpx.HTTPError, ValueError) as exc:
            if primary_error is not None:
                raise exc from primary_error
            # A successful permission-aware UC listing is authoritative even
            # when the compatibility endpoint is not enabled.
            return {}
    return _models_by_claude_family(gateway_ids, marker="databricks-claude-")
