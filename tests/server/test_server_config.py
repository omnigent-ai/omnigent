"""Tests for the shared server-config loader (:mod:`omnigent.server.server_config`).

Covers path resolution (env override → ``<data_dir>/config.yaml`` →
None), loading + fail-open behavior (missing / malformed / non-mapping
→ empty dict, never a crash), and the ``config_str_list`` coercion used
for ``admins`` / ``allowed_domains``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.server.server_config import (
    BRANDING_ASSETS_DIRNAME,
    branding_config,
    branding_logo_asset,
    config_str_list,
    load_server_config,
    resolve_config_path,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"test"
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>'


def _pin_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point <data_dir> at tmp_path and clear the explicit-path override."""
    monkeypatch.delenv("OMNIGENT_CONFIG", raising=False)
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))


# ── path resolution ───────────────────────────────────────────────


def test_resolve_config_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``OMNIGENT_CONFIG`` wins over the data-dir default."""
    p = tmp_path / "custom.yaml"
    p.write_text("{}")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(p))
    assert resolve_config_path() == p


def test_resolve_config_path_default_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Falls back to ``<data_dir>/config.yaml`` when that file exists."""
    _pin_data_dir(monkeypatch, tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("admins: [a@x.com]\n")
    assert resolve_config_path() == cfg


def test_resolve_config_path_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No env, no default file → ``None`` (pure-env back-compat)."""
    _pin_data_dir(monkeypatch, tmp_path)
    assert resolve_config_path() is None


# ── loading ───────────────────────────────────────────────────────


def test_load_server_config_parses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A well-formed config loads into a dict."""
    _pin_data_dir(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("admins:\n  - a@x.com\nallowed_domains: [x.com]\n")
    cfg = load_server_config()
    assert cfg["admins"] == ["a@x.com"]
    assert cfg["allowed_domains"] == ["x.com"]


def test_load_server_config_empty_when_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No config file → empty dict (not an error)."""
    _pin_data_dir(monkeypatch, tmp_path)
    assert load_server_config() == {}


def test_load_server_config_malformed_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Malformed YAML fails open to empty rather than crashing startup."""
    _pin_data_dir(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("admins: [unclosed\n")
    assert load_server_config() == {}


def test_load_server_config_non_mapping_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A top-level non-mapping (e.g. a list) is ignored."""
    _pin_data_dir(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("- a\n- b\n")
    assert load_server_config() == {}


# ── config_str_list ───────────────────────────────────────────────


def test_config_str_list_accepts_list() -> None:
    assert config_str_list(["a@x.com", "b@x.com"]) == ["a@x.com", "b@x.com"]


def test_config_str_list_accepts_scalar() -> None:
    """A single scalar is wrapped — a one-entry value needn't be a list."""
    assert config_str_list("a@x.com") == ["a@x.com"]


def test_config_str_list_none_is_empty() -> None:
    assert config_str_list(None) == []


def test_config_str_list_strips_and_drops_empty() -> None:
    assert config_str_list(["  a@x.com  ", "", "  "]) == ["a@x.com"]


def _write_branding_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, logo: str) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(f"branding:\n  logo:\n    main: {logo}\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    assets = tmp_path / BRANDING_ASSETS_DIRNAME
    assets.mkdir()
    return assets


@pytest.mark.parametrize(
    ("filename", "content", "media_type"),
    [("logo.png", _PNG, "image/png"), ("logo.svg", _SVG, "image/svg+xml")],
)
def test_branding_logo_accepts_valid_images_in_assets_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
    content: bytes,
    media_type: str,
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, filename)
    logo = assets / filename
    logo.write_bytes(content)

    asset = branding_logo_asset()

    assert asset is not None
    assert asset.path == logo
    assert asset.media_type == media_type
    assert branding_config()["logos"]["main"] == "/v1/branding/logo/main"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("secret.txt", b"api_key: super-secret"),
        ("secret.png", b"api_key: super-secret"),
        ("active.svg", b"<svg><script>alert(1)</script></svg>"),
    ],
)
def test_branding_logo_rejects_non_images_and_active_svg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str, content: bytes
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, filename)
    (assets / filename).write_bytes(content)

    assert branding_logo_asset() is None
    assert branding_config()["logos"]["main"] is None


def test_branding_logo_cannot_serve_config_directory_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_branding_config(monkeypatch, tmp_path, "secrets.png")
    (tmp_path / "secrets.png").write_bytes(_PNG)

    assert branding_logo_asset() is None


@pytest.mark.parametrize("logo", ["../secret.png", "/tmp/secret.png"])
def test_branding_logo_rejects_escape_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, logo: str
) -> None:
    _write_branding_config(monkeypatch, tmp_path, logo)
    assert branding_logo_asset() is None


def test_branding_logo_rejects_file_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, "logo.png")
    secret = tmp_path / "secret.png"
    secret.write_bytes(_PNG)
    (assets / "logo.png").symlink_to(secret)

    assert branding_logo_asset() is None


def test_branding_logo_rejects_symlinked_assets_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("branding:\n  logo: logo.png\n")
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    external = tmp_path / "external"
    external.mkdir()
    (external / "logo.png").write_bytes(_PNG)
    (tmp_path / BRANDING_ASSETS_DIRNAME).symlink_to(external, target_is_directory=True)

    assert branding_logo_asset() is None
