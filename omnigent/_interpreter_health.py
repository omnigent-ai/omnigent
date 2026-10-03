"""Detect standard-library import failures and explain them without an Omnigent crash report.

When the interpreter's own standard library cannot be imported, nothing Omnigent ships
can repair it, yet Python prints a dependency traceback and the crash handler offers to
file a bug. These helpers recognise that failure and name the real cause instead.
"""

from __future__ import annotations

import linecache
import os
import re
import sys
import sysconfig
import traceback
from types import TracebackType
from typing import NamedTuple, TextIO

_IMPORT_LINE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import\b|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))"
)


class BrokenStdlibModule(NamedTuple):
    """The standard-library module whose body raised, and the line that did."""

    name: str
    filename: str
    lineno: int


def _is_stdlib_name(name: object) -> bool:
    return isinstance(name, str) and name.partition(".")[0] in sys.stdlib_module_names


def _line_imports(filename: str, lineno: int, top: str) -> bool:
    """Whether the import statement at ``filename:lineno`` requests a module in package ``top``."""
    match = _IMPORT_LINE.match(linecache.getline(filename, lineno))
    if match is None:
        return False
    names = [match.group(1)] if match.group(1) else match.group(2).split(",")
    return any(name.strip().partition(".")[0] == top for name in names)


def broken_stdlib_module(
    exc: BaseException, tb: TracebackType | None = None
) -> BrokenStdlibModule | None:
    """Return the stdlib module that failed to import, or ``None`` for an ordinary crash.

    The innermost module body on the traceback (``tb``, else ``exc.__traceback__``) must be
    a stdlib module; Omnigent or a dependency importing a module this platform lacks is
    still a bug there. Then: an ``ImportError`` naming another stdlib module (``tty``
    without ``termios``) is a platform gap, not damage; one naming a non-stdlib module that
    the stdlib line itself imports is damage whoever raised it, because a healthy stdlib
    never imports such a module; anything else is damage only when every deeper frame is
    stdlib code, so a third-party import hook failing on its own dependency stays a
    reportable crash.
    """
    innermost: BrokenStdlibModule | None = None
    deeper: TracebackType | None = None
    tb = tb if tb is not None else exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_code.co_name == "<module>":
            name = frame.f_globals.get("__name__")
            innermost = None
            # The entry script's own body runs as ``__main__``; it is never stdlib.
            if isinstance(name, str) and name != "__main__" and _is_stdlib_name(name):
                innermost = BrokenStdlibModule(name, frame.f_code.co_filename, tb.tb_lineno)
                deeper = tb.tb_next
        tb = tb.tb_next
    if innermost is None:
        return None
    if isinstance(exc, ImportError) and exc.name:
        top = exc.name.partition(".")[0]
        if top in sys.stdlib_module_names:
            return None
        if _line_imports(innermost.filename, innermost.lineno, top):
            return innermost
    while deeper is not None:
        if not _is_stdlib_name(deeper.tb_frame.f_globals.get("__name__")):
            return None
        deeper = deeper.tb_next
    return innermost


def _inside_stdlib(filename: str) -> bool:
    """Whether ``filename`` is the base interpreter's own copy rather than a shadowing one."""
    if not filename or filename.startswith("<"):
        return True
    real = os.path.normcase(os.path.realpath(filename))
    if {"site-packages", "dist-packages"} & set(real.split(os.sep)):
        return False
    # Inside a venv only ``platstdlib`` points at the venv; redirect it to the base interpreter.
    paths = sysconfig.get_paths(vars={"platbase": sys.base_exec_prefix})
    return any(
        real.startswith(os.path.normcase(os.path.realpath(paths[key])) + os.sep)
        for key in ("stdlib", "platstdlib")
        if paths.get(key)
    )


def render_broken_stdlib_notice(
    exc: BaseException, module: BrokenStdlibModule, stream: TextIO | None = None
) -> None:
    """Explain that the interpreter, not Omnigent, is broken, and how to recover."""
    out = stream if stream is not None else sys.stderr
    version = ".".join(str(part) for part in sys.version_info[:3])
    lines = [
        "",
        "Omnigent cannot run: this Python installation's standard library cannot be imported.",
        "",
        f"  Python {version} ({sys.base_prefix})",
        f"  failed while importing its own module {module.name}:",
        f'    File "{module.filename}", line {module.lineno}',
    ]
    source = linecache.getline(module.filename, module.lineno).strip()
    if source:
        lines.append(f"      {source}")
    lines.extend(
        f"    {line.rstrip()}"
        for line in traceback.format_exception_only(type(exc), exc)
        if line.strip()
    )
    lines.append("")
    if _inside_stdlib(module.filename):
        lines += [
            "This is a problem with the Python installation, not with Omnigent, so there is",
            "nothing to report as an Omnigent bug. Repair or reinstall Python, then run",
            "omnigent again.",
        ]
    else:
        stdlib_dir = sysconfig.get_paths()["stdlib"]
        lines += [
            f"That file is outside this Python installation's standard library ({stdlib_dir}),",
            "so something on PYTHONPATH or in the working directory shadows the real module.",
            "There is nothing to report as an Omnigent bug; remove or fix that copy, then run",
            "omnigent again.",
        ]
    lines.append("")
    out.write("\n".join(lines) + "\n")
    out.flush()


def exit_if_stdlib_broken(exc: BaseException) -> None:
    """Exit 1 with the notice when the interpreter is at fault; otherwise return."""
    module = broken_stdlib_module(exc)
    if module is None:
        return
    render_broken_stdlib_notice(exc, module)
    raise SystemExit(1)
