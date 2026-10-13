"""Deployment-wide release feature management.

Release feature defaults can be overridden by comma-separated names in
``OMNIGENT_FEATURES``; prefix a name with ``-`` to disable it. Values are
resolved once when an application, route factory or runner resource registry
is built, giving each one an immutable snapshot.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

_logger = logging.getLogger(__name__)

FEATURES_ENV_VAR = "OMNIGENT_FEATURES"
_REMOVED_HARNESS_INSTALL_ENV_VAR = "OMNIGENT_HARNESS_INSTALL_ENABLED"
# Features made unconditional; leftover names or ``-name`` kill switches are ignored.
_RETIRED_FEATURES = frozenset({"custom_agents_settings_ui"})


class Feature(StrEnum):
    """Canonical release-feature keys accepted by ``OMNIGENT_FEATURES``."""

    SANDBOX_PROFILES = "sandbox_profiles"
    SESSION_ORCHESTRATION = "session_orchestration"
    SCRIPTED_SESSIONS = "scripted_sessions"
    SIBLING_SESSIONS = "sibling_sessions"
    PEER_SESSION_MESSAGING = "peer_session_messaging"
    CODEX_PLAN_AT_LAUNCH = "codex_plan_at_launch"
    WORKSPACE_UPLOAD = "workspace_upload"
    USAGE_PAGE = "usage_page"
    HARNESS_INSTALL = "harness_install"
    MCP_DISABLED_SERVERS = "mcp_disabled_servers"
    MCP_DISABLED_SERVERS_CLAUDE = "mcp_disabled_servers_claude"
    MCP_DISABLED_SERVERS_CODEX = "mcp_disabled_servers_codex"
    CANVAS = "canvas"
    ARCA_SHUTDOWN_WARNINGS = "arca_shutdown_warnings"
    IMPORT_REVIEW = "import_review"
    SIDEBAR_GROUPING_UI = "sidebar_grouping_ui"
    CLAUDE_OSC_STATUS = "claude_osc_status"
    WORKSPACE_CONTENT_SEARCH = "workspace_content_search"
    GRID_SESSIONS_DISPLAY_UI = "grid_sessions_display_ui"
    AUTOMATIONS_V2 = "automations_v2"
    SPLITTABLE_TERMINAL_VIEW_UI = "splittable_terminal_view_ui"
    SIDEBAR_WINDOWED_ROWS = "sidebar_windowed_rows"
    SESSION_OVERVIEW = "session_overview"
    PROJECT_REPOSITORY_CREATION = "project_repository_creation"
    WORKSPACE_DIRECT_ACCESS = "workspace_direct_access"
    WORKSPACE_API_SERVER_ACCESS = "workspace_api_server_access"


@dataclass(frozen=True)
class FeatureDefinition:
    """Lifecycle metadata for one temporary release feature."""

    feature: Feature
    description: str
    owner: str
    review_by_release: str
    frontend_visible: bool = True
    default_enabled: bool = False


FEATURE_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    FeatureDefinition(
        feature=Feature.CODEX_PLAN_AT_LAUNCH,
        description="Start Codex in Plan mode with host compatibility checks",
        owner="server",
        review_by_release="0.22.0",
        default_enabled=True,
    ),
    FeatureDefinition(
        feature=Feature.SANDBOX_PROFILES,
        description="Sandbox profile discovery, configuration and launch selection",
        owner="server",
        review_by_release="0.22.0",
    ),
    FeatureDefinition(
        feature=Feature.SESSION_ORCHESTRATION,
        description="Private-owner session messaging and independent worker creation",
        owner="server",
        review_by_release="0.22.0",
        # The Automations page offers trigger templates only when managers can
        # start workers.
        default_enabled=True,
    ),
    FeatureDefinition(
        feature=Feature.SCRIPTED_SESSIONS,
        description="Explicit host placement and PR linking for scripted sessions",
        owner="server",
        review_by_release="0.24.0",
        frontend_visible=False,
    ),
    FeatureDefinition(
        feature=Feature.SIBLING_SESSIONS,
        description="Create nested agents on shared or separate compute",
        owner="server",
        review_by_release="0.24.0",
        default_enabled=True,
    ),
    FeatureDefinition(
        feature=Feature.PEER_SESSION_MESSAGING,
        description=(
            "sys_session_message to non-child sessions; both the server and the runner "
            "must leave it enabled"
        ),
        owner="server",
        review_by_release="0.24.0",
        frontend_visible=False,
        default_enabled=True,
    ),
    FeatureDefinition(
        feature=Feature.AUTOMATIONS_V2,
        description=(
            "Automation improvements across scheduling, run management, execution targets and UI"
        ),
        owner="server",
        review_by_release="0.20.0",
        default_enabled=True,
    ),
    FeatureDefinition(
        feature=Feature.GRID_SESSIONS_DISPLAY_UI,
        description="Local session grids with up to four live conversations",
        owner="web",
        review_by_release="0.20.0",
    ),
    FeatureDefinition(
        feature=Feature.SPLITTABLE_TERMINAL_VIEW_UI,
        description="Split the main terminal view into several shell panes",
        owner="web",
        review_by_release="0.22.0",
    ),
    FeatureDefinition(
        feature=Feature.WORKSPACE_UPLOAD,
        description="Resumable uploads into session workspaces",
        owner="server",
        review_by_release="0.20.0",
    ),
    FeatureDefinition(
        feature=Feature.USAGE_PAGE,
        description="Web Usage page with cost timeline and breakdowns",
        owner="web",
        review_by_release="0.11.0",
    ),
    FeatureDefinition(
        feature=Feature.HARNESS_INSTALL,
        description="Install and configure missing harnesses from the web UI",
        owner="onboarding",
        review_by_release="0.11.0",
    ),
    FeatureDefinition(
        feature=Feature.MCP_DISABLED_SERVERS,
        frontend_visible=False,
        description="Per-harness MCP enablement in Omnigent sessions",
        owner="server",
        review_by_release="0.24.0",
    ),
    FeatureDefinition(
        feature=Feature.MCP_DISABLED_SERVERS_CLAUDE,
        frontend_visible=False,
        description="Claude MCP enablement at launch",
        owner="server",
        review_by_release="0.24.0",
    ),
    FeatureDefinition(
        feature=Feature.MCP_DISABLED_SERVERS_CODEX,
        frontend_visible=False,
        description="Codex MCP enablement at launch",
        owner="server",
        review_by_release="0.24.0",
    ),
    FeatureDefinition(
        feature=Feature.CANVAS,
        description="Web Canvas page: sessions as draggable cards grouped by project",
        owner="web",
        review_by_release="0.15.0",
    ),
    FeatureDefinition(
        feature=Feature.ARCA_SHUTDOWN_WARNINGS,
        description="Weekday Arca shutdown warning in the web UI",
        owner="web",
        review_by_release="0.20.0",
        frontend_visible=True,
    ),
    FeatureDefinition(
        feature=Feature.IMPORT_REVIEW,
        description="Auto-opening import review modal for a newly connected host's context",
        owner="onboarding",
        review_by_release="0.18.0",
    ),
    FeatureDefinition(
        feature=Feature.SIDEBAR_GROUPING_UI,
        description="Sidebar grouping, ordering and row metadata options, Shared section",
        owner="web",
        # On by default; remove the flag and its off path in 0.23.0.
        review_by_release="0.23.0",
        default_enabled=True,
    ),
    FeatureDefinition(
        feature=Feature.CLAUDE_OSC_STATUS,
        description="Claude-native working status from OSC 7501 terminal reports",
        owner="runner",
        review_by_release="0.20.0",
        frontend_visible=False,
        default_enabled=True,
    ),
    FeatureDefinition(
        feature=Feature.WORKSPACE_CONTENT_SEARCH,
        description="Search workspace file contents through the session runner",
        owner="web",
        review_by_release="0.20.0",
    ),
    FeatureDefinition(
        feature=Feature.SIDEBAR_WINDOWED_ROWS,
        description="Mount only the sidebar session rows near the viewport",
        owner="web",
        review_by_release="0.24.0",
    ),
    FeatureDefinition(
        feature=Feature.SESSION_OVERVIEW,
        description="Session cost sidebar beside chat, toggled from a header cost chip",
        owner="web",
        review_by_release="0.25.0",
    ),
    FeatureDefinition(
        feature=Feature.PROJECT_REPOSITORY_CREATION,
        description="New project repository linking: clone on a host or save for sandboxes",
        owner="web",
        review_by_release="0.25.0",
        default_enabled=True,
    ),
    FeatureDefinition(
        feature=Feature.WORKSPACE_DIRECT_ACCESS,
        description=(
            "Owners read a session's workspace straight from its host's local socket; "
            "the server and the host must both enable it"
        ),
        owner="server",
        review_by_release="0.27.0",
        frontend_visible=False,
    ),
    FeatureDefinition(
        feature=Feature.WORKSPACE_API_SERVER_ACCESS,
        description="Serve the workspace API through the server, answered by the session's host",
        owner="server",
        review_by_release="0.27.0",
        frontend_visible=False,
    ),
)


@dataclass(frozen=True)
class FeatureFlags:
    """Immutable feature values for one server or runner process."""

    enabled_features: frozenset[Feature] = frozenset()

    def enabled(self, feature: Feature) -> bool:
        """Return whether *feature* is enabled in this snapshot."""
        return feature in self.enabled_features

    def frontend_dict(self) -> dict[str, bool]:
        """Return every frontend-visible feature and its resolved value."""
        return {
            definition.feature.value: self.enabled(definition.feature)
            for definition in FEATURE_DEFINITIONS
            if definition.frontend_visible
        }

    def enabled_names(self) -> tuple[str, ...]:
        """Return enabled canonical names in stable order."""
        return tuple(sorted(feature.value for feature in self.enabled_features))


# A deployment's per-request gate: a static bool, or a callable read on each request.
DeploymentGate = bool | Callable[[], bool]
HarnessInstallGate = DeploymentGate
McpDisabledServersGate = DeploymentGate


def deployment_gate_enabled(gate: DeploymentGate, name: str) -> bool:
    """Resolve a deployment gate for the current request; a failing gate is off."""
    try:
        return bool(gate() if callable(gate) else gate)
    except Exception:  # deployment gates fail closed
        _logger.warning("%s gate failed; disabling it", name, exc_info=True)
        return False


def harness_install_enabled(gate: HarnessInstallGate) -> bool:
    """Resolve the harness-install gate for the current request."""
    return deployment_gate_enabled(gate, "harness_install")


def mcp_disabled_servers_enabled(gate: McpDisabledServersGate) -> bool:
    """Resolve the MCP deployment gate for the current request, failing closed."""
    return deployment_gate_enabled(gate, "mcp_disabled_servers")


MCP_DISABLED_SERVERS_HARNESS_FEATURES = {
    "claude": Feature.MCP_DISABLED_SERVERS_CLAUDE,
    "codex": Feature.MCP_DISABLED_SERVERS_CODEX,
}


def active_harnesses(master: McpDisabledServersGate, features: FeatureFlags) -> list[str]:
    """Harnesses with complete launch hooks and both release gates enabled."""
    from omnigent.mcp_disabled_servers import HARNESS_EXECUTORS

    if not mcp_disabled_servers_enabled(master):
        return []
    return [
        harness
        for harness in HARNESS_EXECUTORS
        if features.enabled(MCP_DISABLED_SERVERS_HARNESS_FEATURES[harness])
    ]


def resolve_feature_flags(environ: Mapping[str, str] | None = None) -> FeatureFlags:
    """Resolve ``OMNIGENT_FEATURES`` into an immutable feature snapshot.

    Comma-separated names enable features; ``-name`` disables them, for example
    ``usage_page,-claude_osc_status``. Unset or empty uses feature defaults.
    Explicit disables win regardless of order. Retired names are ignored with
    a warning; other unknown names fail startup.

    :param environ: Environment mapping; defaults to :data:`os.environ`.
    :returns: Resolved immutable feature values.
    :raises ValueError: If an override contains an unknown feature name.
    """
    source = os.environ if environ is None else environ
    legacy_harness_install = source.get(_REMOVED_HARNESS_INSTALL_ENV_VAR, "").strip().lower()
    if legacy_harness_install not in {"", "0", "false", "no", "off"}:
        raise ValueError(
            f"{_REMOVED_HARNESS_INSTALL_ENV_VAR} is no longer supported; "
            f"enable harness installation with {FEATURES_ENV_VAR}=harness_install"
        )

    raw = source.get(FEATURES_ENV_VAR, "")
    names = [name.strip() for name in raw.split(",") if name.strip()]

    enabled = {
        definition.feature for definition in FEATURE_DEFINITIONS if definition.default_enabled
    }
    disabled: set[Feature] = set()
    unknown: list[str] = []
    for name in names:
        if name.removeprefix("-") in _RETIRED_FEATURES:
            _logger.warning("%s: %s has been retired; ignoring it", FEATURES_ENV_VAR, name)
            continue
        try:
            if name.startswith("-"):
                disabled.add(Feature(name[1:]))
            else:
                enabled.add(Feature(name))
        except ValueError:
            unknown.append(name)

    if unknown:
        known = ", ".join(feature.value for feature in Feature)
        invalid = ", ".join(sorted(set(unknown)))
        raise ValueError(
            f"unknown feature(s) in {FEATURES_ENV_VAR}: {invalid}; known features: {known}"
        )

    return FeatureFlags(frozenset(enabled - disabled))
