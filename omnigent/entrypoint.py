"""Lightweight console dispatch before runtime imports or CLI startup effects."""

from __future__ import annotations

import sys


def offline_arguments(argv: list[str]) -> list[str] | None:
    """Recognize validate, rejecting runtime root flags rather than running them."""
    index = 0
    while index < len(argv) and argv[index] in {"--debug", "--log-to-stderr", "--profiling"}:
        index += 1
    if index < len(argv) and argv[index] == "validate":
        return [*argv[:index], *argv[index + 1 :]]
    return None


def isolate_offline_imports(*, package_init: bool = False) -> None:
    """Keep the working directory and input bundle out of dependency lookup.

    Called before package initialization too: ``python -m omnigent`` otherwise
    allows a bundle's ``hashlib.py``/``yaml.py`` to shadow trusted dependencies.
    Only lexical path operations are used, never filesystem or remote probing.
    """
    args = offline_arguments(sys.argv[1:])
    if args is None:
        return
    import os

    if package_init:
        program = os.path.basename(sys.argv[0]).lower()
        module_cli = (
            program == "-m"
            and "-m" in sys.orig_argv
            and sys.orig_argv[sys.orig_argv.index("-m") + 1 :] == ["omnigent", *sys.argv[1:]]
        )
        if not module_cli and program not in {"omnigent", "omni", "omnigent.exe", "omni.exe"}:
            return
    sys.dont_write_bytecode = True
    roots = {os.path.normcase(os.getcwd())}
    for arg in args:
        if not arg or arg.startswith(("-", "~", "//", "\\\\")) or "://" in arg:
            continue
        try:
            path = os.path.normcase(os.path.abspath(arg))
        except (OSError, ValueError):
            continue  # The command reports invalid arguments; this only limits imports.
        roots.add(path)
        if path.endswith((".yaml", ".yml")):
            roots.add(os.path.dirname(path))
    trusted_paths = []
    for entry in sys.path:
        path = os.path.normcase(os.path.abspath(entry))
        # A project's .venv is a trusted interpreter dependency path, not the
        # project import root; retain nested installation paths.
        if path not in roots:
            trusted_paths.append(entry)
    sys.path[:] = trusted_paths


def main() -> None:
    args = offline_arguments(sys.argv[1:])
    if args is not None:
        isolate_offline_imports()
        from omnigent.cli_validate import validate_command

        validate_command.main(args=args, prog_name="omnigent validate")
    else:
        from omnigent.cli import main as runtime_main

        runtime_main()
