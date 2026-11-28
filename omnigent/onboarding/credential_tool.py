"""Persist explicitly selected tool environments before writing credential configuration."""

from __future__ import annotations

import argparse
import functools
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ParamSpec

_Parameters = ParamSpec("_Parameters")
_INSTALL_TIMEOUT = 90


def persistent_configuration_command(command: Sequence[str]) -> list[str]:
    """Adapt an explicit ``uvx --from DISTRIBUTION ENTRYPOINT`` configure command.

    The caller supplies the distribution and entrypoint in its existing argv.
    Installation runs in the configure subprocess, not while building argv.
    Other command prefixes, including custom helpers, are returned unchanged.
    Callers should select an immutable distribution when reproducibility matters.
    """
    if (
        len(command) < 5
        or Path(command[0]).name.lower() not in {"uvx", "uvx.exe"}
        or command[1] != "--from"
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", command[3]) is None
    ):
        return list(command)
    from omnigent.process_logging import data_dir

    return [
        sys.executable,
        "-P",
        str(Path(__file__).resolve()),
        "--distribution",
        command[2],
        "--entrypoint",
        command[3],
        "--storage",
        str(data_dir().resolve() / "tools" / "credential-helpers"),
        "--",
        *command[4:],
    ]


def persistent_tool_configuration(
    factory: Callable[_Parameters, list[str]],
) -> Callable[_Parameters, list[str]]:
    """Keep a configure-command factory's selected distribution persistent."""

    @functools.wraps(factory)
    def build(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> list[str]:
        return persistent_configuration_command(factory(*args, **kwargs))

    return build


def install_credential_tool(distribution: str, entrypoint: str, storage: Path) -> Path:
    """Install a caller-selected helper without relying on disposable cache files.

    Each distribution/entrypoint pair owns a separate environment. A failed
    installation never substitutes a different helper or credential source.
    """
    if not distribution or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", entrypoint) is None:
        raise ValueError("A distribution and plain executable entrypoint are required")
    identity = hashlib.sha256(f"{distribution}\0{entrypoint}".encode()).hexdigest()
    root = storage / identity
    bin_dir = root / "bin"
    command = bin_dir / (f"{entrypoint}.exe" if os.name == "nt" else entrypoint)
    if os.access(command, os.X_OK):
        return command
    installer = shutil.which("uv")
    if installer is None:
        raise ValueError("uv is required to install the persistent credential helper")
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(root / "environments"),
        "UV_TOOL_BIN_DIR": str(bin_dir),
    }
    env.pop("OMNIGENT_HOST_TOKEN", None)
    subprocess.run(
        [installer, "tool", "install", "--link-mode", "copy", distribution],
        env=env,
        capture_output=True,
        timeout=_INSTALL_TIMEOUT,
        check=True,
    )
    if not os.access(command, os.X_OK):
        raise ValueError("Installation did not provide the selected executable")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    """Install the selected helper, then replace this process with its configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    try:
        command = install_credential_tool(
            options.distribution, options.entrypoint, options.storage
        )
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(command.parent), env.get("PATH", "")])
        arguments = options.arguments
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        os.execvpe(str(command), [str(command), *arguments], env)
    except (OSError, subprocess.SubprocessError, ValueError):
        print(
            "Persistent credential-helper setup failed; check installation and storage.",
            file=sys.stderr,
        )
        return 1


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminate)
    raise SystemExit(main())
