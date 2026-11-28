"""Keep configuration-generated helper entrypoints outside disposable tool caches."""

from __future__ import annotations

import functools
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from filelock import FileLock

_INSTALL_TIMEOUT_SECONDS = 120


def persistent_tool_command(resolve: Callable[[], list[str]]) -> Callable[[], list[str]]:
    """Defer a pinned tool installation until the returned command is executed.

    Existing single-executable commands retain their selected installation.
    A ``uvx --from SOURCE TOOL`` prefix instead installs that exact source in
    an owned tool environment before configuring any generated entrypoints.
    """

    @functools.wraps(resolve)
    def command() -> list[str]:
        prefix = resolve()
        if (
            len(prefix) != 4
            or Path(prefix[0]).name not in {"uvx", "uvx.exe"}
            or prefix[1] != "--from"
        ):
            return prefix
        from omnigent.config import global_config_path

        source, name = prefix[2:]
        revision = hashlib.sha256(source.encode()).hexdigest()[:16]
        root = global_config_path().absolute().parent / "tools" / f"{name}-{revision}"
        return [sys.executable, "-I", str(Path(__file__).resolve()), str(root), source, name]

    return command


def prepare_tool(root: Path, source: str, name: str) -> Path:
    """Install one selected helper persistently, retaining package mirror settings."""
    executable = root / "bin" / (f"{name}.exe" if os.name == "nt" else name)
    if executable.is_file() and os.access(executable, os.X_OK):
        return executable
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(str(root / ".install.lock"), timeout=_INSTALL_TIMEOUT_SECONDS, mode=0o600):
        if executable.is_file() and os.access(executable, os.X_OK):
            return executable
        installer = shutil.which("uv")
        if installer is None:
            raise RuntimeError(
                "Install uv, then retry configuration to install its credential helper."
            )
        environment = {
            **os.environ,
            "UV_TOOL_DIR": str(root / "environments"),
            "UV_TOOL_BIN_DIR": str(executable.parent),
        }
        subprocess.run(
            [installer, "tool", "install", "--force", "--link-mode", "copy", source],
            env=environment,
            check=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError(
                "The selected credential helper was not installed; retry configuration."
            )
    return executable


def main(argv: Sequence[str] | None = None) -> int:
    """Configure with a durable entrypoint and make generated auth select it."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 3:
        print(
            "Expected tool directory, package source, executable name, and arguments.",
            file=sys.stderr,
        )
        return 2
    root, source, name, *tool_args = arguments
    try:
        executable = prepare_tool(Path(root), source, name)
        environment = dict(os.environ)
        environment["PATH"] = os.pathsep.join(
            [str(executable.parent), environment.get("PATH", "")]
        )
        os.execve(str(executable), [str(executable), *tool_args], environment)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Credential helper configuration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
