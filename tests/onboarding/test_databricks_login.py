"""Tests for :mod:`omnigent.onboarding.databricks_login`.

The relay tests run a fake ``databricks`` CLI as a real subprocess: it prints
the authorize URL the way ``databricks auth login`` does and waits on a real
loopback listener, so the paste is delivered over an actual HTTP request.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from omnigent.onboarding import databricks_login
from omnigent.onboarding.databricks_login import (
    browser_is_remote,
    opens_browser,
    run_databricks_auth_login,
)
from tests._helpers.live_server import reserve_port

if TYPE_CHECKING:
    from collections.abc import Iterator

_FAKE_CLI = """
import http.server, os, sys, time, urllib.parse

mode, port = sys.argv[1], int(sys.argv[2])
if mode == "fail":
    print("Error: workspace unreachable", flush=True)
    sys.exit(2)
received = {}

class Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        received.update(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass

server = http.server.HTTPServer(("127.0.0.1", port), Callback)
server.timeout = 10
redirect = urllib.parse.quote(f"http://localhost:{port}", safe="")
print(f"BROWSER={os.environ.get('BROWSER')}")
print("Please complete authentication by opening this link in your browser:")
print(
    f"https://ws.example.com/oidc/v1/authorize?client_id=databricks-cli"
    f"&redirect_uri={redirect}&state=s1",
    flush=True,
)
if mode == "forwarded":
    time.sleep(0.5)
    sys.exit(0)
server.handle_request()
sys.exit(0 if received.get("code") == ["c1"] and received.get("state") == ["s1"] else 3)
"""


class _Stdin:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture()
def fake_cli(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_databricks.py"
    script.write_text(_FAKE_CLI)
    return [sys.executable, "-I", str(script)]


@pytest.fixture()
def pasted(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Route the helper's paste flow to a pipe; yields the pipe's write end."""
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(read_fd, "r"))
    monkeypatch.setattr(databricks_login, "browser_is_remote", lambda: True)
    for name in ("BROWSER", "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    yield write_fd
    os.close(write_fd)
    sys.stdin.close()


@pytest.mark.parametrize(
    ("env", "platform", "tty", "remote", "opens"),
    [
        ({"SSH_CONNECTION": "10.0.0.1 5 10.0.0.2 22"}, "darwin", True, True, False),
        ({}, "linux", True, True, True),
        (
            {"BROWSER": "none", "DISPLAY": ":0", "SSH_TTY": "/dev/pts/1"},
            "linux",
            True,
            True,
            False,
        ),
        ({"BROWSER": "www-browser", "SSH_TTY": "/dev/pts/1"}, "linux", True, False, True),
        ({"DISPLAY": ":0"}, "linux", True, False, True),
        ({}, "darwin", True, False, True),
        ({"SSH_CONNECTION": "10.0.0.1 5 10.0.0.2 22"}, "linux", False, False, True),
        ({"SSH_CONNECTION": "10.0.0.1 5 10.0.0.2 22"}, "win32", True, False, True),
    ],
    ids=[
        "ssh",
        "linux-no-display",
        "browser-disabled",
        "browser-shim",
        "linux-desktop",
        "mac-desktop",
        "no-tty",
        "windows",
    ],
)
def test_browser_is_remote(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    platform: str,
    tty: bool,
    remote: bool,
    opens: bool,
) -> None:
    for name in ("BROWSER", "SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=tty))

    assert browser_is_remote() is remote
    assert opens_browser() is opens


@pytest.mark.parametrize(
    ("ssh", "browser"), [(True, "none"), (False, "None")], ids=["ssh", "local"]
)
def test_pasted_redirect_reaches_the_cli_listener(
    fake_cli: list[str],
    pasted: int,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    ssh: bool,
    browser: str,
) -> None:
    if ssh:
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 5 10.0.0.2 22")
    with reserve_port() as sock:
        port = sock.getsockname()[1]
        os.write(
            pasted,
            b"https://ws.example.com/?code=c1&state=s1\n"
            + f"http://localhost:{port}/?code=c1&state=s1\n".encode(),
        )
    returncode = run_databricks_auth_login([*fake_cli, "callback", str(port)])

    out = capsys.readouterr().out
    assert returncode == 0
    assert f"BROWSER={browser}" in out
    assert out.index("/oidc/v1/authorize") < out.index("finish signing in by hand")
    assert f"That isn't the http://localhost:{port}/?code=... address" in out


def test_forwarded_callback_finishes_without_a_paste(
    fake_cli: list[str], pasted: int, capsys: pytest.CaptureFixture[str]
) -> None:
    with reserve_port() as sock:
        port = sock.getsockname()[1]
    returncode = run_databricks_auth_login([*fake_cli, "forwarded", str(port)])

    assert returncode == 0
    assert "finish signing in by hand" in capsys.readouterr().out


def test_cli_failure_before_the_link_skips_the_prompt(
    fake_cli: list[str], pasted: int, capsys: pytest.CaptureFixture[str]
) -> None:
    returncode = run_databricks_auth_login([*fake_cli, "fail", "0"])

    out = capsys.readouterr().out
    assert returncode == 2
    assert "Error: workspace unreachable" in out
    assert "finish signing in by hand" not in out
