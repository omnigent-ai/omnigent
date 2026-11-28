"""CI headers for Goose's declarative provider configuration."""

from __future__ import annotations

import copy
import logging
from urllib.parse import urlsplit, urlunsplit

from omnigent.databricks_ai_gateway import (
    ci_request_tags,
    gateway_request_tag_headers,
    with_ci_request_tags,
)


def with_goose_ci_request_tags(provider: dict[str, object]) -> dict[str, object]:
    """Tag an explicit custom provider, retaining its model, protocol and auth.

    CI writes this JSON to Goose's config/custom_providers directory and selects
    its name via GOOSE_PROVIDER. Native and ACP both load those provider files.
    OPENAI_CUSTOM_HEADERS cannot carry tag JSON: Goose splits it on commas.
    """
    result = copy.deepcopy(provider)
    if not ci_request_tags():
        return result
    base_url = result.get("base_url")
    if not isinstance(base_url, str):
        return result
    base_path = result.get("base_path")
    if isinstance(base_path, str):
        try:
            parsed = urlsplit(base_url)
        except ValueError:
            logging.getLogger(__name__).warning("Skipping CI tags: malformed Goose provider URL")
            return result
        base_url = urlunsplit(parsed._replace(path="/" + base_path.lstrip("/")))
    if not gateway_request_tag_headers(base_url):
        return result
    headers = result.get("headers", {})
    if isinstance(headers, dict) and all(isinstance(name, str) for name in headers):
        result["headers"] = with_ci_request_tags(headers)
    return result
