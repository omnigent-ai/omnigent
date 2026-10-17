"""``omnigent start`` against a Databricks-fronted server when the browser's ``localhost:8020``
callback cannot reach ``databricks auth login`` (browser on another machine): the terminal must
explain the callback and how to forward it, or give up, instead of hanging silently."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pexpect
import pytest

from tests.e2e._fake_databricks_workspace import (
    BlackHoleListener,
    FakeDatabricksWorkspace,
    wait_for_browser_url,
    write_browser_shim,
)

# Healthy end-to-end login (authorize → callback → token → `Logged in`) takes a
# few seconds locally; the stalled run is observed for an order of magnitude more.
HEALTHY_LOGIN_TIMEOUT_S = 30.0
STALL_OBSERVATION_S = 60.0
HANDOFF_TIMEOUT_S = 90.0

# What an actionable reaction to an unreachable callback would mention.
GUIDANCE_RE = re.compile(
    r"8020|callback|forward|remote machine|another machine|timed out|time out|did not complete",
    re.IGNORECASE,
)

pytestmark = pytest.mark.skipif(
    shutil.which("databricks") is None, reason="databricks CLI not on PATH"
)


def evidence_dir(default: Path) -> Path:
    override = os.environ.get("OMNI_E2E_EVIDENCE_DIR")
    path = Path(override) / default.name if override else default
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class LoginSession:
    """One ``omnigent start`` process under a PTY, logging in to *workspace*."""

    workspace: FakeDatabricksWorkspace
    home: Path
    transcript_path: Path
    url_file: Path
    proc: pexpect.spawn
    _transcript_fh: object

    def expect_browser_handoff(self) -> str:
        """Wait for Omnigent to launch the browser login; return the authorize URL."""
        self.proc.expect(r"Opening browser to log in to", timeout=HANDOFF_TIMEOUT_S)
        return wait_for_browser_url(self.url_file, timeout=HANDOFF_TIMEOUT_S)

    def collect_output(self, seconds: float, *, until: re.Pattern[str] | None = None) -> str:
        """Read the terminal for *seconds*; stop early on *until* or when the process exits."""
        deadline = time.monotonic() + seconds
        chunks: list[str] = []
        while time.monotonic() < deadline:
            try:
                chunks.append(self.proc.read_nonblocking(size=4096, timeout=0.5))
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
            if until is not None and until.search("".join(chunks)):
                break
        return "".join(chunks)

    def transcript(self) -> str:
        self._transcript_fh.flush()
        return self.transcript_path.read_text(errors="replace")

    def close(self) -> None:
        # pexpect's child is a session leader; the group covers the databricks CLI it spawned.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not self.proc.isalive():
                break
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                break
            with contextlib.suppress(pexpect.ExceptionPexpect):
                self.proc.wait()
        self.proc.close(force=True)
        self._transcript_fh.close()


def start_login(workspace: FakeDatabricksWorkspace, run_dir: Path) -> LoginSession:
    """Run ``omnigent start --server <mount>`` on a fresh PTY with isolated state."""
    home = run_dir / "home"
    shutil.rmtree(home, ignore_errors=True)
    shim_dir = write_browser_shim(run_dir / "bin")
    url_file = run_dir / "browser-url.txt"
    url_file.unlink(missing_ok=True)
    env = workspace.login_env(home, browser_shim_dir=shim_dir, browser_url_file=url_file)
    transcript_path = run_dir / "terminal.txt"
    fh = transcript_path.open("w", encoding="utf-8")
    proc = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent.cli", "start", "--server", workspace.omnigent_server_url],
        env=env,
        cwd=str(home),
        encoding="utf-8",
        codec_errors="replace",
        timeout=HANDOFF_TIMEOUT_S,
        dimensions=(40, 160),
    )
    proc.logfile_read = fh
    return LoginSession(workspace, home, transcript_path, url_file, proc, fh)


def callback_origin(authorize_url: str) -> tuple[str, int]:
    """Return the (host, port) of the ``redirect_uri`` the CLI put in *authorize_url*."""
    redirect = parse_qs(urlsplit(authorize_url).query)["redirect_uri"][0]
    parts = urlsplit(redirect)
    return parts.hostname or "localhost", parts.port or 80


@dataclass(eq=False)
class BrowserDrive(threading.Thread):
    """Open *authorize_url* in Chromium and record what happens to the callback;
    *resolve_localhost_to* points the browser's ``localhost`` elsewhere ("another machine")."""

    authorize_url: str
    callback_prefix: str
    wait_s: float
    resolve_localhost_to: str | None = None
    video_dir: Path | None = None
    screenshot: Path | None = None
    events: list[tuple[float, str, str]] = field(default_factory=list)
    callback_requested_at: float | None = None
    callback_response_at: float | None = None
    callback_status: int | None = None
    navigation: str = "not started"
    final_url: str = ""
    error: str = ""
    video_path: Path | None = None

    def __post_init__(self) -> None:
        threading.Thread.__init__(self, name="browser-drive", daemon=True)

    def run(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright

        t0 = time.monotonic()
        args = []
        if self.resolve_localhost_to:
            args.append(f"--host-resolver-rules=MAP localhost {self.resolve_localhost_to}")
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=args)
                context = browser.new_context(
                    ignore_https_errors=True,
                    viewport={"width": 1280, "height": 800},
                    record_video_dir=str(self.video_dir) if self.video_dir else None,
                )
                page = context.new_page()

                def on_request(request: object) -> None:
                    url = request.url  # type: ignore[attr-defined]
                    self.events.append((time.monotonic() - t0, "request", url))
                    if url.startswith(self.callback_prefix) and self.callback_requested_at is None:
                        self.callback_requested_at = time.monotonic()

                def on_response(response: object) -> None:
                    url = response.url  # type: ignore[attr-defined]
                    status = response.status  # type: ignore[attr-defined]
                    self.events.append((time.monotonic() - t0, f"response {status}", url))
                    if url.startswith(self.callback_prefix) and self.callback_response_at is None:
                        self.callback_response_at = time.monotonic()
                        self.callback_status = status

                def on_failed(request: object) -> None:
                    failure = request.failure  # type: ignore[attr-defined]
                    self.events.append((time.monotonic() - t0, f"failed {failure}", request.url))  # type: ignore[attr-defined]

                page.on("request", on_request)
                page.on("response", on_response)
                page.on("requestfailed", on_failed)
                try:
                    page.goto(self.authorize_url, timeout=int(self.wait_s * 1000))
                    self.navigation = "completed"
                except PlaywrightTimeout:
                    self.navigation = f"still pending after {self.wait_s:.0f}s"
                self.final_url = page.url
                if self.screenshot is not None:
                    try:
                        page.screenshot(path=str(self.screenshot), timeout=10_000)
                    except Exception as exc:
                        self.error = f"screenshot: {exc}"
                video = page.video
                context.close()
                if video is not None and self.video_dir is not None:
                    self.video_path = Path(video.path())
                browser.close()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.navigation = "driver error"


def _wait_for(predicate, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.mark.timeout(300)
def test_login_completes_when_browser_callback_reaches_cli(tmp_path: Path) -> None:
    """Control: on one machine the callback is answered and the terminal logs in."""
    run_dir = evidence_dir(tmp_path / "control")
    with FakeDatabricksWorkspace(run_dir / "certs") as workspace:
        session = start_login(workspace, run_dir)
        try:
            authorize_url = session.expect_browser_handoff()
            host, port = callback_origin(authorize_url)
            drive = BrowserDrive(
                authorize_url,
                callback_prefix=f"http://{host}:{port}",
                wait_s=HEALTHY_LOGIN_TIMEOUT_S,
                screenshot=run_dir / "browser-final.png",
            )
            drive.start()
            drive.join(HEALTHY_LOGIN_TIMEOUT_S + 15)
            assert drive.callback_response_at is not None, (
                f"callback never answered: navigation={drive.navigation} error={drive.error} "
                f"events={drive.events}"
            )
            session.proc.expect(r"Logged in", timeout=HEALTHY_LOGIN_TIMEOUT_S)
        finally:
            session.close()


@pytest.mark.timeout(300)
def test_login_reports_when_browser_callback_cannot_reach_cli(tmp_path: Path) -> None:
    """The terminal must say something actionable, not block silently, when the
    browser's callback to localhost:8020 cannot reach the CLI listener."""
    run_dir = evidence_dir(tmp_path / "unreachable")
    with FakeDatabricksWorkspace(run_dir / "certs") as workspace:
        session = start_login(workspace, run_dir)
        hole: BlackHoleListener | None = None
        try:
            authorize_url = session.expect_browser_handoff()
            host, port = callback_origin(authorize_url)
            # The browser's `localhost` lands here: a port something owns but nothing serves.
            hole = BlackHoleListener("127.0.0.2", port)
            drive = BrowserDrive(
                authorize_url,
                callback_prefix=f"http://{host}:{port}",
                wait_s=STALL_OBSERVATION_S,
                resolve_localhost_to="127.0.0.2",
                video_dir=run_dir / "video",
                screenshot=run_dir / "browser-final.png",
            )
            drive.start()
            assert _wait_for(lambda: drive.callback_requested_at is not None, timeout=30), (
                f"browser never reached the callback: navigation={drive.navigation} "
                f"error={drive.error} events={drive.events}"
            )
            terminal_output = session.collect_output(STALL_OBSERVATION_S, until=GUIDANCE_RE)
            drive.join(STALL_OBSERVATION_S + 30)
            alive = session.proc.isalive()
            (run_dir / "browser-events.txt").write_text(
                "\n".join(f"{t:7.2f}s {kind} {url}" for t, kind, url in drive.events) + "\n"
            )

            assert drive.callback_response_at is None and hole.accepted >= 1, (
                "the emulated unreachable callback did not stall as intended: "
                f"status={drive.callback_status} accepted={hole.accepted} events={drive.events}"
            )
            # A silent exit is no better than a silent wait: the guidance must have been printed.
            transcript = session.transcript()
            assert GUIDANCE_RE.search(transcript), (
                "Omnigent gave the user nothing to act on while the browser callback could not "
                f"reach the CLI: {STALL_OBSERVATION_S:.0f}s after the browser hit "
                f"{drive.final_url or 'the callback'} the terminal printed "
                f"{terminal_output!r} and the process "
                f"{'was still waiting' if alive else 'had exited'}.\n--- transcript ---\n"
                f"{transcript}"
            )
            assert alive, f"the login gave up instead of waiting for the callback:\n{transcript}"
            for expected in (
                "Still waiting for the browser login",
                "ssh -L",
                "reload the browser tab",
            ):
                assert expected in transcript, f"guidance lacks {expected!r}:\n{transcript}"
        finally:
            if hole is not None:
                hole.close()
            session.close()
