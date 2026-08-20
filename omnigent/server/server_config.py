"""Server-side YAML config for the non-CLI entrypoints.

The ``omnigent server`` CLI already takes ``-c/--config`` and reads a
YAML file (see ``omnigent/cli.py``). The hosted entrypoints —
``deploy/docker/entrypoint.py`` and ``deploy/databricks/src/app.py`` —
don't go through that CLI; they build the app directly from env vars.
This module gives those entrypoints the *same* config-file experience a
laptop gets from ``-c``, so a deployment can keep most of its settings
(admins, allowed domains, policy modules, artifact location, host/port,
database URI) in one file on the persistent volume instead of a pile of
env vars.

**Secrets stay in the environment, not this file.** ``DATABASE_URL``,
the session cookie secret, and the OIDC client secret are injected by
compose / ``bootstrap.sh`` / the platform — keeping them out of a
mounted YAML is deliberate (12-factor; the file is operator-editable
and often world-readable on the box). This config holds non-secret
*settings* only.

Resolution order for the config path:

1. ``OMNIGENT_CONFIG`` env var, if set (explicit path).
2. ``<data_dir>/config.yaml`` if it exists — ``<data_dir>`` is the same
   directory the admin list / credentials use (``/data`` in the Docker
   stack, ``~/.omnigent`` on a laptop; see
   :func:`omnigent.server.admin_list.resolve_data_dir`).
3. Otherwise ``None`` — no file, pure env config (back-compat: existing
   env-only deploys keep working unchanged).
"""

from __future__ import annotations

import logging
import os
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from PIL import Image, ImageSequence, UnidentifiedImageError

from omnigent.server.admin_list import resolve_data_dir

logger = logging.getLogger(__name__)


def resolve_config_path() -> Path | None:
    """Resolve the server config file path, or ``None`` if there is none.

    :returns: ``OMNIGENT_CONFIG`` if set; else ``<data_dir>/config.yaml``
        when that file exists; else ``None``.
    """
    explicit = os.environ.get("OMNIGENT_CONFIG", "").strip()
    if explicit:
        return Path(explicit)
    default = resolve_data_dir() / "config.yaml"
    return default if default.is_file() else None


def load_server_config() -> dict[str, Any]:
    """Load the resolved server config file into a dict.

    :returns: The parsed mapping, or an empty dict when no config file is
        resolved. A present-but-unreadable / malformed file logs a
        warning and returns ``{}`` rather than crashing startup — the
        entrypoint then falls back to env + defaults.
    """
    path = resolve_config_path()
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("server config %s unreadable/invalid: %s — falling back to env", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("server config %s is not a mapping — ignoring", path)
        return {}
    logger.info("loaded server config from %s", path)
    return data


def config_str_list(value: Any) -> list[str]:
    """Coerce a config value into a list of non-empty strings.

    Accepts a YAML list (``["a", "b"]``) or a single scalar (``"a"``);
    anything else yields an empty list. Used for ``admins`` /
    ``allowed_domains`` so a one-entry value doesn't have to be a list.

    :param value: The raw config value, e.g. ``["alice@example.com"]``.
    :returns: A list of stripped, non-empty strings.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


def _config_positive_int(key: str, default: int) -> int:
    """Read a positive-int setting from the server config, else *default*.

    A missing, non-numeric, or non-positive value falls back to *default*
    rather than crashing — the config file is operator-editable and a typo
    should degrade to the safe built-in limit, not take the server down.

    :param key: Top-level config key, e.g. ``"copy_max_files"``.
    :param default: Value used when the key is absent or invalid.
    :returns: The configured positive int, or *default*.
    """
    raw = load_server_config().get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("server config %s=%r is not an int — using default %d", key, raw, default)
        return default
    if value <= 0:
        logger.warning(
            "server config %s=%d is not positive — using default %d", key, value, default
        )
        return default
    return value


def copy_file_count_limit() -> int:
    """Max number of files a single copy-at-spawn request may copy.

    Config key ``copy_max_files``; defaults to
    :data:`omnigent.runtime.content_resolver.MAX_COPY_FILES`.
    """
    from omnigent.runtime.content_resolver import MAX_COPY_FILES

    return _config_positive_int("copy_max_files", MAX_COPY_FILES)


def copy_total_bytes_limit() -> int:
    """Max summed byte size a single copy-at-spawn request may copy.

    Config key ``copy_max_total_bytes``; defaults to
    :data:`omnigent.runtime.content_resolver.MAX_COPY_TOTAL_BYTES`.
    """
    from omnigent.runtime.content_resolver import MAX_COPY_TOTAL_BYTES

    return _config_positive_int("copy_max_total_bytes", MAX_COPY_TOTAL_BYTES)


def _branding_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the ``branding:`` mapping, or ``{}`` when absent/not a map."""
    section = config.get("branding")
    return section if isinstance(section, dict) else {}


def _branding_str(section: Mapping[str, Any], key: str) -> str | None:
    """Return a stripped non-empty branding string for *key*, else None."""
    raw = section.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _branding_heading(section: Mapping[str, Any]) -> str | None:
    """Return the heading, preserving an explicit ``""``; None only when unset."""
    if section.get("heading") is None:
        return None
    return str(section["heading"]).strip()


def _branding_powered_by(section: Mapping[str, Any]) -> bool:
    """Whether to show the "Powered by Omnigent" attribution (default True)."""
    value = section.get("powered_by")
    return True if value is None else bool(value)


LOGO_VARIANTS: tuple[str, ...] = ("main", "loading", "favicon")
BRANDING_ASSETS_DIRNAME = "branding-assets"
BRANDING_ASSET_MAX_BYTES = 5 * 1024 * 1024
BRANDING_ASSET_MAX_DIMENSION = 4096
BRANDING_ASSET_MAX_FRAMES = 128
BRANDING_ASSET_MAX_PIXELS = 16 * 1024 * 1024
BRANDING_ASSET_MAX_DECODED_PIXELS = 64 * 1024 * 1024


@dataclass(frozen=True)
class BrandingAsset:
    path: Path
    media_type: str
    content: bytes


@dataclass(frozen=True)
class BrandingSnapshot:
    """Immutable branding metadata and validated assets for one app instance."""

    app_name: str | None
    heading: str | None
    logo_assets: Mapping[str, BrandingAsset]
    powered_by: bool

    def config(self) -> dict[str, Any]:
        """Return the public branding block surfaced by ``GET /v1/info``."""
        return {
            "app_name": self.app_name,
            "heading": self.heading,
            "logos": {
                variant: (f"/v1/branding/logo/{variant}" if variant in self.logo_assets else None)
                for variant in LOGO_VARIANTS
            },
            "powered_by": self.powered_by,
        }

    def logo_asset(self, variant: str = "main") -> BrandingAsset | None:
        """Return the already validated asset for *variant*, if configured."""
        return self.logo_assets.get(variant)


_RASTER_FORMATS = {
    ".gif": ("GIF", "image/gif"),
    ".ico": ("ICO", "image/x-icon"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".jpg": ("JPEG", "image/jpeg"),
    ".png": ("PNG", "image/png"),
    ".webp": ("WEBP", "image/webp"),
}


def _jpeg_ends_at_eof(content: bytes) -> bool:
    if not content.startswith(b"\xff\xd8"):
        return False
    offset = 2
    in_scan = False
    while offset < len(content):
        if in_scan:
            marker_start = content.find(b"\xff", offset)
            if marker_start < 0 or marker_start + 1 >= len(content):
                return False
            offset = marker_start + 1
            while offset < len(content) and content[offset] == 0xFF:
                offset += 1
            if offset >= len(content):
                return False
            marker = content[offset]
            offset += 1
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                continue
            in_scan = False
        else:
            if content[offset] != 0xFF:
                return False
            while offset < len(content) and content[offset] == 0xFF:
                offset += 1
            if offset >= len(content):
                return False
            marker = content[offset]
            offset += 1

        if marker == 0xD9:
            return offset == len(content)
        if marker in {0x00, 0x01, 0xD8} or 0xD0 <= marker <= 0xD7:
            return False
        if offset + 2 > len(content):
            return False
        segment_length = int.from_bytes(content[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(content):
            return False
        offset += segment_length
        in_scan = marker == 0xDA
    return False


def _gif_ends_at_eof(content: bytes) -> bool:
    if len(content) < 13 or content[:6] not in {b"GIF87a", b"GIF89a"}:
        return False
    offset = 13
    packed = content[10]
    if packed & 0x80:
        offset += 3 * (1 << ((packed & 0x07) + 1))

    def skip_sub_blocks(start: int) -> int | None:
        while start < len(content):
            block_size = content[start]
            start += 1
            if block_size == 0:
                return start
            start += block_size
            if start > len(content):
                return None
        return None

    while offset < len(content):
        block_type = content[offset]
        offset += 1
        if block_type == 0x3B:
            return offset == len(content)
        if block_type == 0x21:
            if offset >= len(content):
                return False
            next_offset = skip_sub_blocks(offset + 1)
        elif block_type == 0x2C:
            if offset + 9 > len(content):
                return False
            descriptor_packed = content[offset + 8]
            offset += 9
            if descriptor_packed & 0x80:
                offset += 3 * (1 << ((descriptor_packed & 0x07) + 1))
            if offset >= len(content):
                return False
            next_offset = skip_sub_blocks(offset + 1)
        else:
            return False
        if next_offset is None:
            return False
        offset = next_offset
    return False


def _container_ends_at_eof(image_format: str, content: bytes) -> bool:
    """Reject bytes a decoder would silently ignore after the image container."""
    if image_format == "PNG":
        offset = 8
        while offset + 12 <= len(content):
            length = int.from_bytes(content[offset : offset + 4], "big")
            chunk_type = content[offset + 4 : offset + 8]
            chunk_end = offset + 12 + length
            if chunk_end > len(content):
                return False
            if chunk_type == b"IEND":
                return length == 0 and chunk_end == len(content)
            offset = chunk_end
        return False
    if image_format == "JPEG":
        return _jpeg_ends_at_eof(content)
    if image_format == "GIF":
        return _gif_ends_at_eof(content)
    if image_format == "WEBP":
        return (
            len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
            and int.from_bytes(content[4:8], "little") + 8 == len(content)
        )
    return False


def _valid_frame_shape(width: int, height: int) -> bool:
    return (
        0 < width <= BRANDING_ASSET_MAX_DIMENSION
        and 0 < height <= BRANDING_ASSET_MAX_DIMENSION
        and width * height <= BRANDING_ASSET_MAX_PIXELS
    )


@dataclass(frozen=True)
class _IcoEntry:
    width: int
    height: int
    payload_start: int
    payload_end: int


def _valid_ico(content: bytes) -> bool:
    if len(content) < 6 or content[:4] != b"\x00\x00\x01\x00":
        return False
    image_count = int.from_bytes(content[4:6], "little")
    directory_end = 6 + image_count * 16
    if image_count <= 0 or image_count > BRANDING_ASSET_MAX_FRAMES or directory_end > len(content):
        return False

    entries: list[_IcoEntry] = []
    for index in range(image_count):
        entry = 6 + index * 16
        width = content[entry] or 256
        height = content[entry + 1] or 256
        color_count = content[entry + 2]
        reserved = content[entry + 3]
        planes = int.from_bytes(content[entry + 4 : entry + 6], "little")
        bit_count = int.from_bytes(content[entry + 6 : entry + 8], "little")
        payload_size = int.from_bytes(content[entry + 8 : entry + 12], "little")
        payload_start = int.from_bytes(content[entry + 12 : entry + 16], "little")
        payload_end = payload_start + payload_size
        if (
            color_count != 0
            or reserved != 0
            or planes not in {0, 1}
            or bit_count not in {0, 32}
            or not _valid_frame_shape(width, height)
            or payload_size <= 0
            or payload_start < directory_end
            or payload_end > len(content)
        ):
            return False
        entries.append(_IcoEntry(width, height, payload_start, payload_end))

    cursor = directory_end
    for entry in sorted(entries, key=lambda item: item.payload_start):
        if entry.payload_start != cursor:
            return False
        cursor = entry.payload_end
    if cursor != len(content):
        return False

    decoded_pixels = 0
    for entry in entries:
        payload = content[entry.payload_start : entry.payload_end]
        if not payload.startswith(b"\x89PNG\r\n\x1a\n") or not _container_ends_at_eof(
            "PNG", payload
        ):
            return False
        with Image.open(BytesIO(payload)) as image:
            if (
                image.format != "PNG"
                or image.size != (entry.width, entry.height)
                or int(getattr(image, "n_frames", 1)) != 1
            ):
                return False
            image.verify()
        with Image.open(BytesIO(payload)) as image:
            image.load()
        decoded_pixels += entry.width * entry.height
        if decoded_pixels > BRANDING_ASSET_MAX_DECODED_PIXELS:
            return False
    return True


def _raster_media_type(path: Path, content: bytes) -> str | None:
    expected = _RASTER_FORMATS.get(path.suffix.lower())
    if expected is None:
        return None
    expected_format, media_type = expected
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            if expected_format == "ICO":
                return media_type if _valid_ico(content) else None
            with Image.open(BytesIO(content)) as image:
                if image.format != expected_format or not _valid_frame_shape(*image.size):
                    return None
                frame_count = int(getattr(image, "n_frames", 1))
                if (
                    frame_count <= 0
                    or frame_count > BRANDING_ASSET_MAX_FRAMES
                    or image.width * image.height * frame_count > BRANDING_ASSET_MAX_DECODED_PIXELS
                ):
                    return None
                image.verify()
            with Image.open(BytesIO(content)) as image:
                decoded_pixels = 0
                for decoded_frames, frame in enumerate(ImageSequence.Iterator(image), start=1):
                    if not _valid_frame_shape(*frame.size):
                        return None
                    decoded_pixels += frame.width * frame.height
                    if (
                        decoded_frames > BRANDING_ASSET_MAX_FRAMES
                        or decoded_pixels > BRANDING_ASSET_MAX_DECODED_PIXELS
                    ):
                        return None
                    frame.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ):
        return None
    return media_type if _container_ends_at_eof(expected_format, content) else None


def _validated_image(path: Path) -> BrandingAsset | None:
    try:
        size = path.stat().st_size
        if size <= 0 or size > BRANDING_ASSET_MAX_BYTES:
            return None
        content = path.read_bytes()
    except OSError:
        return None
    if len(content) != size:
        return None
    media_type = _raster_media_type(path, content)
    if media_type is None:
        return None
    return BrandingAsset(path=path, media_type=media_type, content=content)


def _branding_assets_dir() -> Path:
    """Return the dedicated branding-assets directory for the resolved config."""
    config_path = resolve_config_path()
    config_dir = config_path.parent if config_path is not None else resolve_data_dir()
    return config_dir / BRANDING_ASSETS_DIRNAME


def _resolve_branding_asset(
    name: str,
    *,
    assets_dir: Path,
    validated_assets: dict[Path, BrandingAsset | None],
) -> BrandingAsset | None:
    """Resolve a validated image below the dedicated branding-assets directory."""
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        logger.warning("branding.logo %r escapes %s — ignoring", name, assets_dir)
        return None
    if assets_dir.is_symlink():
        logger.warning("branding assets directory %s is a symlink — ignoring", assets_dir)
        return None
    try:
        assets_root = assets_dir.resolve(strict=True)
        path = assets_root.joinpath(relative)
        for parent in (path, *path.parents):
            if parent == assets_root.parent:
                break
            if parent.is_symlink():
                logger.warning("branding.logo %r traverses a symlink — ignoring", name)
                return None
        resolved = path.resolve(strict=True)
        resolved.relative_to(assets_root)
    except (FileNotFoundError, OSError, ValueError):
        logger.warning(
            "branding.logo %r is outside or missing from %s — ignoring", name, assets_dir
        )
        return None
    if not resolved.is_file():
        return None
    if resolved in validated_assets:
        return validated_assets[resolved]
    asset = validated_assets[resolved] = _validated_image(resolved)
    if asset is None:
        logger.warning("branding.logo %r is not a supported image — ignoring", name)
        return None
    return asset


def _branding_logo_names(section: Mapping[str, Any]) -> dict[str, str]:
    """Map logo variant to filename from ``branding.logo`` (a bare string sets ``main``)."""
    raw = section.get("logo")
    if isinstance(raw, str):
        name = raw.strip()
        return {"main": name} if name else {}
    if isinstance(raw, dict):
        names: dict[str, str] = {}
        for variant in LOGO_VARIANTS:
            value = raw.get(variant)
            if isinstance(value, str) and value.strip():
                names[variant] = value.strip()
        return names
    return {}


def load_branding_snapshot(config: Mapping[str, Any] | None = None) -> BrandingSnapshot:
    """Load and validate branding once for a single application instance."""
    loaded_config = load_server_config() if config is None else config
    section = _branding_section(loaded_config)
    assets_dir = _branding_assets_dir()
    validated_assets: dict[Path, BrandingAsset | None] = {}
    logo_assets: dict[str, BrandingAsset] = {}
    for variant, name in _branding_logo_names(section).items():
        asset = _resolve_branding_asset(
            name,
            assets_dir=assets_dir,
            validated_assets=validated_assets,
        )
        if asset is not None:
            logo_assets[variant] = asset
    return BrandingSnapshot(
        app_name=_branding_str(section, "app_name"),
        heading=_branding_heading(section),
        logo_assets=MappingProxyType(logo_assets),
        powered_by=_branding_powered_by(section),
    )


def branding_logo_asset(variant: str = "main") -> BrandingAsset | None:
    """Resolve the configured, validated image asset for *variant*."""
    return load_branding_snapshot().logo_asset(variant)


def branding_config() -> dict[str, Any]:
    """Branding block surfaced by ``GET /v1/info`` for the web UI."""
    return load_branding_snapshot().config()
