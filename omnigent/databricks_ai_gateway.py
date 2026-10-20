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
import os
from collections.abc import Collection, Mapping
from pathlib import Path
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


# Usage-attribution headers the Unity Gateway reads. The coding-agent header
# outranks what the gateway infers from the User-Agent; the surfaces header is
# a comma-separated list, outermost first, that the gateway prepends to the
# surfaces it infers from the User-Agent.
CODING_AGENT_HEADER: Final[str] = "Databricks-Custom-Coding-Agent"
CLIENT_SURFACES_HEADER: Final[str] = "Databricks-Custom-Client-Surfaces"

# Coding-agent names, spelled the way the gateway's attribution normalizes them.
CLAUDE_CODE_AGENT: Final[str] = "claude-code"
CODEX_AGENT: Final[str] = "codex"
PI_AGENT: Final[str] = "pi"
OPENCODE_AGENT: Final[str] = "opencode"

OMNIGENT_SURFACE: Final[str] = "omnigent"
# The Unity Gateway names ucode-configured routes ``ug``.
UG_SURFACE: Final[str] = "ug"
# ucode stamps ``ucode/<version> <agent>/<version>`` on the providers it writes.
_UCODE_USER_AGENT_PREFIX: Final[str] = "ucode/"
# Set in a harness's env when its gateway route comes from ug's state, for
# in-process harnesses whose provider config carries no ug User-Agent.
UG_ROUTE_ENV: Final[str] = "OMNIGENT_GATEWAY_ROUTE_VIA_UG"

_CLAUDE_CUSTOM_HEADERS_ENV: Final[str] = "ANTHROPIC_CUSTOM_HEADERS"
_CLAUDE_BASE_URL_ENV: Final[str] = "ANTHROPIC_BASE_URL"

_HeaderValue = TypeVar("_HeaderValue")


def _header_value(headers: Mapping[str, object], name: str) -> str | None:
    """Return the string value of header *name* in *headers*, matched case-insensitively."""
    folded = name.casefold()
    for key, value in headers.items():
        if key.casefold() == folded and isinstance(value, str):
            return value
    return None


def gateway_attribution_headers(
    coding_agent: str,
    *,
    via_ucode: bool = False,
    inner_surfaces: str | None = None,
) -> dict[str, str]:
    """Return the attribution headers for a Unity Gateway request Omnigent originates.

    :param coding_agent: The harness making the request, e.g. ``"codex"``.
    :param via_ucode: Whether the route comes from ucode-generated config, which
        nests ``ug`` inside Omnigent (``"omnigent, ug"``).
    :param inner_surfaces: An already-present surfaces header value, e.g. one
        ucode wrote; its entries follow Omnigent's, duplicates dropped.
    :returns: Header name to value, e.g. ``{"Databricks-Custom-Coding-Agent":
        "codex", "Databricks-Custom-Client-Surfaces": "omnigent, ug"}``.
    """
    surfaces = [OMNIGENT_SURFACE]
    if via_ucode:
        surfaces.append(UG_SURFACE)
    surfaces.extend(part.strip() for part in (inner_surfaces or "").split(","))
    unique = [surface for surface in dict.fromkeys(surfaces) if surface]
    return {CODING_AGENT_HEADER: coding_agent, CLIENT_SURFACES_HEADER: ", ".join(unique)}


def _attribution_for(
    headers: Mapping[str, object], coding_agent: str, *, via_ucode: bool
) -> dict[str, str]:
    """Attribution for a provider whose static headers are *headers*.

    A ucode ``User-Agent`` marks the route as ucode's, and an existing surfaces
    header supplies the inner surfaces.
    """
    user_agent = _header_value(headers, "User-Agent") or ""
    return gateway_attribution_headers(
        coding_agent,
        via_ucode=via_ucode or user_agent.strip().casefold().startswith(_UCODE_USER_AGENT_PREFIX),
        inner_surfaces=_header_value(headers, CLIENT_SURFACES_HEADER),
    )


def with_gateway_attribution(
    headers: Mapping[str, _HeaderValue] | None,
    coding_agent: str,
    *,
    via_ucode: bool = False,
) -> dict[str, _HeaderValue | str]:
    """Return *headers* with the Unity Gateway attribution headers merged in.

    Existing attribution headers are replaced, keeping any inner surfaces they
    named. A ucode ``User-Agent`` among *headers* marks the route as ucode's.

    :param headers: A provider's existing static request headers, e.g. a Codex
        ``http_headers`` table, or ``None``.
    :param coding_agent: The harness making the request, e.g. ``"codex"``.
    :param via_ucode: Force the ``ug`` surface even without a ucode User-Agent.
    :returns: A new header mapping carrying both attribution headers.
    """
    existing = dict(headers or {})
    attribution = _attribution_for(existing, coding_agent, via_ucode=via_ucode)
    replaced = {name.casefold() for name in attribution}
    merged: dict[str, _HeaderValue | str] = {
        name: value for name, value in existing.items() if name.casefold() not in replaced
    }
    merged.update(attribution)
    return merged


def with_gateway_attribution_lines(
    custom_headers: str | None,
    coding_agent: str,
    *,
    via_ucode: bool = False,
) -> str:
    """Merge the attribution headers into a newline-separated ``Name: value`` block.

    The block format is Claude Code's ``ANTHROPIC_CUSTOM_HEADERS``. Lines for
    other headers keep their order; see :func:`with_gateway_attribution`.

    :param custom_headers: The existing block, e.g.
        ``"x-databricks-use-coding-agent-mode: true"``, or ``None``.
    :param coding_agent: The harness making the request, e.g. ``"claude-code"``.
    :param via_ucode: Force the ``ug`` surface even without a ucode User-Agent.
    :returns: The block with both attribution lines appended.
    """
    lines = [line for line in (custom_headers or "").splitlines() if line.strip()]
    parsed: dict[str, str] = {}
    for line in lines:
        name, _, value = line.partition(":")
        parsed.setdefault(name.strip(), value.strip())
    attribution = _attribution_for(parsed, coding_agent, via_ucode=via_ucode)
    replaced = {name.casefold() for name in attribution}
    kept = [line for line in lines if line.partition(":")[0].strip().casefold() not in replaced]
    kept.extend(f"{name}: {value}" for name, value in attribution.items())
    return "\n".join(kept)


def gateway_route_via_ug(env: Mapping[str, str] | None = None) -> bool:
    """Whether *env* (default: the process env) marks the gateway route as ug's."""
    return (os.environ if env is None else env).get(UG_ROUTE_ENV) == "1"


def _claude_settings_file_env(
    project_dir: Path, setting_sources: Collection[str] | None
) -> dict[str, str]:
    """The ``env`` Claude Code would take from its settings files, by file precedence.

    :param project_dir: Claude Code's working directory.
    :param setting_sources: Loaded sources (``"user"``, ``"project"``,
        ``"local"``), or ``None`` for all of them.
    :returns: Merged string-valued env, local over project over user.
    """
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = {
        "user": config_dir / "settings.json",
        "project": project_dir / ".claude" / "settings.json",
        "local": project_dir / ".claude" / "settings.local.json",
    }
    merged: dict[str, str] = {}
    for source, path in files.items():
        if setting_sources is not None and source not in setting_sources:
            continue
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        env = settings.get("env") if isinstance(settings, dict) else None
        if isinstance(env, dict):
            merged.update({key: value for key, value in env.items() if isinstance(value, str)})
    return merged


def claude_settings_attribution_env(
    custom_headers: str | None,
    *,
    project_dir: Path,
    setting_sources: Collection[str] | None = None,
) -> dict[str, str]:
    """Env for Claude's ``--settings`` that keeps attribution past a settings-file header block.

    Claude Code ranks settings-file ``env`` above process env, so a header block
    there silently replaces *custom_headers*. ``--settings`` ranks above those
    files (only managed settings outrank it), so the file's block is re-sent
    from there with the attribution lines merged in.

    :param custom_headers: The ``ANTHROPIC_CUSTOM_HEADERS`` block Omnigent launches with.
    :param project_dir: Claude Code's working directory.
    :param setting_sources: Loaded setting sources, or ``None`` for all.
    :returns: ``{"ANTHROPIC_CUSTOM_HEADERS": ...}``, or ``{}`` when no file
        overrides the block, the block carries no attribution, or the file
        routes Claude off the gateway.
    """
    parsed = {
        name.strip().casefold(): value.strip()
        for name, _, value in (line.partition(":") for line in (custom_headers or "").splitlines())
    }
    coding_agent = parsed.get(CODING_AGENT_HEADER.casefold())
    if not coding_agent:
        return {}
    file_env = _claude_settings_file_env(project_dir, setting_sources)
    file_headers = file_env.get(_CLAUDE_CUSTOM_HEADERS_ENV)
    if file_headers is None:
        return {}
    base_url = file_env.get(_CLAUDE_BASE_URL_ENV)
    if base_url is not None and not is_databricks_ai_gateway_url(base_url):
        return {}
    surfaces = parsed.get(CLIENT_SURFACES_HEADER.casefold(), "")
    via_ug = UG_SURFACE in (part.strip() for part in surfaces.split(","))
    return {
        _CLAUDE_CUSTOM_HEADERS_ENV: with_gateway_attribution_lines(
            file_headers, coding_agent, via_ucode=via_ug
        )
    }
