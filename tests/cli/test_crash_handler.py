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
import subprocess
import sys
import traceback
import urllib.parse
from pathlib import Path

import pytest

from omnigent import crash_handler as ch
from omnigent import crash_ui
from omnigent.version import VERSION


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
    assert f"omnigent {VERSION}" in report or "omnigent unknown" in report  # version line
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
    home = Path("/home/example-user")
    monkeypatch.setattr(Path, "home", lambda: home)
    tokens = [
        "sk-" + "supersecret123456",
        "dapi-" + "synthetic123456",
        "xoxb-" + "synthetic123456",
        "gh" + "p_" + "TEST" + "x" * 30,
        "AIza" + "x" * 24,
        "Bearer synthetic.jwt.payload",
    ]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "omnigent",
            "run",
            "a.yaml",
            "--api-key",
            tokens[0],
            "--note",
            f"{home}/x {' '.join(tokens)}",
        ],
    )
    cmd = ch._command_line()
    assert all(token not in cmd for token in tokens)
    assert "synthetic.jwt.payload" not in cmd
    assert cmd == (
        f"omnigent run a.yaml --api-key sk-s*** --note {home}/x "
        "sk-s*** dapi*** xoxb*** ghp_*** AIza*** Bear***"
    )


# --------------------------------------------------------------------------- #
# Save + rotation
# --------------------------------------------------------------------------- #
def test_save_report_writes_and_rotates(data_dir: Path) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent", keep_reports=2)
    paths = [ch._save_report(f"report {i}\n") for i in range(5)]
    # The newest report always survives its own rotation pass. Earlier paths
    # may legitimately be gone: rotation prunes between saves, which also frees
    # those names for reuse — so this deliberately does not assert 5 distinct
    # paths (see test_save_report_keeps_every_same_second_report, which pins
    # the no-overwrite guarantee with rotation held wide).
    assert paths[-1].exists()
    # Only the newest 2 remain on disk.
    remaining = sorted((data_dir / "crashes").glob("crash-*.md"))
    assert len(remaining) == 2
    # Permissions are tight.
    assert all((p.stat().st_mode & 0o077) == 0 for p in remaining)


def test_save_report_keeps_every_same_second_report(data_dir: Path) -> None:
    """Repeated crashes inside one second/process must not overwrite each other.

    The regression: the same-second fallback disambiguated by pid alone, so a
    process crashing more than twice per second reused one filename and every
    report but the last was silently destroyed. Rotation is held wide here so
    only overwriting could lose a report.
    """
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent", keep_reports=10)
    paths = [ch._save_report(f"report {i}\n") for i in range(5)]

    assert len(set(paths)) == 5
    assert {p.read_text(encoding="utf-8").strip() for p in paths} == {
        f"report {i}" for i in range(5)
    }


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


def test_first_party_sdk_shown_even_in_site_packages(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A core SDK package installed into site-packages stays visible.

    In a shipped wheel the SDKs (``omnigent_client``, ``omnigent_ui_sdk``)
    live under site-packages next to click/yaml. The default first-party
    prefix ``("omnigent",)`` must keep their frames shown rather than
    collapsed — otherwise a crash inside an SDK would be hidden from the
    user (and from the on-screen triage).
    """
    import importlib.util

    # Keep the installed environment read-only, as it is in the test sandbox.
    sp = tmp_path / "site-packages"
    sp.mkdir()
    monkeypatch.syspath_prepend(str(sp))
    assert crash_ui._site_packages_dir() == str(sp)
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
    monkeypatch.chdir(tmp_path)
    out = crash_ui.format_traceback(exc, tb, colored=False, unicode_ok=True)
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
    exc = _make_exc("yes-branch")
    early_sentinel = "EARLY_TRACEBACK_SENTINEL"
    exc.add_note(early_sentinel)
    exc.add_note("x" * 30000)
    exc.add_note("FINAL_TRACEBACK_DETAIL")
    full_tb = "".join(traceback.format_exception(exc))
    ch.handle_crash(exc, interactive=True, source="test")

    out = _strip_ansi(stream.getvalue())
    assert "copied" in calls and "yes-branch" in calls["copied"]
    assert calls["url"].startswith("https://github.com/omnigent-ai/omnigent/issues/new?")
    assert "template=bug_report.yml" in calls["url"]
    assert "review and submit" in out
    reports = list((data_dir / "crashes").glob("crash-*.md"))
    assert len(reports) == 1
    saved_report = reports[0].read_text(encoding="utf-8")
    assert saved_report == calls["copied"]
    assert full_tb in saved_report
    assert early_sentinel in saved_report
    assert len(calls["url"]) <= ch._MAX_URL_LENGTH
    body = _description_of(calls["url"])
    assert early_sentinel not in body
    assert "FINAL_TRACEBACK_DETAIL" in body
    assert "Traceback truncated to fit this link" in body


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
    url, body_included = ch._issue_url(exc, "Traceback...\nValueError: oops\n")
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
def _description_of(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("description", [""])[0]


@pytest.mark.parametrize(
    ("padding", "frame_template", "exception_type", "message", "last_frame", "candidate_size"),
    [
        pytest.param(
            "é",
            '  File "répertoire/modèle_{index}.py", line {index}, in échec\n',
            ValueError,
            "crash",
            'File "répertoire/modèle_119.py", line 119, in échec',
            None,
            id="unicode-encoding",
        ),
        pytest.param(
            "X",
            '  File "/home/example/.cache/app/lib/python3.12/site-packages/pkg/'
            'module_{index}.py", line {index}, in load\n'
            "    return importlib.import_module(name)\n",
            ImportError,
            "/lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.33' not found",
            'File "/home/example/.cache/app/lib/python3.12/site-packages/pkg/'
            'module_119.py", line 119, in load',
            None,
            id="realistic-frames",
        ),
        pytest.param(
            "X",
            '  File "{index}.py"\n',
            ValueError,
            "crash",
            'File "119.py"',
            8001,
            id="one-over-url-limit",
        ),
    ],
)
def test_issue_url_retains_tail_of_long_traceback(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    padding: str,
    frame_template: str,
    exception_type: type[Exception],
    message: str,
    last_frame: str,
    candidate_size: int | None,
) -> None:
    """Encoded Unicode and deep library frames both retain their final diagnostics."""
    monkeypatch.setattr(Path, "home", lambda: data_dir / "home")
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    frames = "".join(frame_template.format(index=index) for index in range(120))
    exception_line = f"{exception_type.__qualname__}: {message}"
    padding_length = 0 if candidate_size else 30000
    tb_text = (
        "Traceback (most recent call last):\n"
        + padding * padding_length
        + "\n"
        + frames
        + exception_line
        + "\n"
    )
    exc = exception_type(message)
    if candidate_size:
        seed_url, seed_included = ch._issue_url(exc, tb_text)
        assert seed_included and "truncated" not in _description_of(seed_url)
        padding_length = candidate_size - len(seed_url)
        assert padding_length > 0
        tb_text = tb_text.replace("\n", "\n" + padding * padding_length, 1)
        parts = urllib.parse.urlparse(seed_url)
        params = urllib.parse.parse_qs(parts.query)
        params["description"] = [ch._issue_body(exc, tb_text)]
        candidate_url = parts._replace(query=urllib.parse.urlencode(params, doseq=True)).geturl()
        assert len(candidate_url) == candidate_size
    url, body_included = ch._issue_url(exc, tb_text)
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is True
    body = _description_of(url)
    assert "truncated" in body
    assert body.count(padding) < 30000
    traceback_body = body.split("**Traceback:**\n", 1)[1]
    assert last_frame in traceback_body
    assert exception_line in traceback_body
    if candidate_size:
        assert len(url) <= 8000 < len(candidate_url)
        assert tb_text not in body


@pytest.mark.parametrize("message_size", [30000, 5_000_000], ids=["oversized", "multi-megabyte"])
def test_issue_url_drops_body_when_summary_alone_is_too_long(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, message_size: int
) -> None:
    """If even the exception summary cannot fit, the body is dropped."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    monkeypatch.setattr(
        ch, "_tail", lambda *_args, **_kwargs: pytest.fail("oversized summary reached tail search")
    )
    huge_message = ValueError("m" * message_size)
    tb_text = f"Traceback (most recent call last):\nValueError: {huge_message}\n"
    url, body_included = ch._issue_url(huge_message, tb_text)
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is False
    assert "description=" not in url


def test_issue_url_drops_body_when_command_alone_is_too_long(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    monkeypatch.setattr(sys, "argv", ["omnigent", "run", "x" * 9000])
    url, body_included = ch._issue_url(_make_exc("failure"), "ValueError: failure\n")
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is False
    assert "description=" not in url
    assert "ValueError" in urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["title"][0]


@pytest.mark.parametrize(
    ("home_directory", "text", "expected"),
    [
        ("/home/example-user", "/home/example-user/app.py", "~/app.py"),
        ("/home/example-user", '"/home/example-user/app.py"', '"~/app.py"'),
        ("/home/example-user", "(/home/example-user)", "(~)"),
        ("/home/example-user", "Home: `/home/example-user`", "Home: `~`"),
        ("/home/example-user", "cannot write to /home/example-user.", "cannot write to ~."),
        ("/home/example-user", "/home/example-user, next", "~, next"),
        ("/home/example-user", "'/home/example-user:/opt/lib'", "'~:/opt/lib'"),
        ("/home/example-user", "--config=/home/example-user/x", "--config=~/x"),
        ("/home/example-user", "file:///home/example-user/notes.txt", "file://~/notes.txt"),
        ("C:\\Users\\example-user", "C:\\Users\\example-user\\app.py", "~\\app.py"),
        ("C:\\Users\\example-user", "C:/Users/example-user/app.py", "~/app.py"),
        ("C:\\Users\\example-user", "C:\\\\Users\\\\example-user\\\\app.py", "~\\\\app.py"),
        (
            "C:\\Users\\example-user",
            "file:///C:/Users/example-user/notes.txt",
            "file:///~/notes.txt",
        ),
        (
            "C:\\Users\\example user",
            "file:///C:/Users/example%20user/private.txt",
            "file:///~/private.txt",
        ),
        (
            "C:\\Users\\example user",
            "file:///c:/users/example%20user/private.txt",
            "file:///~/private.txt",
        ),
        ("C:\\Users\\example user", "file:///C%3A/Users/example%20user/a", "file:///~/a"),
        ("C:\\Users\\example user", "C%3A%5CUsers%5Cexample%20user%5Cx", "~%5Cx"),
        ("C:\\Users\\example user", "c%3a%5cusers%5cexample%20user%5cx", "~%5cx"),
        ("/home/example user", "file://localhost/home/example%20user/x", "~/x"),
        ("/home/example-user", "~example-user/x", "~/x"),
        ("/home/example-user", "%2Fhome%2Fexample-user%2Fx", "~%2Fx"),
        (
            "/home/example-user",
            "file%3A%2F%2F%2Fhome%2Fexample-user%2Fprivate.txt",
            "file%3A%2F%2F~%2Fprivate.txt",
        ),
        (
            "C:\\Users\\example user",
            "file%3A%2F%2F%2FC%3A%2FUsers%2Fexample%20user%2Fprivate.txt",
            "file%3A%2F%2F%2F~%2Fprivate.txt",
        ),
        (
            "/home/example-user",
            "file%3a%2F%2f%2fhome%2Fexample-user%2fprivate.txt",
            "file%3a%2F%2f~%2fprivate.txt",
        ),
        (
            "/home/example-user",
            "%2Fmnt%2Fhome%2Fexample-user%2Fx",
            "%2Fmnt%2Fhome%2Fexample-user%2Fx",
        ),
        # Privacy-first: siblings that share the home prefix are rewritten on purpose.
        ("/home/example-user", "/home/example-user-backup/x", "~-backup/x"),
        ("/home/example-user", "/home/example-user&backup/x", "~&backup/x"),
        ("/home/example-user", "/home/example-user(backup)/x", "~(backup)/x"),
        ("/home/example-user", "/home/example-user backup/x", "~ backup/x"),
        # A home path inside another name is another location, so it is left alone.
        ("/home/example-user", "/mnt/home/example-user/project", "/mnt/home/example-user/project"),
        (
            "/home/example-user",
            "https://example.com/home/example-user/file",
            "https://example.com/home/example-user/file",
        ),
    ],
    ids=[
        "path",
        "double-quoted",
        "parenthesized",
        "backticked",
        "sentence-period",
        "comma-after",
        "quoted-path-list",
        "option-value",
        "file-url",
        "windows-backslash",
        "windows-forward-slash",
        "windows-doubled-backslash",
        "windows-file-url",
        "windows-file-url-encoded-space",
        "windows-file-url-lowercase-drive",
        "windows-file-url-encoded-colon",
        "windows-percent-backslash",
        "windows-lowercase-hex",
        "localhost-file-url",
        "tilde-user",
        "percent-encoded-path",
        "fully-encoded-file-uri",
        "fully-encoded-windows-file-uri",
        "mixed-case-hex-file-uri",
        "encoded-mount-embedded",
        "hyphen-sibling",
        "ampersand-sibling",
        "parenthesis-sibling",
        "space-sibling",
        "mount-embedded",
        "url-embedded",
    ],
)
def test_scrub_replaces_home_at_path_boundaries(
    monkeypatch: pytest.MonkeyPatch, home_directory: str, text: str, expected: str
) -> None:
    """The home path is replaced wherever it starts a path, whatever follows it."""
    monkeypatch.setattr(Path, "home", lambda: Path(home_directory))
    assert ch._scrub(text) == expected


@pytest.mark.parametrize(
    ("home_directory", "uri", "expected"),
    [
        (
            "C:\\Users\\example user",
            "file:///C:/Users/example%20user/private.txt",
            "file:///~/private.txt",
        ),
        (
            "/home/example-user",
            "file%3A%2F%2F%2Fhome%2Fexample-user%2Fprivate.txt",
            "file%3A%2F%2F~%2Fprivate.txt",
        ),
        (
            "C:\\Users\\example user",
            "file%3A%2F%2F%2FC%3A%2FUsers%2Fexample%20user%2Fprivate.txt",
            "file%3A%2F%2F%2F~%2Fprivate.txt",
        ),
        (
            "/home/example-user",
            "file%3a%2F%2f%2fhome%2Fexample-user%2fprivate.txt",
            "file%3a%2F%2f~%2fprivate.txt",
        ),
    ],
    ids=["encoded-space-windows", "fully-encoded", "fully-encoded-windows", "mixed-case-hex"],
)
def test_issue_url_scrubs_encoded_home_in_title_and_description(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, home_directory: str, uri: str, expected: str
) -> None:
    """A percent-encoded file URI to the home directory is scrubbed in title and description."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    monkeypatch.setattr(Path, "home", lambda: Path(home_directory))
    exc = ImportError(f"cannot open {uri}")
    tb_text = f'Traceback (most recent call last):\n  File "{uri}", line 1\nImportError: x\n'
    url, body_included = ch._issue_url(exc, tb_text)
    assert body_included is True
    assert "example" not in urllib.parse.unquote_plus(url)
    title = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["title"][0]
    assert expected in title
    assert expected in _description_of(url)


def test_issue_url_redacts_tokens_before_title_cutoff(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    home = Path("/home/example-user")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(sys, "argv", ["omnigent", "run", "example.yaml"])
    token = "sk-abcdefghijklmnop"
    exc = ImportError(f"{'.' * 132}{token} cannot open {home}/.config/app.toml")
    url, body_included = ch._issue_url(exc, "ImportError: cannot open\n")
    assert body_included is True
    decoded_url = urllib.parse.unquote_plus(url)
    assert f"{home}/.config/app.toml" not in decoded_url
    assert "cannot open ~/.config/app.toml" in _description_of(url)
    assert token not in decoded_url
    title = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["title"][0]
    assert token[:8] not in title
    assert "sk-a***" in title


@pytest.mark.parametrize(
    ("home_directory", "home_uri", "redacted_uri"),
    [
        ("/home/example user", "file:///home/example%20user", "file://~"),
        (r"C:\Users\example user", "file:///C:/Users/example%20user", "file:///~"),
    ],
    ids=["posix", "windows"],
)
def test_issue_url_scrubs_notes_before_truncation(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    home_directory: str,
    home_uri: str,
    redacted_uri: str,
) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    home = Path(home_directory)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        sys, "argv", ["omnigent", "run", f"{home_uri}?download=1", f"{home_uri}#section"]
    )
    noted_exc = _make_exc("failure")
    noted_exc.add_note("x" * 9000)
    noted_exc.add_note(f"Home: ({home})")
    noted_exc.add_note(f"Home: {home}... {home}!!!")
    noted_exc.add_note(f"URI: {home_uri}/private.txt")
    noted_tb = "".join(
        traceback.format_exception(type(noted_exc), noted_exc, noted_exc.__traceback__)
    )
    report = ch._build_report(noted_exc, noted_tb, source="test")
    assert noted_tb in report
    assert f"URI: {home_uri}/private.txt" in report
    assert f"{home_uri}?download=1 {home_uri}#section" in report
    noted_url, body_included = ch._issue_url(noted_exc, noted_tb)
    assert body_included is True
    assert len(noted_url) <= ch._MAX_URL_LENGTH
    assert str(home) not in urllib.parse.unquote_plus(noted_url)
    assert home_uri not in urllib.parse.unquote_plus(noted_url)
    noted_body = _description_of(noted_url)
    assert "truncated" in noted_body
    assert "Home: (~)" in noted_body
    assert "Home: ~... ~!!!" in noted_body
    assert f"URI: {redacted_uri}/private.txt" in noted_body
    assert f"{redacted_uri}?download=1 {redacted_uri}#section" in noted_body


def test_issue_url_oversized_final_note_leaves_empty_traceback(data_dir: Path) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    exc = _make_exc("failure")
    exc.add_note("x" * 9000)
    tb_text = "".join(traceback.format_exception(exc))
    url, body_included = ch._issue_url(exc, tb_text)
    assert body_included is True
    assert len(url) <= ch._MAX_URL_LENGTH
    body = _description_of(url)
    assert "**Exception:** `ValueError: failure`" in body
    assert "Traceback truncated to fit this link" in body
    traceback_body = body.split("**Traceback:**\n", 1)[1].split("```\n", 1)[1]
    assert traceback_body == "```\n"


@pytest.mark.parametrize(
    ("home_directory", "filename_suffix"),
    [
        (r"C:\Users\example-user", r"\private.txt"),
        ("/home/example'user", '/private"file.txt'),
        (r"C:\Users\example'user", r'\private"file.txt'),
    ],
    ids=["windows-backslashes", "posix-escaped-quote", "windows-backslashes-and-quote"],
)
def test_issue_url_scrubs_repr_escaped_home(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    home_directory: str,
    filename_suffix: str,
) -> None:
    """Python's filename quoting must not expose the home in either public field."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    home_path = Path(home_directory)
    monkeypatch.setattr(Path, "home", lambda: home_path)
    exc = FileNotFoundError(2, "missing", f"{home_path}{filename_suffix}")
    url, body_included = ch._issue_url(exc, "".join(traceback.format_exception(exc)))
    assert body_included is True
    expected_message = str(FileNotFoundError(2, "missing", f"~{filename_suffix}"))
    title = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["title"][0]
    summary = _description_of(url).split("**Exception:** ", 1)[1].split("\n\n", 1)[0]
    assert title == f"[Crash] FileNotFoundError: {expected_message}"
    assert summary == f"`FileNotFoundError: {expected_message}`"
    assert "example" not in urllib.parse.unquote_plus(url)


def test_issue_url_scrubs_every_home_reference_in_command(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each home reference in an exception message is scrubbed, in title and description."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    monkeypatch.setattr(Path, "home", lambda: Path("/home/example-user"))
    command = "/home/example-user/.venv/bin/tool --config /home/example-user/.config/app.toml"
    exc = subprocess.CalledProcessError(1, command)
    url, _ = ch._issue_url(exc, "Traceback (most recent call last):\nCalledProcessError\n")
    assert "example-user" not in urllib.parse.unquote_plus(url)
    assert "~/.config/app.toml" in _description_of(url)


@pytest.mark.parametrize(
    ("argv", "expected_command"),
    [
        (
            ["example", "Authorization: Bearer", "synthetic.jwt.payload"],
            "example Authorization: Bear***",
        ),
        (
            [
                "omnigent",
                "run",
                "/home/example-user/app.yaml",
                "--flag",
                "--header",
                "/home/example-user/my dir Bearer",
                "synthetic.token.value",
            ],
            "omnigent run ~/app.yaml --flag --header ~/my dir Bear***",
        ),
    ],
    ids=["plain-prefix", "home-containing-prefix"],
)
def test_bearer_token_split_across_arguments_is_redacted(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected_command: str
) -> None:
    """A bearer value in the next argument is redacted, and no home path leaks."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    monkeypatch.setattr(Path, "home", lambda: Path("/home/example-user"))
    monkeypatch.setattr(sys, "argv", argv)
    assert ch._scrub(ch._command_line()) == expected_command
    url, _ = ch._issue_url(
        _make_exc("failure"), "Traceback (most recent call last):\nValueError\n"
    )
    decoded_url = urllib.parse.unquote_plus(url)
    assert "synthetic.jwt.payload" not in decoded_url
    assert "synthetic.token.value" not in decoded_url
    assert "example-user" not in decoded_url
    assert f"**Command:**\n```\n{expected_command}\n```" in _description_of(url)


def test_issue_url_redacts_multiline_bearer_before_truncation(data_dir: Path) -> None:
    """A credential split across lines is redacted before the head is cut away."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    oversized_line = "x" * 9000 + " Authorization: Bearer\n"
    tb_text = (
        "Traceback (most recent call last):\n"
        + oversized_line
        + "synthetic.jwt.payload\n"
        + "ValueError: failure\n"
    )
    url, body_included = ch._issue_url(_make_exc("failure"), tb_text)
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is True
    assert "synthetic.jwt.payload" not in urllib.parse.unquote_plus(url)
    assert "ValueError: failure" in _description_of(url)


@pytest.mark.parametrize("url_length", [None, 8000], ids=["short", "exact-url-limit"])
def test_issue_url_keeps_short_body_intact(data_dir: Path, url_length: int | None) -> None:
    """A normal-sized traceback is kept in full, with no truncation note."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    short_tb = 'Traceback (most recent call last):\n  File "app.py", line 10\nValueError: boom\n'
    exc = _make_exc("boom")
    url, body_included = ch._issue_url(exc, short_tb)
    if url_length:
        short_tb = short_tb.replace("app.py", "X" * (url_length - len(url)) + "app.py")
        url, body_included = ch._issue_url(exc, short_tb)
        assert len(url) == url_length
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is True
    body = _description_of(url)
    assert "truncated" not in body
    assert short_tb in body
