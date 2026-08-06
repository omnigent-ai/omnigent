"""Canonical predicate for recognizing a Databricks AI Gateway base URL.

Several surfaces need the same answer — pi-native rewrites a gateway Codex
base URL to the Anthropic surface, and host-side routing capability checks ask
whether a resolved harness launch is gateway-backed. Keeping one predicate here
means a look-alike host is rejected identically everywhere.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

# Trusted parent domains for a Databricks-owned host. The AI Gateway lives
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

# A genuine AI Gateway host carries the ``ai-gateway`` DNS label; we require it
# (alongside a trusted suffix) so a non-gateway Databricks host isn't routed as
# the gateway's Anthropic surface.
DATABRICKS_AI_GATEWAY_LABEL: Final[str] = "ai-gateway"

# Databricks-owned parent domain serving Databricks Apps; a workspace app is
# reached at ``<app>-<id>.<cloud>.databricksapps.com``. Kept separate from
# DATABRICKS_TRUSTED_HOST_SUFFIXES so the AI Gateway predicate stays exactly as
# narrow as it is today.
DATABRICKS_APPS_HOST_SUFFIXES: Final[tuple[str, ...]] = (".databricksapps.com",)


def _under_trusted_domain(
    hostname: str, *, parents: tuple[str, ...] = DATABRICKS_TRUSTED_HOST_SUFFIXES
) -> bool:
    """Whether *hostname* is a subdomain of a trusted Databricks parent domain.

    Compares whole DNS labels from the right, so the parent must be an exact
    label-wise suffix with at least one label of its own in front of it. A
    string-suffix test would be looser in both directions.

    :param hostname: Lower-cased hostname from a parsed URL, e.g.
        ``"wkspc.ai-gateway.cloud.databricks.com"``.
    :param parents: Parent domains to accept, written with a leading dot.
    :returns: ``True`` when a trusted parent domain owns *hostname*.
    """
    labels = hostname.split(".")
    for parent in parents:
        parent_labels = parent.strip(".").split(".")
        if len(labels) > len(parent_labels) and labels[-len(parent_labels) :] == parent_labels:
            return True
    return False


def is_databricks_owned_url(url: str) -> bool:
    """Whether *url* is an https URL served by a Databricks-owned domain.

    Broader than :func:`is_databricks_ai_gateway_url`: any workspace control
    plane or Databricks App under a trusted parent domain qualifies. Callers
    use it to decide whether a Databricks credential may be sent to a URL at
    all, so it matches whole DNS labels and requires https for the same
    look-alike-host reasons.

    :param url: An absolute URL, e.g. an Omnigent ``server_url``.
    :returns: ``True`` iff a Databricks-owned parent domain serves *url*.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    return _under_trusted_domain(hostname) or _under_trusted_domain(
        hostname, parents=DATABRICKS_APPS_HOST_SUFFIXES
    )


def is_databricks_ai_gateway_url(base_url: str) -> bool:
    """Return ``True`` only for a genuine Databricks AI Gateway base URL.

    Two URL shapes are accepted:

    1. **Dedicated AI Gateway subdomain** — ``ai-gateway`` is a full DNS label
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
    :returns: ``True`` iff the URL is an https Databricks AI Gateway endpoint.
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
