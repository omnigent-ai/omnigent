"""Trusted project attribution for every Omnigent host launch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from omnigent.entities import Conversation, Project

PROJECT_ENUM_ENV_VAR = "OMNIGENT_PROJECT_ENUM"
QUOTA_ROUTE_ENV_VAR = "OMNIGENT_QUOTA_ROUTE"
PROJECT_ATTRIBUTION_ERROR_CODE = "project_attribution_required"
ALLOWED_PROJECT_ENUMS = frozenset({"chatgpt-playground", "planar-jacobian", "planar-jc-codex"})
PERSONAL_QUOTA_ROUTE = "personal-llmq"
WORK_QUOTA_ROUTE = "work-vertex"
ALLOWED_QUOTA_ROUTES = frozenset({PERSONAL_QUOTA_ROUTE, WORK_QUOTA_ROUTE})
LEGACY_PERSONAL_PROJECT_ENUM = "planar-jacobian"


class LaunchProjectStore(Protocol):
    """Minimal server-side project lookup used at the launch boundary."""

    def get_for_session_launch(self, project_id: str) -> Project | None: ...


class ProjectAttributionError(ValueError):
    """A host launch lacks an authoritative project/route classification."""


@dataclass(frozen=True)
class LaunchAttribution:
    """Server-authoritative routing metadata carried to a host launch."""

    quota_route: str
    project_enum: str | None


def is_allowed_project_enum(value: object) -> bool:
    """Return whether *value* is one exact member of the closed project set."""
    return isinstance(value, str) and value in ALLOWED_PROJECT_ENUMS


def is_allowed_quota_route(value: object) -> bool:
    """Return whether *value* is one exact member of the closed route set."""
    return isinstance(value, str) and value in ALLOWED_QUOTA_ROUTES


def resolve_launch_attribution(
    conversation: Conversation,
    *,
    harness: str | None,
    project_store: LaunchProjectStore | None,
) -> LaunchAttribution:
    """Resolve routing from an authoritative project row for every harness."""
    del harness  # Classification is project-owned, never harness- or env-inferred.
    if conversation.project_id is None:
        # Pre-project sessions are personal by construction.  Preserve their
        # availability with the conservative agent-infra default; explicitly
        # projected work remains the only path to the Vertex route below.
        return LaunchAttribution(
            quota_route=PERSONAL_QUOTA_ROUTE,
            project_enum=LEGACY_PERSONAL_PROJECT_ENUM,
        )
    if project_store is None:
        raise ProjectAttributionError("Omnigent host launch requires the project store")
    project = project_store.get_for_session_launch(conversation.project_id)
    if project is None:
        raise ProjectAttributionError("Omnigent host launch project was not found")

    quota_route = project.config.get("quota_route")
    if not is_allowed_quota_route(quota_route):
        raise ProjectAttributionError("project has no allowed quota_route")
    if quota_route == PERSONAL_QUOTA_ROUTE:
        if not is_allowed_project_enum(project.name):
            raise ProjectAttributionError(
                f"project {project.name!r} is not an allowed personal-agent project"
            )
        return LaunchAttribution(quota_route=PERSONAL_QUOTA_ROUTE, project_enum=project.name)

    if is_allowed_project_enum(project.name):
        raise ProjectAttributionError("personal project name cannot select the work route")
    return LaunchAttribution(quota_route=WORK_QUOTA_ROUTE, project_enum=None)


def resolve_launch_project_enum(
    conversation: Conversation,
    *,
    harness: str | None,
    project_store: LaunchProjectStore | None,
) -> str | None:
    """Resolve a launch's closed personal enum from server-owned project rows.

    Legacy labels are deliberately ignored: they are client-mutable and cannot
    select quota priority. Work launches return ``None`` only after their
    server-owned work-Vertex route has been validated.
    """
    return resolve_launch_attribution(
        conversation,
        harness=harness,
        project_store=project_store,
    ).project_enum
