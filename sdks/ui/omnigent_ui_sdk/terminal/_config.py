"""Persistent user configuration for terminal UI frontends.

The UI SDK keeps this intentionally small: TUI preferences are persisted
under the ``tui:`` table of the shared Omnigent YAML config file
(``$OMNIGENT_CONFIG_HOME/config.yaml`` when configured, otherwise
``$HOME/.omnigent/config.yaml``). Today that means the persisted light/dark
theme selection.

The same file is also written by the ``omnigent`` CLI (top-level keys
such as ``default_agent`` and ``profile``). Reads and writes here are
scoped to ``tui:`` so sibling CLI keys round-trip unchanged.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from ._theme import TerminalThemeName, get_theme

_CONFIG_FILENAME = "config.yaml"
_STATE_DIRNAME = ".omnigent"
_DATA_DIR_ENV_VAR = "OMNIGENT_DATA_DIR"
_CONFIG_HOME_ENV_VAR = "OMNIGENT_CONFIG_HOME"
_TUI_KEY = "tui"


class UserConfigError(ValueError):
    """Raised when user config cannot be parsed or persisted."""


@dataclass(frozen=True)
class UserConfig:
    """User preferences persisted for terminal UI sessions.

    :param theme: Optional persisted theme name, e.g. ``"dark"``.
        ``None`` means "use the built-in default theme."
    """

    theme: TerminalThemeName | None = None


DEFAULT_USER_CONFIG = UserConfig()


def state_dir() -> pathlib.Path:
    """Return the shared Omnigent per-user state directory.

    Honors ``OMNIGENT_DATA_DIR`` so worktrees and dev pods can isolate runtime
    state without replacing ``HOME``. Callers that only compute a path cause
    no filesystem side effects; writers create the directory when saving.

    :returns: ``$OMNIGENT_DATA_DIR`` when set, else
        ``Path.home() / ".omnigent"``.
    """

    value = os.environ.get(_DATA_DIR_ENV_VAR)
    return pathlib.Path(value).expanduser() if value else pathlib.Path.home() / _STATE_DIRNAME


def user_config_path(root: str | pathlib.Path | None = None) -> pathlib.Path:
    """Return the path to the YAML user config file.

    ``OMNIGENT_CONFIG_HOME`` isolates configuration independently from runtime
    state. An explicit *root* takes precedence over the environment.

    :param root: Optional explicit config directory.
    :returns: ``$OMNIGENT_CONFIG_HOME/config.yaml`` when set, else
        ``Path.home() / ".omnigent" / "config.yaml"``.
    """

    if root is not None:
        base = pathlib.Path(root).expanduser()
    elif value := os.environ.get(_CONFIG_HOME_ENV_VAR):
        base = pathlib.Path(value).expanduser()
    else:
        base = pathlib.Path.home() / _STATE_DIRNAME
    return base / _CONFIG_FILENAME


def load_user_config(path: str | pathlib.Path | None = None) -> UserConfig:
    """Load TUI user config from the shared YAML file.

    Missing files (and files with no ``tui:`` table) resolve to
    :data:`DEFAULT_USER_CONFIG`. Malformed YAML and invalid values fail
    loudly with :class:`UserConfigError`.

    :param path: Optional config path override, e.g.
        ``Path("/tmp/fake-home/config.yaml")``.
    :returns: The parsed :class:`UserConfig`.
    :raises UserConfigError: When the file cannot be read or contains
        invalid YAML / values.
    """

    config_path = user_config_path() if path is None else pathlib.Path(path).expanduser()
    raw = _read_raw(config_path)
    if raw is None:
        return DEFAULT_USER_CONFIG
    return _parse_user_config(raw, config_path=config_path)


def save_user_config(
    config: UserConfig,
    path: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """Persist *config* into the shared YAML file's ``tui:`` table.

    Sibling top-level keys written by the ``omnigent`` CLI (e.g.
    ``default_agent``, ``profile``) are preserved. When *config* equals
    :data:`DEFAULT_USER_CONFIG` the ``tui:`` table is removed entirely
    rather than written as an empty mapping.

    The write is atomic (temp file + rename) so concurrent readers never
    see a truncated config.

    :param config: User config values to persist.
    :param path: Optional config path override, e.g.
        ``Path("/tmp/fake-home/config.yaml")``.
    :returns: The path that was written.
    :raises UserConfigError: When the config cannot be written.
    """

    config_path = user_config_path() if path is None else pathlib.Path(path).expanduser()
    merged = dict(_read_raw(config_path) or {})
    if config.theme is None:
        merged.pop(_TUI_KEY, None)
    else:
        merged[_TUI_KEY] = {"theme": config.theme}

    temp_path: pathlib.Path | None = None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f"{config_path.name}.tmp.",
            delete=False,
        ) as handle:
            handle.write(_dump_user_config(merged))
            temp_path = pathlib.Path(handle.name)
        temp_path.replace(config_path)
        temp_path = None
    except OSError as exc:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
        raise UserConfigError(f"Failed to write TUI user config at {config_path}: {exc}") from exc
    return config_path


def update_user_config(
    path: str | pathlib.Path | None = None,
    *,
    theme: str | None = None,
) -> UserConfig:
    """Load, update, save, and return the user config.

    Currently only ``theme`` is persisted. The value is normalized using the
    same validation as the TUI theme registry.

    :param path: Optional config path override, e.g.
        ``Path("/tmp/fake-home/config.yaml")``.
    :param theme: Optional theme override, e.g. ``"dark"``. ``None`` keeps the
        existing persisted value unchanged.
    :returns: The updated user config.
    :raises UserConfigError: If *theme* is supplied but is not a known theme.
    """

    current = load_user_config(path)
    updated = current
    normalized = _normalize_theme(theme)
    if theme is not None:
        if normalized is None:
            raise UserConfigError(f"unknown theme {theme!r}; expected dark or light")
        updated = replace(updated, theme=normalized)
    save_user_config(updated, path)
    return updated


def _read_raw(config_path: pathlib.Path) -> Mapping[str, Any] | None:
    """Read and YAML-decode the shared config file.

    Returns ``None`` when the file is missing or empty. Raises
    :class:`UserConfigError` for read / decode failures.

    :param config_path: Resolved path to read.
    :returns: Decoded mapping, or ``None`` when there is nothing to parse.
    :raises UserConfigError: On I/O or YAML decode errors, or when the
        decoded document is not a mapping.
    """

    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UserConfigError(f"Failed to read TUI user config at {config_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise UserConfigError(
            f"TUI user config at {config_path} is not valid UTF-8 text: {exc}"
        ) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UserConfigError(f"Failed to parse TUI user config at {config_path}: {exc}") from exc

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise UserConfigError(f"TUI user config at {config_path} must decode to a YAML mapping.")
    return raw


def _parse_user_config(
    raw: Mapping[str, Any],
    *,
    config_path: pathlib.Path,
) -> UserConfig:
    """Validate and parse the decoded YAML mapping.

    :param raw: Decoded YAML mapping, e.g. ``{"tui": {"theme": "dark"}}``.
    :param config_path: Source path used in validation errors.
    :returns: Parsed :class:`UserConfig`.
    :raises UserConfigError: When the mapping contains invalid values.
    """

    tui = raw.get(_TUI_KEY)
    if tui is None:
        return DEFAULT_USER_CONFIG
    if not isinstance(tui, Mapping):
        raise UserConfigError(f"TUI user config at {config_path} must use a tui: mapping.")
    raw_theme = tui.get("theme")
    if raw_theme is None:
        return DEFAULT_USER_CONFIG
    if not isinstance(raw_theme, str):
        raise UserConfigError(
            f"tui.theme in {config_path} must be a string; got {type(raw_theme).__name__}."
        )
    theme = _normalize_theme(raw_theme)
    if theme is None:
        raise UserConfigError(
            f"tui.theme in {config_path} must be one of dark, light; got {raw_theme!r}."
        )
    return UserConfig(theme=theme)


def _normalize_theme(value: object) -> TerminalThemeName | None:
    """Normalize an arbitrary value into a known theme name.

    :param value: Candidate theme value, e.g. ``" LIGHT "``.
    :returns: Normalized theme name, e.g. ``"light"``, or ``None`` when
        *value* is not a known theme.
    """

    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    try:
        return get_theme(name).name
    except ValueError:
        return None


def _dump_user_config(data: Mapping[str, Any]) -> str:
    """Render the merged config mapping as YAML text.

    Empty mappings emit a single header comment so the file is never
    visually empty (matches the prior TOML behavior). Non-empty mappings
    emit the same header followed by sorted, block-style YAML.

    :param data: Merged config mapping to render.
    :returns: YAML text ready to write.
    """

    header = "# Omnigent user configuration\n"
    if not data:
        return header
    body = yaml.safe_dump(dict(data), default_flow_style=False, sort_keys=True)
    return header + body
