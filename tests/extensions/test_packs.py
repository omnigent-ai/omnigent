"""Portable agent composition, safe resources, and reproducible snapshots."""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from dataclasses import replace

import pytest

from omnigent.extensions import check_extension_package
from omnigent.extensions.assets import ExtensionAssetError
from omnigent.extensions.conformance import prepare_extension
from omnigent.extensions.packs import LOCK_FILE
from omnigent.spec import load
from tests.extensions.conftest import EXAMPLE_ROOT, PACKAGE_ROOT


def test_reference_pack_is_portable_and_records_resolved_versions(
    incident_state, tmp_path
) -> None:
    manager = prepare_extension(incident_state, "acme.incident-response")
    agent = manager.agents["acme.incident-response.triage"]
    destination = tmp_path / "agent"
    spec = load(agent.content, dest=destination, expand_env=False, enforce_handler_allowlist=True)
    assert spec.name == "incident-triage"
    assert {item.name for item in spec.skills} == {"triage"}
    assert {item.name for item in spec.local_tools} == {"incident_summary"}
    assert spec.guardrails is not None
    assert {item.name for item in spec.guardrails.policies} == {
        "acme.incident-response.approve-os-tools"
    }
    assert json.loads((destination / LOCK_FILE).read_text()) == {
        "api_version": 1,
        "extensions": {"acme.incident-response": "0.1.0"},
    }
    assert (destination / "tools/mcp/observability.json.example").is_file()
    assert "omnigent_incident_response" not in (destination / "config.yaml").read_text()


def test_reference_source_package_conforms(incident_manifest) -> None:
    assert (
        check_extension_package(
            incident_manifest, package_root=PACKAGE_ROOT, project_root=EXAMPLE_ROOT
        )
        is None
    )


def test_pack_digest_ignores_file_metadata(incident_state, tmp_path) -> None:
    original = prepare_extension(incident_state, "acme.incident-response")
    copied = tmp_path / "package"
    shutil.copytree(PACKAGE_ROOT, copied)
    (copied / "agents/triage/config.yaml").chmod(0o600)
    rebuilt = prepare_extension(
        incident_state, "acme.incident-response", package_roots={"acme.incident-response": copied}
    )
    assert rebuilt.agents == original.agents


@pytest.mark.parametrize("resource", ["missing-agent", "symlink", "duplicate-policy", "lock"])
def test_invalid_resource_rejects_entire_pack(
    incident_state, incident_manifest, tmp_path, resource
) -> None:
    copied = tmp_path / "package"
    shutil.copytree(PACKAGE_ROOT, copied)
    agent = copied / "agents/triage"
    if resource == "missing-agent":
        (agent / "config.yaml").unlink()
    elif resource == "symlink":
        (agent / "outside").symlink_to(tmp_path, target_is_directory=True)
    elif resource == "duplicate-policy":
        with (agent / "config.yaml").open("a") as stream:
            stream.write(
                "guardrails:\n  policies:\n    acme.incident-response.approve-os-tools:\n"
                "      type: function\n"
                "      function: omnigent.policies.builtins.safety.ask_on_os_tools\n"
            )
    else:
        (agent / LOCK_FILE).write_text("{}")
    from omnigent.extensions.manager import ExtensionManager

    manager = ExtensionManager(
        incident_state,
        {incident_manifest.id: {"enabled": True, "permissions": ["events.subscribe"]}},
        package_roots={incident_manifest.id: copied},
    )
    assert not manager.active
    assert not manager.agents
    assert not manager.policies
    assert incident_manifest.id in manager.errors


@pytest.mark.parametrize("limit", ["MAX_PACK_BYTES", "MAX_PACK_FILES", "MAX_PACK_DEPTH"])
def test_resource_limits_are_enforced(incident_state, monkeypatch, limit) -> None:
    monkeypatch.setattr(f"omnigent.extensions.packs.{limit}", 1)
    with pytest.raises(ExtensionAssetError):
        prepare_extension(incident_state, "acme.incident-response")


def test_policy_preset_cannot_claim_core_names(incident_state, tmp_path) -> None:
    copied = tmp_path / "package"
    shutil.copytree(PACKAGE_ROOT, copied)
    (copied / "policies/approvals.yaml").write_text(
        "core-policy:\n  type: function\n"
        "  function: omnigent.policies.builtins.safety.ask_on_os_tools\n"
    )
    with pytest.raises(ExtensionAssetError, match="namespaced"):
        prepare_extension(
            incident_state,
            "acme.incident-response",
            package_roots={"acme.incident-response": copied},
        )


def test_environment_placeholders_are_preserved(incident_state, tmp_path, monkeypatch) -> None:
    copied = tmp_path / "package"
    shutil.copytree(PACKAGE_ROOT, copied)
    example = copied / "agents/triage/tools/mcp/observability.json.example"
    example.rename(example.with_suffix(""))
    monkeypatch.setenv("OBSERVABILITY_MCP_URL", "https://private.example.test/mcp")
    monkeypatch.setenv("OBSERVABILITY_MCP_TOKEN", "secret-token-never-export")
    manager = prepare_extension(
        incident_state, "acme.incident-response", package_roots={"acme.incident-response": copied}
    )
    content = manager.agents["acme.incident-response.triage"].content
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        bundled = archive.extractfile("./tools/mcp/observability.json").read()
    assert b"${OBSERVABILITY_MCP_TOKEN}" in bundled
    assert b"secret-token-never-export" not in bundled


def test_agent_can_only_use_its_own_or_declared_dependency_policies(
    incident_state, incident_manifest
) -> None:
    agent = replace(incident_manifest.agents[0], policies=("other.extension.approvals",))
    state = replace(incident_state, manifests=(replace(incident_manifest, agents=(agent,)),))
    with pytest.raises(ExtensionAssetError, match=r"policy preset .* is unavailable"):
        prepare_extension(state, incident_manifest.id)


def test_pack_preserves_hidden_harness_configuration(incident_state, tmp_path) -> None:
    copied = tmp_path / "package"
    shutil.copytree(PACKAGE_ROOT, copied)
    configuration = copied / "agents/triage/.claude/settings.json"
    configuration.parent.mkdir()
    configuration.write_text('{"permissions": {"defaultMode": "default"}}')
    manager = prepare_extension(
        incident_state, "acme.incident-response", package_roots={"acme.incident-response": copied}
    )
    load(
        manager.agents["acme.incident-response.triage"].content,
        dest=tmp_path / "agent",
        expand_env=False,
    )
    assert (tmp_path / "agent/.claude/settings.json").read_text() == configuration.read_text()


def test_unregistered_policy_is_rejected_even_without_an_agent(
    incident_state, incident_manifest, tmp_path
) -> None:
    copied = tmp_path / "package"
    shutil.copytree(PACKAGE_ROOT, copied)
    (copied / "policies/approvals.yaml").write_text(
        "acme.incident-response.unsafe:\n  type: function\n  function: subprocess.Popen\n"
    )
    state = replace(incident_state, manifests=(replace(incident_manifest, agents=()),))
    with pytest.raises(ExtensionAssetError, match="registered handlers"):
        prepare_extension(
            state, "acme.incident-response", package_roots={"acme.incident-response": copied}
        )
