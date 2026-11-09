"""Canonical predicate for recognizing a Databricks Unity Gateway base URL.

Several surfaces need the same answer — pi-native rewrites a gateway Codex
base URL to the Anthropic surface, and host-side routing capability checks ask
whether a resolved harness launch is gateway-backed. Keeping one predicate here
means a look-alike host is rejected identically everywhere.

The usage-attribution headers every harness sends to the gateway live here too,
so each harness names the coding agent and client surfaces the same way.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
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
REQUEST_TAGS_HEADER: Final[str] = "Databricks-Ai-Gateway-Request-Tags"
CI_CONTEXT_ENV: Final[str] = "OMNIGENT_CI_CONTEXT"
_HeaderValue = TypeVar("_HeaderValue")
_MAX_CI_TAG_HEADER_LENGTH: Final[int] = 4096
_logger = logging.getLogger(__name__)

_CI_TAG_ENV: Final[dict[str, str]] = {
    "pipeline": "OMNIGENT_CI_PIPELINE",
    "repository": "GITHUB_REPOSITORY",
    "workflow": "GITHUB_WORKFLOW_REF",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
    "job": "GITHUB_JOB",
    "source_session_id": "OMNIGENT_CI_SOURCE_SESSION_ID",
    "pr_number": "OMNIGENT_CI_PR_NUMBER",
    "reviewed_sha": "OMNIGENT_CI_REVIEWED_SHA",
}


def _decode_request_tags(value: str) -> dict[str, str]:
    """Validate a bounded JSON string-to-string tag object."""
    if len(value) > _MAX_CI_TAG_HEADER_LENGTH:
        raise ValueError("Request tags exceed the CI header size budget")
    tags = json.loads(value)
    if not isinstance(tags, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in tags.items()
    ):
        raise ValueError("Request tags must contain a JSON string-to-string object")
    return tags


def ci_request_tags(
    source: Mapping[str, str] | None = None, *, strict: bool = False
) -> dict[str, str]:
    """Read opted-in CI tags; invalid optional metadata cannot block model calls.

    CI setup commands use ``strict=True`` to reject invalid attribution early.
    """
    env = os.environ if source is None else source
    try:
        if context := env.get(CI_CONTEXT_ENV):
            tags = {
                key: value
                for key, value in _decode_request_tags(context).items()
                if key in _CI_TAG_ENV and value
            }
        elif env.get("OMNIGENT_CI_PIPELINE"):
            tags = {key: env[name] for key, name in _CI_TAG_ENV.items() if env.get(name)}
        else:
            return {}
        if not tags.get("pipeline"):
            raise ValueError("CI attribution requires a nonempty pipeline")
        if workflow := tags.get("workflow"):
            tags["workflow"] = workflow.split("@", 1)[0]
        if len(json.dumps(tags, separators=(",", ":"))) > _MAX_CI_TAG_HEADER_LENGTH:
            raise ValueError("Request tags exceed the CI header size budget")
        return tags
    except (ValueError, RecursionError) as exc:
        if strict:
            raise ValueError("Invalid CI request tags") from exc
        _logger.warning("Ignoring invalid CI request tags; check OMNIGENT_CI_CONTEXT/PIPELINE")
        return {}


def with_ci_request_tags(headers: Mapping[str, _HeaderValue]) -> dict[str, _HeaderValue | str]:
    """Merge CI tags into a gateway header map, preserving other request tags."""
    merged: dict[str, _HeaderValue | str] = dict(headers)
    tags = ci_request_tags()
    if not tags:
        return merged
    existing_tags: dict[str, str] = {}
    try:
        for name in list(merged):
            if name.lower() != REQUEST_TAGS_HEADER.lower():
                continue
            value = merged.pop(name)
            if not isinstance(value, str):
                raise ValueError("Request tag header must be a string")
            existing_tags.update(_decode_request_tags(value))
        encoded = json.dumps({**existing_tags, **tags}, separators=(",", ":"))
        if len(encoded) > _MAX_CI_TAG_HEADER_LENGTH:
            raise ValueError("Request tags exceed the CI header size budget")
    except (ValueError, RecursionError):
        _logger.warning("Skipping CI tags: existing request tags cannot be safely merged")
        return dict(headers)
    merged[REQUEST_TAGS_HEADER] = encoded
    return merged


def gateway_request_tag_headers(base_url: str | None) -> dict[str, str]:
    """Add optional CI headers without validating unrelated provider configuration."""
    if not ci_request_tags():
        return {}
    try:
        if base_url and is_databricks_ai_gateway_url(base_url):
            return with_ci_request_tags({})
    except ValueError:
        _logger.warning("Skipping CI tags: malformed provider URL")
    return {}


def ci_gateway_base_url(base_url: str, protocol: str) -> str:
    """Resolve a CI Databricks URL to a supported gateway protocol surface."""
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not _under_trusted_domain(parsed.hostname)
    ):
        raise ValueError("CI model usage requires an HTTPS Databricks workspace or gateway URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "CI gateway URL must not contain credentials, query parameters, or fragments"
        )
    if protocol not in {"anthropic", "codex/v1", "mlflow/v1"}:
        raise ValueError("Unsupported CI gateway protocol")
    prefix = "" if DATABRICKS_AI_GATEWAY_LABEL in parsed.hostname.split(".") else "/ai-gateway"
    return f"https://{parsed.netloc}{prefix}/{protocol}"


# Coding-agent names, spelled the way the gateway's attribution normalizes them.
CLAUDE_CODE_AGENT: Final[str] = "claude-code"
CODEX_AGENT: Final[str] = "codex"
PI_AGENT: Final[str] = "pi"
OPENCODE_AGENT: Final[str] = "opencode"

# ug stamps ``ucode/<version> <agent>/<version>`` on the providers it writes.
_UG_USER_AGENT_PREFIX: Final[str] = "ucode/"

# Session client surface (``omnigent.telemetry.surface``) -> the gateway's surface name.
_OMNIGENT_CLIENT_SURFACES: Final[dict[str, str]] = {
    "desktop": "omnigent-desktop",
    "web": "omnigent-web",
    "ios": "omnigent-mobile",
    "android": "omnigent-mobile",
    "mobile": "omnigent-mobile",
    "cli": "omnigent-cli",
}
_ATTRIBUTION_HEADER_NAMES: Final[tuple[str, str]] = (CODING_AGENT_HEADER, CLIENT_SURFACES_HEADER)
_ATTRIBUTION_HEADER_KEYS: Final[frozenset[str]] = frozenset(
    name.lower() for name in _ATTRIBUTION_HEADER_NAMES
)


def gateway_attribution_headers(
    coding_agent: str,
    *,
    via_ug: bool = False,
    client_surfaces: Iterable[str] = (),
    client_surface: str | None = None,
) -> dict[str, str]:
    """Return the attribution headers for a Unity Gateway request Omnigent originates.

    :param coding_agent: The harness making the request, e.g. ``"codex"``.
    :param via_ug: Whether the route comes from ug's configuration.
    :param client_surfaces: Surfaces already on the request; stale Omnigent ones are dropped.
    :param client_surface: The session's creating interface, e.g. ``"desktop"``;
        ``None`` or an unknown value sends only ``omnigent``.
    :returns: e.g. ``{"Databricks-Custom-Coding-Agent": "codex",
        "Databricks-Custom-Client-Surfaces": "omnigent-desktop, ug"}``.
    """
    qualified = _OMNIGENT_CLIENT_SURFACES.get(client_surface or "")
    stale = {"omnigent", *_OMNIGENT_CLIENT_SURFACES.values()}
    candidates = (
        qualified or "omnigent",
        *(surface for surface in client_surfaces if surface.strip().lower() not in stale),
        *(("ug",) if via_ug else ()),
    )
    surfaces: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        name = candidate.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            surfaces.append(name)
    return with_ci_request_tags(
        {CODING_AGENT_HEADER: coding_agent, CLIENT_SURFACES_HEADER: ", ".join(surfaces)}
    )


def is_ug_route(base_url: str) -> bool:
    """Whether ug has configured the workspace that serves *base_url*."""
    from omnigent.onboarding.ucode_state import read_ucode_state

    hostname = urlparse(base_url).hostname
    return hostname is not None and read_ucode_state(f"https://{hostname}") is not None


def with_gateway_attribution_pairs(
    headers: Iterable[tuple[str, _HeaderValue]],
    coding_agent: str,
    *,
    via_ug: bool = False,
    client_surface: str | None = None,
) -> list[tuple[str, _HeaderValue | str]]:
    """Replace the attribution in an ordered header list, keeping every other entry.

    Repeated headers survive; existing surfaces are merged and a ug ``User-Agent`` adds ``ug``.
    """
    kept: list[tuple[str, _HeaderValue | str]] = []
    surfaces: list[str] = []
    for name, value in headers:
        lowered = name.lower()
        if lowered in _ATTRIBUTION_HEADER_KEYS:
            if lowered == CLIENT_SURFACES_HEADER.lower() and isinstance(value, str):
                surfaces.extend(value.split(","))
            continue
        if lowered == "user-agent" and isinstance(value, str):
            via_ug = via_ug or any(
                product.startswith(_UG_USER_AGENT_PREFIX) for product in value.split()
            )
        kept.append((name, value))
    attribution = gateway_attribution_headers(
        coding_agent, via_ug=via_ug, client_surfaces=surfaces, client_surface=client_surface
    )
    if REQUEST_TAGS_HEADER in attribution:
        attribution.pop(REQUEST_TAGS_HEADER)
        tag_headers = {
            name: value for name, value in kept if name.lower() == REQUEST_TAGS_HEADER.lower()
        }
        merged_tags = with_ci_request_tags(tag_headers)
        if merged_tags != tag_headers:
            kept = [
                (name, value)
                for name, value in kept
                if name.lower() != REQUEST_TAGS_HEADER.lower()
            ]
            kept.extend(merged_tags.items())
    return [*kept, *attribution.items()]


def with_gateway_attribution(
    headers: Mapping[str, _HeaderValue] | None,
    coding_agent: str,
    base_url: str,
    *,
    via_ug: bool = False,
    client_surface: str | None = None,
) -> dict[str, _HeaderValue | str]:
    """Return a provider's static *headers* plus the attribution headers.

    The provider is ug's when ug configured its workspace or wrote its ``User-Agent``.
    """
    return dict(
        with_gateway_attribution_pairs(
            (headers or {}).items(),
            coding_agent,
            via_ug=via_ug or is_ug_route(base_url),
            client_surface=client_surface,
        )
    )


def with_gateway_attribution_lines(
    custom_headers: str | None,
    coding_agent: str,
    *,
    via_ug: bool = False,
    client_surface: str | None = None,
) -> str:
    """Merge attribution into Claude Code's ``ANTHROPIC_CUSTOM_HEADERS`` block.

    :param custom_headers: The existing newline-separated ``Name: value`` block, or ``None``.
    :param coding_agent: The harness making the request, e.g. ``"claude-code"``.
    :param via_ug: Whether the route comes from ug's configuration.
    :param client_surface: The session's creating interface, e.g. ``"web"``.
    :returns: The block with its other lines unchanged and one line per attribution header.
    """
    lines: list[str] = []
    headers: list[tuple[str, str]] = []
    replaced_headers = _ATTRIBUTION_HEADER_KEYS
    if ci_request_tags():
        replaced_headers = replaced_headers | {REQUEST_TAGS_HEADER.lower()}
    for line in (custom_headers or "").splitlines():
        name, separator, value = line.partition(":")
        if separator:
            headers.append((name.strip(), value.strip()))
        if name.strip().lower() not in replaced_headers:
            lines.append(line)
    attributed = with_gateway_attribution_pairs(
        headers, coding_agent, via_ug=via_ug, client_surface=client_surface
    )
    lines.extend(
        f"{name}: {value}" for name, value in attributed if name.lower() in replaced_headers
    )
    return "\n".join(lines)
