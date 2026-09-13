"""Lightweight console dispatch before runtime imports or CLI startup effects."""

from __future__ import annotations

import sys


def offline_arguments(argv: list[str]) -> list[str] | None:
    """Recognize validate, rejecting runtime root flags rather than running them."""
    index = 0
    while index < len(argv) and argv[index] in {"--debug", "--log-to-stderr", "--profiling"}:
        index += 1
    command_index = index + 1 if index < len(argv) and argv[index] == "--" else index
    if command_index < len(argv) and argv[command_index] == "validate":
        return [*argv[:index], *argv[command_index + 1 :]]
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

    def normalized(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    # Bootstrap metadata discovery from the running interpreter's stdlib, never
    # a bundle's sysconfig.py. Do not trust a path merely named "site-packages".
    stdlib = getattr(sys, "_stdlib_dir", None) or os.path.dirname(os.__file__)
    trusted = {
        normalized(stdlib),
        normalized(os.path.join(stdlib, "lib-dynload")),
        normalized(
            os.path.join(
                os.path.dirname(stdlib),
                f"python{sys.version_info.major}{sys.version_info.minor}.zip",
            )
        ),
    }
    if os.name == "nt":
        trusted.add(normalized(os.path.join(sys.base_exec_prefix, "DLLs")))
    original_paths = [(entry, normalized(entry)) for entry in sys.path]

    def filtered_paths() -> list[str]:
        return [
            entry
            for entry, path in original_paths
            if path in trusted
            or not any(
                path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots
            )
        ]

    sys.path[:] = filtered_paths()
    import sysconfig

    trusted.update(
        normalized(sysconfig.get_path(name))
        for name in ("stdlib", "platstdlib", "purelib", "platlib")
    )
    extension_path = sysconfig.get_config_var("DESTSHARED")
    if isinstance(extension_path, str):
        trusted.add(normalized(extension_path))
    site = sys.modules.get("site")
    if site is not None:
        trusted.update(normalized(path) for path in site.getsitepackages())
        if site.ENABLE_USER_SITE and site.USER_SITE:
            trusted.add(normalized(site.USER_SITE))
    sys.path[:] = filtered_paths()


def main() -> None:
    args = offline_arguments(sys.argv[1:])
    if args is not None:
        isolate_offline_imports()
        from omnigent.cli_validate import validate_command

        validate_command.main(args=args, prog_name="omnigent validate")
    else:
        from omnigent.cli import main as runtime_main

        runtime_main()
