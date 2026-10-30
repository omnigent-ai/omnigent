"""Provider-scoped CI headers for Qwen's native and ACP configuration."""

from __future__ import annotations

import copy

from omnigent.databricks_ai_gateway import gateway_request_tag_headers, with_ci_request_tags


def with_qwen_ci_request_tags(settings: dict[str, object]) -> dict[str, object]:
    """Add gateway tags to explicit model providers without replacing the registry.

    CI owns the settings file and selects it with QWEN_CODE_SYSTEM_SETTINGS_PATH
    for both native and ACP launches. Environment-only provider selection does
    not transport custom headers. Do not install a partial system registry over
    a user's settings: Qwen replaces, rather than merges, modelProviders.
    """
    result = copy.deepcopy(settings)
    providers = result.get("modelProviders", {})
    if not isinstance(providers, dict):
        return result
    for models in providers.values():
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            base_url = model.get("baseUrl")
            if not isinstance(base_url, str) or not gateway_request_tag_headers(base_url):
                continue
            generation = model.get("generationConfig", {})
            if not isinstance(generation, dict):
                continue
            headers = generation.get("customHeaders", {})
            if isinstance(headers, dict) and all(isinstance(name, str) for name in headers):
                model["generationConfig"] = {
                    **generation,
                    "customHeaders": with_ci_request_tags(headers),
                }
    return result
