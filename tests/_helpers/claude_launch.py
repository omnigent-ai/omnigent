"""A configured native Claude command that records each launch, for launch-probe tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def write_launch_recorder(launcher: Path, launches: Path, script: str) -> None:
    """
    Write an executable that appends its argv and cwd to *launches*, then runs *script*.

    :param launcher: Executable path to create, e.g. ``tmp_path / "bin" / "claude"``.
    :param launches: JSON-lines file each launch appends to.
    :param script: Python run after recording; ``json``, ``os``, ``sys`` and
        ``time`` are imported, e.g. ``"time.sleep(30)\\n"``.
    """
    launcher.write_text(
        f"#!{sys.executable}\nimport json, os, sys, time\n"
        f"with open({str(launches)!r}, 'a') as out:\n"
        "    out.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n" + script
    )
    launcher.chmod(0o755)


def configure_claude_launch(config_home: Path, launches: Path, script: str) -> None:
    """
    Make ``harness.claude-native.command`` a launch recorder in *config_home*'s config.

    :param config_home: The isolated ``OMNIGENT_CONFIG_HOME``, e.g. from
        ``isolate_provider_resolution``.
    :param launches: JSON-lines file each launch appends to.
    :param script: Python the launch runs after recording itself.
    """
    launcher = config_home / "claude-launcher"
    write_launch_recorder(launcher, launches, script)
    (config_home / "config.yaml").write_text(
        json.dumps({"harness": {"claude-native": {"command": str(launcher), "args": ["--"]}}})
    )
