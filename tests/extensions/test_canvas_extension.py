from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from omnigent.extensions import ExtensionManifest, ExtensionPermission, check_extension_package
from omnigent.extensions.assets import ASSET_SCRIPT, ASSET_STYLES

_REPO_ROOT = Path(__file__).parents[2]
_EXTENSION_ROOT = _REPO_ROOT / "extensions" / "canvas"
_PACKAGE_ROOT = _EXTENSION_ROOT / "src" / "omnigent_canvas"


def _load_manifest() -> ExtensionManifest:
    spec = importlib.util.spec_from_file_location(
        "omnigent_canvas.plugin",
        _PACKAGE_ROOT / "plugin.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    return module.get_manifest()


def test_canvas_package_is_conformant_and_self_contained() -> None:
    manifest = _load_manifest()
    bundle = check_extension_package(
        manifest,
        package_root=_PACKAGE_ROOT,
        project_root=_EXTENSION_ROOT,
    )

    assert bundle is not None
    assert manifest.id == "omnigent.canvas"
    assert manifest.permissions == frozenset(
        {
            ExtensionPermission.NAVIGATION,
            ExtensionPermission.PROJECTS_READ,
            ExtensionPermission.PROJECTS_WRITE,
            ExtensionPermission.SESSIONS_READ,
            ExtensionPermission.STORAGE_USER,
        }
    )
    assert manifest.pages[0].route == "canvas"
    assert b"omnigent-extension" in bundle.assets[ASSET_SCRIPT].content
    assert b"react-flow" in bundle.assets[ASSET_STYLES].content
