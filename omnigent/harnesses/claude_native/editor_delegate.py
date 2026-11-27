"""Preserve an editor's arguments, environment, terminal and exit behavior."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

_MAX_MANIFEST_BYTES = 1024 * 1024
_EDITOR_ENVIRONMENT = {"EDITOR", "VISUAL"}


def _read_delegate(path: Path) -> tuple[list[str], dict[str, str]]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _MAX_MANIFEST_BYTES
        ):
            raise ValueError("Invalid editor delegation file")
        raw = os.read(descriptor, _MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("Editor delegation file is too large")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise ValueError("Unsupported editor delegation")
    supplied_command = value.get("command")
    if not isinstance(supplied_command, list) or not supplied_command:
        raise ValueError("Missing editor command")
    command: list[str] = []
    for argument in supplied_command:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ValueError("Invalid editor argument")
        command.append(argument)
    if not command[0]:
        raise ValueError("Missing editor executable")
    restored = value.get("restore_environment")
    injected = value.get("injected_environment")
    if (
        not isinstance(restored, dict)
        or not isinstance(injected, dict)
        or restored.keys() != injected.keys()
    ):
        raise ValueError("Missing original editor environment")
    environment = dict(os.environ)
    for name, original in restored.items():
        if name not in _EDITOR_ENVIRONMENT:
            raise ValueError("Unexpected editor environment field")
        supplied = injected[name]
        if not isinstance(supplied, str) or "\x00" in supplied:
            raise ValueError("Invalid injected editor environment")
        if original is not None and (not isinstance(original, str) or "\x00" in original):
            raise ValueError("Invalid original editor environment")
        if environment.get(name) == supplied:
            if original is None:
                environment.pop(name, None)
            else:
                environment[name] = original
    prefix = value.get("path_prefix")
    if prefix is not None:
        if not isinstance(prefix, str) or not prefix or "\x00" in prefix:
            raise ValueError("Invalid editor path prefix")
        current_path = environment.get("PATH")
        if current_path is not None:
            environment["PATH"] = os.pathsep.join(
                part for part in current_path.split(os.pathsep) if part != prefix
            )
    return command, environment


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    try:
        command, environment = _read_delegate(args.manifest)
        os.execvpe(command[0], [*command, *arguments], environment)
    except (OSError, UnicodeError, ValueError):
        print(
            "Could not open the configured editor. Check its command and retry.", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
