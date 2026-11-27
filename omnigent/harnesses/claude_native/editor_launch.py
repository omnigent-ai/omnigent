"""Prepare private editor delegates without changing the native editor choice."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from omnigent.harnesses.claude_native import bridge

_DEFAULT_EDITORS = (
    "code",
    "code-insiders",
    "cursor",
    "windsurf",
    "zed",
    "subl",
    "atom",
    "sensible-editor",
    "editor",
    "emacsclient",
    "nvim",
    "vim",
    "vi",
    "nano",
    "emacs",
    "open",
)


def prepare_editor_environment(
    bridge_dir: Path,
    *,
    executable: Path,
    environment: Mapping[str, str],
    python_executable: str = sys.executable,
) -> dict[str, str] | None:
    """Return launch overrides, retaining immutable delegates for older launches.

    Unsupported editor commands leave the launch unchanged. Delegate files
    remain with the bridge because an earlier terminal may still invoke them.
    """
    if sys.platform == "win32":
        return None
    try:
        native = executable.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not native.is_file():
        return None
    path = environment.get("PATH")
    configured: dict[str, tuple[str, list[str]]] = {}
    for key in ("VISUAL", "EDITOR"):
        value = environment.get(key)
        if not value:
            continue
        if re.search(r"[;&|<>`$\n\r\x00]", value):
            return None
        try:
            arguments = shlex.split(value)
        except ValueError:
            return None
        if not arguments or (os.sep in arguments[0] and not Path(arguments[0]).is_absolute()):
            return None
        selected = shutil.which(arguments[0], path=path or os.defpath)
        if selected is None:
            return None
        configured[key] = selected, arguments[1:]
    defaults = (
        {
            name: selected
            for name in _DEFAULT_EDITORS
            if (selected := shutil.which(name, path=path))
        }
        if not configured and path is not None
        else {}
    )
    if not configured and not defaults:
        return None
    parent = bridge_dir / "editor-launches"
    try:
        bridge._ensure_secure_dir(parent)
        launch = Path(tempfile.mkdtemp(prefix="launch-", dir=parent))
    except (OSError, RuntimeError, ValueError):
        return None
    overrides: dict[str, str] = {}
    entries: list[tuple[Path, str, str]] = []
    prefix: str | None = None
    try:
        if configured:
            for key, (selected, arguments) in configured.items():
                directory = launch / key.lower()
                directory.mkdir(mode=0o700)
                shim = directory / Path(selected).name
                manifest = launch / (key.lower() + ".json")
                overrides[key] = shlex.join([str(shim), *arguments])
                entries.append((shim, selected, str(manifest)))
        else:
            directory = launch / "bin"
            directory.mkdir(mode=0o700)
            prefix = str(directory)
            overrides["PATH"] = prefix + os.pathsep + str(path)
            for name, selected in defaults.items():
                entries.append((directory / name, selected, str(launch / (name + ".json"))))
        replaced = set(overrides) & {"VISUAL", "EDITOR"}
        for shim, selected, manifest_path in entries:
            manifest = Path(manifest_path)
            contents = {
                "version": 1,
                "command": [selected],
                "restore_environment": {key: environment.get(key) for key in replaced},
                "injected_environment": {key: overrides[key] for key in replaced},
                "path_prefix": prefix,
            }
            descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(contents, stream)
                stream.write("\n")
            command = [
                python_executable,
                "-I",
                "-m",
                "omnigent.harnesses.claude_native.editor_delegate",
                "--manifest",
                str(manifest),
                "--bridge-dir",
                str(bridge_dir),
                "--native-executable",
                str(native),
                "--",
            ]
            shim.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
            shim.chmod(0o700)
    except (OSError, ValueError):
        shutil.rmtree(launch)
        return None
    return overrides
