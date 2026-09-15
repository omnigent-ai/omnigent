"""Small conformance entry point for extension package tests."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import tomllib
from packaging.utils import canonicalize_name
from packaging.version import Version

from omnigent.extensions.api import ExtensionManifest
from omnigent.extensions.assets import ExtensionAssetError, ResolvedBundle, resolve_bundle
from omnigent.extensions.registry import validate_manifest


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
) -> ResolvedBundle | None:
    """Validate a manifest, distribution identity, and optional browser bundle."""
    validate_manifest(manifest)
    if package_root is not None:
        if project_root is None:
            raise ExtensionAssetError("project_root is required with package_root")
        _validate_project_metadata(manifest, project_root)
    elif package is not None:
        _validate_installed_metadata(manifest, package)
    if manifest.entrypoints.browser is None:
        return None
    return resolve_bundle(manifest, package=package, root_override=package_root)
