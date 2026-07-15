"""Tests for operator-managed agent registration."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.agent_registration import register_agent
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore


def _write_agent(root: Path, instructions: str) -> None:
    root.mkdir()
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "managed-agent",
                "executor": {"type": "omnigent", "config": {"harness": "codex-native"}},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(instructions, encoding="utf-8")


def test_register_agent_is_idempotent_for_unchanged_bundle(db_uri: str, tmp_path: Path) -> None:
    source = tmp_path / "managed-agent"
    _write_agent(source, "You are the managed agent.\n")
    store = SqlAlchemyAgentStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(artifacts, tmp_path / "cache")

    first = register_agent(source, store, artifacts, cache)
    second = register_agent(source, store, artifacts, cache)

    assert first is not None
    assert second is not None
    assert second.agent_id == first.agent_id
    assert second.version == 1
    assert second.changed is False


def test_register_agent_replaces_bundle_and_preserves_id(db_uri: str, tmp_path: Path) -> None:
    source = tmp_path / "managed-agent"
    _write_agent(source, "Original instructions.\n")
    store = SqlAlchemyAgentStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(artifacts, tmp_path / "cache")

    first = register_agent(source, store, artifacts, cache)
    assert first is not None
    (source / "AGENTS.md").write_text("Updated instructions.\n", encoding="utf-8")
    second = register_agent(source, store, artifacts, cache)

    assert second is not None
    assert second.agent_id == first.agent_id
    assert second.version == 2
    assert second.changed is True
    stored = store.get(first.agent_id)
    assert stored is not None
    assert cache.load(stored.id, stored.bundle_location, expand_env=True).spec.instructions == (
        "Updated instructions.\n"
    )
