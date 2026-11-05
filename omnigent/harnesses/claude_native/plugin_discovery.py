"""Ask a customized Claude launch which plugins it loads, without a model turn."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import yaml

from omnigent.inner import _proc

_log = logging.getLogger(__name__)

LAUNCH_PLUGINS_TIMEOUT_S = 10.0
# The launch identity keys each answer, so this only bounds how long a missed change lasts.
LAUNCH_PLUGINS_TTL_S = 3600.0
# Each consecutive failure of one launch doubles the wait before it is spawned again.
FAILED_LAUNCH_BACKOFF_S = 30.0
MAX_FAILED_LAUNCH_BACKOFF_S = 600.0
# Listing plugins must not run the user's session hooks.
_PROBE_SETTINGS = json.dumps({"disableAllHooks": True})
# What an unreadable or malformed Omnigent config raises, e.g. a project config that is a list.
CONFIG_READ_ERRORS = (OSError, AttributeError, TypeError, ValueError, yaml.YAMLError)


class LaunchPluginsPending(Exception):
    """The launch has not reported its plugins yet; a background probe is asking it."""


class LaunchPluginsFailed(Exception):
    """The launch's last probe failed; the message summarizes why, e.g. ``timed out after 10s``."""


@dataclass
class _LaunchProbe:
    """One launch identity's last answer and the probe refreshing it."""

    plugins: dict[str, Path] | None = None
    failure: str | None = None
    backoff_s: float = 0.0
    refresh_at: float = 0.0
    running: threading.Event | None = None


_probes_lock = threading.Lock()
_probes: dict[tuple[object, ...], _LaunchProbe] = {}
_generation = 0


def claude_launch_is_customized() -> bool:
    """Whether native Claude starts through a configured command, arguments or launcher plugin.

    :raises CONFIG_READ_ERRORS: The Omnigent config could not be read.
    """
    from omnigent.claude_launcher import CLAUDE_LAUNCHER_ENV_VAR
    from omnigent.config import load_effective_config
    from omnigent.harness_startup_config import resolve_harness_args, resolve_harness_command

    if os.environ.get(CLAUDE_LAUNCHER_ENV_VAR, "").strip():
        return True
    cfg = load_effective_config()
    command = resolve_harness_command("claude-native", default="claude", cfg=cfg)
    return command != "claude" or bool(resolve_harness_args("claude-native", (), cfg=cfg))


def launch_plugins_generation() -> int:
    """Count finished launch probes; a change means catalogs built earlier may be stale."""
    return _generation


def prewarm_claude_launch_plugins() -> None:
    """Start probing a customized launch, so the first skill menu can already list its plugins.

    Config and probe failures are left for the menu to report; anything else is logged.
    """
    try:
        if claude_launch_is_customized():
            claude_launch_plugin_dirs(wait=False)
    except (LaunchPluginsPending, LaunchPluginsFailed, *CONFIG_READ_ERRORS):
        pass
    except Exception:  # noqa: BLE001 — a background prewarm has no caller to report to
        _log.warning("Could not start probing the configured Claude launch", exc_info=True)


def _plugin_dirs(plugins: object) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for plugin in plugins if isinstance(plugins, list) else ():
        if not isinstance(plugin, dict):
            continue
        # Settings and installs key plugins by source; a bare name could evade their disables.
        key = plugin.get("source")
        path = plugin.get("path")
        # Built-in plugins report the path "builtin" and ship no files to read.
        if not (
            isinstance(key, str) and key and isinstance(path, str) and Path(path).is_absolute()
        ):
            continue
        if Path(path).is_dir():
            out.setdefault(key, Path(path))
    return out


def claude_launch_plugin_dirs(*, wait: bool = True) -> dict[str, Path]:
    """Map each plugin the native launch loads to its directory, keyed ``<plugin>@<source>``.

    Claude's init event lists loaded plugins, including session-only ones a
    launcher adds with ``--plugin-dir``, before it would contact a model. One
    background probe at a time asks each launch; its answer is reused until the
    command, arguments or executable change.

    :param wait: Wait for a first answer rather than raise :class:`LaunchPluginsPending`.
    :raises LaunchPluginsPending: The launch has not answered yet.
    :raises LaunchPluginsFailed: The last probe failed and the next waits out a backoff.
    :raises CONFIG_READ_ERRORS: The Omnigent config could not be read.
    """
    from omnigent.harnesses.claude_native.main import _claude_model_probe_invocation
    from omnigent.models.model_catalog_store import binary_identity

    command, launch_args, env = _claude_model_probe_invocation(
        None, ("--settings", _PROBE_SETTINGS)
    )
    key = (command, *launch_args, binary_identity(command))
    with _probes_lock:
        # Only the current launch's answer is reused, so idle earlier launches are dropped.
        for idle in [k for k, probe in _probes.items() if k != key and probe.running is None]:
            del _probes[idle]
        probe = _probes.setdefault(key, _LaunchProbe())
        if probe.running is None and probe.refresh_at <= time.monotonic():
            probe.running = threading.Event()
            try:
                # Not a daemon, so exiting still waits for a stalled launch to be killed.
                threading.Thread(
                    target=_refresh,
                    args=(probe, command, launch_args, env),
                    name="claude-launch-plugins",
                ).start()
            except RuntimeError:
                probe.running = None
                raise
        running = probe.running
    if wait and running is not None and probe.plugins is None:
        running.wait(LAUNCH_PLUGINS_TIMEOUT_S + 5)
    with _probes_lock:
        if probe.plugins is not None:
            return dict(probe.plugins)
        if probe.running is not None:
            raise LaunchPluginsPending
        raise LaunchPluginsFailed(probe.failure)


def _refresh(
    probe: _LaunchProbe, command: str, launch_args: list[str], env: dict[str, str]
) -> None:
    global _generation
    plugins: dict[str, Path] | None = None
    failure = "did not report its plugins"
    try:
        plugins = _probe_plugin_dirs(command, launch_args, env)
    except subprocess.TimeoutExpired as exc:
        failure = f"timed out after {exc.timeout:g}s"
        # The exception's text includes the launch's arguments, which can carry credentials.
        _log.warning("The configured Claude launch %s", failure)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _log.warning("The configured Claude launch %s: %s", failure, exc)
    finally:
        with _probes_lock:
            if plugins is None:
                # A refresh that fails keeps serving the plugins this launch reported before.
                probe.failure = failure
                probe.backoff_s = min(
                    2 * probe.backoff_s or FAILED_LAUNCH_BACKOFF_S, MAX_FAILED_LAUNCH_BACKOFF_S
                )
                probe.refresh_at = time.monotonic() + probe.backoff_s
            else:
                probe.plugins, probe.failure, probe.backoff_s = plugins, None, 0.0
                probe.refresh_at = time.monotonic() + LAUNCH_PLUGINS_TTL_S
            done, probe.running = probe.running, None
            _generation += 1
        if done is not None:
            done.set()


def _probe_plugin_dirs(
    command: str, launch_args: list[str], env: dict[str, str]
) -> dict[str, Path]:
    process = subprocess.Popen(
        _proc.pipe_supervised_command(command, *launch_args),
        cwd=Path.home(),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        **_proc.spawn_kwargs(),
    )
    _proc.remember_process_group(process)
    try:
        stdout, stderr = process.communicate(timeout=LAUNCH_PLUGINS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _proc.kill_tree(process)
        with suppress(Exception):
            process.communicate(timeout=1)
        raise
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            return _plugin_dirs(event.get("plugins"))
    raise ValueError(f"exited {process.returncode} without an init event: {stderr.strip()[-500:]}")
