"""Friendly crash reporting with one-tap GitHub issue filing.

Replaces Python's default wall-of-red traceback with a calm, branded
crash screen (see :mod:`omnigent.crash_ui`) and lets the user file a
GitHub issue from the repo's pre-filled bug-report template.

Design notes
------------
* **No token is shipped.** We can't embed a GitHub credential in a
  distributed binary — anyone could extract it. Instead we open the
  repo's bug-report template in the browser with the title, version,
  OS, and the full traceback pre-filled into the Description field via
  URL query params. The clipboard carries the full report as a backup
  in case the URL is too long and the description gets truncated.
* **One chokepoint.** ``sys.excepthook`` (main thread) +
  ``threading.excepthook`` (background threads) + ``faulthandler``
  (C-level segfaults, captured to a file since the process is already
  dying) cover every normal crash path.
* **TTY-aware.** The interactive "file a bug?" prompt only runs when
  stdin AND stderr are real TTYs, so scripts/CI never hang — they get
  the saved report path and the issue link printed plainly.
* **KeyboardInterrupt / SystemExit** are not crashes: they defer to the
  original hooks so Ctrl-C and normal exits behave exactly as before.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from pathlib import Path
from typing import TextIO

from omnigent.crash_ui import real_stderr, render_crash_screen
from omnigent.process_logging import data_dir

# Saved at install time so we can defer to the originals for
# KeyboardInterrupt / SystemExit and as a last-resort fallback if our
# own handler misfires.
_ORIG_EXCEPTHOOK = sys.__excepthook__
_ORIG_THREADING_EXCEPTHOOK = threading.excepthook

# Runtime configuration, populated by install_crash_handler().
_CONFIG: dict = {
    "app_name": "omnigent",
    "repo": "omnigent-ai/omnigent",
    "version": "unknown",
    "crashes_dir": None,
    "keep_reports": 10,
}

# Reentrancy guard: if handling a crash itself raises, we must not
# recurse forever through excepthook. Per-thread so a crash in one
# thread can't block another's reporting.
_HANDLING = threading.local()

# File handle kept open for faulthandler so C-level segfaults dump to a
# file (out of the terminal) instead of screaming at the user.
_FH_FILE: object | None = None

# Maximum total URL length for the pre-filled GitHub issue link. GitHub
# and some browsers reject or silently truncate very long URLs (a deep
# recursion crash can produce a many-KB traceback). 8000 chars is the
# widely-cited safe limit for cross-browser compatibility; the clipboard
# always carries the full report as a backup when we truncate.
_MAX_URL_LENGTH = 8000


# --------------------------------------------------------------------------- #
# Installation
# --------------------------------------------------------------------------- #
def install_crash_handler(
    app_name: str,
    repo: str,
    *,
    version: str | None = None,
    crashes_dir: str | Path | None = None,
    keep_reports: int = 10,
    enable_faulthandler: bool = True,
    first_party_prefixes: tuple[str, ...] = ("omnigent",),
) -> None:
    """Install the friendly crash handler.

    Call once, as early as possible in the binary's entrypoint, so
    unhandled exceptions anywhere downstream are caught.

    :param app_name:  Human name shown in the crash header (``omnigent``).
    :param repo:      ``owner/repo`` for the GitHub issues URL.
    :param version:   App version string for the report. Defaults to
                      ``omnigent.version.VERSION``.
    :param crashes_dir: Where to write ``crash-*.md`` reports. Defaults
                      to ``<data-dir>/crashes`` (honors
                      ``OMNIGENT_DATA_DIR``).
    :param keep_reports: Rotate to keep at most this many crash reports.
    :param enable_faulthandler: Capture C-level segfaults to a file
                      (off the terminal) instead of the default stderr
                      dump.
    :param first_party_prefixes: Top-level package prefixes treated as
                      own code in the compact traceback (always shown,
                      never collapsed — even when installed under
                      site-packages in a distributed wheel). Defaults to
                      ``("omnigent")``, which covers the three core
                      packages (``omnigent``, ``omnigent_client``,
                      ``omnigent_ui_sdk``) via the ``<prefix>_`` rule.
    """
    _CONFIG.update(
        app_name=app_name,
        repo=repo,
        version=version if version is not None else _read_version(),
        crashes_dir=str(crashes_dir) if crashes_dir else None,
        keep_reports=keep_reports,
        first_party_prefixes=tuple(first_party_prefixes) or ("omnigent",),
    )
    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook
    if enable_faulthandler:
        _enable_faulthandler()


def _read_version() -> str:
    try:
        from omnigent.version import VERSION

        return VERSION
    except Exception:  # noqa: BLE001  pragma: no cover
        return "unknown"


def _crashes_dir() -> Path:
    override = _CONFIG.get("crashes_dir")
    if override:
        return Path(override).expanduser()
    return data_dir() / "crashes"


def _enable_faulthandler() -> None:
    """Route C-level segfault dumps to a file, off the terminal.

    Our Python excepthook can't run after a segfault (the process is
    already dying), so we can't make that pretty or interactive. But we
    can at least keep the faulthandler dump out of the user's terminal
    by redirecting it to ``<crashes>/faulthandler.log``.
    """
    global _FH_FILE
    try:
        import faulthandler

        # Close any handle left open by a previous install (e.g. repeated
        # ``main()`` calls in the test suite) so we never leak file
        # descriptors across reinstalls.
        if _FH_FILE is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                _FH_FILE.close()
        d = _crashes_dir()
        d.mkdir(parents=True, exist_ok=True)
        # Intentionally held open for the process lifetime: faulthandler
        # writes here on a segfault, when we can't run cleanup code.
        f = open(d / "faulthandler.log", "ab", buffering=0)  # noqa: SIM115
        faulthandler.enable(file=f, all_threads=True)
        _FH_FILE = f
    except Exception:  # noqa: BLE001  pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #
def _excepthook(etype, value, tb) -> None:
    # Ctrl-C and normal exits are not crashes — defer to the originals.
    if issubclass(etype, (KeyboardInterrupt, SystemExit)):
        _ORIG_EXCEPTHOOK(etype, value, tb)
        return
    handle_crash(value, tb=tb, source="uncaught")
    # After an uncaught exception in the main thread the interpreter
    # exits with code 1 once this hook returns — no explicit exit needed.


def _threading_excepthook(args) -> None:
    if issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
        _ORIG_THREADING_EXCEPTHOOK(args)
        return
    # A crashed background thread shouldn't block the main thread with
    # an interactive prompt (racy and surprising). Save the report and
    # print a concise notice + link instead.
    name = getattr(getattr(args, "thread", None), "name", "?")
    handle_crash(
        args.exc_value,
        tb=args.exc_traceback,
        source=f"thread:{name}",
        interactive=False,
    )


# --------------------------------------------------------------------------- #
# Core: render screen, save report, offer to file a bug
# --------------------------------------------------------------------------- #
def handle_crash(
    exc: BaseException,
    *,
    tb=None,
    source: str = "uncaught",
    interactive: bool | None = None,
) -> None:
    """Handle one uncaught exception end-to-end.

    1. Build + save a GitHub-ready crash report (rotated).
    2. Print the calm crash screen + de-emphasized traceback.
    3. If interactive, prompt to file a GitHub issue; on yes, open a
       pre-filled bug-report page (traceback, version, OS in the URL).
       The clipboard gets the full report as a backup.

    Reentrant-safe: a crash inside this function falls back to the
    original excepthook rather than looping forever.
    """
    if getattr(_HANDLING, "on", False):
        return
    _HANDLING.on = True
    try:
        stream = real_stderr()
        # Full, unmodified traceback for the saved report (real paths, every
        # frame — what a developer needs to debug). The on-screen version is
        # a compacted view of the same exception (shortened paths, collapsed
        # library frames); see omnigent/crash_ui.render_crash_screen.
        formatted = traceback.format_exception(type(exc), exc, tb or exc.__traceback__)
        tb_text = "".join(formatted)

        report_md = _build_report(exc, tb_text, source=source)
        report_path = _save_report(report_md)

        render_crash_screen(
            app_name=_CONFIG["app_name"],
            report_path=str(report_path),
            exc=exc,
            tb=tb or exc.__traceback__,
            stream=stream,
            first_party_prefixes=_CONFIG.get("first_party_prefixes", ("omnigent",)),
        )

        if interactive is None:
            interactive = _is_interactive(stream)
        if interactive:
            _interactive_flow(report_md, report_path, exc, tb_text, stream)
        else:
            _fallback_notice(report_path, exc, tb_text, stream)
    except Exception:  # noqa: BLE001 — crash handler must never crash visibly
        # Never let the crash handler itself crash visibly.
        with contextlib.suppress(Exception):
            _ORIG_EXCEPTHOOK(type(exc), exc, tb or exc.__traceback__)
    finally:
        _HANDLING.on = False


def _is_interactive(stream: TextIO) -> bool:
    """Interactive only when BOTH stderr and stdin are real TTYs.

    This avoids hanging in scripts/CI where stderr happens to be a TTY
    but stdin is closed or piped.
    """
    stderr_tty = bool(getattr(stream, "isatty", lambda: False)())
    stdin_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    return stderr_tty and stdin_tty


# --------------------------------------------------------------------------- #
# Report building + persistence
# --------------------------------------------------------------------------- #
# Light redaction of common bearer/token shapes that could appear on
# the command line (e.g. ``--api-key sk-...``). The user reviews the
# report before posting; this just catches the obvious ones.
_TOKEN_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),  # OpenAI-style
    re.compile(r"dapi-[A-Za-z0-9_\-]{6,}"),  # Anthropic-style
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{6,}"),  # Slack
    re.compile(r"gh[opusr]_[A-Za-z0-9]{16,}"),  # GitHub
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),  # Google API
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{6,}"),  # Authorization header
]


def _redact(text: str) -> str:
    for pat in _TOKEN_PATTERNS:
        text = pat.sub(lambda m: m.group(0)[:4] + "***", text)
    return text


def _command_line() -> str:
    try:
        return _redact(" ".join(sys.argv))
    except Exception:  # noqa: BLE001  pragma: no cover
        return "(unavailable)"


def _build_report(exc: BaseException, tb_text: str, *, source: str) -> str:
    """Compose the GitHub-ready markdown report."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    exc_type = type(exc).__qualname__
    msg = str(exc).strip() or "(no message)"
    version = _CONFIG["version"]
    app_name = _CONFIG["app_name"]
    repo = _CONFIG["repo"]

    return f"""# Crash Report — {app_name}

**Date:** {now}
**Version:** {app_name} {version}
**Platform:** {platform.platform()}
**Python:** {platform.python_version()}
**Source:** {source}
**Repository:** https://github.com/{repo}

## Summary

`{exc_type}`: {msg}

## Traceback

```
{tb_text}
```

## Command

```
{_command_line()}
```

---

> The full traceback is also pre-filled into the GitHub issue's
> Description field when filing from the crash prompt. Review the
> **Command** line above for secrets before submitting.
"""


def _save_report(report_md: str) -> Path:
    """Write the report to the crashes dir (rotated) and return its path."""
    d = _crashes_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"crash-{stamp}.md"
    if path.exists():  # same-second collision — disambiguate by pid.
        path = d / f"crash-{stamp}-{os.getpid()}.md"
    path.write_text(report_md, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    _rotate(d, _CONFIG.get("keep_reports", 10))
    return path


def _rotate(d: Path, keep: int) -> None:
    """Keep only the newest ``keep`` ``crash-*.md`` reports."""
    with contextlib.suppress(Exception):
        files = sorted(
            d.glob("crash-*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[keep:]:
            old.unlink()


# --------------------------------------------------------------------------- #
# Interactive bug-filing flow (TTY only)
# --------------------------------------------------------------------------- #
def _interactive_flow(
    report_md: str, report_path: Path, exc: BaseException, tb_text: str, stream: TextIO
) -> None:
    url, body_included = _issue_url(exc, _issue_body(exc, tb_text))
    if _prompt_yes_no(
        "Help us fix it — file a GitHub issue with this report? [Y/n] ",
        stream,
    ):
        _copy_to_clipboard(report_md)  # always: backup, or primary if body dropped
        opened = _open_browser(url)
        _print(stream, "")
        if opened and body_included:
            _print(stream, "  ✓ Opening a pre-filled GitHub issue — review and submit.")
        elif opened:
            _print(
                stream,
                "  ✓ Opening a GitHub issue — paste the report from your clipboard (Ctrl+V).",
            )
        else:
            _print(stream, "  Couldn't open the browser. Open this link to file the issue:")
            _print(stream, f"    {url}")
            _print(stream, "  The report is in your clipboard (Ctrl+V to paste).")
    else:
        _print(stream, "")
        _print(stream, "  Report saved here:")
        _print(stream, f"    {report_path}")
    _print(stream, "")


def _fallback_notice(report_path: Path, exc: BaseException, tb_text: str, stream: TextIO) -> None:
    """Non-interactive: just state where the report is and where to file."""
    url, _ = _issue_url(exc, _issue_body(exc, tb_text))
    _print(stream, "")
    _print(stream, f"A crash report was saved to: {report_path}")
    _print(stream, f"File an issue: {url}")
    _print(stream, "")


def _issue_body(exc: BaseException, tb_text: str) -> str:
    """Build the markdown for the issue template's Description field.

    Leaner than :func:`_build_report`: the version and OS live in their
    own prefilled template fields, so they're omitted here to avoid
    duplication. What remains is the exception summary, the command
    that triggered the crash, and the **full** traceback (every frame,
    real paths) — exactly what a developer needs to reproduce and fix it.
    """
    exc_type = type(exc).__qualname__
    msg = str(exc).strip() or "(no message)"
    return (
        "This crash was auto-reported by Omnigent's crash handler.\n\n"
        f"**Exception:** `{exc_type}: {msg}`\n\n"
        "**Command:**\n"
        f"```\n{_command_line()}\n```\n\n"
        "**Traceback:**\n"
        f"```\n{tb_text}```\n"
    )


def _issue_url(exc: BaseException, body: str = "") -> tuple[str, bool]:
    """Pre-filled ``bug_report.yml`` issue URL.

    Returns ``(url, body_included)``: the URL has the template, title,
    version, and OS prefilled always; the Description (full traceback)
    is included only if it fits :data:`_MAX_URL_LENGTH`. When the body
    is dropped (``body_included is False``), the caller should tell the
    user to paste the report from the clipboard instead.

    Field IDs (``version``, ``os``, ``description``) come from
    ``.github/ISSUE_TEMPLATE/bug_report.yml``; keep in sync if it changes.
    """
    first_line = (str(exc).strip().splitlines() or [""])[0]
    title = f"[Crash] {type(exc).__qualname__}: {first_line[:140]}"
    repo = _CONFIG["repo"]
    version = _CONFIG.get("version", "unknown")
    os_str = platform.platform(terse=True)
    base = f"https://github.com/{repo}/issues/new?"

    params = {
        "template": "bug_report.yml",
        "title": title,
        "version": version,
        "os": os_str,
    }
    if not body:
        return base + urllib.parse.urlencode(params), False

    # Try the full body in the URL. If it would exceed the safe limit,
    # drop the description entirely — the clipboard carries the full
    # report and the caller tells the user to paste it. No half-measures.
    params["description"] = body
    url = base + urllib.parse.urlencode(params)
    if len(url) <= _MAX_URL_LENGTH:
        return url, True
    params.pop("description")
    return base + urllib.parse.urlencode(params), False


def _prompt_yes_no(prompt: str, stream: TextIO) -> bool:
    """Ask a yes/no question; default Yes on empty Enter. Never raises."""
    try:
        stream.write(prompt)
        stream.flush()
        line = sys.stdin.readline().strip().lower()
    except Exception:  # noqa: BLE001  pragma: no cover  (EOF / closed stdin)
        return False
    return line in ("", "y", "yes")


def _print(stream: TextIO, text: str) -> None:
    stream.write(text + "\n")
    stream.flush()


# --------------------------------------------------------------------------- #
# Clipboard + browser (all best-effort, silent on failure)
# --------------------------------------------------------------------------- #
def _copy_to_clipboard(text: str) -> bool:
    """Copy ``text`` to the system clipboard via the native tool. No deps."""
    try:
        if sys.platform == "darwin":
            return _pipe(["pbcopy"], text)
        if sys.platform.startswith("win"):
            # clip.exe mangles some UTF-8 but is always present; good
            # enough for tracebacks (mostly ASCII).
            return _pipe(["clip"], text)
        # Linux / *nix: prefer xclip, fall back to xsel.
        for cmd in (("xclip", "-selection", "clipboard"), ("xsel", "--clipboard", "--input")):
            if shutil.which(cmd[0]):
                return _pipe(list(cmd), text)
        return False
    except Exception:  # noqa: BLE001  pragma: no cover
        return False


def _pipe(cmd: list[str], text: str) -> bool:
    proc = subprocess.run(
        cmd,
        input=text.encode("utf-8", "replace"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    return proc.returncode == 0


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001  pragma: no cover
        return False
