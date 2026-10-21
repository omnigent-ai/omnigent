from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from types import MappingProxyType

import yaml
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPluginState,
    check_extension_package,
)
from omnigent.extensions.assets import ResolvedBundle
from omnigent.spec import load


def _manifest() -> ExtensionManifest:
    return ExtensionManifest(
        id="acme.review",
        display_name="Review",
        distribution="acme-review",
        version="1.0.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
    )


def test_extensions_list(monkeypatch) -> None:
    monkeypatch.setattr(
        "omnigent.extensions.plugin_state",
        lambda: ExtensionPluginState(manifests=(_manifest(),)),
    )

    result = CliRunner().invoke(cli, ["extensions", "list"])

    assert result.exit_code == 0
    assert "acme.review\t1.0.0\tReview" in result.output


def test_extensions_doctor_reports_resolved_bundle(monkeypatch) -> None:
    manifest = replace(
        _manifest(),
        entrypoints=ExtensionEntrypoints(browser="dist/extension.js"),
    )
    state = ExtensionPluginState(manifests=(manifest,))
    bundle = ResolvedBundle(
        extension_id=manifest.id,
        digest="a" * 64,
        assets=MappingProxyType({}),
    )
    monkeypatch.setattr("omnigent.extensions.plugin_state", lambda: state)
    monkeypatch.setattr(
        "omnigent.extensions.assets.resolve_bundle",
        lambda _manifest, **_kwargs: bundle,
    )

    result = CliRunner().invoke(cli, ["extensions", "doctor", manifest.id])

    assert result.exit_code == 0
    assert f"browser bundle: ok ({'a' * 64})" in result.output


def test_extensions_doctor_honors_development_override(monkeypatch, tmp_path) -> None:
    manifest = replace(
        _manifest(),
        entrypoints=ExtensionEntrypoints(browser="dist/extension.js"),
    )
    package_root = tmp_path / "package"
    (package_root / "dist").mkdir(parents=True)
    (package_root / "dist" / "extension.js").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(
        "omnigent.extensions.plugin_state",
        lambda: ExtensionPluginState(manifests=(manifest,)),
    )
    monkeypatch.setenv(
        "OMNIGENT_EXTENSION_DEV_BUNDLES",
        f'{{"{manifest.id}": "{package_root}"}}',
    )

    result = CliRunner().invoke(cli, ["extensions", "doctor", manifest.id])

    assert result.exit_code == 0
    assert "development override" in result.output


def test_extensions_doctor_reports_invalid_development_override(monkeypatch) -> None:
    manifest = replace(
        _manifest(),
        entrypoints=ExtensionEntrypoints(browser="dist/extension.js"),
    )
    monkeypatch.setattr(
        "omnigent.extensions.plugin_state",
        lambda: ExtensionPluginState(manifests=(manifest,)),
    )
    monkeypatch.setenv("OMNIGENT_EXTENSION_DEV_BUNDLES", "not-json")

    result = CliRunner().invoke(cli, ["extensions", "doctor", manifest.id])

    assert result.exit_code == 1
    assert "Invalid development bundle override" in result.output


def test_extensions_doctor_rejects_unknown_extension(monkeypatch) -> None:
    monkeypatch.setattr(
        "omnigent.extensions.plugin_state",
        lambda: ExtensionPluginState(manifests=()),
    )

    result = CliRunner().invoke(cli, ["extensions", "doctor", "acme.missing"])

    assert result.exit_code == 1
    assert "not installed or was rejected" in result.output


def test_extensions_doctor_surfaces_load_error_for_rejected_extension(monkeypatch) -> None:
    monkeypatch.setattr(
        "omnigent.extensions.plugin_state",
        lambda: ExtensionPluginState(
            manifests=(),
            load_errors={"acme-review:review": "manifest version does not match distribution"},
        ),
    )

    result = CliRunner().invoke(cli, ["extensions", "doctor", "acme.review"])

    assert result.exit_code == 1
    assert (
        "rejected acme-review:review: manifest version does not match distribution"
        in result.output
    )
    assert "was rejected while loading" in result.output


def test_init_creates_conforming_standalone_package(tmp_path) -> None:
    target = tmp_path / "team-extension"
    result = CliRunner().invoke(
        cli,
        [
            "extensions",
            "init",
            "acme.triage",
            "--owner",
            "reliability",
            "--path",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    package_root = target / "src/omnigent_acme_triage"
    specification = importlib.util.spec_from_file_location(
        "generated_manifest", package_root / "plugin.py"
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    check_extension_package(module.get_manifest(), package_root=package_root, project_root=target)
    assert (target / "tests/test_package.py").is_file()
    assert "extensions enable acme.triage" in (target / "README.md").read_text()


def test_init_never_overwrites_existing_directory(tmp_path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "data"
    marker.write_text("keep")
    result = CliRunner().invoke(
        cli,
        [
            "extensions",
            "init",
            "acme.triage",
            "--owner",
            "reliability",
            "--path",
            str(existing),
        ],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert list(existing.iterdir()) == [marker]
    assert marker.read_text() == "keep"


def test_init_rejects_invalid_identifier_before_writing(tmp_path) -> None:
    target = tmp_path / "extension"
    result = CliRunner().invoke(
        cli,
        [
            "extensions",
            "init",
            "../unsafe",
            "--owner",
            "reliability",
            "--path",
            str(target),
        ],
    )
    assert result.exit_code == 1
    assert not target.exists()


def test_enable_pins_version_and_permissions_then_disable_preserves_config(
    incident_state, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("omnigent.cli_extensions.plugin_state", lambda: incident_state)
    config = tmp_path / "server.yaml"
    config.write_text("port: 12345\nextensions:\n  acme.other:\n    enabled: false\n")
    result = CliRunner().invoke(
        cli,
        [
            "extensions",
            "enable",
            "acme.incident-response",
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "restart the server" in result.output
    saved = yaml.safe_load(config.read_text())
    assert saved == {
        "port": 12345,
        "extensions": {
            "acme.other": {"enabled": False},
            "acme.incident-response": {
                "enabled": True,
                "version": "==0.1.0",
                "permissions": ["events.subscribe"],
            },
        },
    }
    assert config.stat().st_mode & 0o777 == 0o600
    result = CliRunner().invoke(
        cli,
        [
            "extensions",
            "disable",
            "acme.incident-response",
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    saved = yaml.safe_load(config.read_text())
    assert saved["extensions"]["acme.incident-response"]["enabled"] is False
    assert saved["extensions"]["acme.other"] == {"enabled": False}
    assert saved["port"] == 12345


def test_enable_unknown_extension_preserves_original_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "omnigent.cli_extensions.plugin_state", lambda: ExtensionPluginState(manifests=())
    )
    config = tmp_path / "server.yaml"
    config.write_text("port: 12345\n")
    result = CliRunner().invoke(
        cli,
        [
            "extensions",
            "enable",
            "acme.unknown",
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 1
    assert config.read_text() == "port: 12345\n"


def test_disable_can_recover_from_an_uninstalled_required_extension(tmp_path) -> None:
    config = tmp_path / "server.yaml"
    config.write_text("extensions:\n  acme.unknown:\n    enabled: true\n    required: true\n")
    result = CliRunner().invoke(
        cli,
        [
            "extensions",
            "disable",
            "acme.unknown",
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(config.read_text()) == {
        "extensions": {"acme.unknown": {"enabled": False}}
    }


def test_check_and_export_validate_without_importing_server_hooks(
    incident_state, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("omnigent.cli_extensions.plugin_state", lambda: incident_state)
    monkeypatch.delitem(sys.modules, "omnigent_incident_response.hooks", raising=False)
    runner = CliRunner()
    checked = runner.invoke(cli, ["extensions", "check", "acme.incident-response"])
    assert checked.exit_code == 0, checked.output
    assert "agents: 1, policy presets: 1" in checked.output
    output = tmp_path / "agent.tar.gz"
    exported = runner.invoke(
        cli,
        [
            "extensions",
            "export",
            "acme.incident-response",
            "acme.incident-response.triage",
            "--output",
            str(output),
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert "sha256:" in exported.output
    destination = tmp_path / "unpacked"
    spec = load(output.read_bytes(), dest=destination, expand_env=False)
    assert spec.name == "incident-triage"
    assert json.loads((destination / "omnigent-extension-lock.json").read_text())[
        "extensions"
    ] == {"acme.incident-response": "0.1.0"}
    assert "omnigent_incident_response.hooks" not in sys.modules
    repeated = runner.invoke(
        cli,
        [
            "extensions",
            "export",
            "acme.incident-response",
            "acme.incident-response.triage",
            "--output",
            str(output),
        ],
    )
    assert repeated.exit_code == 1
    assert "File exists" in repeated.output
