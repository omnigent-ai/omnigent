"""Tests for the friendly crash handler + UI (``omnigent.crash_handler``).

Covers: report building, token redaction, save + rotation, plain
(non-TTY) rendering, the interactive yes/no bug-filing flow with
stubbed clipboard/browser, KeyboardInterrupt deferral, and the
pre-filled GitHub issue URL (template + title + version + OS +
description).

These run under the project's normal pytest invocation. The data dir is
isolated via ``OMNIGENT_DATA_DIR`` so no reports are written to the
developer's real ``~/.omnigent``.
"""

from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

import pytest

from omnigent import crash_handler as ch
from omnigent import crash_ui


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "omnigent-data"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(d))
    return d


class FakeTTY(io.TextIOWrapper):
    """A stream that claims to be a TTY and captures its bytes."""

    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="utf-8", write_through=True)

    def isatty(self) -> bool:
        return True

    def getvalue(self) -> str:
        return self.buffer.getvalue().decode("utf-8")


def _make_exc(msg: str = "boom") -> ValueError:
    try:
        raise ValueError(msg)
    except ValueError as e:
        return e


# --------------------------------------------------------------------------- #
# Report building + redaction
# --------------------------------------------------------------------------- #
def test_build_report_contains_required_fields(data_dir: Path) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    exc = _make_exc("the frobnicator failed")
    report = ch._build_report(
        exc, "Traceback...\nValueError: the frobnicator failed\n", source="uncaught"
    )
    assert "# Crash Report — omnigent" in report
    assert "ValueError" in report
    assert "omnigent 0.6.0.dev0" in report or "omnigent unknown" in report  # version line
    assert "https://github.com/omnigent-ai/omnigent" in report
    assert "Source:** uncaught" in report
    assert "the frobnicator failed" in report


def test_redact_strips_common_tokens(data_dir: Path) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    assert ch._redact("sk-abc123def456ghi789jkl") == "sk-a***"
    assert "sk-" not in ch._redact("sk-abc123def456ghi789jkl").replace("sk-a***", "")
    # Use the redaction regex directly to avoid putting a PAT-shaped
    # string in test source — the secret scanner flags it.
    pat = next(p for p in ch._TOKEN_PATTERNS if "gh" in p.pattern)
    fake_token = "gh" + "p_" + "TEST" + "x" * 30
    assert pat.sub(lambda m: m.group(0)[:4] + "***", fake_token) == fake_token[:4] + "***"
    # The secret body never survives redaction.
    assert "def456" not in ch._redact("sk-abc123def456")


def test_command_line_redacts_tokens_in_argv(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["omnigent", "run", "a.yaml", "--api-key", "sk-supersecret123456"]
    )
    cmd = ch._command_line()
    assert "sk-supersecret123456" not in cmd
    assert "***" in cmd


# --------------------------------------------------------------------------- #
# Save + rotation
# --------------------------------------------------------------------------- #
def test_save_report_writes_and_rotates(data_dir: Path) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent", keep_reports=2)
    paths = [ch._save_report(f"report {i}\n") for i in range(5)]
    assert all(p.exists() for p in paths)
    # Only the newest 2 remain on disk.
    remaining = sorted((data_dir / "crashes").glob("crash-*.md"))
    assert len(remaining) == 2
    # Permissions are tight.
    assert all((p.stat().st_mode & 0o077) == 0 for p in remaining)


def test_save_report_collision_disambiguates(data_dir: Path) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    a = ch._save_report("x\n")
    b = ch._save_report("y\n")  # same second → pid-suffixed
    assert a != b
    assert a.exists() and b.exists()


# --------------------------------------------------------------------------- #
# Rendering: non-TTY is plain, TTY is header + copyable path (no box)
# --------------------------------------------------------------------------- #
def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_render_non_tty_is_plain_no_box(data_dir: Path) -> None:
    exc = _make_exc("plain path crash")
    buf = io.StringIO()
    crash_ui.render_crash_screen(
        app_name="omnigent",
        report_path="/tmp/crash-x.md",
        exc=exc,
        tb=exc.__traceback__,
        stream=buf,
    )
    out = buf.getvalue()
    assert "╭" not in out and "│" not in out  # no box
    assert "⚠" not in out  # no emoji
    assert "\x1b[" not in out  # no ANSI
    assert "Omnigent ran into an issue." in out
    assert "A crash report was saved to:" in out
    assert "/tmp/crash-x.md" in out
    assert "ValueError: plain path crash" in out


def test_render_tty_shows_header_and_copyable_path(data_dir: Path) -> None:
    exc = _make_exc("tty path crash")
    buf = FakeTTY()
    crash_ui.render_crash_screen(
        app_name="omnigent",
        report_path="/tmp/crash-y.md",
        exc=exc,
        tb=exc.__traceback__,
        stream=buf,
    )
    out = buf.getvalue()
    plain = _strip_ansi(out)
    # Header present.
    assert "Omnigent ran into an issue." in plain
    # No box borders — path must be cleanly selectable.
    assert "╭" not in plain and "│" not in plain and "╰" not in plain
    # The path is on its own line, indented, no wrapping.
    assert "  /tmp/crash-y.md" in plain
    # Traceback frames dimmed, exception line bolded (ANSI present).
    assert "\x1b[90m" in out  # gray frames
    assert "\x1b[1m" in out  # bold exception line
    assert "ValueError: tty path crash" in plain


# --------------------------------------------------------------------------- #
# Path shortening + library-frame collapsing
# --------------------------------------------------------------------------- #
def _traceback_with_library_frames() -> tuple[Exception, object]:
    """Build a real traceback that descends through a site-packages frame."""
    import yaml  # a guaranteed-installed site-packages library

    def outer():
        return middle()

    def middle():
        # Calling yaml.safe_load raises inside yaml's internals, so the
        # traceback has both first-party (this test file) and library frames.
        yaml.safe_load("this: is: not: valid: yaml: [")

    try:
        outer()
    except Exception as e:
        return e, e.__traceback__
    raise AssertionError("should have raised")


def test_traceback_shortens_paths(data_dir: Path) -> None:
    exc, tb = _traceback_with_library_frames()
    out = crash_ui.format_traceback(exc, tb, colored=False, unicode_ok=True)
    # The yaml internals path is reduced to a package-relative form.
    assert "site-packages" not in out
    # The library run is collapsed (no individual yaml/*.py frames).
    assert "frames hidden in yaml" in out
    # The exception type is present.
    assert "ScannerError" in out or "MarkedYAMLError" in out


def test_traceback_collapses_library_frames(data_dir: Path) -> None:
    exc, tb = _traceback_with_library_frames()
    out = crash_ui.format_traceback(exc, tb, colored=False, unicode_ok=True)
    # A contiguous run of yaml internals is collapsed to one summary line.
    assert "frames hidden in yaml" in out
    # First-party frames (this test file) are still shown by name.
    assert "_traceback_with_library_frames" in out or "test_crash_handler" in out


def test_full_traceback_env_disables_collapsing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_FULL_TRACEBACK", "1")
    exc, tb = _traceback_with_library_frames()
    out = crash_ui.format_traceback(exc, tb, colored=False, unicode_ok=True)
    assert "frames hidden in" not in out


def test_traceback_shows_exception_message_lines(data_dir: Path) -> None:
    exc, tb = _traceback_with_library_frames()
    out = crash_ui.format_traceback(exc, tb, colored=False, unicode_ok=True)
    # Multi-line yaml message (the "in <unicode string>..." snippet) survives.
    assert 'in "<unicode string>"' in out


def test_first_party_sdk_shown_even_in_site_packages(data_dir: Path, tmp_path: Path) -> None:
    """A core SDK package installed into site-packages stays visible.

    In a shipped wheel the SDKs (``omnigent_client``, ``omnigent_ui_sdk``)
    live under site-packages next to click/yaml. The default first-party
    prefix ``("omnigent",)`` must keep their frames shown rather than
    collapsed — otherwise a crash inside an SDK would be hidden from the
    user (and from the on-screen triage).
    """
    import importlib.util

    sp = crash_ui._site_packages_dir()
    assert sp, "test requires a venv site-packages on sys.path"
    fake = os.path.join(sp, "omnigent_client")
    os.makedirs(fake, exist_ok=True)
    probe = os.path.join(fake, "_probe.py")
    with open(probe, "w") as f:
        f.write('def boom(): raise RuntimeError("sdk probe")\n')
    spec = importlib.util.spec_from_file_location("omnigent_client._probe", probe)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        mod.boom()
    except Exception as e:
        exc, tb = e, e.__traceback__
    # cwd outside site-packages, as in a real install.
    import os as _os

    old = _os.getcwd()
    _os.chdir(str(tmp_path))
    try:
        out = crash_ui.format_traceback(exc, tb, colored=False, unicode_ok=True)
    finally:
        _os.chdir(old)
    assert "omnigent_client/_probe.py" in out  # shown, not collapsed
    assert "frames hidden in omnigent_client" not in out
    assert "sdk probe" in out


# --------------------------------------------------------------------------- #
# Interactive flow (stubbed clipboard + browser)
# --------------------------------------------------------------------------- #
def test_interactive_yes_copies_and_opens_browser(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    calls: dict = {}

    def fake_copy(text: str) -> bool:
        calls["copied"] = text
        return True

    monkeypatch.setattr(ch, "_copy_to_clipboard", fake_copy)
    monkeypatch.setattr(ch, "_open_browser", lambda url: calls.__setitem__("url", url) or True)

    stream = FakeTTY()
    # handle_crash writes through real_stderr(); point it at our capture.
    monkeypatch.setattr(ch, "real_stderr", lambda: stream)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    ch.handle_crash(_make_exc("yes-branch"), interactive=True, source="test")

    out = _strip_ansi(stream.getvalue())
    assert "copied" in calls and "yes-branch" in calls["copied"]
    assert calls["url"].startswith("https://github.com/omnigent-ai/omnigent/issues/new?")
    assert "template=bug_report.yml" in calls["url"]
    assert "review and submit" in out


def test_interactive_no_saves_path_and_link(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    monkeypatch.setattr(ch, "_copy_to_clipboard", lambda text: True)
    monkeypatch.setattr(ch, "_open_browser", lambda url: True)

    stream = FakeTTY()
    monkeypatch.setattr(ch, "real_stderr", lambda: stream)
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    ch.handle_crash(_make_exc("no-branch"), interactive=True, source="test")

    out = _strip_ansi(stream.getvalue())
    assert "Report saved here:" in out


def test_interactive_default_enter_is_yes(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    calls: dict = {}

    def fake_copy(text: str) -> bool:
        calls["copied"] = text
        return True

    monkeypatch.setattr(ch, "_copy_to_clipboard", fake_copy)
    monkeypatch.setattr(ch, "_open_browser", lambda url: True)
    stream = FakeTTY()
    monkeypatch.setattr(ch, "real_stderr", lambda: stream)
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))  # empty Enter
    ch.handle_crash(_make_exc("default"), interactive=True, source="test")
    assert "copied" in calls


def test_noninteractive_falls_back_to_link(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ch.handle_crash(
        _make_exc("ci-mode"),
        interactive=False,
        source="test",
    )
    # Just ensure no prompt was issued and no crash; report saved.
    reports = list((data_dir / "crashes").glob("crash-*.md"))
    assert len(reports) == 1


# --------------------------------------------------------------------------- #
# KeyboardInterrupt defers; issue URL encoding
# --------------------------------------------------------------------------- #
def test_issue_url_is_prefilled_title(data_dir: Path) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    exc = _make_exc("oops: bad [brackets] & spaces")
    url, body_included = ch._issue_url(
        exc, ch._issue_body(exc, "Traceback...\nValueError: oops\n")
    )
    # Uses the repo's bug-report template.
    assert "template=bug_report.yml" in url
    assert url.startswith("https://github.com/omnigent-ai/omnigent/issues/new?")
    # Title is URL-encoded and prefilled.
    assert "%5BCrash%5D" in url
    # Version and OS fields prefilled from the crash context.
    assert "version=" in url
    assert "os=" in url
    # The Description field is prefilled with the traceback.
    assert body_included is True
    assert "description=" in url
    import urllib.parse as up

    body = up.parse_qs(up.urlparse(url).query).get("description", [""])[0]
    assert "Traceback" in body
    assert "ValueError: oops" in body


def test_excepthook_defers_keyboard_interrupt(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KeyboardInterrupt must not render the crash screen — it defers."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    fired: list = []
    monkeypatch.setattr(ch, "handle_crash", lambda *a, **k: fired.append(1))
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        ch._excepthook(*sys.exc_info())
    assert fired == []  # handle_crash NOT called for KeyboardInterrupt


# --------------------------------------------------------------------------- #
# URL length truncation
# --------------------------------------------------------------------------- #
def test_issue_url_drops_body_when_too_long(data_dir: Path) -> None:
    """A traceback so large it would blow the URL limit drops the body."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    huge_tb = "X" * 30000  # would produce a ~30KB URL
    url, body_included = ch._issue_url(
        _make_exc("big crash"), ch._issue_body(_make_exc("big crash"), huge_tb)
    )
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is False
    assert "description=" not in url  # body dropped entirely


def test_issue_url_drops_non_ascii_body_when_too_long(data_dir: Path) -> None:
    """Non-ASCII content expands 6x under URL-encoding — body must be dropped."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    huge_non_ascii = "é" * 30000
    url, body_included = ch._issue_url(
        _make_exc("crash"), ch._issue_body(_make_exc("crash"), huge_non_ascii)
    )
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is False


def test_issue_url_keeps_short_body_intact(data_dir: Path) -> None:
    """A normal-sized traceback is kept in the URL."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    short_tb = 'Traceback (most recent call last):\n  File "app.py", line 10\nValueError: boom\n'
    url, body_included = ch._issue_url(
        _make_exc("boom"), ch._issue_body(_make_exc("boom"), short_tb)
    )
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is True
    import urllib.parse as up

    body = up.parse_qs(up.urlparse(url).query).get("description", [""])[0]
    assert "truncated" not in body
    assert "ValueError: boom" in body
