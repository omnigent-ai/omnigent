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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

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


def _branding_section() -> dict[str, Any]:
    """Return the ``branding:`` mapping, or ``{}`` when absent/not a map."""
    section = load_server_config().get("branding")
    return section if isinstance(section, dict) else {}


def _branding_str(key: str) -> str | None:
    """Return a stripped non-empty branding string for *key*, else None."""
    raw = _branding_section().get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _branding_heading() -> str | None:
    """Return the heading, preserving an explicit ``""``; None only when unset."""
    section = _branding_section()
    if section.get("heading") is None:
        return None
    return str(section["heading"]).strip()


def _branding_powered_by() -> bool:
    """Whether to show the "Powered by Omnigent" attribution (default True)."""
    value = _branding_section().get("powered_by")
    return True if value is None else bool(value)


LOGO_VARIANTS: tuple[str, ...] = ("main", "loading", "favicon")
BRANDING_ASSETS_DIRNAME = "branding-assets"
BRANDING_ASSET_MAX_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class BrandingAsset:
    path: Path
    media_type: str


_RASTER_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _raster_media_type(path: Path, header: bytes) -> str | None:
    media_type = _RASTER_MEDIA_TYPES.get(path.suffix.lower())
    if media_type == "image/png" and header.startswith(b"\x89PNG\r\n\x1a\n"):
        return media_type
    if media_type == "image/jpeg" and header.startswith(b"\xff\xd8\xff"):
        return media_type
    if media_type == "image/gif" and header.startswith((b"GIF87a", b"GIF89a")):
        return media_type
    if media_type == "image/webp" and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return media_type
    if media_type == "image/x-icon" and header.startswith(b"\x00\x00\x01\x00"):
        return media_type
    return None


def _svg_media_type(path: Path, content: bytes) -> str | None:
    if path.suffix.lower() != ".svg":
        return None
    lowered = content.lower()
    if any(
        marker in lowered
        for marker in (b"<!doctype", b"<!entity", b"<script", b"<foreignobject", b"javascript:")
    ):
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    return "image/svg+xml" if root.tag.rsplit("}", 1)[-1] == "svg" else None


def _validated_image_media_type(path: Path) -> str | None:
    try:
        size = path.stat().st_size
        if size <= 0 or size > BRANDING_ASSET_MAX_BYTES:
            return None
        content = path.read_bytes()
    except OSError:
        return None
    return _svg_media_type(path, content) or _raster_media_type(path, content[:16])


def _resolve_branding_asset(name: str) -> BrandingAsset | None:
    """Resolve a validated image below the dedicated branding-assets directory."""
    config_path = resolve_config_path()
    config_dir = config_path.parent if config_path is not None else resolve_data_dir()
    assets_dir = config_dir / BRANDING_ASSETS_DIRNAME
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
    media_type = _validated_image_media_type(resolved)
    if media_type is None:
        logger.warning("branding.logo %r is not a supported image — ignoring", name)
        return None
    return BrandingAsset(path=resolved, media_type=media_type)


def _branding_logo_names() -> dict[str, str]:
    """Map logo variant to filename from ``branding.logo`` (a bare string sets ``main``)."""
    raw = _branding_section().get("logo")
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


def branding_logo_asset(variant: str = "main") -> BrandingAsset | None:
    """Resolve the configured, validated image asset for *variant*."""
    name = _branding_logo_names().get(variant)
    if name is None:
        return None
    return _resolve_branding_asset(name)


def branding_config() -> dict[str, Any]:
    """Branding block surfaced by ``GET /v1/info`` for the web UI."""
    return {
        "app_name": _branding_str("app_name"),
        "heading": _branding_heading(),
        "logos": {
            variant: (f"/v1/branding/logo/{variant}" if branding_logo_asset(variant) else None)
            for variant in LOGO_VARIANTS
        },
        "powered_by": _branding_powered_by(),
    }
