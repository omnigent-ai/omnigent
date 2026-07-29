"""Presentation layer for crash reporting.

Renders a calm, less-scary crash screen in place of Python's default
wall-of-red traceback: an amber header, the report path, then the
traceback de-emphasized underneath (shortened paths, collapsed library
frames, muted-gray stack frames, the final exception line in bold).

Everything visual — ANSI color, Unicode glyphs, emoji — is TTY-gated
and degrades to plain ASCII when output is piped or the terminal
can't render it, so log files and CI captures stay clean.

This module is purely presentational; crash mechanics (report
building, file saving, clipboard, browser, the bug-filing prompt)
live in :mod:`omnigent.crash_handler`. Keeping them separate means the
look-and-feel can be tuned without touching crash logic.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import traceback as _tb
from pathlib import Path
from typing import TextIO

# --------------------------------------------------------------------------- #
# ANSI — applied only on a TTY; empty strings otherwise so piped/CI
# output contains no escape codes.
# --------------------------------------------------------------------------- #
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_AMBER = "\033[33m"
_AMBER_BOLD = "\033[33;1m"
_GRAY = "\033[90m"


def real_stderr() -> TextIO:
    """Return the user's real terminal stderr.

    ``omnigent.cli_diagnostics.setup_cli_logging`` may replace
    ``sys.stderr`` with a wrapper that tees into the CLI diagnostics
    log; the original terminal is stashed on ``_original_stderr``. The
    crash screen must land on the actual terminal, not get buried in a
    log file, so reach through that attribute when present.
    """
    return getattr(sys.stderr, "_original_stderr", sys.stderr)


def _is_tty(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _color(stream: TextIO) -> bool:
    """Whether to emit ANSI color: only on a real, color-capable TTY."""
    if not _is_tty(stream):
        return False
    # Respect NO_COLOR (https://no-color.org/) and explicit disable.
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("CLICOLOR") == "0" or os.environ.get("CLICOLOR_FORCE") == "0":
        return False
    return True


def _supports_unicode(stream: TextIO) -> bool:
    """Best-effort probe for box-drawing + emoji rendering.

    Returns False for ASCII/Latin-only encodings and dumb terminals so
    we fall back to plain ASCII glyphs rather than emitting tofu (U+FFFD)
    squares. Emoji is gated more conservatively than box-drawing (see
    :func:`_supports_emoji`) since it renders inconsistently.
    """
    enc = (getattr(stream, "encoding", None) or "").lower()
    if enc and "utf" not in enc and "utf" not in enc.replace("-", ""):
        return False
    term = os.environ.get("TERM", "")
    if term in ("", "dumb"):
        return False
    return True


def _supports_emoji(stream: TextIO) -> bool:
    """Emoji rendering is less universal than box-drawing — gate it hard.

    We require a UTF-capable TTY and common modern terminal families
    where emoji glyphs are known to render. When in doubt, fall back to
    a plain ``!`` so we never print a missing-glyph square as the very
    first thing a distressed user sees.
    """
    if not _is_tty(stream):
        return False
    enc = (getattr(stream, "encoding", None) or "").lower()
    if "utf" not in enc and "utf" not in enc.replace("-", ""):
        return False
    term = os.environ.get("TERM", "").lower()
    if term in ("", "dumb", "linux"):
        return False
    # WSL/ConEmu/Windows Terminal set WT_SESSION / WT_PROFILE; classic
    # cmd.exe (TERM unset, no WT_*) is iffy for emoji — require a sign.
    if sys.platform == "win32" and not os.environ.get("WT_SESSION"):
        return False
    return True


def _term_width(default: int = 80) -> int:
    """Current terminal column count (best-effort)."""
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:  # noqa: BLE001  pragma: no cover
        return default


# --------------------------------------------------------------------------- #
# Rendering primitives
# --------------------------------------------------------------------------- #


def _site_packages_dir() -> str | None:
    """Best-effort locate the active venv's ``site-packages`` dir.

    Used to classify frames as "library" (collapse) vs "first-party" (show):
    frames under site-packages are click / yaml / etc. internals that scare
    users and aren't actionable on screen — they still go into the saved
    report in full.
    """
    for entry in sys.path:
        if entry and entry.endswith("site-packages") and os.path.isdir(entry):
            return os.path.abspath(entry)
    return None


def _shorten_path(filename: str, *, cwd: str, site_packages: str | None) -> str:
    """Make a traceback file path compact for on-screen display.

    * under site-packages → package-relative (``click/core.py``)
    * under cwd           → relative (``omnigent/cli.py``)
    * under $HOME         → ``~/...``
    * otherwise           → unchanged (rare; better verbose than wrong)
    """
    with contextlib.suppress(Exception):
        filename = os.path.abspath(filename)
    if site_packages:
        sp = site_packages + os.sep
        if filename.startswith(sp):
            return filename[len(sp) :]
    if cwd:
        cd = cwd + os.sep
        if filename.startswith(cd):
            return filename[len(cd) :]
    home = str(Path.home())
    if home and filename.startswith(home + os.sep):
        return "~" + filename[len(home) :]
    return filename


def _frame_pkg(short_path: str) -> str:
    """Top-level package name of a site-packages-relative path."""
    parts = short_path.replace("\\", "/").split("/")
    return parts[0] if parts and parts[0] else short_path


def _resolve_top_package(filename: str) -> str | None:
    """Return the top-level package/dir name a frame's file belongs to.

    Matches the absolute path against ``sys.path`` entries (longest prefix
    wins) and takes the first path component after the match. This works
    for editable installs (``sdks/python-client/omnigent_client/foo.py`` →
    ``omnigent_client``) and for wheel installs into site-packages
    (``site-packages/omnigent_client/foo.py`` → ``omnigent_client``).

    Used to tell first-party packages apart from third-party libs even
    when both live under ``site-packages`` in a distributed wheel —
    without this, our own SDK packages would be wrongly collapsed.
    """
    try:
        abspath = os.path.abspath(filename)
    except Exception:  # noqa: BLE001  pragma: no cover
        return None
    best_entry, best_len = None, -1
    for entry in sys.path:
        if not entry:
            continue
        try:
            e = os.path.abspath(entry)
        except Exception:  # noqa: BLE001  pragma: no cover
            continue
        if abspath.startswith(e + os.sep) and len(e) > best_len:
            best_entry, best_len = e, len(e)
    if best_entry is None:
        return None
    rel = abspath[len(best_entry) + len(os.sep) :]
    top = rel.split(os.sep, 1)[0]
    if top.endswith(".py"):
        top = top[:-3]
    return top or None


# Default first-party prefix. Frames whose top-level package equals this
# or starts with ``<prefix>_`` are always shown (never collapsed), even
# when installed under site-packages in a distributed wheel. Covers the
# three core packages — ``omnigent``, ``omnigent_client``,
# ``omnigent_ui_sdk`` — plus the ``omnigent_slack`` integration.
_DEFAULT_FIRST_PARTY_PREFIXES = ("omnigent",)


def _is_first_party_pkg(pkg: str | None, prefixes: tuple[str, ...]) -> bool:
    """True when *pkg* is an own-code package (exact or ``<prefix>_``)."""
    if not pkg:
        return False
    return any(pkg == p or pkg.startswith(p + "_") for p in prefixes)


def _full_traceback_env() -> bool:
    """``OMNIGENT_FULL_TRACEBACK=1`` disables library-frame collapsing."""
    return os.environ.get("OMNIGENT_FULL_TRACEBACK", "").strip().lower() in ("1", "true", "yes")


def format_traceback(
    exc: BaseException,
    tb,
    *,
    colored: bool,
    unicode_ok: bool,
    first_party_prefixes: tuple[str, ...] = _DEFAULT_FIRST_PARTY_PREFIXES,
) -> str:
    """Render an exception + traceback with calm, compact styling.

    Two transforms make the wall-of-red readable:

    1. **Path shortening** — venv ``site-packages`` paths become
       package-relative (``click/core.py``), the cwd becomes relative
       (``omnigent/cli.py``), ``$HOME`` becomes ``~``. Long absolute
       paths are the biggest source of visual noise.

    2. **Library-frame collapsing** — contiguous frames inside
       ``site-packages`` (click, yaml, …) are replaced with one dim
       summary line ``⋯ N frames hidden in <pkgs>  (see the saved
       report)``. First-party frames (the user's own code) stay visible.
       The full, unmodified traceback always lives in the saved report.

    The ``Traceback (most recent call last):`` banner and all shown
    frames are muted gray; the final exception line is bold so the eye
    lands on the one line that matters. Set ``OMNIGENT_FULL_TRACEBACK=1``
    to disable collapsing (power users / library-bug debugging).
    """
    gray = _GRAY if colored else ""
    bold = _BOLD if colored else ""
    dim = _DIM if colored else ""
    reset = _RESET if colored else ""
    ell = "⋯" if unicode_ok else "..."

    cwd = os.getcwd()
    site_packages = _site_packages_dir()
    collapse_libs = not _full_traceback_env() and site_packages is not None

    frames = _tb.extract_tb(tb or (exc.__traceback__ if exc else None))

    # Group contiguous frames into runs of "first" (own code) / "lib".
    # Each item carries the rendered source line (if any) for context.
    groups: list[tuple[str, list[tuple[str, str, str]]]] = []  # (kind, [(name, short, src)])
    for fr in frames:
        short = _shorten_path(fr.filename, cwd=cwd, site_packages=site_packages)
        abspath = os.path.abspath(fr.filename)
        under_sp = bool(site_packages and abspath.startswith(site_packages + os.sep))
        under_cwd = bool(cwd and abspath.startswith(cwd + os.sep))
        top_pkg = _resolve_top_package(fr.filename)
        # "Own code" = under the project root BUT not inside a venv's
        # site-packages (in a dev checkout the venv lives under the repo,
        # so every library frame would otherwise count as first-party),
        # OR a first-party package by name (catches the SDK packages even
        # when installed into site-packages in a shipped wheel).
        is_own = (under_cwd and not under_sp) or _is_first_party_pkg(top_pkg, first_party_prefixes)
        # Only collapse genuine third-party library frames (under
        # site-packages AND not our own packages). Own code is always
        # shown — even when installed in site-packages, as the SDKs are
        # in a distributed wheel.
        is_lib = collapse_libs and under_sp and not is_own
        kind = "lib" if is_lib else "first"
        src = f"{fr.lineno}: {fr.line}" if fr.line else f"{fr.lineno}"
        if groups and groups[-1][0] == kind:
            groups[-1][1].append((fr.name, short, src))
        else:
            groups.append((kind, [(fr.name, short, src)]))

    out: list[str] = [f"{gray}Traceback (most recent call last):{reset}"]
    for kind, items in groups:
        if kind == "first":
            for name, short, src in items:
                out.append(
                    f'{gray}  File "{short}", line {src.split(":", 1)[0]}, in {name}{reset}'
                )
                if ":" in src:
                    out.append(f"{gray}    {src.split(':', 1)[1].strip()}{reset}")
        else:
            pkgs = sorted({_frame_pkg(s) for _, s, _ in items})
            label = ", ".join(pkgs)
            out.append(
                f"{dim}  {ell} {len(items)} frames hidden in {label}  "
                f"(see the saved report){reset}"
            )

    # The final exception lines (handles multi-line messages like yaml's
    # "in <unicode string>, line 1, column 9: ...").
    exc_lines = _tb.format_exception_only(type(exc), exc)
    for i, line in enumerate(exc_lines):
        line = line.rstrip("\n")
        if not line:
            continue
        # First line bold (the ``Etype: msg``); continuation (e.g. the
        # yaml snippet) stays default so it reads as a quote.
        if i == 0:
            out.append(f"{bold}{line}{reset}")
        else:
            out.append(line)
    return "\n".join(out)


def _title(name: str) -> str:
    """Capitalize the app name for sentence display (``omnigent`` → ``Omnigent``)."""
    return name[:1].upper() + name[1:] if name else name


def render_crash_screen(
    *,
    app_name: str,
    report_path: str,
    exc: BaseException,
    tb=None,
    stream: TextIO | None = None,
    first_party_prefixes: tuple[str, ...] = _DEFAULT_FIRST_PARTY_PREFIXES,
) -> None:
    """Print the static crash screen to ``stream`` (default: real stderr).

    Layout (non-interactive — path at top, no prompt follows)::

        <blank>
        <amber> ⚠️  <App> ran into an issue. </amber>
        <indented: report path>
        <dim>─── technical details ───</dim>
        <compact traceback>

    Layout (interactive — path deferred to the end, next to the prompt)::

        <blank>
        <amber> ⚠️  <App> ran into an issue. </amber>
        <dim>─── technical details ───</dim>
        <compact traceback>
        <indented: report path>   ← printed last, right before the [Y/n] prompt

    The interactive "file a bug?" prompt is intentionally NOT part of
    this static screen — :mod:`omnigent.crash_handler` owns that, so it
    can gate it on stdin/stdin TTY and handle non-interactive contexts.
    """
    stream = stream if stream is not None else real_stderr()
    colored = _color(stream)
    on_tty = _is_tty(stream)
    unicode_ok = _supports_unicode(stream) if on_tty else False
    tb_text = format_traceback(
        exc,
        tb,
        colored=colored,
        unicode_ok=unicode_ok,
        first_party_prefixes=first_party_prefixes,
    )
    display = _title(app_name)

    if not on_tty:
        # Piped / CI / log file: plain text, no box, no emoji, no color.
        # Path at the top here since there's no interactive prompt after.
        lines = [
            "",
            f"{display} ran into an issue.",
            "",
            "A crash report was saved to:",
            f"  {report_path}",
            "",
            "--- technical details ---",
            tb_text,
            "",
        ]
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        return

    # Interactive terminal: header + traceback first, then the report
    # path printed LAST so it sits right above the [Y/n] prompt (which
    # crash_handler prints next) — the user sees the path when they need
    # it, not scrolled away above the traceback.
    stream.write("\r\033[?25h\033[2K")
    w = max(40, min(_term_width() - 4, 78))
    icon = "⚠️  " if _supports_emoji(stream) else "!  "
    header = f"{icon}{display} ran into an issue."
    sep = "─" * 3 + " technical details " + "─" * max(0, w - 3 - len(" technical details ") - 3)

    lines: list[str] = [""]
    lines.append(f"{_AMBER_BOLD if colored else ''}{header}{_RESET if colored else ''}")
    lines.append("")
    lines.append(f"{_DIM if colored else ''}{sep}{_RESET if colored else ''}")
    lines.append(tb_text)
    lines.append("")
    # Report path at the very end — next to the prompt that follows.
    lines.append("  A crash report was saved to:")
    lines.append(f"  {report_path}")
    lines.append("")

    stream.write("\n".join(lines) + "\n")
    stream.flush()
