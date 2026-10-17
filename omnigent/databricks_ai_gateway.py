"""Canonical predicate for recognizing a Databricks Unity Gateway base URL.

Several surfaces need the same answer — pi-native rewrites a gateway Codex
base URL to the Anthropic surface, and host-side routing capability checks ask
whether a resolved harness launch is gateway-backed. Keeping one predicate here
means a look-alike host is rejected identically everywhere.
"""

from __future__ import annotations

import os
import re
from typing import Final
from urllib.parse import urlparse

# Trusted parent domains for a Databricks-owned host. The Unity Gateway lives
# under a per-workspace subdomain of one of these (the canonical form is
# ``<workspace>.ai-gateway.cloud.databricks.com``); the Azure / GCP control
# planes serve workspaces under their own parent domains. Written with the
# leading "." for readability — the match is on whole DNS labels
# (:func:`_under_trusted_domain`), never on a string suffix, so neither
# ``evilcloud.databricks.com`` nor ``....cloud.databricks.com.evil.test`` can
# pass as one of these.
DATABRICKS_TRUSTED_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    ".cloud.databricks.com",  # AWS workspaces + ai-gateway (incl. *.staging.cloud.databricks.com)
    ".azuredatabricks.net",  # Azure Databricks
    ".gcp.databricks.com",  # GCP Databricks
)

# A genuine Unity Gateway host carries the ``ai-gateway`` DNS label; we require it
# (alongside a trusted suffix) so a non-gateway Databricks host isn't routed as
# the gateway's Anthropic surface.
DATABRICKS_AI_GATEWAY_LABEL: Final[str] = "ai-gateway"


def _under_trusted_domain(hostname: str) -> bool:
    """Whether *hostname* is a subdomain of a trusted Databricks parent domain.

    Compares whole DNS labels from the right, so the parent must be an exact
    label-wise suffix with at least one label of its own in front of it. A
    string-suffix test would be looser in both directions.

    :param hostname: Lower-cased hostname from a parsed URL, e.g.
        ``"wkspc.ai-gateway.cloud.databricks.com"``.
    :returns: ``True`` when a trusted parent domain owns *hostname*.
    """
    labels = hostname.split(".")
    for parent in DATABRICKS_TRUSTED_HOST_SUFFIXES:
        parent_labels = parent.strip(".").split(".")
        if len(labels) > len(parent_labels) and labels[-len(parent_labels) :] == parent_labels:
            return True
    return False


def is_databricks_ai_gateway_url(base_url: str) -> bool:
    """Return ``True`` only for a genuine Databricks Unity Gateway base URL.

    Two URL shapes are accepted:

    1. **Dedicated Unity Gateway subdomain** — ``ai-gateway`` is a full DNS label
       in the hostname (e.g. ``<id>.ai-gateway.cloud.databricks.com``). Used by
       the standard ``isaac configure codex`` setup.
    2. **Workspace-hosted gateway** — the hostname is a plain Databricks
       workspace (under a trusted parent domain) and the path starts with
       ``/ai-gateway/`` (e.g. ``<workspace>.cloud.databricks.com/ai-gateway/...``).
       Used by ucode / Codex app profile setups.

    Both cases require ``https`` and a hostname a trusted Databricks-owned
    parent domain owns label-for-label, to prevent token forwarding to a
    look-alike host.

    :param base_url: An inference base URL, e.g. the codex provider table's
        ``base_url``.
    :returns: ``True`` iff the URL is an https Databricks Unity Gateway endpoint.
    """
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    hostname = hostname.lower()
    if not _under_trusted_domain(hostname):
        return False
    # Shape 1: ``ai-gateway`` is a full DNS label in the hostname.
    labels = hostname.split(".")
    if DATABRICKS_AI_GATEWAY_LABEL in labels:
        return True
    # Shape 2: workspace hostname + /ai-gateway/ path prefix.
    path = parsed.path or ""
    return path.startswith("/ai-gateway/")


# Unity Gateway routes a request to the model provider service (an external
# provider such as Amazon Bedrock or Azure OpenAI) named in this header; the
# request's ``model`` is then the provider's own model id, e.g.
# ``us.anthropic.claude-sonnet-5``.
MODEL_PROVIDER_SERVICE_HEADER: Final = "Databricks-Model-Provider-Service"

# Spawn-env var carrying an agent's ``executor.config.model_provider_service``
# from the runner to the SDK harness processes.
MODEL_PROVIDER_SERVICE_ENV_VAR: Final = "HARNESS_DATABRICKS_MODEL_PROVIDER_SERVICE"

# Harnesses that send the provider-service header on their gateway requests.
MODEL_PROVIDER_SERVICE_HARNESSES: Final = frozenset({"claude-sdk", "openai-agents"})

# Prefixes the Databricks catalog puts on the models it serves itself.
_DATABRICKS_MODEL_PREFIXES: Final = ("databricks/", "system.ai.", "databricks-")

# Three-part Unity Catalog name, e.g. ``main.ai.bedrock_prod``.
# Restricted to safe identifier characters because the value is sent as an HTTP
# header (and joined into Claude Code's newline-separated header list).
_MODEL_PROVIDER_SERVICE_NAME = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def validate_model_provider_service(name: str) -> str:
    """Return *name* stripped, or raise if it is not a three-part UC name.

    :param name: The configured model provider service, e.g.
        ``"main.ai.bedrock_prod"``.
    :returns: The stripped name.
    :raises ValueError: If *name* is not ``catalog.schema.service`` made of
        letters, digits, ``_`` and ``-``.
    """
    stripped = name.strip()
    if not _MODEL_PROVIDER_SERVICE_NAME.fullmatch(stripped):
        raise ValueError(
            f"model_provider_service must be a three-part Unity Catalog name "
            f"(catalog.schema.service), got {name!r}"
        )
    return stripped


def model_provider_service_headers() -> dict[str, str]:
    """Return the provider-service routing header for this harness process.

    :returns: ``{MODEL_PROVIDER_SERVICE_HEADER: name}`` when
        :data:`MODEL_PROVIDER_SERVICE_ENV_VAR` holds a valid name, else ``{}``.
    :raises ValueError: If the variable is set to an invalid name.
    """
    raw = os.environ.get(MODEL_PROVIDER_SERVICE_ENV_VAR, "")
    if not raw.strip():
        return {}
    return {MODEL_PROVIDER_SERVICE_HEADER: validate_model_provider_service(raw)}


def is_databricks_catalog_model(model: str) -> bool:
    """Return whether *model* is a Databricks-served id, which no provider service serves.

    :param model: A model id, e.g. ``"databricks-claude-sonnet-5"`` (``True``) or
        ``"us.anthropic.claude-sonnet-5"`` (``False``).
    :returns: ``True`` when *model* carries a Databricks catalog prefix.
    """
    return model.strip().lower().startswith(_DATABRICKS_MODEL_PREFIXES)
