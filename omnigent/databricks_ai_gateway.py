"""Canonical predicate for recognizing a Databricks Unity Gateway base URL.

Several surfaces need the same answer — pi-native rewrites a gateway Codex
base URL to the Anthropic surface, and host-side routing capability checks ask
whether a resolved harness launch is gateway-backed. Keeping one predicate here
means a look-alike host is rejected identically everywhere.

The usage-attribution headers every harness sends to the gateway live here too,
so each harness names the coding agent and client surfaces the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, TypeVar
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


# Usage-attribution headers the Unity Gateway reads: the coding agent, and the
# client surfaces as a comma-separated list, outermost first.
CODING_AGENT_HEADER: Final[str] = "Databricks-Custom-Coding-Agent"
CLIENT_SURFACES_HEADER: Final[str] = "Databricks-Custom-Client-Surfaces"

# Coding-agent names, spelled the way the gateway's attribution normalizes them.
CLAUDE_CODE_AGENT: Final[str] = "claude-code"
CODEX_AGENT: Final[str] = "codex"
PI_AGENT: Final[str] = "pi"
OPENCODE_AGENT: Final[str] = "opencode"

# ug stamps ``ucode/<version> <agent>/<version>`` on the providers it writes.
_UG_USER_AGENT_PREFIX: Final[str] = "ucode/"

_HeaderValue = TypeVar("_HeaderValue")


def gateway_attribution_headers(coding_agent: str, *, via_ug: bool = False) -> dict[str, str]:
    """Return the attribution headers for a Unity Gateway request Omnigent originates.

    :param coding_agent: The harness making the request, e.g. ``"codex"``.
    :param via_ug: Whether the route comes from ug's configuration.
    :returns: e.g. ``{"Databricks-Custom-Coding-Agent": "codex",
        "Databricks-Custom-Client-Surfaces": "omnigent, ug"}``.
    """
    surfaces = "omnigent, ug" if via_ug else "omnigent"
    return {CODING_AGENT_HEADER: coding_agent, CLIENT_SURFACES_HEADER: surfaces}


def is_ug_route(base_url: str) -> bool:
    """Whether ug has configured the workspace that serves *base_url*."""
    from omnigent.onboarding.ucode_state import read_ucode_state

    hostname = urlparse(base_url).hostname
    return hostname is not None and read_ucode_state(f"https://{hostname}") is not None


def with_gateway_attribution(
    headers: Mapping[str, _HeaderValue] | None, coding_agent: str, base_url: str
) -> dict[str, _HeaderValue | str]:
    """Return a provider's static *headers* plus the attribution headers.

    The provider is ug's when ug configured its workspace or wrote its ``User-Agent``.
    """
    existing: dict[str, _HeaderValue | str] = dict(headers or {})
    user_agent = existing.get("User-Agent")
    via_ug = (
        isinstance(user_agent, str) and user_agent.startswith(_UG_USER_AGENT_PREFIX)
    ) or is_ug_route(base_url)
    return {**existing, **gateway_attribution_headers(coding_agent, via_ug=via_ug)}


def with_gateway_attribution_lines(
    custom_headers: str | None, coding_agent: str, *, via_ug: bool = False
) -> str:
    """Append the attribution headers to Claude Code's ``ANTHROPIC_CUSTOM_HEADERS`` block.

    :param custom_headers: The existing newline-separated ``Name: value`` block, or ``None``.
    :param coding_agent: The harness making the request, e.g. ``"claude-code"``.
    :param via_ug: Whether the route comes from ug's configuration.
    :returns: The block with both attribution lines appended.
    """
    lines = [custom_headers] if custom_headers else []
    attribution = gateway_attribution_headers(coding_agent, via_ug=via_ug)
    lines.extend(f"{name}: {value}" for name, value in attribution.items())
    return "\n".join(lines)
