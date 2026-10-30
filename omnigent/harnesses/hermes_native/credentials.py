"""Gateway request headers for Hermes' provider-scoped configuration."""

from __future__ import annotations

import copy

from omnigent.databricks_ai_gateway import gateway_request_tag_headers, with_ci_request_tags


def with_hermes_ci_request_tags(config: dict[str, object]) -> dict[str, object]:
    """Preserve provider routing and add tags only to Databricks gateway entries."""
    result = copy.deepcopy(config)
    providers = result.get("providers", {})
    entries = list(providers.values()) if isinstance(providers, dict) else []
    legacy_entries = result.get("custom_providers", [])
    if isinstance(legacy_entries, list):
        entries.extend(legacy_entries)
    for provider in entries:
        if not isinstance(provider, dict):
            continue
        base_url = provider.get("base_url", provider.get("api"))
        if not isinstance(base_url, str) or not gateway_request_tag_headers(base_url):
            continue
        existing = provider.get("extra_headers", {})
        if isinstance(existing, dict) and all(isinstance(name, str) for name in existing):
            provider["extra_headers"] = with_ci_request_tags(existing)
    return result
