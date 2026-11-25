"""Building a project role's agent as a copy of its native base agent."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from omnigent.entities import Agent
from omnigent.errors import OmnigentError
from omnigent.server.app import _build_native_bundle, native_provider_for_key
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.project_agents import (
    build_role_agent_bundle,
    check_base_agent,
    create_role_agent,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from tests._helpers.session import bundle_files

_OWNER = "owner@example.com"


@pytest.fixture(scope="module")
def claude_bundle() -> bytes:
    provider = native_provider_for_key("claude")
    assert provider is not None
    return _build_native_bundle(provider)


def test_role_copy_appends_instructions_and_sets_model_and_effort(claude_bundle: bytes) -> None:
    base = validate_agent_bundle(claude_bundle, enforce_handler_allowlist=True)

    bundle, spec = build_role_agent_bundle(
        claude_bundle,
        role_name="Reviewer",
        instructions="Review every change.",
        model="claude-opus-test",
        effort="high",
    )

    assert spec.name == base.name, "the editor treats the spec name as the agent's identity"
    assert spec.executor.config.get("harness") == "claude-native"
    assert spec.instructions == f"{base.instructions}\n\n## Reviewer\n\nReview every change."
    assert (spec.executor.model, spec.executor.reasoning_effort) == ("claude-opus-test", "high")
    assert validate_agent_bundle(bundle, enforce_handler_allowlist=True).name == base.name


def test_role_copy_without_settings_keeps_the_base_behavior(claude_bundle: bytes) -> None:
    base = validate_agent_bundle(claude_bundle, enforce_handler_allowlist=True)

    _bundle, spec = build_role_agent_bundle(claude_bundle, role_name="PM", instructions="  ")

    assert spec.instructions == base.instructions
    assert (spec.executor.model, spec.executor.reasoning_effort) == (
        base.executor.model,
        base.executor.reasoning_effort,
    )


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            b"spec_version: 1\nname: sdk\nexecutor:\n  config:\n    harness: claude-sdk\n",
            "native coding agent",
        ),
        (
            b"spec_version: 1\nname: env\nexecutor:\n  model: ${ROLE_MODEL}\n"
            b"  config:\n    harness: claude-native\n",
            "${...}",
        ),
    ],
)
def test_role_copy_refuses_non_native_and_env_dependent_bases(config: bytes, message: str) -> None:
    with pytest.raises(OmnigentError, match=message.replace("$", r"\$").replace(".", r"\.")):
        build_role_agent_bundle(
            bundle_files({"config.yaml": config}), role_name="R", instructions="x"
        )


def _agent(**fields: object) -> Agent:
    defaults: dict[str, object] = {
        "id": uuid.uuid4().hex,
        "created_at": 0,
        "name": "base",
        "bundle_location": "x/y",
    }
    return Agent(**{**defaults, **fields})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("base", "allowed"),
    [
        (_agent(), True),
        (_agent(kind="user", created_by=_OWNER), True),
        (_agent(kind="user", created_by="other@example.com"), False),
        (_agent(kind="user", created_by=_OWNER, project_id=uuid.uuid4().hex), False),
        (_agent(kind="user", created_by=_OWNER, session_id=uuid.uuid4().hex), False),
    ],
    ids=["server", "own", "someone-elses", "project-agent", "session-copy"],
)
def test_only_server_or_own_agents_can_be_bases(base: Agent, allowed: bool) -> None:
    if allowed:
        check_base_agent(base, owner=_OWNER)
    else:
        with pytest.raises(OmnigentError):
            check_base_agent(base, owner=_OWNER)


def test_create_role_agent_stores_a_project_scoped_copy(
    tmp_path: Path, claude_bundle: bytes
) -> None:
    agents = SqlAlchemyAgentStore(f"sqlite:///{tmp_path / 'agents.db'}")
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    base_id = uuid.uuid4().hex
    artifacts.put(f"{base_id}/base", claude_bundle)
    base = agents.create(base_id, "claude-native-ui", f"{base_id}/base")
    project = uuid.uuid4().hex

    role = create_role_agent(
        agents,
        artifacts,
        owner=_OWNER,
        project_id=project,
        base=base,
        role_name="Reviewer",
        instructions="Review every change.",
    )

    assert (role.name, role.project_id, role.created_by, role.kind) == (
        "Reviewer",
        project,
        _OWNER,
        "user",
    )
    stored = validate_agent_bundle(
        artifacts.get(role.bundle_location), enforce_handler_allowlist=True
    )
    assert stored.instructions.endswith("## Reviewer\n\nReview every change.")
    assert [a.id for a in agents.list_project_agents(_OWNER, project).data] == [role.id]
    assert agents.list_user_agents(_OWNER).data == []
