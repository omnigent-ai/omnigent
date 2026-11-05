"""Ask a customized Claude launch which plugins it loads, without a model turn."""

from __future__ import annotations

import atexit
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
# Readers stop waiting here, inside the server's 15s limit on a skill-content request.
LAUNCH_PLUGINS_DEADLINE_S = LAUNCH_PLUGINS_TIMEOUT_S + 2
# The launch identity keys each answer, so this only bounds how long a missed change lasts.
LAUNCH_PLUGINS_TTL_S = 3600.0
# Each consecutive failure of one launch doubles the wait before it is spawned again.
FAILED_LAUNCH_BACKOFF_S = 30.0
MAX_FAILED_LAUNCH_BACKOFF_S = 600.0
# Listing plugins must not run the user's session hooks.
_PROBE_SETTINGS = json.dumps({"disableAllHooks": True})
# What an unreadable or malformed Omnigent config raises, e.g. a project config that is a list.
CONFIG_READ_ERRORS = (OSError, AttributeError, TypeError, ValueError, yaml.YAMLError)


def describe_config_error(exc: BaseException) -> str:
    """Say what failed and where, without the message, which can quote a config value."""
    name = type(exc).__name__
    mark = exc.problem_mark if isinstance(exc, yaml.MarkedYAMLError) else None
    if mark is not None:
        return f"{name} in {mark.name}, line {mark.line + 1}, column {mark.column + 1}"
    if isinstance(exc, OSError) and exc.filename is not None:
        return f"{name}: {exc.strerror}: {exc.filename}"
    return name


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
    # A launcher plugin can hang, so waiting and "still loading" end here.
    deadline: float = 0.0
    # A launcher plugin can resolve to an executable outside the configured launch.
    executable: str | None = None
    executable_identity: object = None


_probes_lock = threading.Lock()
_probes: dict[tuple[object, ...], _LaunchProbe] = {}
_generation = 0
_launches_lock = threading.Lock()
_running_launches: set[subprocess.Popen[str]] = set()
_exiting = False


def _kill_running_launches() -> None:
    global _exiting
    # Probe threads are daemons, so exit kills the launches they still wait on.
    with _launches_lock:
        _exiting = True
        launches = list(_running_launches)
    for process in launches:
        _proc.kill_tree(process)


atexit.register(_kill_running_launches)


def claude_launch_is_customized() -> bool:
    """Whether native Claude starts through a configured command, arguments or launcher plugin.

    :raises CONFIG_READ_ERRORS: The Omnigent config could not be read.
    """
    from omnigent.claude_launcher import CLAUDE_LAUNCHER_ENV_VAR

    if os.environ.get(CLAUDE_LAUNCHER_ENV_VAR, "").strip():
        return True
    command, args = _configured_command()
    return command != "claude" or bool(args)


def _configured_command() -> tuple[str, list[str]]:
    """The native Claude command and arguments the Omnigent config sets.

    :raises CONFIG_READ_ERRORS: The Omnigent config could not be read.
    """
    from omnigent.config import load_effective_config
    from omnigent.harness_startup_config import resolve_harness_args, resolve_harness_command

    cfg = load_effective_config()
    command = resolve_harness_command("claude-native", default="claude", cfg=cfg)
    return command, resolve_harness_args("claude-native", (), cfg=cfg)


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
    background probe at a time resolves each launch, including any launcher
    plugin, and asks it; its answer is reused until the configured launch or an
    executable it runs changes. A probe that misses its deadline is reported as
    failed, and the launch is asked again only once that probe returns.

    :param wait: Wait for a first answer, up to the probe's deadline, rather than
        raise :class:`LaunchPluginsPending`.
    :raises LaunchPluginsPending: The launch has not answered yet.
    :raises LaunchPluginsFailed: The last probe failed or could not start, or the running
        one missed its deadline.
    :raises CONFIG_READ_ERRORS: The Omnigent config could not be read.
    """
    from omnigent.claude_launcher import CLAUDE_LAUNCHER_ENV_VAR
    from omnigent.models.model_catalog_store import binary_identity

    # Resolving the launch can run a launcher plugin, so only the background probe does.
    command, args = _configured_command()
    launcher = os.environ.get(CLAUDE_LAUNCHER_ENV_VAR, "").strip()
    key = (launcher, command, *args, binary_identity(command))
    with _probes_lock:
        probe = _probes.get(key)
        executable = probe.executable if probe is not None else None
    executable_identity = binary_identity(executable)
    with _probes_lock:
        # Only the current launch's answer is reused, so idle earlier launches are dropped.
        for idle in [k for k, probe in _probes.items() if k != key and probe.running is None]:
            del _probes[idle]
        probe = _probes.get(key)
        if probe is None or (
            probe.running is None
            and probe.executable == executable
            and probe.executable_identity != executable_identity
        ):
            probe = _probes[key] = _LaunchProbe()
        if probe.running is None and probe.refresh_at <= time.monotonic():
            probe.running = threading.Event()
            probe.deadline = time.monotonic() + LAUNCH_PLUGINS_DEADLINE_S
            try:
                # A daemon, so a hung launcher plugin cannot keep the process from exiting.
                threading.Thread(
                    target=_refresh, args=(probe,), name="claude-launch-plugins", daemon=True
                ).start()
            except RuntimeError as exc:
                probe.running = None
                _record_failure(probe, "could not be probed")
                _log.warning("Could not start probing the configured Claude launch: %s", exc)
        running, deadline = probe.running, probe.deadline
    if wait and running is not None and probe.plugins is None:
        running.wait(max(0.0, deadline - time.monotonic()))
    with _probes_lock:
        if probe.plugins is not None:
            return dict(probe.plugins)
        if probe.running is not None:
            if time.monotonic() < probe.deadline:
                raise LaunchPluginsPending
            stalled = f"did not answer within {LAUNCH_PLUGINS_DEADLINE_S:g}s"
            if probe.failure != stalled:
                probe.failure = stalled
                _log.warning("The configured Claude launch %s", stalled)
        raise LaunchPluginsFailed(probe.failure)


def _refresh(probe: _LaunchProbe) -> None:
    global _generation
    from omnigent.harnesses.claude_native.main import _claude_model_probe_invocation
    from omnigent.models.model_catalog_store import binary_identity

    plugins: dict[str, Path] | None = None
    executable: str | None = None
    executable_identity: object = None
    failure = "did not report its plugins"
    try:
        command, launch_args, env = _claude_model_probe_invocation(
            None, ("--settings", _PROBE_SETTINGS)
        )
        executable, executable_identity = command, binary_identity(command)
        plugins = _probe_plugin_dirs(command, launch_args, env)
    except subprocess.TimeoutExpired as exc:
        failure = f"timed out after {exc.timeout:g}s"
        # The exception's text includes the launch's arguments, which can carry credentials.
        _log.warning("The configured Claude launch %s", failure)
    except LaunchPluginsFailed as exc:
        _log.warning("The configured Claude launch %s: %s", failure, exc)
    except (subprocess.SubprocessError, *CONFIG_READ_ERRORS) as exc:
        _log.warning("The configured Claude launch %s: %s", failure, describe_config_error(exc))
    finally:
        with _probes_lock:
            probe.executable, probe.executable_identity = executable, executable_identity
            if plugins is None:
                _record_failure(probe, failure)
            else:
                probe.plugins, probe.failure, probe.backoff_s = plugins, None, 0.0
                probe.refresh_at = time.monotonic() + LAUNCH_PLUGINS_TTL_S
            done, probe.running = probe.running, None
            _generation += 1
        if done is not None:
            done.set()


def _record_failure(probe: _LaunchProbe, failure: str) -> None:
    """Back off before asking the launch again; callers hold ``_probes_lock``."""
    # A refresh that fails keeps serving the plugins this launch reported before.
    probe.failure = failure
    probe.backoff_s = min(
        2 * probe.backoff_s or FAILED_LAUNCH_BACKOFF_S, MAX_FAILED_LAUNCH_BACKOFF_S
    )
    probe.refresh_at = time.monotonic() + probe.backoff_s


def _probe_plugin_dirs(
    command: str, launch_args: list[str], env: dict[str, str]
) -> dict[str, Path]:
    # Spawning under the lock means exit either kills this launch or stops it from starting.
    with _launches_lock:
        if _exiting:
            raise LaunchPluginsFailed("was not started because the host is exiting")
        process = subprocess.Popen(
            _proc.pipe_supervised_command(command, *launch_args),
            cwd=Path.home(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # Stderr can echo the launch's arguments, so only the exit status is reported.
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            **_proc.spawn_kwargs(),
        )
        _proc.remember_process_group(process)
        _running_launches.add(process)
    try:
        stdout, _ = process.communicate(timeout=LAUNCH_PLUGINS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _proc.kill_tree(process)
        with suppress(Exception):
            process.communicate(timeout=1)
        raise
    finally:
        with _launches_lock:
            _running_launches.discard(process)
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
    raise LaunchPluginsFailed(f"exited {process.returncode} without an init event")
