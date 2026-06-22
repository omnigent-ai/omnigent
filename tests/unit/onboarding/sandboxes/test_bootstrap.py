"""Tests for :mod:`omnigent.onboarding.sandboxes.bootstrap`."""

from __future__ import annotations

import shlex
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import click
import httpx
import pytest

from omnigent.onboarding.sandboxes import bootstrap as bootstrap_mod
from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxCapabilityError,
    SandboxLauncher,
)
from omnigent.onboarding.sandboxes.bootstrap import (
    DEFAULT_SANDBOX_NAME,
    DerivedWorkspace,
    _extract_oauth_url,
    _loopback_port_from_authorize_url,
    _read_login_url,
    bootstrap_sandbox_host,
    build_wheels,
    connect_sandbox_host,
    derive_workspace,
    login_app_oauth_in_sandbox,
    ship_wheels,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# ── Fake launcher plumbing ──────────────────────────────────


class _FakeRemoteProcess(RemoteProcess):
    """
    Canned :class:`RemoteProcess` for login-flow tests.

    :param lines: Output lines the process streams (a single shared
        iterator, matching the production contract that repeated
        ``lines`` reads resume the same stream).
    :param returncode: Exit code ``wait()`` returns.
    """

    def __init__(self, lines: list[str], *, returncode: int = 0) -> None:
        self._lines = iter(lines)
        self._returncode = returncode
        self.closed = False

    @property
    def lines(self) -> Iterator[str]:
        """The shared line iterator (resumes across reads)."""
        return self._lines

    def wait(self) -> int:
        """Return the canned exit code."""
        return self._returncode

    def close(self) -> None:
        """Record that the caller cleaned the process up."""
        self.closed = True


@dataclass
class _PutCall:
    """
    One recorded ``put`` invocation.

    :param local_path: Local file shipped.
    :param remote_path: Sandbox destination path.
    """

    local_path: Path
    remote_path: str


@dataclass
class _StreamCall:
    """
    One recorded ``stream_exec`` invocation.

    :param command: Remote shell command.
    :param pty: Whether a PTY was requested.
    """

    command: str
    pty: bool


class _FakeLauncher(SandboxLauncher):
    """
    Recording :class:`SandboxLauncher` for bootstrap-flow tests.

    Every primitive appends a compact entry to :attr:`log` (for
    ordering assertions) and records its arguments in a typed list.
    The OAuth forward is recorded but performs no real networking.
    """

    provider: ClassVar[str] = "fake"
    # This fake overrides forward_local_port with a recorder, so it
    # must advertise the capability or the bootstrap fail-fasts.
    supports_local_port_forward: ClassVar[bool] = True

    def __init__(
        self,
        *,
        login_lines: list[str] | None = None,
        login_returncode: int = 0,
        exec_foreground_returncode: int = 0,
    ) -> None:
        """
        :param login_lines: Lines the fake login process streams.
        :param login_returncode: Exit code of the fake login process.
        :param exec_foreground_returncode: Exit code of
            ``exec_foreground``.
        """
        self._login_lines = list(login_lines or [])
        self._login_returncode = login_returncode
        self._exec_foreground_returncode = exec_foreground_returncode
        self.log: list[str] = []
        self.run_commands: list[str] = []
        self.puts: list[_PutCall] = []
        self.stream_calls: list[_StreamCall] = []
        self.stream_processes: list[_FakeRemoteProcess] = []
        self.forwarded_ports: list[int] = []
        self.foreground_commands: list[str] = []

    def prepare(self) -> None:
        """Record the preflight call."""
        self.log.append("prepare")

    def provision(self, name: str) -> str:
        """Record provisioning and return a fixed id."""
        self.log.append(f"provision:{name}")
        return "sb-new"

    def attach(self, sandbox_id: str) -> None:
        """Record the attach call."""
        self.log.append(f"attach:{sandbox_id}")

    def keep_alive(self, sandbox_id: str) -> None:
        """Record the keep-alive call."""
        self.log.append(f"keep_alive:{sandbox_id}")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Record the remote command and report success."""
        self.log.append(f"run:{command}")
        self.run_commands.append(command)
        return RemoteCommandResult(returncode=0, stdout="", stderr="")

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """Record the file upload."""
        self.log.append(f"put:{remote_path}")
        self.puts.append(_PutCall(local_path=local_path, remote_path=remote_path))

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """Record the spawn and hand back the canned login process."""
        self.log.append(f"stream:{command}")
        self.stream_calls.append(_StreamCall(command=command, pty=pty))
        process = _FakeRemoteProcess(self._login_lines, returncode=self._login_returncode)
        self.stream_processes.append(process)
        return process

    @contextmanager
    def forward_local_port(self, sandbox_id: str, port: int) -> Iterator[None]:
        """Record the forward window (no real tunnel)."""
        self.forwarded_ports.append(port)
        self.log.append(f"forward_enter:{port}")
        try:
            yield
        finally:
            self.log.append(f"forward_exit:{port}")

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """Record the foreground command and return the canned code."""
        self.log.append(f"foreground:{command}")
        self.foreground_commands.append(command)
        return self._exec_foreground_returncode

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """Marker command proving the launcher hook supplied the pip line."""
        return f"FAKE-INSTALL {remote_tgz_path}"


class _NoForwardLauncher(_FakeLauncher):
    """
    Fake launcher that does NOT support local port forwarding,
    exercising the bootstrap's fail-fast on the capability flag (the
    Modal shape).
    """

    provider: ClassVar[str] = "no-forward"
    supports_local_port_forward: ClassVar[bool] = False

    # Reuse SandboxLauncher's default rather than _FakeLauncher's
    # recording override.
    forward_local_port = SandboxLauncher.forward_local_port


# ── OAuth URL/port parsing helpers ──────────────────────────

_AUTHORIZE_URL = (
    "https://example.databricks.com/oidc/v1/authorize?"
    "client_id=databricks-cli&code_challenge=Wl9sLW4&code_challenge_method=S256&"
    "redirect_uri=http%3A%2F%2Flocalhost%3A8022&response_type=code&"
    "scope=offline_access+all-apis&state=ITgOCJ"
)


def test_extract_oauth_url_pulls_url_from_pty_line() -> None:
    """A PTY line wrapped in ANSI codes still yields the bare URL."""
    line = f"\x1b[2m{_AUTHORIZE_URL}\x1b[0m\r\n"
    assert _extract_oauth_url(line) == _AUTHORIZE_URL


def test_extract_oauth_url_ignores_non_authorize_lines() -> None:
    """Banner / prose lines (no authorize URL) yield ``None``."""
    assert _extract_oauth_url("Please continue in your browser:\r\n") is None
    # A different https URL without the authorize path must not match —
    # otherwise we'd forward against a port that isn't the callback.
    assert _extract_oauth_url("https://example.databricks.com/login\r\n") is None


def test_loopback_port_parsed_from_authorize_url() -> None:
    """The dynamic callback port is read from the encoded redirect_uri."""
    assert _loopback_port_from_authorize_url(_AUTHORIZE_URL) == 8022


def test_loopback_port_raises_when_redirect_uri_absent() -> None:
    """A URL missing redirect_uri must fail loud, not guess a port."""
    with pytest.raises(click.ClickException) as exc:
        _loopback_port_from_authorize_url(
            "https://example.databricks.com/oidc/v1/authorize?client_id=databricks-cli"
        )
    assert "callback port" in str(exc.value)


def test_read_login_url_returns_first_authorize_url() -> None:
    """Scanning login output stops at the first authorize URL line."""
    lines = [
        "  Provisioning your sandbox... (0s)\r\n",
        "Please continue the authentication process in your browser:\r\n",
        f"{_AUTHORIZE_URL}\r\n",
        "ignored-trailing-line\r\n",
    ]
    assert _read_login_url(lines) == _AUTHORIZE_URL


def test_read_login_url_returns_none_when_no_url_printed() -> None:
    """
    A stream that ends without an authorize URL yields ``None`` — not
    an exception: ``omnigent login`` legitimately completes without a
    browser step when a cached workspace grant verifies, and the
    caller decides success vs. failure from the exit code.
    """
    assert _read_login_url(["some output\r\n", "another line\r\n"]) is None


# ── login_app_oauth_in_sandbox ──────────────────────────────


def test_login_skips_entirely_when_skip_flag() -> None:
    """``skip=True`` must not touch the launcher at all."""
    launcher = _FakeLauncher()
    login_app_oauth_in_sandbox(
        launcher,
        "sb-1",
        server_url="https://app.example.com",
        workspace=DerivedWorkspace(host="https://ws.example.com", workspace_id="123456"),
        skip=True,
    )
    # Any launcher interaction here means --no-auth didn't skip auth
    # (a non-None workspace must not sneak the cfg-seed step past skip).
    assert launcher.log == []


def test_login_requires_server_url_unless_skipped() -> None:
    """A missing server URL must fail loud, not invent a default."""
    launcher = _FakeLauncher()
    with pytest.raises(click.ClickException) as exc:
        login_app_oauth_in_sandbox(launcher, "sb-1", server_url=None, workspace=None)
    # The remediation must name both the missing flag and the
    # --no-auth escape hatch.
    assert "--server" in str(exc.value)
    assert "--no-auth" in str(exc.value)
    # Validation happens before any sandbox interaction.
    assert launcher.log == []


def test_login_runs_in_sandbox_and_forwards_callback_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Golden path: run ``omnigent login`` inside the box with a forced
    PTY, parse the dynamic callback port from the printed URL, forward
    exactly that port BEFORE opening the URL locally, and clean the
    process up.
    """
    launcher = _FakeLauncher(
        login_lines=[
            "Please continue the authentication process in your browser:\r\n",
            f"{_AUTHORIZE_URL}\r\n",
            "Authentication successful!\r\n",
        ],
    )
    opened_urls: list[str] = []

    def _fake_open(url: str) -> bool:
        """Record the URL the browser would have opened."""
        opened_urls.append(url)
        launcher.log.append("browser_open")
        return True

    monkeypatch.setattr(bootstrap_mod.webbrowser, "open", _fake_open)

    # The CLI derives the workspace once per command and threads it
    # down — the login itself performs no derivation (and no HTTP).
    login_app_oauth_in_sandbox(
        launcher,
        "sb-1",
        server_url="https://app.example.com",
        workspace=DerivedWorkspace(host="https://ws.example.com", workspace_id="123456"),
    )

    # The login runs INSIDE the sandbox with a forced PTY — and it is
    # `omnigent login <server>` (which infers the fronting workspace
    # itself), NOT a raw `databricks auth login` with profile flags.
    assert launcher.stream_calls == [
        _StreamCall(
            command="omnigent login https://app.example.com",
            pty=True,
        )
    ]
    # The forward maps the DYNAMIC port parsed from the URL (8022), not
    # a hardcoded 8020 — this is the core of the callback fix.
    assert launcher.forwarded_ports == [8022]
    # The browser must open inside the forward window: if it opens
    # before forward_enter, the OAuth redirect can race the tunnel.
    assert launcher.log.index("forward_enter:8022") < launcher.log.index("browser_open")
    assert launcher.log.index("browser_open") < launcher.log.index("forward_exit:8022")
    assert opened_urls == [_AUTHORIZE_URL]
    # The login process was cleaned up after the flow.
    assert launcher.stream_processes[0].closed is True
    # Exactly one remote pre-step: reset ~/.databrickscfg to a single
    # credential-less [DEFAULT] entry shaped like what `databricks
    # auth login` itself writes (host + auth_type + workspace_id).
    # The baked PAT must go (it shadows the minted OAuth grant in
    # host-keyed resolution and the Apps edge 302s it); a host entry
    # must exist (`databricks auth login` stalls on an interactive
    # profile-name prompt without one); auth_type = databricks-cli
    # pins resolution to the CLI token cache so the minted grant is
    # actually found ("no token resolves" otherwise).
    expected_cfg_body = (
        "[DEFAULT]\nhost = https://ws.example.com\n"
        "auth_type = databricks-cli\nworkspace_id = 123456\n"
    )
    assert launcher.run_commands == [
        f"rm -f ~/.databrickscfg && printf '%s' {shlex.quote(expected_cfg_body)} "
        "> ~/.databrickscfg"
    ]
    # The seed lands before the login spawn (the login reads the cfg).
    assert launcher.log.index(f"run:{launcher.run_commands[0]}") < launcher.log.index(
        "stream:omnigent login https://app.example.com"
    )


def test_login_completes_without_browser_when_no_url_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``omnigent login`` reuses a cached workspace grant when one
    verifies against the server — it then exits 0 without printing an
    authorize URL. The flow must treat that as success: no port
    forward, no browser, no error.
    """
    launcher = _FakeLauncher(
        login_lines=["Logged in as user@example.com.\r\n"],
        login_returncode=0,
    )

    def _fail_open(url: str) -> bool:
        """The browser must never open when no URL was printed."""
        raise AssertionError(f"browser opened unexpectedly: {url}")

    monkeypatch.setattr(bootstrap_mod.webbrowser, "open", _fail_open)
    login_app_oauth_in_sandbox(
        launcher,
        "sb-1",
        server_url="https://app.example.com",
        workspace=DerivedWorkspace(host="https://ws.example.com", workspace_id="123456"),
    )
    # No callback forward was stood up — there is no redirect to bridge.
    assert launcher.forwarded_ports == []
    # The login process was still reaped.
    assert launcher.stream_processes[0].closed is True


def test_login_skips_cfg_seed_for_non_databricks_server() -> None:
    """
    Accounts / OIDC / header-auth servers are not Databricks-fronted
    (the CLI's derivation returned ``None``, so it threads
    ``workspace=None`` down): the sandbox's ~/.databrickscfg must be
    left alone — wiping the baked credential would cost in-sandbox
    workspace API access for zero auth benefit.
    """
    launcher = _FakeLauncher(
        login_lines=["Logged in.\r\n"],
        login_returncode=0,
    )
    login_app_oauth_in_sandbox(
        launcher, "sb-1", server_url="https://oss.example.com", workspace=None
    )
    # No remote pre-step ran: the cfg was not touched.
    assert launcher.run_commands == []
    # The in-sandbox login itself still ran.
    assert len(launcher.stream_calls) == 1


def test_login_seed_omits_workspace_id_line_when_unknown() -> None:
    """
    When the workspace didn't reveal its org id (``workspace_id`` is
    ``None``), the cfg seed must omit the ``workspace_id`` line while
    keeping host + auth_type — writing ``workspace_id = None`` would
    poison the profile, and dropping the whole seed would re-expose
    the baked-PAT shadowing the seed exists to fix.
    """
    launcher = _FakeLauncher(
        login_lines=["Logged in.\r\n"],
        login_returncode=0,
    )
    login_app_oauth_in_sandbox(
        launcher,
        "sb-1",
        server_url="https://app.example.com",
        workspace=DerivedWorkspace(host="https://ws.example.com", workspace_id=None),
    )
    # Exactly one remote pre-step ran (the cfg seed) — zero would mean
    # the missing org id wrongly disabled the seed entirely.
    assert len(launcher.run_commands) == 1
    seed_command = launcher.run_commands[0]
    assert "host = https://ws.example.com" in seed_command
    assert "auth_type = databricks-cli" in seed_command
    # No workspace_id line in any form (the key name itself is absent).
    assert "workspace_id" not in seed_command


def test_derive_workspace_extracts_workspace_from_apps_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A Databricks Apps edge answers the unauthenticated probe with a
    302 to the workspace OIDC authorize endpoint — the derived
    coordinates must carry that workspace host (scheme + netloc only)
    plus the org id read from the workspace itself.
    """
    response = httpx.Response(
        302,
        headers={"location": "https://ws.example.com/oidc/oauth2/v2.0/authorize?client_id=x"},
    )
    monkeypatch.setattr(bootstrap_mod, "_probe_server", lambda url: response)
    org_probes: list[str] = []

    def _fake_org_id(workspace_host: str) -> str:
        """Record which host was probed and return a canned org id."""
        org_probes.append(workspace_host)
        return "654321"

    monkeypatch.setattr(bootstrap_mod, "_workspace_org_id", _fake_org_id)
    assert derive_workspace("https://myapp.example.com") == DerivedWorkspace(
        host="https://ws.example.com", workspace_id="654321"
    )
    # The org-id probe must target the DERIVED workspace host, not the
    # app server — the Apps edge doesn't stamp the workspace org id.
    assert org_probes == ["https://ws.example.com"]


def test_derive_workspace_none_when_server_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An unreachable server yields ``None`` (no cfg seed) — the
    in-sandbox login then surfaces the real connectivity error instead
    of this pre-step guessing at one. The org-id probe must not run
    either: there is no workspace host to probe.
    """
    monkeypatch.setattr(bootstrap_mod, "_probe_server", lambda url: None)

    def _explode(workspace_host: str) -> str:
        """Fail the test if the org-id probe runs without a host."""
        raise AssertionError(f"org-id probe ran for unreachable server: {workspace_host}")

    monkeypatch.setattr(bootstrap_mod, "_workspace_org_id", _explode)
    assert derive_workspace("https://down.example.com") is None


@contextmanager
def _login_page_server(org_id_header: str | None) -> Iterator[str]:
    """
    Serve ``GET /login.html`` on an ephemeral loopback port, optionally
    stamping the ``x-databricks-org-id`` response header — a real-socket
    workspace stand-in so :func:`_workspace_org_id` (whose httpx.get
    has no module-local indirection to patch) is tested over a real
    HTTP round trip.

    :param org_id_header: Header value to stamp, or ``None`` to omit
        the header (the GA-workspace-misconfigured shape).
    :yields: The server's base URL, e.g. ``"http://127.0.0.1:54321"``.
    """

    class _Handler(BaseHTTPRequestHandler):
        """Minimal handler answering 200 with the optional org header."""

        def do_GET(self) -> None:  # http.server API requires this name
            """Answer 200, stamping x-databricks-org-id when configured."""
            self.send_response(200)
            if org_id_header is not None:
                self.send_header("x-databricks-org-id", org_id_header)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            """Silence per-request stderr logging."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize(
    ("org_id_header", "expected"),
    [
        # Header present → its value is the workspace id.
        ("654321", "654321"),
        # Header absent → None (cfg seed then omits workspace_id).
        (None, None),
    ],
)
def test_workspace_org_id_reads_header_from_login_page(
    org_id_header: str | None, expected: str | None
) -> None:
    """
    ``_workspace_org_id`` must return exactly the
    ``x-databricks-org-id`` header from an unauthenticated GET of the
    workspace's ``/login.html`` — and ``None`` when the workspace
    doesn't stamp it. Exercised against a real loopback HTTP server
    (no httpx patching), so a wrong path or header name fails here.
    """
    with _login_page_server(org_id_header) as workspace_host:
        assert bootstrap_mod._workspace_org_id(workspace_host) == expected


def test_login_raises_when_no_url_and_nonzero_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A login that dies BEFORE printing the authorize URL (e.g. the
    server is unreachable from the sandbox) must surface the exit code
    and the sandbox id — and must have echoed the login's own output,
    which is the only evidence of WHY it died (a swallowed stream
    leaves the user with nothing but "exited with code 1").
    """
    launcher = _FakeLauncher(
        login_lines=["Could not reach https://app.example.com/v1/me\r\n"],
        login_returncode=1,
    )
    with pytest.raises(click.ClickException) as exc:
        login_app_oauth_in_sandbox(
            launcher, "sb-1", server_url="https://app.example.com", workspace=None
        )
    assert "sb-1" in str(exc.value)
    assert launcher.stream_processes[0].closed is True
    # The in-sandbox error line was echoed through, not swallowed.
    assert "Could not reach https://app.example.com/v1/me" in capsys.readouterr().out


def test_login_raises_when_in_sandbox_login_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonzero login exit surfaces as ClickException with the id."""
    launcher = _FakeLauncher(
        login_lines=[f"{_AUTHORIZE_URL}\r\n"],
        login_returncode=1,
    )
    monkeypatch.setattr(bootstrap_mod.webbrowser, "open", lambda url: True)
    # workspace=None: this test's subject is the nonzero-exit error
    # path, not the cfg seed (None = no seed step, the non-Databricks
    # shape).
    with pytest.raises(click.ClickException) as exc:
        login_app_oauth_in_sandbox(
            launcher, "sb-1", server_url="https://app.example.com", workspace=None
        )
    assert "sb-1" in str(exc.value)
    # The failed login process must still be reaped on the error path.
    assert launcher.stream_processes[0].closed is True


def test_login_fails_fast_for_unforwardable_provider() -> None:
    """
    A launcher without local-port forwarding (the Modal shape) must
    surface the SandboxCapabilityError naming --no-auth BEFORE touching
    the sandbox — spawning the in-sandbox login first would strand a
    process waiting on a callback that can never arrive.
    """
    launcher = _NoForwardLauncher(login_lines=[f"{_AUTHORIZE_URL}\r\n"])
    with pytest.raises(SandboxCapabilityError) as exc:
        login_app_oauth_in_sandbox(
            launcher,
            "sb-1",
            server_url="https://app.example.com",
            workspace=DerivedWorkspace(host="https://ws.example.com", workspace_id="123456"),
        )
    # The message must name the provider and explain the limitation.
    assert "no-forward" in str(exc.value)
    assert "App auth" in str(exc.value)
    # Fail-fast: NOTHING ran against the sandbox (no login spawn).
    assert launcher.log == []


# ── build_wheels ────────────────────────────────────────────


def test_build_wheels_rebuilds_over_stale_tarball(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A pre-existing tarball must NOT short-circuit the build — the
    sandbox must always get the current checkout's code. (An earlier
    existence-based cache forced users to reason about a
    --rebuild-wheels flag, with silently shipping stale code as the
    failure mode.)
    """
    monkeypatch.setattr(bootstrap_mod.shutil, "which", lambda name: f"/fake/{name}")
    tgz = tmp_path / "wheels.tgz"
    tgz.write_bytes(b"stale-cache")

    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Stand-in uv build that writes a fresh wheel into out-dir."""
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        (out_dir / "pkg-1.0-py3-none-any.whl").write_bytes(b"fresh")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(bootstrap_mod.subprocess, "run", _fake_run)
    build_wheels(repo_root=tmp_path, tgz_path=tgz)
    # The stale bytes are gone — the tarball was rebuilt from fresh wheels.
    assert tgz.read_bytes() != b"stale-cache"


def test_build_wheels_invokes_uv_for_each_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Each entry in :data:`WHEEL_PACKAGE_PATHS` must trigger a separate
    ``uv build`` invocation in the correct working directory.
    """
    monkeypatch.setattr(bootstrap_mod.shutil, "which", lambda name: f"/fake/{name}")
    tgz = tmp_path / "wheels.tgz"

    @dataclass
    class _UvCall:
        """Captured cwd + argv from a fake uv build invocation."""

        argv: list[str] = field(default_factory=list)
        cwd: str = ""

    captured: list[_UvCall] = []

    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Stand-in that captures cwd + writes a fake wheel into out-dir."""
        captured.append(_UvCall(argv=argv, cwd=str(kwargs.get("cwd"))))
        # uv writes wheels to the staging dir — fake one per call so
        # the tarball-packing step has something to pick up.
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        (out_dir / f"pkg{len(captured)}-1.0-py3-none-any.whl").write_bytes(b"fake")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(bootstrap_mod.subprocess, "run", _fake_run)
    build_wheels(repo_root=tmp_path, tgz_path=tgz, pypi_proxy=None)
    # 3 cwds = 3 packages built (sdks/python-client, sdks/ui, .). If we see
    # 2, the root package build was dropped; if 1, only the first SDK ran.
    cwds_relative = [Path(c.cwd).relative_to(tmp_path).as_posix() for c in captured]
    assert cwds_relative == ["sdks/python-client", "sdks/ui", "."]
    # Tarball was actually produced (proves the pack step ran).
    assert tgz.exists()


def test_build_wheels_raises_when_uv_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Clear remediation when uv isn't installed."""
    monkeypatch.setattr(bootstrap_mod.shutil, "which", lambda name: None)
    with pytest.raises(click.ClickException) as exc:
        build_wheels(repo_root=tmp_path, tgz_path=tmp_path / "wheels.tgz")
    assert "astral.sh/uv/install.sh" in str(exc.value)


# ── ship_wheels ─────────────────────────────────────────────


def test_ship_wheels_puts_installs_and_persists_path(tmp_path: Path) -> None:
    """
    Ship must perform exactly three remote steps in order: upload the
    tarball, run the LAUNCHER-SUPPLIED install command (image-specific
    pip flags live behind the launcher hook), then persist
    ``~/.local/bin`` on the sandbox PATH.
    """
    wheels_tgz = tmp_path / "oa-wheels.tgz"
    wheels_tgz.write_bytes(b"")
    launcher = _FakeLauncher()

    ship_wheels(launcher, "sb-1", wheels_tgz=wheels_tgz)

    assert launcher.puts == [_PutCall(local_path=wheels_tgz, remote_path="/tmp/oa-wheels.tgz")]
    # The marker command proves ship used launcher.wheel_install_command
    # with the shipped tarball path — if a generic pip line appears
    # instead, the provider's image-specific flags were bypassed.
    assert launcher.run_commands[0] == "FAKE-INSTALL /tmp/oa-wheels.tgz"
    # PATH persistence makes `omnigent` resolvable for later
    # `bash -lc` foreground runs.
    assert ".local/bin" in launcher.run_commands[1]
    # Upload must precede install (can't install a tarball that isn't
    # there yet).
    assert launcher.log.index("put:/tmp/oa-wheels.tgz") < launcher.log.index(
        "run:FAKE-INSTALL /tmp/oa-wheels.tgz"
    )


# ── connect_sandbox_host ────────────────────────────────────


def test_connect_runs_bare_host_command() -> None:
    """
    The foreground command must be the bare ``omnigent host --server
    <url>`` — ``omnigent host`` no longer takes ``--profile``, so any
    extra flag here makes the remote command exit with
    "no such option" and the sandbox never registers.
    """
    launcher = _FakeLauncher()
    connect_sandbox_host(
        launcher,
        "sb-1",
        server_url="https://app.example.com",
    )
    assert launcher.foreground_commands == ["omnigent host --server https://app.example.com"]


def test_connect_sets_host_name_before_connecting() -> None:
    """
    When ``host_name`` is set, connect must (a) edit the sandbox's
    ``~/.omnigent/config.yaml`` to use that name, and (b) THEN run
    ``omnigent host``. Order matters — the host reads config.yaml at
    startup.
    """
    launcher = _FakeLauncher()
    connect_sandbox_host(
        launcher,
        "sb-1",
        server_url="https://app.example.com",
        host_name="shivam-sandbox-1",
    )
    # The name edit is a python one-liner over config.yaml.
    name_command = launcher.run_commands[0]
    assert "python3" in name_command
    assert "shivam-sandbox-1" in name_command
    assert "config.yaml" in name_command
    # The edit lands before the host starts (which reads config.yaml).
    name_entry = next(entry for entry in launcher.log if entry.startswith("run:python3"))
    foreground_entry = next(entry for entry in launcher.log if entry.startswith("foreground:"))
    assert launcher.log.index(name_entry) < launcher.log.index(foreground_entry)


def test_connect_raises_on_nonzero_exit() -> None:
    """Foreground host failure must surface with the sandbox id."""
    launcher = _FakeLauncher(exec_foreground_returncode=1)
    with pytest.raises(click.ClickException) as exc:
        connect_sandbox_host(
            launcher,
            "sb-1",
            server_url="https://app.example.com",
        )
    # Error must include the sandbox id so multi-sandbox debugging is
    # feasible.
    assert "sb-1" in str(exc.value)


# ── bootstrap_sandbox_host orchestration ────────────────────


def _bootstrap_kwargs(tmp_path: Path) -> dict[str, Any]:
    """
    Build the keyword arguments shared by the orchestrator tests.

    :param tmp_path: pytest tmp directory used for the wheel tarball path.
    :returns: A ready-to-spread kwargs dict for ``bootstrap_sandbox_host``,
        including the CLI-derived ``workspace`` the orchestrator must
        thread down to the login step untouched.
    """
    return {
        "sandbox_name": DEFAULT_SANDBOX_NAME,
        "server_url": "https://app.example.com",
        "workspace": DerivedWorkspace(host="https://ws.example.com", workspace_id="123456"),
        "repo_root": tmp_path,
        "skip_auth": False,
    }


def test_bootstrap_runs_steps_in_order_and_returns_provisioned_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The orchestrator must invoke its steps in the documented order.
    The wheel/ship/login steps are replaced with recorders (their
    bodies are covered by their own tests); the launcher records its
    own primitives.
    """
    launcher = _FakeLauncher()

    def _record_build(repo_root: Path, **kwargs: Any) -> None:
        """Record the build step with its proxy source."""
        launcher.log.append(f"build:{kwargs['pypi_proxy']}")

    def _record_ship(launcher_arg: Any, sid: str, **kwargs: Any) -> None:
        """Record the ship step."""
        launcher.log.append(f"ship:{sid}")

    def _record_login(launcher_arg: Any, sid: str, **kwargs: Any) -> None:
        """Record the login step, including the threaded workspace pin."""
        launcher.log.append(
            f"login:{sid}:{kwargs['server_url']}:{kwargs['workspace'].host}:{kwargs['skip']}"
        )

    monkeypatch.setattr(bootstrap_mod, "build_wheels", _record_build)
    monkeypatch.setattr(bootstrap_mod, "ship_wheels", _record_ship)
    monkeypatch.setattr(bootstrap_mod, "login_app_oauth_in_sandbox", _record_login)

    sid = bootstrap_sandbox_host(launcher, sandbox_id=None, **_bootstrap_kwargs(tmp_path))

    # Provisioned sandbox id is threaded through to downstream steps.
    assert sid == "sb-new"
    # Auth happens INSIDE the sandbox, after the wheels are shipped.
    # build's pypi_proxy must come from the launcher (None for the
    # fake) — a hardcoded proxy here would leak the lakebox-only index
    # into every provider. The login entry carries the CLI-derived
    # workspace host — a missing/None segment there means the
    # orchestrator dropped the workspace pin on its way to the login.
    assert launcher.log == [
        "prepare",
        f"provision:{DEFAULT_SANDBOX_NAME}",
        "keep_alive:sb-new",
        "build:None",
        "ship:sb-new",
        "login:sb-new:https://app.example.com:https://ws.example.com:False",
    ], f"Step order is contract. Got: {launcher.log}"


def test_bootstrap_fails_fast_without_forward_capability(tmp_path: Path) -> None:
    """
    With auth requested, an unforwardable provider must fail BEFORE any
    step runs — otherwise the user only learns about --no-auth after
    paying for provisioning, the wheel build, and the ship.
    """
    launcher = _NoForwardLauncher()
    with pytest.raises(SandboxCapabilityError) as exc:
        bootstrap_sandbox_host(launcher, sandbox_id=None, **_bootstrap_kwargs(tmp_path))
    assert "App auth" in str(exc.value)
    # Nothing ran: no prepare, no provision, no build/ship.
    assert launcher.log == []


def test_bootstrap_no_auth_unblocks_unforwardable_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    skip_auth (the --no-auth flag) is the documented path for providers
    like Modal — the full bootstrap must run to completion with it.
    """
    launcher = _NoForwardLauncher()
    monkeypatch.setattr(bootstrap_mod, "build_wheels", lambda repo_root, **kwargs: None)
    monkeypatch.setattr(bootstrap_mod, "ship_wheels", lambda launcher_arg, sid, **kwargs: None)

    sid = bootstrap_sandbox_host(
        launcher,
        sandbox_id=None,
        **{**_bootstrap_kwargs(tmp_path), "skip_auth": True},
    )
    assert sid == "sb-new"
    # The provider-side steps all ran despite the missing capability.
    assert "prepare" in launcher.log
    assert "keep_alive:sb-new" in launcher.log


def test_bootstrap_attaches_to_existing_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Re-ship flow: when sandbox_id is set, provision is skipped and
    attach refreshes the provider's handle on the existing sandbox.
    """
    launcher = _FakeLauncher()
    monkeypatch.setattr(bootstrap_mod, "build_wheels", lambda repo_root, **kwargs: None)
    monkeypatch.setattr(bootstrap_mod, "ship_wheels", lambda launcher_arg, sid, **kwargs: None)

    sid = bootstrap_sandbox_host(
        launcher,
        sandbox_id="existing-sb",
        **{**_bootstrap_kwargs(tmp_path), "skip_auth": True},
    )
    assert sid == "existing-sb"
    # Critical invariant: the attach flow must never provision — that
    # would create a fresh sandbox on every re-ship of an existing one.
    assert not any(entry.startswith("provision") for entry in launcher.log), launcher.log
    assert "attach:existing-sb" in launcher.log
