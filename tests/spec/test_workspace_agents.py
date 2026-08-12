"""Tests for :mod:`omnigent.spec.workspace_agents`.

The scanner summarizes ``.omnigent/agent-configs/`` entries (flat YAMLs
and bundle directories) without executing anything; the packager builds
deterministic tar.gz bytes whose digest anchors the client-side consent
grant. Both must never raise on broken repo content.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
import yaml

from omnigent.spec import load
from omnigent.spec.workspace_agents import (
    package_workspace_agent,
    scan_workspace_agent_configs,
)


def _configs_dir(workspace: Path) -> Path:
    d = workspace / ".omnigent" / "agent-configs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_single_file(workspace: Path, filename: str, payload: dict) -> Path:
    path = _configs_dir(workspace) / filename
    path.write_text(yaml.safe_dump(payload))
    return path


def _write_bundle(workspace: Path, dirname: str, config: dict) -> Path:
    bundle = _configs_dir(workspace) / dirname
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "config.yaml").write_text(yaml.safe_dump(config))
    return bundle


# ── scanner ──────────────────────────────────────────────


def test_scans_flat_yaml_and_bundle_dirs(tmp_path: Path) -> None:
    _write_single_file(
        tmp_path,
        "helper.yaml",
        {"name": "Repo Helper", "prompt": "hi", "executor": {"type": "omnigent"}},
    )
    bundle = _write_bundle(
        tmp_path,
        "reviewer",
        {
            "spec_version": 1,
            "name": "Repo Reviewer",
            "description": "Reviews PRs.",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
            "tools": {"agents": ["claude_code", "codex"]},
        },
    )
    (bundle / "tools" / "python").mkdir(parents=True)
    (bundle / "tools" / "python" / "tool.py").write_text("x = 1\n")
    (bundle / "tools" / "mcp").mkdir(parents=True)
    (bundle / "tools" / "mcp" / "server.yaml").write_text("name: s\n")

    entries = scan_workspace_agent_configs(tmp_path)

    assert [(e.slug, e.kind) for e in entries] == [
        ("repo-helper", "file"),
        ("repo-reviewer", "bundle"),
    ]
    helper, reviewer = entries
    assert helper.path == ".omnigent/agent-configs/helper.yaml"
    assert helper.harness == "omnigent"
    assert reviewer.path == ".omnigent/agent-configs/reviewer"
    assert reviewer.description == "Reviews PRs."
    assert reviewer.harness == "claude-sdk"
    assert reviewer.sub_agents == ("claude_code", "codex")
    assert reviewer.has_local_tools is True
    assert reviewer.has_mcp_servers is True


def test_missing_dir_and_malformed_entries(tmp_path: Path) -> None:
    assert scan_workspace_agent_configs(tmp_path) == []
    _configs_dir(tmp_path)
    (_configs_dir(tmp_path) / "broken.yaml").write_text("acp: [unclosed")
    (_configs_dir(tmp_path) / "notes.txt").write_text("not yaml")
    (_configs_dir(tmp_path) / "empty-dir").mkdir()
    assert scan_workspace_agent_configs(tmp_path) == []


def test_name_falls_back_to_stem_and_slugs_dedupe(tmp_path: Path) -> None:
    _write_single_file(tmp_path, "helper.yaml", {"prompt": "hi"})
    _write_bundle(tmp_path, "zz-helper", {"spec_version": 1, "name": "helper!"})
    entries = scan_workspace_agent_configs(tmp_path)
    assert [e.slug for e in entries] == ["helper", "helper-2"]
    assert entries[0].name == "helper"


# ── packager ─────────────────────────────────────────────


def test_package_single_file_is_deterministic_and_loadable(tmp_path: Path) -> None:
    _write_single_file(
        tmp_path,
        "helper.yaml",
        {"name": "helper", "prompt": "You are terse.", "executor": {"harness": "claude-sdk"}},
    )
    rel = ".omnigent/agent-configs/helper.yaml"
    first = package_workspace_agent(tmp_path, rel)
    second = package_workspace_agent(tmp_path, rel)
    assert first == second

    spec = load(first, dest=tmp_path / "extract")
    assert spec.name == "helper"


def test_package_bundle_includes_sub_agents(tmp_path: Path) -> None:
    bundle = _write_bundle(
        tmp_path,
        "reviewer",
        {
            "spec_version": 1,
            "name": "reviewer",
            "instructions": "AGENTS.md",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
            "tools": {"agents": ["worker"]},
        },
    )
    (bundle / "AGENTS.md").write_text("Review things.\n")
    worker = bundle / "agents" / "worker"
    worker.mkdir(parents=True)
    (worker / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "worker",
                "instructions": "AGENTS.md",
                "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
            }
        )
    )
    (worker / "AGENTS.md").write_text("Work.\n")

    bundle_bytes = package_workspace_agent(tmp_path, ".omnigent/agent-configs/reviewer")
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        names = sorted(tar.getnames())
    assert names == [
        "AGENTS.md",
        "agents/worker/AGENTS.md",
        "agents/worker/config.yaml",
        "config.yaml",
    ]

    spec = load(bundle_bytes, dest=tmp_path / "extract")
    assert spec.name == "reviewer"
    assert [sub.name for sub in spec.sub_agents] == ["worker"]


def test_package_rejects_escape_and_unknown_shapes(tmp_path: Path) -> None:
    (tmp_path / "outside.yaml").write_text("name: x\nprompt: hi\n")
    with pytest.raises(ValueError, match="escapes the workspace"):
        package_workspace_agent(tmp_path / "sub", "../outside.yaml")
    _configs_dir(tmp_path)
    (_configs_dir(tmp_path) / "plain").mkdir()
    with pytest.raises(ValueError, match="not an agent YAML or bundle"):
        package_workspace_agent(tmp_path, ".omnigent/agent-configs/plain")


def test_package_enforces_size_ceiling(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, "big", {"spec_version": 1, "name": "big"})
    (bundle / "blob.bin").write_bytes(b"x" * 2048)
    with pytest.raises(ValueError, match="exceeds"):
        package_workspace_agent(tmp_path, ".omnigent/agent-configs/big", max_bytes=1024)
