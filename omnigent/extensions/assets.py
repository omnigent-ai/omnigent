"""Resolve immutable browser bundles from installed extension packages.

Bundle digests provide cache keys and torn catalog/bundle detection. They are
not an integrity boundary: catalog and assets come from the same trusted server.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import importlib.util
import logging
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from omnigent.extensions.api import ExtensionManifest, ExtensionPluginState

ASSET_SCRIPT = "extension.js"
ASSET_STYLES = "extension.css"
ASSET_MEDIA_TYPES = {
    ASSET_SCRIPT: "text/javascript; charset=utf-8",
    ASSET_STYLES: "text/css; charset=utf-8",
}
MAX_ASSET_BYTES = 4 * 1024 * 1024

_logger = logging.getLogger(__name__)
_SAFE_PART = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


class ExtensionAssetError(ValueError):
    """An extension browser bundle could not be resolved safely."""


@dataclass(frozen=True)
class ResolvedAsset:
    """One in-memory asset from a validated extension bundle."""

    name: str
    media_type: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ResolvedBundle:
    """A complete browser bundle and its content-addressed URL key."""

    extension_id: str
    digest: str
    assets: Mapping[str, ResolvedAsset]

    def url(self, name: str) -> str:
        """Return the server-relative URL for one logical asset."""
        if name not in self.assets:
            raise KeyError(name)
        return f"/v1/extensions/{self.extension_id}/assets/{self.digest}/{name}"


def _safe_parts(declared: str) -> tuple[str, ...]:
    """Validate an already-declared resource path again at the I/O boundary."""
    if not isinstance(declared, str) or not declared.isprintable():
        raise ExtensionAssetError("extension asset path is invalid")
    path = PurePosixPath(declared)
    if (
        path.is_absolute()
        or declared.startswith("./")
        or declared != path.as_posix()
        or len(path.parts) > 16
        or any(not _SAFE_PART.fullmatch(part) for part in path.parts)
    ):
        raise ExtensionAssetError(f"unsafe extension asset path {declared!r}")
    return path.parts


def _read_filesystem_asset(root: Path, parts: tuple[str, ...], max_bytes: int) -> bytes:
    resolved_root = root.resolve(strict=True)
    target = root.joinpath(*parts)
    resolved_target = target.resolve(strict=True)
    if not resolved_target.is_relative_to(resolved_root):
        raise ExtensionAssetError("extension asset escapes its package root")
    file_stat = resolved_target.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise ExtensionAssetError("extension asset is not a regular file")
    with resolved_target.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ExtensionAssetError(f"extension asset exceeds {max_bytes} bytes")
    return content


def _read_traversable_asset(root: Traversable, parts: tuple[str, ...], max_bytes: int) -> bytes:
    target = root.joinpath(*parts)
    if not target.is_file():
        raise ExtensionAssetError("extension asset is not a regular file")
    with target.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ExtensionAssetError(f"extension asset exceeds {max_bytes} bytes")
    return content


def _resource_root(package: str) -> Traversable:
    try:
        spec = importlib.util.find_spec(package)
        if spec is None or spec.origin is None:
            raise ExtensionAssetError("extension asset root must be a regular package")
        root = importlib.resources.files(package)
    except (ImportError, ModuleNotFoundError) as exc:
        raise ExtensionAssetError(f"extension asset package {package!r} is unavailable") from exc
    return root


def _read_asset(root: Traversable, declared: str, max_bytes: int) -> bytes:
    parts = _safe_parts(declared)
    if isinstance(root, Path):
        return _read_filesystem_asset(root, parts, max_bytes)
    return _read_traversable_asset(root, parts, max_bytes)


def resolve_bundle(
    manifest: ExtensionManifest,
    *,
    package: str | None = None,
    root_override: Path | None = None,
    max_bytes: int = MAX_ASSET_BYTES,
) -> ResolvedBundle:
    """Read, bound, and hash one extension's declared browser bundle."""
    if manifest.entrypoints.browser is None:
        raise ExtensionAssetError(f"extension {manifest.id!r} has no browser bundle")
    if package is None and root_override is None:
        raise ExtensionAssetError(f"extension {manifest.id!r} has no verified asset package")
    if package is not None and root_override is not None:
        raise ExtensionAssetError("asset package and development override are mutually exclusive")
    if manifest.entrypoints.browser != "dist/extension.js":
        raise ExtensionAssetError("browser entrypoint must be dist/extension.js")
    if manifest.entrypoints.browser_css not in {None, "dist/extension.css"}:
        raise ExtensionAssetError("browser CSS entrypoint must be dist/extension.css")
    if root_override is not None:
        root: Traversable = root_override
    else:
        assert package is not None
        root = _resource_root(package)

    declarations = {ASSET_SCRIPT: manifest.entrypoints.browser}
    if manifest.entrypoints.browser_css is not None:
        declarations[ASSET_STYLES] = manifest.entrypoints.browser_css

    assets: dict[str, ResolvedAsset] = {}
    for name, declared in declarations.items():
        try:
            content = _read_asset(root, declared, max_bytes)
        except FileNotFoundError as exc:
            raise ExtensionAssetError(f"declared extension asset {declared!r} is missing") from exc
        digest = hashlib.sha256(content).hexdigest()
        assets[name] = ResolvedAsset(
            name=name,
            media_type=ASSET_MEDIA_TYPES[name],
            content=content,
            sha256=digest,
        )

    digest_input = b"omnigent-extension-bundle/1\n" + b"".join(
        f"{name} {asset.sha256}\n".encode() for name, asset in sorted(assets.items())
    )
    bundle_digest = hashlib.sha256(digest_input).hexdigest()
    return ResolvedBundle(
        extension_id=manifest.id,
        digest=bundle_digest,
        assets=MappingProxyType(assets),
    )


def build_asset_index(
    state: ExtensionPluginState,
    *,
    overrides: Mapping[str, Path] | None = None,
    max_bytes: int = MAX_ASSET_BYTES,
) -> tuple[dict[str, ResolvedBundle], dict[str, str]]:
    """Resolve all declared bundles, isolating failures by extension."""
    bundles: dict[str, ResolvedBundle] = {}
    errors: dict[str, str] = {}
    override_paths = overrides or {}
    for manifest in state.manifests:
        if manifest.entrypoints.browser is None:
            continue
        try:
            override = override_paths.get(manifest.id)
            bundles[manifest.id] = resolve_bundle(
                manifest,
                package=None if override is not None else state.asset_package(manifest.id),
                root_override=override,
                max_bytes=max_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            errors[manifest.id] = str(exc)
            _logger.warning(
                "could not resolve browser bundle for extension %s (%s)",
                manifest.id,
                exc,
                exc_info=True,
            )
    return dict(sorted(bundles.items())), dict(sorted(errors.items()))
