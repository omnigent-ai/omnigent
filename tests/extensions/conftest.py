"""Shared reference pack used by resource, activation, and CLI scenarios."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from omnigent.extensions import ExtensionManifest, ExtensionPluginState
from omnigent.version import VERSION

EXAMPLE_ROOT = Path(__file__).parents[2] / "examples/extensions/incident-response"
PACKAGE_ROOT = EXAMPLE_ROOT / "src/omnigent_incident_response"


@pytest.fixture
def incident_manifest(monkeypatch: pytest.MonkeyPatch) -> ExtensionManifest:
    monkeypatch.setattr("omnigent.extensions.registry.VERSION", VERSION)
    monkeypatch.syspath_prepend(str(EXAMPLE_ROOT / "src"))
    return importlib.import_module("omnigent_incident_response.plugin").get_manifest()


@pytest.fixture
def incident_state(incident_manifest: ExtensionManifest) -> ExtensionPluginState:
    return ExtensionPluginState(
        manifests=(incident_manifest,),
        asset_packages={incident_manifest.id: "omnigent_incident_response"},
    )
