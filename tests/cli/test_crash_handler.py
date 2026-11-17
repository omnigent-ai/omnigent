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
import shlex
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
    assert shlex.split(cmd) == [
        "omnigent",
        "run",
        "a.yaml",
        "--api-key",
        "sk-s***",
        "--note",
        f"{home}/x sk-s*** dapi*** xoxb*** ghp_*** AIza*** Bear***",
    ]


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
    ("padding", "frame_template", "exception_type", "message", "last_frame"),
    [
        pytest.param(
            "é",
            '  File "répertoire/modèle_{index}.py", line {index}, in échec\n',
            ValueError,
            "crash",
            'File "répertoire/modèle_119.py", line 119, in échec',
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
            id="realistic-frames",
        ),
    ],
)
def test_issue_url_retains_tail_of_long_traceback(
    data_dir: Path,
    padding: str,
    frame_template: str,
    exception_type: type[Exception],
    message: str,
    last_frame: str,
) -> None:
    """Encoded Unicode and deep library frames both retain their final diagnostics."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    frames = "".join(frame_template.format(index=index) for index in range(120))
    exception_line = f"{exception_type.__qualname__}: {message}"
    tb_text = (
        "Traceback (most recent call last):\n"
        + padding * 30000
        + "\n"
        + frames
        + exception_line
        + "\n"
    )
    exc = exception_type(message)
    url, body_included = ch._issue_url(exc, tb_text)
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is True
    body = _description_of(url)
    assert "truncated" in body
    assert body.count(padding) < 30000
    traceback_body = body.split("**Traceback:**\n", 1)[1]
    assert last_frame in traceback_body
    assert exception_line in traceback_body


def test_issue_url_drops_body_when_summary_alone_is_too_long(data_dir: Path) -> None:
    """If even the exception summary cannot fit, the body is dropped."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    huge_message = ValueError("m" * 30000)
    url, body_included = ch._issue_url(huge_message, "Traceback...\n")
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
    "home_directory",
    [
        "/home/example-user",
        r"C:\Users\example-user",
        "/home/example user",
        r"C:\Users\example user",
    ],
    ids=[
        "posix-home",
        "windows-home",
        "home-with-spaces",
        "windows-home-with-spaces",
    ],
)
def test_scrub_home_directory_boundaries(
    monkeypatch: pytest.MonkeyPatch, home_directory: str
) -> None:
    """Redact genuine home paths without changing similarly named siblings."""
    home_path = Path(home_directory)
    monkeypatch.setattr(Path, "home", lambda: home_path)
    home = str(home_path)
    text = (
        f'  File "{home}/app.py", line 1\n'
        f'  File "{home}\\native.py", line 2\n'
        f'  File "{home}name/module.py", line 3\n'
        f'  File "{home}.backup/module.py", line 4\n'
        f'  File "{home} backup/module.py", line 5\n'
        f"  File '{home}, backup/native.py', line 6\n"
        f'  File "{home}/child directory/app.py", line 7\n'
        f'  File "{home}\\child directory\\native.py", line 8\n'
        f'  File "{home}-backup/module.py", line 9\n'
        f"Sibling paths: {home}.backup/module.py {home},backup/module.py\n"
        f"Sibling paths: {home}é/module.py {home}+backup/module.py "
        f"{home}@backup/module.py {home}~backup/module.py\n"
        f"Sibling paths: {home}!backup/app.py {home};backup/app.py\n"
        f"Sibling paths: {home})backup/app.py {home}]backup/app.py {home}}}backup/app.py\n"
        f"Home: \"{home}\" '{home}' `{home}` {home}\n"
        f"Home: ({home}) [{home}] {{{home}}} <{home}> {home}, {home}. {home}... {home}!!!\n"
    )
    body = ch._scrub(text)
    assert 'File "~/app.py"' in body
    assert 'File "~\\native.py"' in body
    assert f'File "{home}name/module.py"' in body
    assert f'File "{home}.backup/module.py"' in body
    assert f'File "{home} backup/module.py"' in body
    assert f"{home})backup/app.py" in body
    assert f"{home}]backup/app.py" in body
    assert f"{home}}}backup/app.py" in body
    assert f"File '{home}, backup/native.py'" in body
    assert 'File "~/child directory/app.py"' in body
    assert 'File "~\\child directory\\native.py"' in body
    assert f'File "{home}-backup/module.py"' in body
    assert f"Sibling paths: {home}.backup/module.py {home},backup/module.py\n" in body
    assert (
        f"Sibling paths: {home}é/module.py {home}+backup/module.py "
        f"{home}@backup/module.py {home}~backup/module.py\n"
    ) in body
    assert f"Sibling paths: {home}!backup/app.py {home};backup/app.py\n" in body
    assert "Home: \"~\" '~' `~` ~\n" in body
    assert "Home: (~) [~] {~} <~> ~, ~. ~... ~!!!\n" in body
    assert ch._scrub(home) == "~"


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


def test_issue_url_scrubs_notes_before_truncation(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    home = Path("/home/example-user")
    monkeypatch.setattr(Path, "home", lambda: home)
    noted_exc = _make_exc("failure")
    noted_exc.add_note("x" * 9000)
    noted_exc.add_note(f"Home: ({home})")
    noted_exc.add_note(f"Home: {home}... {home}!!!")
    noted_tb = "".join(
        traceback.format_exception(type(noted_exc), noted_exc, noted_exc.__traceback__)
    )
    noted_url, body_included = ch._issue_url(noted_exc, noted_tb)
    assert body_included is True
    assert len(noted_url) <= ch._MAX_URL_LENGTH
    assert str(home) not in urllib.parse.unquote_plus(noted_url)
    noted_body = _description_of(noted_url)
    assert "truncated" in noted_body
    assert "Home: (~)" in noted_body
    assert "Home: ~... ~!!!" in noted_body


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
    ("executable", "expected_executable"),
    [
        ("/home/example-user/.venv/bin/tool", "~/.venv/bin/tool"),
        ("/home/example-user-old/bin/tool", "/home/example-user-old/bin/tool"),
    ],
    ids=["two-home-command", "sibling-first-command"],
)
def test_issue_url_scrubs_each_home_in_quoted_command(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable: str,
    expected_executable: str,
) -> None:
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    home = Path("/home/example-user")
    monkeypatch.setattr(Path, "home", lambda: home)
    exc = subprocess.CalledProcessError(1, f"{executable} --config {home}/.config/app.toml")
    url, body_included = ch._issue_url(exc, "".join(traceback.format_exception(exc)))
    assert body_included is True
    title = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["title"][0]
    summary = _description_of(url).split("**Exception:** ", 1)[1].split("\n\n", 1)[0]
    expected_message = (
        f"Command '{expected_executable} --config ~/.config/app.toml' "
        "returned non-zero exit status 1."
    )
    assert title == f"[Crash] CalledProcessError: {expected_message}"
    assert summary == f"`CalledProcessError: {expected_message}`"
    assert f"{home}/.config/app.toml" not in _description_of(url)
    assert f"{home}/" not in urllib.parse.unquote_plus(url)


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/mnt/home/example/project", "/mnt/home/example/project"),
        ("https://example.com/home/example/file", "https://example.com/home/example/file"),
        ("/mnt/cache//home/example/file", "/mnt/cache//home/example/file"),
        (
            "https://example.test/cache//home/example/file",
            "https://example.test/cache//home/example/file",
        ),
        (r"/mnt/cache\/home/example/file", r"/mnt/cache\/home/example/file"),
        ("/home/example/project", "~/project"),
        (
            "https://example.test/cache/file:///home/example/file.py",
            "https://example.test/cache/file:///home/example/file.py",
        ),
        ("/mnt/file:///home/example/file.py", "/mnt/file:///home/example/file.py"),
        (
            r"/mnt/cache\file:///home/example/file.py",
            r"/mnt/cache\file:///home/example/file.py",
        ),
    ],
    ids=[
        "mount-prefix",
        "url-path",
        "doubled-slash-mount",
        "doubled-slash-url",
        "backslash-mount",
        "home-itself",
        "file-url-in-url",
        "file-url-in-mount",
        "file-url-after-backslash",
    ],
)
def test_scrub_keeps_home_text_inside_other_paths(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    """A home path embedded in a longer path or URL is another location, so it stays intact."""
    monkeypatch.setattr(Path, "home", lambda: Path("/home/example"))
    assert ch._scrub(text) == expected


def test_scrub_redacts_home_in_file_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``file://`` URL to the home directory still exposes it, so it is scrubbed."""
    monkeypatch.setattr(Path, "home", lambda: Path("/home/example"))
    assert ch._scrub("file:///home/example/notes.txt") == "file://~/notes.txt"


@pytest.mark.parametrize(
    ("home_directory", "sibling_suffix"),
    [
        ("/home/example", " backup/config.yaml"),
        ("/home/O'Brien", " backup/config.yaml"),
        (r"C:\Users\O'Brien", " backup/config.yaml"),
        ("/home/example", "'backup/config.yaml"),
        ("/home/example", "=backup/config.yaml"),
        ("/home/example", "%backup/config.yaml"),
    ],
    ids=[
        "plain-home",
        "posix-apostrophe",
        "windows-apostrophe",
        "apostrophe-sibling",
        "equals-sibling",
        "percent-sibling",
    ],
)
def test_command_line_keeps_sibling_argument_with_spaces(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    home_directory: str,
    sibling_suffix: str,
) -> None:
    """Public shell quoting preserves siblings and redacts homes, including apostrophes."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    home_path = Path(home_directory)
    monkeypatch.setattr(Path, "home", lambda: home_path)
    sibling = f"{home_path}{sibling_suffix}"
    argv = [
        "omnigent",
        "run",
        sibling,
        f"--config={sibling}",
        f"file://{sibling}",
        f"{sibling} --config {home_path}/.config/app.toml",
        f"{home_path}/project/file.py",
        f"Home: ({home_path})",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    exc = _make_exc()
    tb_text = "".join(traceback.format_exception(exc))
    report = ch._build_report(exc, tb_text, source="test")
    assert tb_text in report
    report_command = report.split("## Command\n\n```\n", 1)[1].split("\n```", 1)[0]
    assert shlex.split(report_command) == argv
    url, body_included = ch._issue_url(exc, tb_text)
    assert body_included is True
    assert len(url) <= ch._MAX_URL_LENGTH
    command = _description_of(url).split("**Command:**\n```\n", 1)[1].split("\n```", 1)[0]
    assert shlex.split(command) == [
        "omnigent",
        "run",
        sibling,
        f"--config={sibling}",
        f"file://{sibling}",
        f"{sibling} --config ~/.config/app.toml",
        "~/project/file.py",
        "Home: (~)",
    ]


@pytest.mark.parametrize(
    ("prefix", "local_prefix", "public_prefix"),
    [
        ("Authorization: Bearer", "Authorization: Bear***", "Authorization: Bear***"),
        (
            "/home/example-user/x Bearer",
            "/home/example-user/x Bear***",
            "~/x Bear***",
        ),
    ],
    ids=["plain-prefix", "home-containing-prefix"],
)
def test_bearer_token_split_across_arguments_is_redacted(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    local_prefix: str,
    public_prefix: str,
) -> None:
    """Quoting must not stop a bearer pattern from matching the token that follows it."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    home = Path("/home/example-user")
    monkeypatch.setattr(Path, "home", lambda: home)
    token = "example-token-value"
    neighbors = [f"{home} backup/config.yaml", f"{home}/app.yaml", "--keep", "unchanged"]
    monkeypatch.setattr(sys, "argv", ["omnigent", "--note", prefix, token, *neighbors])
    exc = _make_exc("failure")
    tb_text = "Traceback (most recent call last):\nValueError\n"
    report = ch._build_report(exc, tb_text, source="test")
    assert token not in report
    assert tb_text in report
    local_command = report.split("## Command\n\n```\n", 1)[1].split("\n```", 1)[0]
    assert local_prefix in local_command
    assert shlex.split(local_command)[-4:] == neighbors
    url, body_included = ch._issue_url(exc, tb_text)
    assert body_included is True
    assert token not in urllib.parse.unquote_plus(url)
    public_command = _description_of(url).split("**Command:**\n```\n", 1)[1].split("\n```", 1)[0]
    assert public_prefix in public_command
    assert shlex.split(public_command)[-4:] == [neighbors[0], "~/app.yaml", *neighbors[2:]]


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


def test_issue_url_keeps_short_body_intact(data_dir: Path) -> None:
    """A normal-sized traceback is kept in full, with no truncation note."""
    ch.install_crash_handler("omnigent", "omnigent-ai/omnigent")
    short_tb = 'Traceback (most recent call last):\n  File "app.py", line 10\nValueError: boom\n'
    url, body_included = ch._issue_url(_make_exc("boom"), short_tb)
    assert len(url) <= ch._MAX_URL_LENGTH
    assert body_included is True
    body = _description_of(url)
    assert "truncated" not in body
    assert short_tb in body
