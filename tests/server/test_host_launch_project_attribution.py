"""Server-authoritative quota project attribution for host launches."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.entities import Conversation, Project
from omnigent.host.project_attribution import (
    ProjectAttributionError,
    resolve_launch_attribution,
    resolve_launch_project_enum,
)


def _conversation(*, project_id: str | None, labels: dict[str, str] | None = None) -> Conversation:
    return Conversation(
        id="conv_1",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_1",
        project_id=project_id,
        labels=labels or {},
    )


def _store(
    *,
    project_id: str = "project_uuid",
    name: str = "chatgpt-playground",
    config: dict[str, str] | None = None,
):
    project = Project(
        id=project_id,
        name=name,
        user_id="owner",
        created_at=1,
        config=config or {"quota_route": "personal-llmq"},
    )
    return SimpleNamespace(
        get_for_session_launch=lambda requested_id: project if requested_id == project_id else None
    )


def test_personal_native_launch_uses_project_store_name() -> None:
    """The opaque session project id resolves through the authoritative store."""
    conv = _conversation(
        project_id="project_uuid",
        labels={"omni_project": "planar-jacobian"},
    )

    assert (
        resolve_launch_project_enum(conv, harness="codex-native", project_store=_store())
        == "chatgpt-playground"
    )


@pytest.mark.parametrize("harness", ["codex-native", "claude-native"])
def test_legacy_unprojected_native_launch_defaults_to_personal_agent_infra(
    harness: str,
) -> None:
    attribution = resolve_launch_attribution(
        _conversation(project_id=None),
        harness=harness,
        project_store=_store(),
    )
    assert attribution.quota_route == "personal-llmq"
    assert attribution.project_enum == "planar-jacobian"


def test_personal_native_launch_rejects_unknown_project_name() -> None:
    with pytest.raises(ProjectAttributionError, match="not an allowed"):
        resolve_launch_project_enum(
            _conversation(project_id="project_uuid"),
            harness="claude-native",
            project_store=_store(name="attacker-selected-priority"),
        )


def test_legacy_label_cannot_override_project_store() -> None:
    conv = _conversation(
        project_id="project_uuid",
        labels={"omni_project": "attacker-selected-priority"},
    )

    assert (
        resolve_launch_project_enum(conv, harness="claude-native", project_store=_store())
        == "chatgpt-playground"
    )


def test_non_personal_harness_preserves_legacy_behavior_without_project_store() -> None:
    assert (
        resolve_launch_project_enum(
            _conversation(project_id=None), harness="claude-sdk", project_store=None
        )
        == "planar-jacobian"
    )


def test_every_harness_carries_personal_route_and_project() -> None:
    attribution = resolve_launch_attribution(
        _conversation(project_id="project_uuid"),
        harness="opifex",
        project_store=_store(),
    )

    assert attribution.quota_route == "personal-llmq"
    assert attribution.project_enum == "chatgpt-playground"


def test_work_route_requires_explicit_vertex_route() -> None:
    with pytest.raises(ProjectAttributionError, match="allowed quota_route"):
        resolve_launch_attribution(
            _conversation(project_id="project_uuid"),
            harness="claude-native",
            project_store=_store(
                name="work-agents",
                config={"quota_route": "work"},
            ),
        )

    attribution = resolve_launch_attribution(
        _conversation(project_id="project_uuid"),
        harness="claude-native",
        project_store=_store(
            name="work-agents",
            config={"quota_route": "work-vertex"},
        ),
    )
    assert attribution.quota_route == "work-vertex"
    assert attribution.project_enum is None
