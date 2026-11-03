"""Harness ``mcp login`` driven through a PTY: URL parsing, pasted redirects, lifecycle."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import psutil
import pytest

from omnigent.host import mcp_login
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
# does (OSC 8 hyperlink), then waits for a pasted redirect on its terminal.
FAKE_CLI = f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
log = Path(os.environ["FAKE_LOG"])
log.write_text(json.dumps({{"argv": sys.argv[1:], "tty": sys.stdin.isatty(),
    "env": sorted(k for k in os.environ if k.startswith("OMNIGENT_")), "pid": os.getpid()}}))
mode = os.environ.get("FAKE_MODE", "paste")
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


def _server(name: str = "demo", harness: str = "claude", plugin: str | None = None, **summary):
    fields = {"name": name, "harness": harness, "transport": "http", **summary}
    if plugin is not None:
        fields["plugin"] = plugin
    return ConfiguredMcpServer(fields, {"url": "https://mcp.example.test/mcp"})


async def _settled(logins: HostMcpLogins, login_id: str) -> dict:
    async with asyncio.timeout(budget(10)):
        while (status := logins.status(login_id))["state"] == "pending":
            await asyncio.sleep(0.02)
    return dict(status)


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
        (f"https://user:pw@auth.example.test/a?redirect_uri={_REDIRECT}\n", None),
    ],
)
def test_find_authorization_url(output: str, expected: str | None) -> None:
    found = find_authorization_url(output)
    assert (found[0] if found else None) == expected


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
        ("http://localhost:43117/callback?code=" + "a" * 4096, False),
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
    callback = "http://localhost:43117/callback?code=abc&state=s"
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


async def test_cli_without_a_sign_in_page_fails_start(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "silent")
    monkeypatch.setattr(mcp_login, "URL_WAIT_SECONDS", 0.5)
    logins = HostMcpLogins()
    started = await logins.start(_server())
    assert (started["state"], started["authorization_url"]) == ("failed", None)
    assert started["error"] == "The harness didn't offer a sign-in page"
    await _assert_gone(json.loads(fake_cli.read_text())["pid"])


async def test_cancel_restart_and_close_stop_the_cli(fake_cli: Path) -> None:
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


async def test_unsupported_servers_never_spawn(fake_cli: Path) -> None:
    with pytest.raises(LoginError) as refused:
        await HostMcpLogins().start(_server(harness="cursor"))
    assert refused.value.status == "invalid"
    assert not fake_cli.exists()
    assert await harness_sign_in_state(_server(harness="cursor")) is None
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
