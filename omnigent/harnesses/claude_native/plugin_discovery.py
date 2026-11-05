"""Ask a customized Claude launch which plugins it loads, without a model turn."""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import yaml

from omnigent.inner import _proc

_log = logging.getLogger(__name__)

LAUNCH_PLUGINS_TIMEOUT_S = 10.0
# A probe still running here is reported as stalled rather than loading.
LAUNCH_PLUGINS_DEADLINE_S = LAUNCH_PLUGINS_TIMEOUT_S + 2
# Readers wait at most this long, leaving the server's 10s runner skill lookup room to answer.
LAUNCH_PLUGINS_WAIT_S = 7.0
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


def stop_claude_launch_probes() -> None:
    """Kill the launches probes still wait on, and start no more; call when exiting."""
    global _exiting
    # Daemon probe threads die at exit without reaping their launches, so kill them here.
    with _launches_lock:
        _exiting = True
        launches = list(_running_launches)
    for process in launches:
        _proc.kill_tree(process)


atexit.register(stop_claude_launch_probes)


def claude_launch_is_customized() -> bool:
    """Whether native Claude starts through a configured command, arguments or launcher plugin.

    :raises CONFIG_READ_ERRORS: The Omnigent config could not be read.
    """
    from omnigent.claude_launcher import CLAUDE_LAUNCHER_ENV_VAR

    if os.environ.get(CLAUDE_LAUNCHER_ENV_VAR, "").strip():
        return True
    _, command, args = _configured_launch()
    return command != "claude" or bool(args)


def _configured_launch() -> tuple[Mapping[str, object], str, list[str]]:
    """The Omnigent config, and the native Claude command and arguments it sets.

    :raises CONFIG_READ_ERRORS: The Omnigent config could not be read.
    """
    from omnigent.config import load_effective_config
    from omnigent.harness_startup_config import resolve_harness_args, resolve_harness_command

    cfg = load_effective_config()
    command = resolve_harness_command("claude-native", default="claude", cfg=cfg)
    return cfg, command, resolve_harness_args("claude-native", (), cfg=cfg)


def launch_plugins_generation() -> int:
    """Count launch answers and failures; a change means catalogs built earlier may be stale."""
    return _generation


def prewarm_claude_launch_plugins() -> None:
    """Start probing a customized launch, so the first skill menu can already list its plugins.

    A stored answer is not reused, so a restarted host asks a launcher plugin it reloaded.
    Config and probe failures are left for the menu to report; anything else is logged.
    """
    try:
        if claude_launch_is_customized():
            claude_launch_plugin_dirs(wait=False, reuse_stored=False)
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


def claude_launch_plugin_dirs(*, wait: bool = True, reuse_stored: bool = True) -> dict[str, Path]:
    """Map each plugin the native launch loads to its directory, keyed ``<plugin>@<source>``.

    Claude's init event lists loaded plugins, including session-only ones a
    launcher adds with ``--plugin-dir``, before it would contact a model. One
    background probe at a time resolves each launch, including any launcher
    plugin, and asks it; its answer is reused until the configured launch or an
    executable it runs changes. A probe that misses its deadline is reported as
    failed, and the launch is asked again only once that probe returns.

    :param wait: Wait for a first answer, up to :data:`LAUNCH_PLUGINS_WAIT_S` and the
        probe's deadline, rather than raise :class:`LaunchPluginsPending` at once.
    :param reuse_stored: Reuse a fresh answer another process stored instead of
        probing, as a runner does with its host's.
    :raises LaunchPluginsPending: The launch has not answered yet.
    :raises LaunchPluginsFailed: The last probe failed or could not start, or the running
        one missed its deadline.
    :raises CONFIG_READ_ERRORS: The Omnigent config could not be read.
    """
    global _generation
    from omnigent.claude_launcher import CLAUDE_LAUNCHER_ENV_VAR
    from omnigent.models.model_catalog_store import binary_identity

    # Resolving the launch can run a launcher plugin, so only the background probe does.
    cfg, command, args = _configured_launch()
    launcher = os.environ.get(CLAUDE_LAUNCHER_ENV_VAR, "").strip()
    key = (launcher, command, *args, binary_identity(command))
    with _probes_lock:
        probe = _probes.get(key)
        executable = probe.executable if probe is not None else None
        due = probe is None or (probe.running is None and probe.refresh_at <= time.monotonic())
    executable_identity = binary_identity(executable)
    stored = _read_stored_answer(key) if due and reuse_stored else None
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
        if probe.running is None and probe.refresh_at <= time.monotonic() and stored is not None:
            # Another process, such as the host for its runners, already asked this launch.
            probe = _probes[key] = stored
            _generation += 1
        elif probe.running is None and probe.refresh_at <= time.monotonic():
            probe.running = threading.Event()
            probe.deadline = time.monotonic() + LAUNCH_PLUGINS_DEADLINE_S
            try:
                # A daemon, so a hung launcher plugin cannot keep the process from exiting.
                threading.Thread(
                    target=_refresh,
                    args=(key, probe, cfg),
                    name="claude-launch-plugins",
                    daemon=True,
                ).start()
            except RuntimeError as exc:
                probe.running = None
                _record_failure(probe, "could not be probed")
                _log.warning("Could not start probing the configured Claude launch: %s", exc)
        running, deadline = probe.running, probe.deadline
    if wait and running is not None and probe.plugins is None:
        running.wait(max(0.0, min(deadline - time.monotonic(), LAUNCH_PLUGINS_WAIT_S)))
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


def stored_answers_dir() -> Path:
    """Where launch answers are shared with this host's other processes, such as its runners."""
    from omnigent.process_logging import data_dir

    return data_dir() / "cache" / "claude-launch-plugins"


def _answer_path(key: tuple[object, ...]) -> Path:
    from omnigent.models.model_catalog_store import fingerprint_of

    return stored_answers_dir() / f"{fingerprint_of(*key)}.json"


def _store_answer(
    key: tuple[object, ...],
    executable: str | None,
    executable_identity: tuple[str, int, int] | None,
    plugins: dict[str, Path],
) -> None:
    from omnigent.install_ledger import atomic_write_json

    payload: dict[str, object] = {
        "written_at": time.time(),
        "executable": executable,
        "executable_identity": executable_identity,
        "plugins": [{"source": source, "path": str(path)} for source, path in plugins.items()],
    }
    try:
        atomic_write_json(_answer_path(key), payload)
    except OSError as exc:
        _log.warning("Could not store the Claude launch's plugins: %s", describe_config_error(exc))


def _read_stored_answer(key: tuple[object, ...]) -> _LaunchProbe | None:
    """A stored answer for this launch that is fresh and whose executable is unchanged."""
    from omnigent.models.model_catalog_store import binary_identity

    try:
        payload = json.loads(_answer_path(key).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    written_at, executable = payload.get("written_at"), payload.get("executable")
    if not isinstance(written_at, (int, float)) or not isinstance(executable, str):
        return None
    remaining = written_at + LAUNCH_PLUGINS_TTL_S - time.time()
    identity = binary_identity(executable)
    if not 0 < remaining <= LAUNCH_PLUGINS_TTL_S or payload.get("executable_identity") != (
        list(identity) if identity is not None else None
    ):
        return None
    return _LaunchProbe(
        plugins=_plugin_dirs(payload.get("plugins")),
        refresh_at=time.monotonic() + remaining,
        executable=executable,
        executable_identity=identity,
    )


def _refresh(key: tuple[object, ...], probe: _LaunchProbe, cfg: Mapping[str, object]) -> None:
    global _generation
    from omnigent.harnesses.claude_native.main import _claude_model_probe_invocation
    from omnigent.models.model_catalog_store import binary_identity

    plugins: dict[str, Path] | None = None
    executable: str | None = None
    executable_identity: tuple[str, int, int] | None = None
    failure = "did not report its plugins"
    try:
        # The config the key was read from, so a changed config cannot answer for this launch.
        command, launch_args, env = _claude_model_probe_invocation(
            None, ("--settings", _PROBE_SETTINGS), cfg=cfg
        )
        executable, executable_identity = command, binary_identity(command)
        plugins = _probe_plugin_dirs(command, launch_args, env)
    except subprocess.TimeoutExpired as exc:
        failure = f"timed out after {exc.timeout:g}s"
        # The exception's text includes the launch's arguments, which can carry credentials.
        _log.warning("The configured Claude launch %s", failure)
    except LaunchPluginsFailed as exc:
        failure = str(exc)
        _log.warning("The configured Claude launch %s", failure)
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
        if plugins is not None:
            _store_answer(key, executable, executable_identity, plugins)
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
