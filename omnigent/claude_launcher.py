"""Pluggable launch-command resolution for the native Claude harness.

The native Claude terminal is normally spawned as ``claude <args>`` -- the
``command`` defaults to ``"claude"`` in both launch paths:
:func:`omnigent.claude_native._claude_terminal_request` (local CLI) and
``_auto_create_claude_terminal`` in :mod:`omnigent.runner.app` (managed-host
runner). Downstream integrations need to launch that *same* Claude Code process
through a wrapper binary so the wrapper's process-level setup -- auth, telemetry,
cost controls, enforcement hooks, plugin management -- is always applied. The
motivating case is Databricks' ``isaac``, which wraps Claude/Codex with that
tooling; running ``isaac claude`` instead of bare ``claude`` keeps it in force.

Rather than hardcode the binary at each site, both paths route the
``(command, args)`` pair through :func:`resolve_claude_launch`. By default this
is the identity, so behaviour is unchanged. When ``OMNIGENT_CLAUDE_LAUNCHER``
names a plugin, that plugin decides the final command and args. The argv handed
to it is already fully augmented (MCP config, hook settings and skill flags
injected by :func:`augment_claude_args`), so a plugin that merely wraps the
command -- e.g. ``("isaac", ["claude", "--omni-internal", "--", *args])`` --
preserves the Omnigent bridge unchanged.

Plugin reference format is ``module.path:callable`` resolving to::

    def launch(command: str, args: list[str]) -> tuple[str, list[str]]: ...

Selection is per-process via the environment so the runner (which spawns the
terminal on managed hosts) and the local CLI each opt in independently; the
bootstrapping integration sets the env var before the launching process starts.
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Callable

#: Environment variable naming the launcher plugin as ``module.path:callable``.
CLAUDE_LAUNCHER_ENV_VAR = "OMNIGENT_CLAUDE_LAUNCHER"

_logger = logging.getLogger(__name__)

#: Signature a registered launcher plugin must implement.
ClaudeLauncher = Callable[[str, list[str]], tuple[str, list[str]]]


def resolve_claude_launch(command: str, args: list[str]) -> tuple[str, list[str]]:
    """
    Resolve the final launch command/args for the native Claude terminal.

    Delegates to the plugin named by :data:`CLAUDE_LAUNCHER_ENV_VAR` when set;
    otherwise returns the inputs unchanged. Any failure to load or run the
    plugin -- bad reference, import error, raised exception, malformed return
    value -- is logged and falls back to the default ``(command, args)`` so a
    broken plugin can never block a Claude launch.

    :param command: Default terminal command, e.g. ``"claude"``.
    :param args: Fully-augmented Claude CLI args (MCP/hooks/skills already
        injected by :func:`augment_claude_args`).
    :returns: The ``(command, args)`` to spawn. ``args`` is always a fresh list.
    """
    default = (command, list(args))
    spec = os.environ.get(CLAUDE_LAUNCHER_ENV_VAR, "").strip()
    if not spec:
        return default
    launcher = _load_launcher(spec)
    if launcher is None:
        return default
    try:
        result = launcher(command, list(args))
    except Exception:
        _logger.exception("Claude launcher plugin %r raised; falling back to default launch", spec)
        return default
    return _validated_result(result, spec, default)


def _load_launcher(spec: str) -> ClaudeLauncher | None:
    """
    Import the launcher callable from a ``module.path:callable`` reference.

    :param spec: Plugin reference, e.g. ``"isaac_omni.launcher:launch_claude"``.
    :returns: The resolved callable, or ``None`` when the reference is malformed
        or cannot be imported.
    """
    module_path, sep, attr = spec.partition(":")
    if not sep or not module_path or not attr:
        _logger.error(
            "Ignoring %s=%r: expected 'module.path:callable'",
            CLAUDE_LAUNCHER_ENV_VAR,
            spec,
        )
        return None
    try:
        module = importlib.import_module(module_path)
        launcher = getattr(module, attr)
    except (ImportError, AttributeError):
        _logger.exception("Could not load Claude launcher plugin %r", spec)
        return None
    if not callable(launcher):
        _logger.error("Claude launcher plugin %r is not callable", spec)
        return None
    return launcher


def _validated_result(
    result: object, spec: str, default: tuple[str, list[str]]
) -> tuple[str, list[str]]:
    """
    Coerce and validate a plugin's return value to ``(str, list[str])``.

    :param result: Raw plugin return value.
    :param spec: Plugin reference, for diagnostics.
    :param default: Fallback ``(command, args)`` when ``result`` is malformed.
    :returns: A validated ``(command, args)`` tuple, or ``default``.
    """
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[0], str)
        and result[0]
        and isinstance(result[1], list)
        and all(isinstance(arg, str) for arg in result[1])
    ):
        return result[0], list(result[1])
    _logger.error(
        "Claude launcher plugin %r returned %r; expected (str, list[str]); "
        "falling back to default launch",
        spec,
        result,
    )
    return default
