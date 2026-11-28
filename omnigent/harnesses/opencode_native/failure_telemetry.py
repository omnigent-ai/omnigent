"""Sanitized observed provider evidence for OpenCode's existing failed edge."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from omnigent.harnesses.opencode_native.bridge import xdg_config_home_for_bridge_dir
from omnigent.native.failure_telemetry import FailureContext, normalize_failure_context
from omnigent.process_logging import redact_log_text

_CONFIG_LIMIT = 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or "://" in value:
        return None
    if redact_log_text(value, include_whitespace_credentials=True) != value:
        return None
    return value


def _configured_route(bridge_dir: Path | None, provider: str, model: str) -> FailureContext:
    if bridge_dir is None:
        return {}
    config_path = xdg_config_home_for_bridge_dir(bridge_dir) / "opencode" / "opencode.json"
    try:
        with config_path.open("rb") as config_file:
            raw = config_file.read(_CONFIG_LIMIT + 1)
        if len(raw) > _CONFIG_LIMIT:
            return {}
        config = json.loads(raw)
        providers = config.get("providers") if isinstance(config, dict) else None
        entry = providers.get(provider) if isinstance(providers, dict) else None
        settings = entry.get("settings") if isinstance(entry, dict) else None
        base_url = settings.get("baseURL") if isinstance(settings, dict) else None
        models = entry.get("models") if isinstance(entry, dict) else None
        model_entry = models.get(model) if isinstance(models, dict) else None
        model_settings = model_entry.get("settings") if isinstance(model_entry, dict) else None
        if isinstance(model_settings, dict) and "baseURL" in model_settings:
            base_url = model_settings["baseURL"]
        if not isinstance(base_url, str) or any(ord(char) < 32 for char in base_url):
            return {}
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return {}
        origin = parsed.hostname
        if ":" in origin:
            origin = f"[{origin}]"
        if parsed.port is not None:
            origin = f"{origin}:{parsed.port}"
        if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,256}", origin):
            return {}
        route = {
            "/ai-gateway/mlflow/v1": "mlflow",
            "/ai-gateway/anthropic": "anthropic",
            "/serving-endpoints": "serving-endpoints",
        }.get(parsed.path.rstrip("/"), "custom")
    except (OSError, ValueError, RecursionError):
        return {}
    return {"configured_provider_origin": origin, "configured_provider_route_family": route}


def opencode_failure_context(
    *, error: object, model: str | None, bridge_dir: Path | None
) -> FailureContext:
    """Retain typed evidence, never raw error/config payloads or inferred routes."""
    context: dict[str, object] = {
        "native_harness": "opencode-native",
        "failure_source": "opencode_execution_failed",
    }
    if isinstance(error, dict):
        context["provider_error_type"] = _identifier(error.get("type"))
        context["http_status"] = error.get("status")
        context["provider_request_id"] = _identifier(error.get("provider_request_id"))
    observed = _identifier(model)
    if observed is not None and "/" in observed:
        provider, model_id = observed.split("/", 1)
        if provider and model_id:
            context["native_model"] = observed
            context["native_provider"] = provider
            context.update(_configured_route(bridge_dir, provider, model_id))
    return normalize_failure_context(context)
