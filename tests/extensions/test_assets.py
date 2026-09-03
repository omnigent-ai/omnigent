from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import importlib.util
import sys
from pathlib import Path

import pytest

from omnigent.extensions import EXTENSION_API_VERSION, ExtensionEntrypoints, ExtensionManifest
from omnigent.extensions.api import ExtensionPluginState
from omnigent.extensions.assets import (
    ASSET_SCRIPT,
    ASSET_STYLES,
    ExtensionAssetError,
    build_asset_index,
    parse_dev_bundle_overrides,
    resolve_bundle,
)


def _manifest(
    extension_id: str = "acme.assets",
    *,
    browser: str = "dist/extension.js",
    css: str | None = "dist/extension.css",
) -> ExtensionManifest:
    return ExtensionManifest(
        id=extension_id,
        display_name="Assets",
        distribution="acme-assets",
        version="1.0.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(browser=browser, browser_css=css),
    )


def _package(
    tmp_path: Path,
    name: str,
    *,
    js: bytes = b"console.log('ok')",
    css: bytes = b"body{}",
) -> Path:
    root = tmp_path / name
    (root / "dist").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "dist" / "extension.js").write_bytes(js)
    (root / "dist" / "extension.css").write_bytes(css)
    return root


def test_resolves_and_hashes_complete_bundle(tmp_path: Path) -> None:
    root = _package(tmp_path, "acme_ext")

    bundle = resolve_bundle(_manifest(), root_override=root)

    assert bundle.assets[ASSET_SCRIPT].content == b"console.log('ok')"
    assert bundle.assets[ASSET_STYLES].content == b"body{}"
    assert bundle.assets[ASSET_SCRIPT].sha256 == hashlib.sha256(b"console.log('ok')").hexdigest()
    assert len(bundle.digest) == 64
    assert bundle.url(ASSET_SCRIPT).endswith(f"/{bundle.digest}/extension.js")


def test_css_change_changes_whole_bundle_digest(tmp_path: Path) -> None:
    root = _package(tmp_path, "acme_ext")
    first = resolve_bundle(_manifest(), root_override=root)
    (root / "dist" / "extension.css").write_bytes(b"body{color:red}")

    second = resolve_bundle(_manifest(), root_override=root)

    assert first.digest != second.digest


def test_missing_and_oversized_assets_fail(tmp_path: Path) -> None:
    root = _package(tmp_path, "acme_ext", js=b"12345")
    (root / "dist" / "extension.css").unlink()

    with pytest.raises(ExtensionAssetError, match="missing"):
        resolve_bundle(_manifest(), root_override=root)
    with pytest.raises(ExtensionAssetError, match="exceeds"):
        resolve_bundle(_manifest(css=None), root_override=root, max_bytes=4)


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = _package(tmp_path, "acme_ext")
    outside = tmp_path / "outside.js"
    outside.write_text("secret", encoding="utf-8")
    (root / "dist" / "extension.js").unlink()
    (root / "dist" / "extension.js").symlink_to(outside)

    with pytest.raises(ExtensionAssetError, match="escapes"):
        resolve_bundle(_manifest(css=None), root_override=root)


@pytest.mark.parametrize("path", ["../../etc/passwd.js", "dist\\..\\escape.js", "/tmp/x.js"])
def test_revalidates_paths_at_io_boundary(tmp_path: Path, path: str) -> None:
    root = _package(tmp_path, "acme_ext")

    with pytest.raises(ExtensionAssetError):
        resolve_bundle(_manifest(browser=path, css=None), root_override=root)


def test_rejects_directory_target(tmp_path: Path) -> None:
    root = _package(tmp_path, "acme_ext")
    (root / "dist" / "extension.js").unlink()
    (root / "dist" / "extension.js").mkdir()

    with pytest.raises(ExtensionAssetError, match="regular file"):
        resolve_bundle(_manifest(css=None), root_override=root)


def test_rejects_namespace_package_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = "extension_namespace_fixture"
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        (root / package).mkdir(parents=True)
        monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    sys.modules.pop(package, None)

    with pytest.raises(ExtensionAssetError, match="regular package"):
        resolve_bundle(_manifest(css=None), package=package)


def test_parses_only_existing_absolute_development_roots(tmp_path: Path) -> None:
    assert parse_dev_bundle_overrides(f'{{"acme.assets": "{tmp_path}"}}') == {
        "acme.assets": tmp_path
    }
    with pytest.raises(ExtensionAssetError, match="valid JSON"):
        parse_dev_bundle_overrides("not-json")
    with pytest.raises(ExtensionAssetError, match="existing absolute directory"):
        parse_dev_bundle_overrides('{"acme.assets": "relative"}')


def test_rejects_unsupported_traversable_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    class Spec:
        origin = "/virtual/package.py"

    monkeypatch.setattr(importlib.util, "find_spec", lambda _package: Spec())
    monkeypatch.setattr(importlib.resources, "files", lambda _package: object())

    with pytest.raises(ExtensionAssetError, match="unsupported package loader"):
        resolve_bundle(_manifest(css=None), package="virtual_package")


def test_asset_index_isolates_broken_extension(tmp_path: Path) -> None:
    healthy_root = _package(tmp_path, "healthy")
    broken_root = _package(tmp_path, "broken")
    (broken_root / "dist" / "extension.js").unlink()
    healthy = _manifest("acme.healthy", css=None)
    broken = _manifest("acme.broken", css=None)
    state = ExtensionPluginState(manifests=(broken, healthy))

    bundles, errors = build_asset_index(
        state,
        overrides={healthy.id: healthy_root, broken.id: broken_root},
    )

    assert set(bundles) == {"acme.healthy"}
    assert set(errors) == {"acme.broken"}
