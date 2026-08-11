"""Tests for the shared server-config loader (:mod:`omnigent.server.server_config`).

Covers path resolution (env override → ``<data_dir>/config.yaml`` →
None), loading + fail-open behavior (missing / malformed / non-mapping
→ empty dict, never a crash), and the ``config_str_list`` coercion used
for ``admins`` / ``allowed_domains``.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from omnigent.server.server_config import (
    BRANDING_ASSET_MAX_BYTES,
    BRANDING_ASSET_MAX_DIMENSION,
    BRANDING_ASSETS_DIRNAME,
    branding_config,
    branding_logo_asset,
    config_str_list,
    load_server_config,
    resolve_config_path,
)


def _image_bytes(image_format: str, *, size: tuple[int, int] = (2, 2)) -> bytes:
    mode = "RGB" if image_format == "JPEG" else "RGBA"
    image = Image.new(mode, size, (25, 100, 200) if mode == "RGB" else (25, 100, 200, 255))
    output = BytesIO()
    save_kwargs: dict[str, object] = {}
    if image_format == "ICO":
        save_kwargs["sizes"] = [size]
    if image_format == "WEBP":
        save_kwargs["lossless"] = True
    image.save(output, format=image_format, **save_kwargs)
    return output.getvalue()


_VALID_RASTERS = {
    "logo.gif": (_image_bytes("GIF"), "image/gif"),
    "logo.ico": (_image_bytes("ICO", size=(16, 16)), "image/x-icon"),
    "logo.jpeg": (_image_bytes("JPEG"), "image/jpeg"),
    "logo.jpg": (_image_bytes("JPEG"), "image/jpeg"),
    "logo.png": (_image_bytes("PNG"), "image/png"),
    "logo.webp": (_image_bytes("WEBP"), "image/webp"),
}
_PNG = _VALID_RASTERS["logo.png"][0]


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
    [
        pytest.param(filename, *fixture, id=filename)
        for filename, fixture in _VALID_RASTERS.items()
    ],
)
def test_branding_logo_accepts_fully_decoded_rasters_in_assets_dir(
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
    assert asset.content == content
    assert branding_config()["logos"]["main"] == "/v1/branding/logo/main"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("secret.txt", b"api_key: super-secret"),
        ("secret.png", b"api_key: super-secret"),
        ("passive.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        ("active.svg", b"<svg><script>alert(1)</script></svg>"),
        ("event.svg", b'<svg onload="alert(1)"></svg>'),
        ("external.svg", b'<svg><image href="https://example.com/x"/></svg>'),
        ("css.svg", b"<svg><style>@import url(https://example.com/x)</style></svg>"),
    ],
)
def test_branding_logo_rejects_non_images_and_all_svg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str, content: bytes
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, filename)
    (assets / filename).write_bytes(content)

    assert branding_logo_asset() is None
    assert branding_config()["logos"]["main"] is None


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param("secret.png", b"\x89PNG\r\n\x1a\nAPI_TOKEN=top-secret", id="png"),
        pytest.param("secret.jpg", b"\xff\xd8\xffAPI_TOKEN=top-secret\xff\xd9", id="jpeg"),
        pytest.param("secret.gif", b"GIF89aAPI_TOKEN=top-secret;", id="gif"),
        pytest.param("secret.webp", b"RIFF\x14\x00\x00\x00WEBPAPI_TOKEN=top-secret", id="webp"),
        pytest.param("secret.ico", b"\x00\x00\x01\x00API_TOKEN=top-secret", id="ico"),
    ],
)
def test_branding_logo_rejects_magic_prefixed_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str, content: bytes
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, filename)
    (assets / filename).write_bytes(content)

    assert branding_logo_asset() is None


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param(
            filename,
            fixture[0][: max(1, len(fixture[0]) // 2)],
            id=filename,
        )
        for filename, fixture in _VALID_RASTERS.items()
    ],
)
def test_branding_logo_rejects_truncated_rasters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str, content: bytes
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, filename)
    (assets / filename).write_bytes(content)

    assert branding_logo_asset() is None


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param(filename, fixture[0] + b"API_TOKEN=top-secret", id=filename)
        for filename, fixture in _VALID_RASTERS.items()
    ],
)
def test_branding_logo_rejects_trailing_payloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str, content: bytes
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, filename)
    (assets / filename).write_bytes(content)

    assert branding_logo_asset() is None


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param(
            "logo.jpg",
            _VALID_RASTERS["logo.jpg"][0] + b"API_TOKEN=top-secret\xff\xd9",
            id="jpeg-fake-final-eoi",
        ),
        pytest.param(
            "logo.gif",
            _VALID_RASTERS["logo.gif"][0] + b"API_TOKEN=top-secret;",
            id="gif-fake-final-trailer",
        ),
    ],
)
def test_branding_logo_rejects_payloads_hidden_before_fake_terminators(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str, content: bytes
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, filename)
    (assets / filename).write_bytes(content)

    assert branding_logo_asset() is None


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"x" * (BRANDING_ASSET_MAX_BYTES + 1), id="oversized"),
    ],
)
def test_branding_logo_rejects_empty_and_oversized_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, "logo.png")
    (assets / "logo.png").write_bytes(content)

    assert branding_logo_asset() is None


def test_branding_logo_rejects_excessive_dimensions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets = _write_branding_config(monkeypatch, tmp_path, "logo.png")
    (assets / "logo.png").write_bytes(
        _image_bytes("PNG", size=(BRANDING_ASSET_MAX_DIMENSION + 1, 1))
    )

    assert branding_logo_asset() is None


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
