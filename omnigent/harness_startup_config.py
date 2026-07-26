"""Per-harness startup command/args resolution from config.

Lets users override the executable (``command``) and base launch args
(``args``) for each harness in ``config.yaml`` via a polymorphic
``harness:`` key — a scalar (legacy default) or a mapping with
``default`` plus per-harness overrides. See
``~/.pi/plans/omnigent/harness-startup-command-overrides.md``.

This is a leaf resolver module: it lazy-imports
:func:`omnigent.harness_aliases.canonicalize_harness` so it can be used
from :mod:`omnigent.config` (and the CLI) without pulling heavy
entry-point discovery at config-load time.

Precedence (first non-empty wins):

``command`` —
  1. explicit CLI flag (``--command``)
  2. ambient env var (``OMNIGENT_<NAME>_PATH``)
  3. config ``harness.<canonical>.command``
  4. built-in default

``args`` —
  1. CLI pass-through args (always present, may be empty), appended
     *after* the config base
  2. config ``harness.<canonical>.args``
  3. ``[]``

Validation is warn+skip: an unknown harness id or a structurally
malformed entry warns and is ignored, so a bad config never crashes
``config list`` / ``doctor`` / every command's config load.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

# The release in which the legacy ``HARNESS_<NAME>_PATH`` read is removed.
# Deprecated in v0.6.0; two versions of back-compat, then removal.
_LEGACY_PATH_REMOVAL_VERSION = "v0.8.0"

# Legacy ``HARNESS_*_PATH`` env vars and their canonical ``OMNIGENT_<NAME>_PATH``
# replacement. Keep in sync with the ``_LEGACY_ENV_*`` constants in the inner
# harness modules. Remove this mapping (and the legacy reads) in v0.8.0.
_LEGACY_PATH_VARS: dict[str, str] = {
    "HARNESS_CODEX_PATH": "OMNIGENT_CODEX_PATH",
    "HARNESS_PI_PATH": "OMNIGENT_PI_PATH",
    "HARNESS_KIMI_PATH": "OMNIGENT_KIMI_PATH",
    "HARNESS_GOOSE_PATH": "OMNIGENT_GOOSE_PATH",
    "HARNESS_QWEN_PATH": "OMNIGENT_QWEN_PATH",
    "HARNESS_HERMES_PATH": "OMNIGENT_HERMES_PATH",
}

# Legacy ``HARNESS_*_PATH`` vars we have already warned about in this process,
# so a long-lived runner doesn't spam the deprecation once per session.
_LEGACY_PATH_WARNED: set[str] = set()

# Keys read from a per-harness override entry in the ``harness:`` mapping.
_OVERRIDE_KEY_COMMAND = "command"
_OVERRIDE_KEY_ARGS = "args"


def _canonicalize(harness: str) -> str:
    """Return the canonical harness id for *harness* (lazy import).

    Falls back to *harness* unchanged when the alias helper can't
    resolve it, so callers can still surface their own validation.
    """
    from omnigent.harness_aliases import canonicalize_harness

    return canonicalize_harness(harness) or harness


# Harness canonical ids whose binary base name differs from the id with
# ``-native`` stripped. The env var keys off the *binary* the harness spawns,
# not the harness id, so ``claude-sdk`` (which runs the ``claude`` CLI) shares
# ``OMNIGENT_CLAUDE_PATH`` with ``claude-native``. Add entries here only when a
# harness id doesn't match its underlying command name.
_HARNESS_BINARY_BASE: dict[str, str] = {
    "claude-sdk": "claude",
}


def _harness_path_env_var(canonical: str) -> str:
    """Build the ``OMNIGENT_<NAME>_PATH`` env-var name for *canonical*.

    The name keys off the underlying *binary* the harness spawns, not the
    harness id: ``-native`` is stripped (``pi`` and ``pi-native`` both →
    ``OMNIGENT_PI_PATH``), and ``_HARNESS_BINARY_BASE`` remaps ids whose binary
    name differs (``claude-sdk`` → ``claude`` → ``OMNIGENT_CLAUDE_PATH``).
    """
    base = _HARNESS_BINARY_BASE.get(canonical) or canonical.removesuffix("-native")
    return f"OMNIGENT_{base.upper().replace('-', '_')}_PATH"


def resolve_harness_path(canonical: str) -> str | None:
    """Resolve a harness binary-path override from env, warning on legacy use.

    Precedence: the canonical ``OMNIGENT_<base>_PATH`` env var, then the
    deprecated ``HARNESS_<base>_PATH`` (emitting a one-time-per-process
    deprecation warning naming the replacement and removal version), then
    ``None`` so the caller falls back to ``PATH``. *base* is *canonical* with
    the ``-native`` suffix stripped, so a harness's headless and native forms
    share one env var.

    Use this from the inner harness wraps (runner-side) to locate the vendor
    CLI binary. The CLI side uses :func:`resolve_harness_command` instead,
    which adds the ``--command`` flag and config layers on top of this env read.

    :param canonical: A harness id (e.g. ``"codex"`` or ``"pi-native"``).
    :returns: The override path/name, or ``None`` when neither env var is set.
    """
    canonical_env = _harness_path_env_var(canonical)
    value = os.environ.get(canonical_env, "").strip()
    if value:
        return value
    base = _HARNESS_BINARY_BASE.get(canonical) or canonical.removesuffix("-native")
    legacy_env = f"HARNESS_{base.upper().replace('-', '_')}_PATH"
    # Only honor the legacy fallback for the 6 harnesses that historically
    # documented a ``HARNESS_*_PATH`` var. Other harnesses (cursor, kiro,
    # opencode, antigravity, …) never had one — honoring a speculative
    # ``HARNESS_CURSOR_PATH`` would invent a new knob under a deprecated name.
    if legacy_env not in _LEGACY_PATH_VARS:
        return None
    legacy = os.environ.get(legacy_env, "").strip()
    if legacy:
        _warn_legacy_path(legacy_env, canonical_env)
        return legacy
    return None


def _warn_legacy_path(legacy_env: str, canonical_env: str) -> None:
    """Emit a one-time-per-process deprecation warning for *legacy_env*."""
    if legacy_env in _LEGACY_PATH_WARNED:
        return
    _LEGACY_PATH_WARNED.add(legacy_env)
    _logger.warning(
        "%s is deprecated; set %s instead. %s support will be removed in %s.",
        legacy_env,
        canonical_env,
        legacy_env,
        _LEGACY_PATH_REMOVAL_VERSION,
    )


def legacy_harness_path_env_vars_set() -> list[tuple[str, str]]:
    """Return ``(legacy_var, canonical_replacement)`` for each deprecated
    ``HARNESS_*_PATH`` env var currently set in the environment.

    Used by the CLI entrypoint to surface a terminal-visible deprecation
    notice at startup (before any command runs), so a user with a legacy var
    in their shell/systemd/CI sees the replacement regardless of which harness
    they launch or whether the run is local or remote. One line per set var.
    """
    return [
        (legacy, canonical)
        for legacy, canonical in _LEGACY_PATH_VARS.items()
        if os.environ.get(legacy, "").strip()
    ]


def resolve_harness_config(
    cfg: dict[str, Any],  # type: ignore[explicit-any]
) -> tuple[str | None, dict[str, dict[str, Any]]]:  # type: ignore[explicit-any]
    """Read the ``harness:`` key from effective config.

    Accepts both legacy forms:

    - Scalar (``harness: claude-sdk``) → ``(str, {})``. Fully functional;
      the scalar is the default and there are no per-harness overrides.
    - Mapping (``harness: {default: …, <id>: {command, args}}``) →
      ``(default, overrides)``. Per-harness sub-keys are canonicalized;
      unknown ids and malformed entries are warned + skipped (never
      raise), so a bad config can't break ``config list`` / ``doctor``.

    :param cfg: Effective config dict (global + local merged). Reads
        only the ``harness`` key.
    :returns: ``(default, overrides)`` where ``default`` is the default
        harness id (or ``None`` when absent) and ``overrides`` maps
        canonical harness id → ``{command: str, args: list[str]}`` (each
        field optional, only present when the user set it).
    """
    raw = cfg.get("harness")
    if raw is None:
        return None, {}
    if isinstance(raw, str):
        return raw, {}
    if not isinstance(raw, dict):
        from omnigent.inner import ui

        ui.warn(
            f"config `harness:` is a {type(raw).__name__}, expected a string "
            "or mapping — ignoring it."
        )
        return None, {}
    default: str | None = None
    overrides: dict[str, dict[str, Any]] = {}  # type: ignore[explicit-any]
    for key, value in raw.items():
        if key == "default":
            if isinstance(value, str):
                default = value
            elif value is not None:
                from omnigent.inner import ui

                ui.warn(
                    f"config `harness.default` must be a string, got "
                    f"{type(value).__name__} — ignoring it."
                )
            continue
        # Per-harness override entry. Canonicalize the id so aliases
        # (``claude`` → ``claude-sdk``) and reversed spellings resolve to
        # one override slot.
        canonical = _canonicalize(key)
        parsed = _parse_override_entry(key, value)
        if parsed:
            # Merge into an existing slot so ``claude`` and ``claude-sdk``
            # don't clobber each other; later entries win per-field. An entry
            # whose fields all failed validation yields an empty dict and is
            # skipped so the overrides map stays clean.
            overrides.setdefault(canonical, {}).update(parsed)
    return default, overrides


def _parse_override_entry(
    key: str,
    value: Any,  # type: ignore[explicit-any]
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """Validate one per-harness override entry; warn+skip on malformed.

    :param key: The raw harness id as written in config (for messages).
    :param value: The entry value — expected ``{command: str, args: list}``.
    :returns: A validated ``{command?, args?}`` dict, or ``None`` when the
        entry is structurally invalid (already warned).
    """
    from omnigent.inner import ui

    if value is None:
        return {}
    if not isinstance(value, dict):
        ui.warn(
            f"config `harness.{key}` must be a mapping, got {type(value).__name__} — ignoring it."
        )
        return None
    parsed: dict[str, Any] = {}  # type: ignore[explicit-any]
    command = value.get(_OVERRIDE_KEY_COMMAND)
    if command is not None:
        if isinstance(command, str) and command.strip():
            parsed[_OVERRIDE_KEY_COMMAND] = command.strip()
        else:
            ui.warn(f"config `harness.{key}.command` must be a non-empty string — ignoring it.")
    args = value.get(_OVERRIDE_KEY_ARGS)
    if args is not None:
        if isinstance(args, list) and all(isinstance(a, str) for a in args):
            parsed[_OVERRIDE_KEY_ARGS] = list(args)
        else:
            ui.warn(f"config `harness.{key}.args` must be a list of strings — ignoring it.")
    return parsed


def resolve_harness_command(
    harness: str,
    *,
    default: str,
    explicit: str | None = None,
    cfg: dict[str, Any] | None = None,  # type: ignore[explicit-any]
) -> str:
    """Resolve the executable to launch for *harness*.

    Precedence (first non-empty wins):

    1. *explicit* — the per-invocation CLI ``--command`` flag (most
       specific; only the native CLI commands set this).
    2. ambient env var ``OMNIGENT_<NAME>_PATH``.
    3. config ``harness.<canonical>.command`` (when *cfg* is provided).
    4. *default* — the harness's built-in executable name.

    :param harness: A harness id (canonical or alias), e.g.
        ``"claude-native"`` or ``"codex"``.
    :param default: Built-in fallback executable, e.g. ``"claude"``.
    :param explicit: The ``--command`` flag value, or ``None``.
    :param cfg: Effective config dict (for the config-layer lookup), or
        ``None`` to skip it (e.g. when the caller already extracted
        overrides).
    :returns: The resolved command string (never empty — *default* is
        the floor).
    """
    if explicit and explicit.strip():
        return explicit.strip()
    canonical = _canonicalize(harness)
    # Check both the canonical OMNIGENT_* and the deprecated HARNESS_* env var
    # (via resolve_harness_path, which warns on legacy use) so that env always
    # wins over config per the shared precedence — a legacy HARNESS_* must not
    # be shadowed by a config ``harness.<id>.command``.
    env_value = resolve_harness_path(canonical)
    if env_value:
        return env_value
    if cfg is not None:
        _, overrides = resolve_harness_config(cfg)
        entry = overrides.get(canonical)
        if entry is not None:
            command = entry.get(_OVERRIDE_KEY_COMMAND)
            if isinstance(command, str) and command.strip():
                return command.strip()
    return default


def resolve_harness_args(
    harness: str,
    cli_args: tuple[str, ...],
    *,
    cfg: dict[str, Any] | None = None,  # type: ignore[explicit-any]
) -> list[str]:
    """Resolve the base launch args for *harness*.

    Config ``harness.<canonical>.args`` form the base; the CLI
    pass-through *cli_args* append *after* so a per-invocation flag
    wins for last-wins CLIs. When *cfg* is ``None`` (or no config args
    are set), the result is just ``list(cli_args)``.

    :param harness: A harness id (canonical or alias).
    :param cli_args: Explicit CLI pass-through args (always present,
        may be empty), e.g. ``("--dangerously-skip-permissions",)``.
    :param cfg: Effective config dict, or ``None`` to skip the config
        layer.
    :returns: The combined arg list: config base + CLI pass-through.
    """
    base: list[str] = []
    if cfg is not None:
        canonical = _canonicalize(harness)
        _, overrides = resolve_harness_config(cfg)
        entry = overrides.get(canonical)
        if entry is not None:
            config_args = entry.get(_OVERRIDE_KEY_ARGS)
            if isinstance(config_args, list):
                base = list(config_args)
    return [*base, *cli_args]


def config_harness_path_override(
    harness: str,
    cfg: dict[str, Any],  # type: ignore[explicit-any]
) -> str | None:
    """Return config's ``command`` override for *harness* when no env var is set.

    Used by the CLI-subprocess spawn-env builders to thread a config
    ``harness.<canonical>.command`` into the inner harness via its
    ``OMNIGENT_<NAME>_PATH`` env var — but only when the user hasn't already
    set that env var (ambient env wins, per the shared precedence). Returns
    ``None`` when config has no ``command`` for this harness or when the
    ambient env var already holds a value, so a caller can do
    ``if v: env["OMNIGENT_X_PATH"] = v``.

    :param harness: A harness id (canonical or alias), e.g. ``"codex"``.
    :param cfg: Effective config dict.
    :returns: The config command string to set as ``OMNIGENT_<NAME>_PATH``,
        or ``None`` when config has no override or the ambient env var is set.
    """
    canonical = _canonicalize(harness)
    # Ambient env wins over config — check BOTH the canonical OMNIGENT_* and
    # the deprecated HARNESS_* (via resolve_harness_path, which warns on legacy
    # use) so a legacy HARNESS_* isn't shadowed by a config ``command``.
    if resolve_harness_path(canonical) is not None:
        return None  # ambient env already wins (canonical or legacy)
    _, overrides = resolve_harness_config(cfg)
    entry = overrides.get(canonical)
    if entry is None:
        return None
    command = entry.get(_OVERRIDE_KEY_COMMAND)
    if isinstance(command, str) and command.strip():
        return command.strip()
    return None


__all__ = [
    "config_harness_path_override",
    "resolve_harness_args",
    "resolve_harness_command",
    "resolve_harness_config",
    "resolve_harness_path",
]
