"""Small conformance entry point for extension package tests."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import TYPE_CHECKING

import tomllib
from packaging.utils import canonicalize_name
from packaging.version import Version

from omnigent.extensions.api import ExtensionManifest, ExtensionPluginState
from omnigent.extensions.assets import ExtensionAssetError, ResolvedBundle, resolve_bundle
from omnigent.extensions.registry import validate_manifest

if TYPE_CHECKING:
    from omnigent.extensions.manager import ExtensionManager


def prepare_extension(
    state: ExtensionPluginState,
    extension_id: str,
    *,
    package_roots: dict[str, Path] | None = None,
) -> ExtensionManager:
    """Check one installed pack and its dependencies without starting server code."""
    from omnigent.extensions.manager import ExtensionManager

    requested: dict[str, dict[str, object]] = {}

    def include(identifier: str) -> None:
        if identifier in requested:
            return
        manifest = state.get(identifier)
        if manifest is None:
            raise ExtensionAssetError(f"extension {identifier!r} is not installed or was rejected")
        requested[identifier] = {
            "enabled": True,
            "version": f"=={manifest.version}",
            "permissions": sorted(item.value for item in manifest.permissions),
        }
        for dependency in manifest.dependencies:
            include(dependency.id)

    include(extension_id)
    manager = ExtensionManager(state, requested, package_roots=package_roots)
    if extension_id not in manager.active:
        error = manager.errors.get(extension_id, "extension is unavailable")
        raise ExtensionAssetError(error)
    return manager


def _validate_project_metadata(manifest: ExtensionManifest, project_root: Path) -> None:
    project_file = project_root / "pyproject.toml"
    try:
        project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
        distribution = str(project["name"])
        version = str(project["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ExtensionAssetError(f"could not read extension project metadata: {exc}") from exc
    if canonicalize_name(distribution) != canonicalize_name(manifest.distribution):
        raise ExtensionAssetError("manifest distribution does not match pyproject.toml")
    if Version(version) != Version(manifest.version):
        raise ExtensionAssetError("manifest version does not match pyproject.toml")


def _validate_installed_metadata(manifest: ExtensionManifest, package: str) -> None:
    try:
        distribution = importlib.metadata.distribution(manifest.distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ExtensionAssetError(
            f"installed distribution {manifest.distribution!r} is unavailable"
        ) from exc
    if Version(distribution.version) != Version(manifest.version):
        raise ExtensionAssetError("manifest version does not match installed distribution")
    top_level = distribution.read_text("top_level.txt") or ""
    if package not in {line.strip() for line in top_level.splitlines()}:
        raise ExtensionAssetError("asset package is not owned by the manifest distribution")


def check_extension_package(
    manifest: ExtensionManifest,
    *,
    package: str | None = None,
    package_root: Path | None = None,
    project_root: Path | None = None,
    dependencies: ExtensionPluginState | None = None,
) -> ResolvedBundle | None:
    """Validate a manifest, distribution identity, and optional browser bundle."""
    validate_manifest(manifest)
    if package_root is not None:
        if project_root is None:
            raise ExtensionAssetError("project_root is required with package_root")
        _validate_project_metadata(manifest, project_root)
    elif package is not None:
        _validate_installed_metadata(manifest, package)
    resource_package = package or (package_root.name if package_root is not None else None)
    manifests = (manifest,)
    packages: dict[str, str] = {}
    if dependencies is not None:
        manifests += tuple(item for item in dependencies.manifests if item.id != manifest.id)
        packages.update(dependencies.asset_packages)
    if resource_package is not None:
        packages[manifest.id] = resource_package
    prepare_extension(
        ExtensionPluginState(manifests=manifests, asset_packages=packages),
        manifest.id,
        package_roots={manifest.id: package_root} if package_root is not None else None,
    )
    if manifest.entrypoints.browser is None:
        return None
    return resolve_bundle(manifest, package=package, root_override=package_root)
