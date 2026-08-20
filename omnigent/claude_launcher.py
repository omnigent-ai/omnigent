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
is the identity, so behaviour is unchanged.

Launcher plugins follow the same shape as MLflow's plugins: a plugin is a normal
installed Python package whose class implements the :class:`ClaudeLauncher`
interface and registers it as a setuptools entry point in the
:data:`CLAUDE_LAUNCHER_ENTRY_POINT_GROUP` group::

    # the plugin package's pyproject.toml
    [project.entry-points."omnigent.claude_launcher"]
    isaac = "isaac_omni_launcher:IsaacClaudeLauncher"

    # isaac_omni_launcher.py
    from omnigent.claude_launcher import ClaudeLauncher

    class IsaacClaudeLauncher(ClaudeLauncher):
        def launch(self, command, args):
            return "isaac", ["claude", "--", *args]

Any caller attaches a plugin by ``pip install``-ing such a package into the
environment the runner runs in -- no Omnigent code change, no in-tree import
path. At launch time, the ``OMNIGENT_CLAUDE_LAUNCHER`` environment variable
selects *which* registered launcher to use, by entry-point name (e.g.
``OMNIGENT_CLAUDE_LAUNCHER=isaac``). Unset -> default launch. The selected
launcher receives the fully-augmented argv (MCP config, hook settings and skill
flags injected by :func:`augment_claude_args`), so a launcher that merely wraps
the command preserves the Omnigent bridge unchanged.

Selection is per-process via the environment so the runner (which spawns the
terminal on managed hosts) and the local CLI each opt in independently; the
bootstrapping integration sets the env var before the launching process starts.
"""

from __future__ import annotations

import abc
import importlib.metadata
import logging
import os

#: Environment variable selecting a launcher plugin by entry-point name.
CLAUDE_LAUNCHER_ENV_VAR = "OMNIGENT_CLAUDE_LAUNCHER"

#: setuptools entry-point group launcher plugins register themselves in.
CLAUDE_LAUNCHER_ENTRY_POINT_GROUP = "omnigent.claude_launcher"

_logger = logging.getLogger(__name__)


class ClaudeLauncher(abc.ABC):
    """
    Interface a native-Claude launcher plugin implements.

    A plugin subclasses this and registers the subclass as an entry point in the
    :data:`CLAUDE_LAUNCHER_ENTRY_POINT_GROUP` group (see the module docstring).
    Omnigent instantiates the subclass (no-arg constructor) and calls
    :meth:`launch` to decide the final spawn command.
    """

    @abc.abstractmethod
    def launch(self, command: str, args: list[str]) -> tuple[str, list[str]]:
        """
        Return the ``(command, args)`` to actually spawn for this Claude launch.

        :param command: Default terminal command Omnigent would otherwise spawn,
            e.g. ``"claude"``.
        :param args: Fully-augmented Claude CLI args (MCP config, hook settings
            and skill flags already injected by :func:`augment_claude_args`).
            Forward these unchanged (e.g. after a ``--`` separator) to preserve
            the Omnigent bridge.
        :returns: The ``(command, args)`` Omnigent should spawn instead.
        """
        raise NotImplementedError


def resolve_claude_launch(command: str, args: list[str]) -> tuple[str, list[str]]:
    """
    Resolve the final launch command/args for the native Claude terminal.

    Selects the launcher plugin named by :data:`CLAUDE_LAUNCHER_ENV_VAR` from the
    :data:`CLAUDE_LAUNCHER_ENTRY_POINT_GROUP` entry-point group when set;
    otherwise returns the inputs unchanged. Any failure to find, load or run the
    plugin -- unknown name, load/instantiate error, wrong type, raised exception,
    malformed return value -- is logged and falls back to the default
    ``(command, args)`` so a broken or missing plugin can never block a Claude
    launch.

    :param command: Default terminal command, e.g. ``"claude"``.
    :param args: Fully-augmented Claude CLI args (MCP/hooks/skills already
        injected by :func:`augment_claude_args`).
    :returns: The ``(command, args)`` to spawn. ``args`` is always a fresh list.
    """
    default = (command, list(args))
    name = os.environ.get(CLAUDE_LAUNCHER_ENV_VAR, "").strip()
    if not name:
        return default
    launcher = _load_launcher(name)
    if launcher is None:
        return default
    try:
        result = launcher.launch(command, list(args))
    except Exception:
        _logger.exception("Claude launcher plugin %r raised; falling back to default launch", name)
        return default
    return _validated_result(result, name, default)


def _load_launcher(name: str) -> ClaudeLauncher | None:
    """
    Resolve and instantiate the launcher registered under *name* via entry points.

    :param name: Entry-point name from :data:`CLAUDE_LAUNCHER_ENV_VAR`, e.g.
        ``"isaac"``.
    :returns: A :class:`ClaudeLauncher` instance, or ``None`` when no matching
        entry point is registered, it fails to load/instantiate, or it does not
        implement :class:`ClaudeLauncher`.
    """
    try:
        entry_points = importlib.metadata.entry_points(group=CLAUDE_LAUNCHER_ENTRY_POINT_GROUP)
    except Exception:
        _logger.exception("Failed to enumerate %r entry points", CLAUDE_LAUNCHER_ENTRY_POINT_GROUP)
        return None
    matches = [entry_point for entry_point in entry_points if entry_point.name == name]
    if not matches:
        _logger.error(
            "No Claude launcher named %r registered in entry-point group %r",
            name,
            CLAUDE_LAUNCHER_ENTRY_POINT_GROUP,
        )
        return None
    if len(matches) > 1:
        _logger.warning("Multiple Claude launchers named %r registered; using the first", name)
    try:
        launcher_cls = matches[0].load()
        launcher = launcher_cls() if isinstance(launcher_cls, type) else launcher_cls
    except Exception:
        _logger.exception("Could not load Claude launcher plugin %r", name)
        return None
    if not isinstance(launcher, ClaudeLauncher):
        _logger.error("Claude launcher plugin %r does not implement ClaudeLauncher", name)
        return None
    return launcher


def _validated_result(
    result: object, name: str, default: tuple[str, list[str]]
) -> tuple[str, list[str]]:
    """
    Coerce and validate a plugin's return value to ``(str, list[str])``.

    :param result: Raw plugin return value.
    :param name: Launcher entry-point name, for diagnostics.
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
        name,
        result,
    )
    return default
