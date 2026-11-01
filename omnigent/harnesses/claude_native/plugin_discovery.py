"""Ask a customized Claude launch which plugins it loads, without a model turn."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import suppress
from pathlib import Path

from omnigent.inner import _proc

LAUNCH_PLUGINS_TIMEOUT_S = 10.0
# Listing plugins must not run the user's session hooks.
_PROBE_SETTINGS = json.dumps({"disableAllHooks": True})


def claude_launch_is_customized() -> bool:
    """Whether native Claude starts through a configured command, arguments or launcher plugin."""
    from omnigent.claude_launcher import CLAUDE_LAUNCHER_ENV_VAR
    from omnigent.config import load_effective_config
    from omnigent.harness_startup_config import resolve_harness_args, resolve_harness_command

    if os.environ.get(CLAUDE_LAUNCHER_ENV_VAR, "").strip():
        return True
    cfg = load_effective_config()
    command = resolve_harness_command("claude-native", default="claude", cfg=cfg)
    return command != "claude" or bool(resolve_harness_args("claude-native", (), cfg=cfg))


def _plugin_dirs(plugins: object) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for plugin in plugins if isinstance(plugins, list) else ():
        if not isinstance(plugin, dict):
            continue
        key = plugin.get("source") or plugin.get("name")
        path = plugin.get("path")
        # Built-in plugins report the path "builtin" and ship no files to read.
        if not (
            isinstance(key, str) and key and isinstance(path, str) and Path(path).is_absolute()
        ):
            continue
        if Path(path).is_dir():
            out.setdefault(key, Path(path))
    return out


def claude_launch_plugin_dirs() -> dict[str, Path]:
    """Map each plugin the native launch loads to its directory, keyed ``<plugin>@<source>``.

    Claude's init event lists loaded plugins, including session-only ones a
    launcher adds with ``--plugin-dir``, before it would contact a model.

    :raises OSError, subprocess.SubprocessError, ValueError: The launch did not report its plugins.
    """
    from omnigent.harnesses.claude_native.main import _claude_model_probe_invocation

    command, launch_args, env = _claude_model_probe_invocation(
        None, ("--settings", _PROBE_SETTINGS)
    )
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
