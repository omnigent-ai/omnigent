"""Harness ``mcp login`` driven through a PTY: URL parsing, pasted redirects, lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import psutil
import pytest

from omnigent.host import mcp_login
from omnigent.host.frames import MCP_LOGIN_MAX_CALLBACK, MCP_LOGIN_MAX_URL
from omnigent.host.mcp_inventory import ConfiguredMcpServer
from omnigent.host.mcp_login import (
    HostMcpLogins,
    LoginError,
    find_authorization_url,
    harness_server_name,
    harness_sign_in_state,
    valid_callback,
)
from tests._helpers.polling import poll_until_async
from tests.budgets import budget

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="harness sign-in needs a PTY")

_REDIRECT = "http%3A%2F%2Flocalhost%3A43117%2Fcallback"
_AUTH_URL = f"https://auth.example.test/authorize?client_id=c&redirect_uri={_REDIRECT}&state=s"

# Stands in for ``claude``/``codex``: logs its argv, prints the URL the way Claude Code
# does (OSC 8 hyperlink), then waits for a pasted redirect on its terminal. With
# FAKE_OLD_CLI it rejects ``--no-browser``; FAKE_MODE=listen waits on a loopback listener.
FAKE_CLI = f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
log = Path(os.environ["FAKE_LOG"])
if "--help" in sys.argv:
    with open(str(log) + ".help", "a") as probes:
        probes.write("probe\\n")
    if os.environ.get("FAKE_HELP_HANG"):
        time.sleep(60)
    print("Usage: mcp login <NAME>" + ("" if os.environ.get("FAKE_OLD_CLI") else " --no-browser"))
    sys.exit(int(os.environ.get("FAKE_HELP_EXIT", "0")))
if os.environ.get("FAKE_OLD_CLI") and "--no-browser" in sys.argv:
    print("error: unexpected argument '--no-browser' found")
    sys.exit(2)
log.write_text(json.dumps({{"argv": sys.argv[1:], "tty": sys.stdin.isatty(),
    "env": sorted(k for k in os.environ if k.startswith("OMNIGENT_")), "pid": os.getpid(),
    "browser": os.environ.get("BROWSER"), "codex_home": os.environ.get("CODEX_HOME")}}))
mode = os.environ.get("FAKE_MODE", "paste")
if mode == "listen":
    import http.server
    class Listener(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            with open(log.with_suffix(".received"), "a") as received:
                received.write(self.path + "\\n")
            time.sleep(float(os.environ.get("FAKE_LISTEN_DELAY", "0")))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    listener = http.server.HTTPServer(("127.0.0.1", 0), Listener)
    redirect = "http%3A%2F%2F127.0.0.1%3A" + str(listener.server_address[1]) + "%2Fcallback"
    print("Visit: https://auth.example.test/authorize?redirect_uri=" + redirect, flush=True)
    listener.handle_request()
    sys.exit(0 if "code=" in log.with_suffix(".received").read_text() else 3)
if sys.argv[1:3] in (["mcp", "get"], ["mcp", "list"]) and mode == "status_hang":
    time.sleep(60)
if sys.argv[1:3] == ["mcp", "get"]:
    print("demo:\\n  Type: http\\n  Status: " + os.environ["FAKE_STATUS"])
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
if sys.argv[1:3] == ["mcp", "list"]:
    print(os.environ["FAKE_LIST"])
    sys.exit(0)
if mode == "silent":
    time.sleep(60)
url = {_AUTH_URL!r}
link = "\\x1b]8;;" + url + "\\x07" + url + "\\x1b]8;;\\x07"
print("Visit this URL to authorize:\\n" + link, flush=True)
if mode == "fail":
    sys.exit(2)
if mode == "hang":
    time.sleep(60)
line = sys.stdin.readline().strip()
log.with_suffix(".pasted").write_text(line)
sys.exit(0 if "code=" in line else 3)
"""


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake-cli"
    script.write_text(FAKE_CLI)
    script.chmod(0o755)
    log = tmp_path / "cli.json"
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", str(script))
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", str(script))
    monkeypatch.setenv("OMNIGENT_RUNNER_TOKEN", "private")
    monkeypatch.setenv("FAKE_LOG", str(log))
    return log


def _server(
    name: str = "demo",
    harness: str = "claude",
    plugin: str | None = None,
    shared_name: bool = False,
    **summary,
):
    fields = {"name": name, "harness": harness, "transport": "http", **summary}
    if plugin is not None:
        fields["plugin"] = plugin
    return ConfiguredMcpServer(
        fields, {"url": "https://mcp.example.test/mcp"}, shared_name=shared_name
    )


async def _settled(logins: HostMcpLogins, login_id: str) -> dict:
    await poll_until_async(
        lambda: logins.status(login_id)["state"] != "pending",
        timeout_s=budget(10),
        interval_s=0.02,
    )
    return dict(logins.status(login_id))


def _exited(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


async def _assert_gone(pid: int) -> None:
    await poll_until_async(lambda: _exited(pid), timeout_s=budget(5))


_LOOPBACK_AUTH_URL = f"http://127.0.0.1:9/authorize?redirect_uri={_REDIRECT}"


@pytest.mark.parametrize(
    "output,expected",
    [
        (f"Visit:\n\x1b]8;;{_AUTH_URL}\x07{_AUTH_URL}\x1b]8;;\x07\n", _AUTH_URL),
        (f"Authorize by opening this URL:\r\n{_AUTH_URL}\r\n", _AUTH_URL),
        (f"\x1b[1m{_AUTH_URL}\x1b[0m\n", _AUTH_URL),
        # A loopback authorization server may use plain http; a remote one may not.
        (f"{_LOOPBACK_AUTH_URL}\n", _LOOPBACK_AUTH_URL),
        (f"http://auth.example.test/authorize?redirect_uri={_REDIRECT}\n", None),
        # A URL still arriving is not accepted until a delimiter follows it.
        (_AUTH_URL, None),
        ("https://auth.example.test/authorize?client_id=c\n", None),
        ("https://auth.example.test/a?redirect_uri=https%3A%2F%2Fevil.test%2Fcb\n", None),
        ("https://auth.example.test/a?redirect_uri=http%3A%2F%2Fevil.test%3A8080%2Fcb\n", None),
        (f"https://user:pw@auth.example.test/a?redirect_uri={_REDIRECT}\n", None),
        # The server won't relay a link longer than this, so the host doesn't offer it.
        (f"{_AUTH_URL}&pad={'a' * MCP_LOGIN_MAX_URL}\n", None),
    ],
)
def test_find_authorization_url(output: str, expected: str | None) -> None:
    found = find_authorization_url(output)
    assert (found[0] if found else None) == expected


_CALLBACK = "http://localhost:43117/callback?code="


def _padded(callback: str, size: int) -> str:
    """*callback* with a query parameter that makes it *size* bytes long."""
    return callback + "&pad=" + "a" * (size - len(callback.encode()) - len("&pad="))


@pytest.mark.parametrize(
    "callback,valid",
    [
        ("http://localhost:43117/callback?code=abc&state=s", True),
        ("http://localhost:43117/callback?error=access_denied", True),
        ("http://localhost:43117/callback?state=s", False),
        ("http://127.0.0.1:43117/callback?code=abc", False),
        ("http://localhost:43118/callback?code=abc", False),
        ("http://localhost:43117/other?code=abc", False),
        ("https://localhost:43117/callback?code=abc", False),
        ("http://localhost:43117/callback?code=abc\nx", False),
        (_padded(_CALLBACK + "abc", MCP_LOGIN_MAX_CALLBACK), True),
        (_padded(_CALLBACK + "abc", MCP_LOGIN_MAX_CALLBACK + 1), False),
        # The limit counts bytes, not characters.
        (_padded(_CALLBACK + "\u00e9", MCP_LOGIN_MAX_CALLBACK + 1), False),
    ],
)
def test_valid_callback(callback: str, valid: bool) -> None:
    found = find_authorization_url(_AUTH_URL + "\n")
    assert found is not None
    assert valid_callback(callback, found[1]) is valid


@pytest.mark.parametrize(
    "server,expected",
    [
        (_server(), "demo"),
        (_server(plugin="tools"), "plugin:tools:demo"),
        (_server(plugin="tools", shared_name=True), None),
        (_server(harness="codex"), "demo"),
        (_server(harness="codex", plugin="tools"), None),
        (_server(harness="cursor"), None),
        (_server(name="--help"), None),
        (ConfiguredMcpServer({"name": "s", "harness": "claude", "transport": "stdio"}, {}), None),
    ],
)
def test_harness_server_name(server: ConfiguredMcpServer, expected: str | None) -> None:
    assert harness_server_name(server) == expected


async def test_pasted_redirect_completes_sign_in(fake_cli: Path) -> None:
    completed = []
    logins = HostMcpLogins(
        # Caches clear before a poll can observe the success.
        on_success=lambda: completed.append(logins.status(started["login_id"])["state"])
    )
    started = await logins.start(_server(plugin="tools"))
    assert started["state"] == "pending"
    assert started["authorization_url"] == _AUTH_URL
    recorded = json.loads(fake_cli.read_text())
    assert recorded["argv"] == ["mcp", "login", "plugin:tools:demo", "--no-browser"]
    assert recorded["tty"] is True
    assert recorded["env"] == []
    with pytest.raises(LoginError) as rejected:
        await logins.submit(started["login_id"], "http://localhost:1/callback?code=abc")
    assert rejected.value.status == "invalid"
    # Nothing listens on the redirect port, so the CLI reads it from its terminal,
    # which takes the longest accepted address whole.
    callback = _padded("http://localhost:43117/callback?code=abc&state=s", MCP_LOGIN_MAX_CALLBACK)
    await logins.submit(started["login_id"], callback)
    finished = await _settled(logins, started["login_id"])
    assert finished == {
        "login_id": started["login_id"],
        "state": "succeeded",
        "authorization_url": None,
        "error": None,
    }
    assert fake_cli.with_suffix(".pasted").read_text() == callback
    assert completed == ["pending"]
    with pytest.raises(LoginError):
        await logins.submit(started["login_id"], callback)
    await logins.close()


async def test_redirect_written_to_a_busy_terminal_arrives_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    real_write = os.write
    calls = []
    terminal = write_fd

    def short_write(fd: int, data: bytes) -> int:
        calls.append(len(data))
        if len(calls) == 2:
            raise BlockingIOError
        return real_write(fd, data[:10])

    monkeypatch.setattr(mcp_login.os, "write", short_write)
    try:
        await mcp_login._write_all(lambda: terminal, b"x" * 25)
        assert os.read(read_fd, 100) == b"x" * 25

        # A sign-in that ends mid-write gets no more of the address.
        def write_then_end(fd: int, data: bytes) -> int:
            nonlocal terminal
            terminal = -1
            return real_write(fd, data[:10])

        monkeypatch.setattr(mcp_login.os, "write", write_then_end)
        with pytest.raises(LoginError) as ended:
            await mcp_login._write_all(lambda: terminal, b"y" * 25)
        assert ended.value.status == "invalid"
        assert os.read(read_fd, 100) == b"y" * 10

        monkeypatch.setattr(mcp_login.os, "write", short_write)
        terminal = write_fd
        os.close(read_fd)
        calls.clear()
        with pytest.raises(LoginError) as failed:
            await mcp_login._write_all(lambda: terminal, b"x")
        assert failed.value.status == "failed"
    finally:
        os.close(write_fd)


async def test_sign_in_that_ends_during_delivery_gets_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pipe stands in for a file that reused the closed terminal's number.
    read_fd, reused_fd = os.pipe()
    terminal = reused_fd

    async def sign_in_ends_meanwhile(*_args: object, **_kwargs: object) -> None:
        nonlocal terminal
        terminal = -1
        raise httpx.ConnectError("no listener")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", sign_in_ends_meanwhile)
    try:
        with pytest.raises(LoginError) as ended:
            await mcp_login._deliver_callback(lambda: terminal, _CALLBACK)
        assert ended.value.status == "invalid"
        os.close(reused_fd)
        assert os.read(read_fd, 100) == b""
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("slow_listener", [False, True])
async def test_older_cli_gets_the_redirect_on_its_listener(
    fake_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    slow_listener: bool,
) -> None:
    for name in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name)
        # These loggers may not propagate to the root, where caplog listens.
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", [*logger.handlers, caplog.handler])
    monkeypatch.setenv("FAKE_OLD_CLI", "1")
    monkeypatch.setenv("FAKE_MODE", "listen")
    # The redirect goes straight to the CLI's listener, never through a proxy.
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    if slow_listener:
        # The listener has the code even when its reply is late, so it is sent once.
        monkeypatch.setenv("FAKE_LISTEN_DELAY", "1")
        monkeypatch.setattr(mcp_login, "CALLBACK_TIMEOUT_SECONDS", 0.3)
    typed: list[bytes] = []

    async def type_into_terminal(terminal: Callable[[], int], data: bytes) -> None:
        typed.append(data)

    monkeypatch.setattr(mcp_login, "_write_all", type_into_terminal)
    logins = HostMcpLogins()
    await logins.start(_server())
    started = await logins.start(_server())
    recorded = json.loads(fake_cli.read_text())
    assert (recorded["argv"], recorded["browser"]) == (["mcp", "login", "demo"], "true")
    # The flag check runs once per installed CLI build.
    assert Path(f"{fake_cli}.help").read_text().count("probe") == 1
    redirect = parse_qs(urlsplit(started["authorization_url"]).query)["redirect_uri"][0]
    callback = f"{redirect}?code=abc&state=s"
    if slow_listener:
        # A second Finish while the first is delivering must not resend the code.
        first, second = await asyncio.gather(
            logins.submit(started["login_id"], callback),
            logins.submit(started["login_id"], callback),
            return_exceptions=True,
        )
        assert isinstance(second, LoginError) and second.status == "invalid", (first, second)
    else:
        await logins.submit(started["login_id"], callback)
    finished = await _settled(logins, started["login_id"])
    assert finished["state"] == "succeeded"
    received = fake_cli.with_suffix(".received").read_text().splitlines()
    assert received == ["/callback?code=abc&state=s"]
    assert typed == []
    # The host's logs never hold the one-time code.
    assert "code=abc" not in caplog.text
    await logins.close()


@pytest.mark.parametrize("help_failure", ["FAKE_HELP_HANG", "FAKE_HELP_EXIT"])
async def test_an_unanswered_flag_check_leaves_the_flag_out(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, help_failure: str
) -> None:
    monkeypatch.setenv("FAKE_OLD_CLI", "1")
    monkeypatch.setenv("FAKE_MODE", "listen")
    monkeypatch.setenv(help_failure, "1")
    monkeypatch.setattr(mcp_login, "STATUS_TIMEOUT_SECONDS", 0.5)
    logins = HostMcpLogins()
    started = await logins.start(_server())
    assert started["state"] == "pending", started
    recorded = json.loads(fake_cli.read_text())
    assert (recorded["argv"], recorded["browser"]) == (["mcp", "login", "demo"], "true")
    await logins.close()


async def test_redirect_is_resent_only_after_a_failed_delivery(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered: list[str] = []

    async def deliver(terminal: Callable[[], int], callback_url: str) -> None:
        delivered.append(callback_url)
        if len(delivered) == 1:
            raise LoginError("failed", "Couldn't hand the address to the harness")

    monkeypatch.setattr(mcp_login, "_deliver_callback", deliver)
    logins = HostMcpLogins()
    started = await logins.start(_server())
    callback = f"{_CALLBACK}abc&state=s"
    with pytest.raises(LoginError) as failed:
        await logins.submit(started["login_id"], callback)
    assert failed.value.status == "failed"
    assert (await logins.submit(started["login_id"], callback))["state"] == "pending"
    # The CLI is still exchanging the code, so a later Finish must not resend it.
    with pytest.raises(LoginError) as repeated:
        await logins.submit(started["login_id"], callback)
    assert repeated.value.status == "invalid"
    assert delivered == [callback, callback]
    await logins.close()


@pytest.mark.parametrize(
    "mode,state,error",
    [
        ("fail", "failed", "The harness couldn't finish signing in"),
        ("hang", "expired", "Sign-in timed out"),
    ],
)
async def test_failed_and_expired_sign_in(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, mode: str, state: str, error: str
) -> None:
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setattr(mcp_login, "LOGIN_TIMEOUT_SECONDS", 0.5)
    logins = HostMcpLogins(on_success=lambda: pytest.fail("must not report success"))
    started = await logins.start(_server(harness="codex"))
    finished = await _settled(logins, started["login_id"])
    assert (finished["state"], finished["error"]) == (state, error)
    await _assert_gone(json.loads(fake_cli.read_text())["pid"])
    await logins.close()


async def test_codex_signs_in_to_the_home_that_lists_its_servers(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # A nested launch inherits its parent session's private home.
    private_home = home / ".omnigent" / "codex-native" / "abc" / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(private_home))
    logins = HostMcpLogins()
    await logins.start(_server(harness="codex"))
    assert json.loads(fake_cli.read_text())["codex_home"] == str(home / ".codex")
    await logins.close()


async def test_cli_without_a_sign_in_page_fails_start(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "silent")
    # Long enough for the flag check and spawn on a loaded runner.
    monkeypatch.setattr(mcp_login, "START_TIMEOUT_SECONDS", 3.0)
    logins = HostMcpLogins()
    started = await logins.start(_server())
    assert (started["state"], started["authorization_url"]) == ("failed", None)
    assert started["error"] == "The harness didn't offer a sign-in page"
    await _assert_gone(json.loads(fake_cli.read_text())["pid"])


async def test_queued_starts_finish_within_the_start_budget(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stalled flag check holds the start lock; neither start may outlive the budget.
    monkeypatch.setenv("FAKE_HELP_HANG", "1")
    monkeypatch.setenv("FAKE_MODE", "silent")
    monkeypatch.setattr(mcp_login, "START_TIMEOUT_SECONDS", 1.0)
    logins = HostMcpLogins()
    loop = asyncio.get_running_loop()
    began = loop.time()
    first, second = await asyncio.gather(
        logins.start(_server()), logins.start(_server("other")), return_exceptions=True
    )
    # Stopping the CLI may follow the budget, still well inside the server's wait.
    assert loop.time() - began < budget(1.0 + 2.5)
    assert isinstance(first, dict) and first["state"] == "failed"
    assert isinstance(second, LoginError) and second.status == "busy"
    assert [login.state for login in logins._logins.values()] == ["failed"]
    assert all(login.process.returncode is not None for login in logins._logins.values())


async def test_cancel_restart_and_close_stop_the_cli(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logins = HostMcpLogins()
    first = await logins.start(_server())
    first_pid = json.loads(fake_cli.read_text())["pid"]
    # Signing in to the same server again replaces the earlier attempt.
    second = await logins.start(_server())
    assert logins.status(first["login_id"])["state"] == "cancelled"
    await _assert_gone(first_pid)
    second_pid = json.loads(fake_cli.read_text())["pid"]
    assert (await logins.cancel(second["login_id"]))["state"] == "cancelled"
    await _assert_gone(second_pid)
    third = await logins.start(_server(name="other"))
    third_pid = json.loads(fake_cli.read_text())["pid"]
    # Past the retention window, ended sign-ins are forgotten but a pending one is kept.
    monkeypatch.setattr(mcp_login, "FINISHED_RETENTION_SECONDS", 0.0)
    for ended in (first, second):
        with pytest.raises(LoginError) as forgotten:
            logins.status(ended["login_id"])
        assert forgotten.value.status == "not_found"
    assert logins.status(third["login_id"])["state"] == "pending"
    await logins.close()
    await _assert_gone(third_pid)
    with pytest.raises(LoginError) as missing:
        logins.status(third["login_id"])
    assert missing.value.status == "not_found"


async def test_concurrent_sign_ins_are_bounded(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_login, "MAX_ACTIVE_LOGINS", 1)
    logins = HostMcpLogins()
    await logins.start(_server(name="one"))
    with pytest.raises(LoginError) as busy:
        await logins.start(_server(name="two"))
    assert busy.value.status == "busy"
    await logins.close()
    # Simultaneous starts for one server, e.g. a double-click, leave one CLI running.
    racing = HostMcpLogins()
    first, second = await asyncio.gather(racing.start(_server()), racing.start(_server()))
    states = sorted(racing.status(started["login_id"])["state"] for started in (first, second))
    assert states == ["cancelled", "pending"]
    await racing.close()


class _Tunnel:
    """Stands in for the tunnel connection that owns a start request."""


async def test_disconnect_stops_an_in_flight_start(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawning = asyncio.Event()
    release = asyncio.Event()
    spawn = HostMcpLogins._spawn

    pids = []

    async def paused_spawn(self, *args):
        spawning.set()
        await release.wait()
        login = await spawn(self, *args)
        pids.append(login.process.pid)
        return login

    monkeypatch.setattr(HostMcpLogins, "_spawn", paused_spawn)
    tunnel = _Tunnel()
    logins = HostMcpLogins()
    start = asyncio.create_task(logins.start(_server(), tunnel))
    await spawning.wait()
    close = asyncio.create_task(logins.close(tunnel))
    await asyncio.sleep(0)
    release.set()
    started, _ = await asyncio.gather(start, close)
    assert started["state"] == "cancelled"
    await _assert_gone(pids[0])
    assert not logins.pids()

    with pytest.raises(LoginError) as refused:
        await logins.start(_server(), tunnel)
    assert refused.value.status == "failed"
    assert len(pids) == 1
    # The next tunnel signs in normally.
    assert (await logins.start(_server(), _Tunnel()))["state"] == "pending"
    await logins.close()


@pytest.mark.parametrize("during", ["spawn", "url"])
async def test_cancelled_start_stops_its_cli(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, during: str
) -> None:
    if during == "url":
        monkeypatch.setenv("FAKE_MODE", "silent")
    spawned = asyncio.Event()
    spawn = HostMcpLogins._spawn

    pids = []

    async def observed_spawn(self, *args):
        login = await spawn(self, *args)
        pids.append(login.process.pid)
        spawned.set()
        if during == "spawn":
            # Still finishing the spawn when the request is cancelled.
            await asyncio.sleep(0.2)
        return login

    monkeypatch.setattr(HostMcpLogins, "_spawn", observed_spawn)
    logins = HostMcpLogins()
    start = asyncio.create_task(logins.start(_server()))
    await spawned.wait()
    if during == "url":
        await poll_until_async(lambda: bool(logins.pids()), timeout_s=budget(5))
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    await _assert_gone(pids[0])
    assert not logins.pids()
    await logins.close()


async def test_spawn_holds_the_reaper_guard_until_the_login_is_tracked(fake_cli: Path) -> None:
    tracked_on_release = []

    @contextlib.contextmanager
    def guard():
        try:
            yield
        finally:
            tracked_on_release.append(bool(logins.pids()))

    logins = HostMcpLogins(subprocess_guard=guard)
    await logins.start(_server())
    assert tracked_on_release == [True]
    await logins.close()


@pytest.mark.parametrize(
    "server", [_server(harness="cursor"), _server(plugin="tools", shared_name=True)]
)
async def test_unsupported_servers_never_spawn(
    fake_cli: Path, server: ConfiguredMcpServer
) -> None:
    with pytest.raises(LoginError) as refused:
        await HostMcpLogins().start(server)
    assert refused.value.status == "invalid"
    assert not fake_cli.exists()
    assert await harness_sign_in_state(server) is None
    assert not fake_cli.exists()


@pytest.mark.parametrize(
    "harness,env,expected",
    [
        ("claude", {"FAKE_STATUS": "✔ Connected"}, "signed_in"),
        ("claude", {"FAKE_STATUS": "! Needs authentication"}, "available"),
        ("claude", {"FAKE_STATUS": "x", "FAKE_EXIT": "1"}, None),
        (
            "codex",
            {"FAKE_LIST": json.dumps([{"name": "demo", "auth_status": "o_auth"}])},
            "signed_in",
        ),
        (
            "codex",
            {"FAKE_LIST": json.dumps([{"name": "demo", "auth_status": "not_logged_in"}])},
            "available",
        ),
        (
            "codex",
            {"FAKE_LIST": json.dumps([{"name": "demo", "auth_status": "bearer_token"}])},
            None,
        ),
        ("codex", {"FAKE_LIST": "[]"}, None),
        ("codex", {"FAKE_LIST": "not json"}, None),
    ],
)
async def test_harness_sign_in_state(
    fake_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    env: dict[str, str],
    expected: str | None,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert await harness_sign_in_state(_server(harness=harness)) == expected
    argv = json.loads(fake_cli.read_text())["argv"]
    assert argv == (["mcp", "get", "demo"] if harness == "claude" else ["mcp", "list", "--json"])


@pytest.mark.parametrize("harness", ["claude", "codex"])
async def test_hanging_status_check_offers_sign_in_and_stops_the_cli(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    monkeypatch.setenv("FAKE_MODE", "status_hang")
    server = _server(harness=harness)
    assert await harness_sign_in_state(server, timeout=budget(1.0)) == "available"
    assert _exited(json.loads(fake_cli.read_text())["pid"])
