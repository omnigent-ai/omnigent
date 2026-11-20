"""Normalize and cache live Google Gemini Models API capability metadata."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from cachetools import TTLCache

from omnigent.models.model_metadata import (
    ModelCapability,
    ModelMetadata,
    ModelWireAPI,
)

_logger = logging.getLogger(__name__)

_CANONICAL_GEMINI_HOST = "generativelanguage.googleapis.com"
_MODEL_METADATA_TIMEOUT_S = 10.0
_MODEL_METADATA_TTL_S = 300.0
_MODEL_METADATA_CACHE_PARTITION_SALT = secrets.token_bytes(32)
# One round avoids blocking the event loop; API keys are already high-entropy secrets.
_MODEL_METADATA_CACHE_PARTITION_ITERATIONS = 1
_MODEL_METADATA_CACHE: TTLCache[tuple[str, str, bytes], ModelMetadata] = TTLCache(
    maxsize=128,
    ttl=_MODEL_METADATA_TTL_S,
)
_CONTEXT_WINDOW_CACHE: TTLCache[str, int] = TTLCache(
    maxsize=256,
    ttl=_MODEL_METADATA_TTL_S,
)
_MODEL_METADATA_IN_FLIGHT: dict[tuple[str, str, bytes], asyncio.Task[ModelMetadata | None]] = {}
_MODEL_METADATA_CACHE_LOCK = threading.Lock()

_GENERATE_CONTENT_METHODS = frozenset({"generateContent", "streamGenerateContent"})
_GEMINI_VERSION_SEGMENT_RE = re.compile(
    r"/v[0-9]+(?:(?:alpha|beta)[0-9]*)?\Z",
    re.IGNORECASE,
)


def _positive_int(value: object) -> int | None:
    """Return *value* when it is a positive int (excluding bool), else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _is_canonical_gemini_host(url: str) -> bool:
    """Return whether *url* targets Google's canonical Gemini API host."""
    hostname = urlsplit(url.strip()).hostname
    return hostname is not None and hostname.lower() == _CANONICAL_GEMINI_HOST


def normalize_gemini_model_id(model: str) -> str:
    """Strip provider and ``models/`` prefixes from a Gemini model identifier."""
    trimmed = model.strip().lstrip("/")
    for prefix in ("gemini/", "vertex/"):
        if trimmed.lower().startswith(prefix):
            trimmed = trimmed[len(prefix) :].lstrip("/")
            break
    if trimmed.lower().startswith("models/"):
        trimmed = trimmed[len("models/") :].lstrip("/")
    return trimmed


def extract_gemini_model_id(payload: Mapping[str, Any]) -> str | None:
    """Extract the bare model id from a Gemini Models API object."""
    for key in ("name", "baseModelId"):
        raw = payload.get(key)
        if isinstance(raw, str):
            normalized = normalize_gemini_model_id(raw)
            if normalized:
                return normalized
    return None


def supports_gemini_generate_content(payload: Mapping[str, Any]) -> bool:
    """Return whether a Gemini model object supports ``generateContent``."""
    raw_methods = payload.get("supportedGenerationMethods")
    if not isinstance(raw_methods, list):
        return True
    return any(
        isinstance(method, str) and method in _GENERATE_CONTENT_METHODS for method in raw_methods
    )


def parse_gemini_model_metadata(payload: Mapping[str, Any]) -> ModelMetadata:
    """Convert one Gemini Models API object to normalized metadata.

    Reads ``inputTokenLimit``, ``outputTokenLimit``, ``thinking``, and
    ``supportedGenerationMethods`` from Google's ``GET /v1beta/models``
    response schema.
    """
    supported: set[ModelCapability] = set()
    unsupported: set[ModelCapability] = set()

    thinking = payload.get("thinking")
    if isinstance(thinking, bool):
        (supported if thinking else unsupported).add(ModelCapability.REASONING)

    raw_methods = payload.get("supportedGenerationMethods")
    if isinstance(raw_methods, list):
        has_generate = any(
            isinstance(method, str) and method in _GENERATE_CONTENT_METHODS
            for method in raw_methods
        )
        wire_apis = (
            frozenset({ModelWireAPI.GEMINI_GENERATE_CONTENT}) if has_generate else frozenset()
        )
    else:
        wire_apis = frozenset({ModelWireAPI.GEMINI_GENERATE_CONTENT})

    return ModelMetadata(
        supported_capabilities=frozenset(supported),
        unsupported_capabilities=frozenset(unsupported),
        context_window=_positive_int(payload.get("inputTokenLimit")),
        max_output_tokens=_positive_int(payload.get("outputTokenLimit")),
        wire_apis=wire_apis,
    )


def gemini_models_url(base_url: str) -> str:
    """Derive the native Gemini ``/models`` URL from a provider base URL.

    Strips Google's OpenAI-compatibility ``/openai`` suffix (and any query or
    fragment) so providers configured with ``.../v1beta/openai`` query the
    native ``.../v1beta/models`` endpoint that returns token limits and
    thinking flags.
    """
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    if path.lower().endswith("/openai"):
        path = path[: -len("/openai")].rstrip("/")
    if path.lower().endswith("/models"):
        new_path = path
    elif _GEMINI_VERSION_SEGMENT_RE.search(path):
        new_path = f"{path}/models"
    else:
        new_path = f"{path}/v1beta/models"
    return urlunsplit((parsed.scheme, parsed.netloc, new_path, "", ""))


def record_gemini_model_metadata(
    model_id: str,
    metadata: ModelMetadata,
    *,
    base_url: str | None = None,
) -> None:
    """Record a discovered Gemini model's context window in the TTL cache.

    Restricts writes to ``gemini-*`` ids (and, when *base_url* is supplied,
    canonical Google Gemini hosts) so custom proxies cannot poison global
    context-window lookups.
    """
    window = _positive_int(metadata.context_window)
    if window is None:
        return
    if base_url is not None and not _is_canonical_gemini_host(base_url):
        return
    key = normalize_gemini_model_id(model_id).lower()
    if not key.startswith("gemini-"):
        return
    with _MODEL_METADATA_CACHE_LOCK:
        _CONTEXT_WINDOW_CACHE[key] = window


def lookup_cached_gemini_context_window(model: str) -> int | None:
    """Look up a previously discovered Gemini model's context window without I/O."""
    key = normalize_gemini_model_id(model).lower()
    if not key.startswith("gemini-"):
        return None
    with _MODEL_METADATA_CACHE_LOCK:
        return _CONTEXT_WINDOW_CACHE.get(key)


def _credential_partition(headers: Mapping[str, str]) -> bytes:
    """Derive an opaque process-local cache partition from auth headers."""
    lowered = {k.lower(): v for k, v in headers.items()}
    material = f"{lowered.get('x-goog-api-key', '')}\0{lowered.get('authorization', '')}".encode()
    # PBKDF2 creates an opaque process-local cache partition, not a persisted credential hash.
    return hashlib.pbkdf2_hmac(
        "sha256",
        material,
        _MODEL_METADATA_CACHE_PARTITION_SALT,
        _MODEL_METADATA_CACHE_PARTITION_ITERATIONS,
    )


async def get_gemini_model_metadata(
    headers: Mapping[str, str],
    base_url: str,
    model: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelMetadata | None:
    """Fetch and cache one Gemini model's live capability metadata."""
    normalized_model = normalize_gemini_model_id(model)
    if not normalized_model:
        return None
    request_headers = dict(headers)
    models_url = gemini_models_url(base_url)
    cache_key = (models_url, normalized_model.lower(), _credential_partition(request_headers))
    with _MODEL_METADATA_CACHE_LOCK:
        if cache_key in _MODEL_METADATA_CACHE:
            return _MODEL_METADATA_CACHE[cache_key]
        task = _MODEL_METADATA_IN_FLIGHT.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                _fetch_gemini_model_metadata(
                    request_headers,
                    models_url,
                    normalized_model,
                    cache_key,
                    transport=transport,
                )
            )
            _MODEL_METADATA_IN_FLIGHT[cache_key] = task

    # Keep the shared lookup alive if one waiting request is cancelled.
    return await asyncio.shield(task)


async def _fetch_gemini_model_metadata(
    headers: dict[str, str],
    models_url: str,
    model: str,
    cache_key: tuple[str, str, bytes],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelMetadata | None:
    """Fetch Gemini model metadata and cache successful lookups."""
    try:
        try:
            url = f"{models_url}/{quote(model, safe='')}"
            async with httpx.AsyncClient(
                transport=transport,
                timeout=_MODEL_METADATA_TIMEOUT_S,
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
            metadata = parse_gemini_model_metadata(payload) if isinstance(payload, dict) else None
        except (httpx.HTTPError, ValueError):
            _logger.warning(
                "Gemini model metadata lookup failed for %s",
                model,
                exc_info=True,
            )
            return None

        if metadata is not None:
            with _MODEL_METADATA_CACHE_LOCK:
                _MODEL_METADATA_CACHE[cache_key] = metadata
            record_gemini_model_metadata(model, metadata, base_url=models_url)
        return metadata
    finally:
        current_task = asyncio.current_task()
        with _MODEL_METADATA_CACHE_LOCK:
            if _MODEL_METADATA_IN_FLIGHT.get(cache_key) is current_task:
                del _MODEL_METADATA_IN_FLIGHT[cache_key]


def _clear_model_metadata_cache() -> None:
    """Clear cached Gemini model metadata for tests and catalog resets."""
    with _MODEL_METADATA_CACHE_LOCK:
        _MODEL_METADATA_CACHE.clear()
        _CONTEXT_WINDOW_CACHE.clear()
        tasks = list(_MODEL_METADATA_IN_FLIGHT.values())
        _MODEL_METADATA_IN_FLIGHT.clear()
    for task in tasks:
        task.cancel()
