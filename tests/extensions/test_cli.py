from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPluginState,
)
from omnigent.extensions.assets import ResolvedBundle


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
