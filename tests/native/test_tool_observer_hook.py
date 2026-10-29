"""The post-tool observer hook posts to the runner's relay with curl, else runs Python."""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.app_server import _POLICY_HOOK_MODULE, _our_hooks_from_list
from omnigent.native.tool_observer_hook import hook_settings, main

_TOKEN = "relay-token"
_PAYLOAD = json.dumps(
    {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": "a.txt"}}
)
_DELIVERED = [
    {
        "path": "/hook/observe-tool",
        "authorization": f"Bearer {_TOKEN}",
        "content_type": "application/json",
        "body": _PAYLOAD.encode(),
    }
]

requires_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")


@contextmanager
def _recording_server() -> Iterator[tuple[str, list[dict[str, object]]]]:
    """Serve HTTP on loopback, recording each request."""
    received: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                }
            )
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def relay() -> Iterator[tuple[str, list[dict[str, object]]]]:
    """Stand in for the runner's tool relay."""
    with _recording_server() as server:
        yield server


@pytest.fixture
def proxy() -> Iterator[tuple[str, list[dict[str, object]]]]:
    """Stand in for an HTTP proxy the environment points at."""
    with _recording_server() as server:
        yield server


def _proxy_env(url: str) -> dict[str, str]:
    return dict.fromkeys(("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"), url)


def _write_relay_env(bridge_dir: Path, url: str) -> None:
    """Write ``tool_relay.env`` in the format the relay writes it."""
    (bridge_dir / "tool_relay.env").write_text(
        f"OMNIGENT_RELAY_URL='{url}'\nOMNIGENT_RELAY_TOKEN='{_TOKEN}'\n", encoding="utf-8"
    )


def _fake_python(directory: Path) -> Path:
    """Write an executable that records its arguments, stdin and one env var."""
    script = directory / "python"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(directory / "argv"))}\n'
        f"/bin/cat > {shlex.quote(str(directory / 'stdin'))}\n"
        f'printf "%s" "$OMNIGENT_TEST_WRAPPED" > {shlex.quote(str(directory / "wrapped"))}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _recording_curl(directory: Path) -> str:
    """Put a curl first on PATH that records its arguments and one env var, then runs curl."""
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    script = bin_dir / "curl"
    wrapped = shlex.quote(str(directory / "curl-wrapped"))
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(directory / "curl-argv"))}\n'
        f'printf "%s" "${{OMNIGENT_TEST_WRAPPED-unset}}" > {wrapped}\n'
        f'exec {shlex.quote(str(shutil.which("curl")))} "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def _wrapped(command: str) -> str:
    return f"env OMNIGENT_TEST_WRAPPED=1 {command}"


def _run(command: object, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a hook command as the harnesses do: through a shell, payload on stdin."""
    return subprocess.run(
        ["/bin/sh", "-c", str(command)],
        input=_PAYLOAD,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )


@requires_curl
def test_posts_the_event_to_the_relay_without_exposing_the_token(
    tmp_path: Path, relay: tuple[str, list[dict[str, object]]]
) -> None:
    url, received = relay
    _write_relay_env(tmp_path, url)
    home = tmp_path / "home"
    home.mkdir()
    trace = home / "trace.log"
    (home / ".curlrc").write_text(f'verbose\ntrace-ascii = "{trace}"\n', encoding="utf-8")
    hook = hook_settings(
        tmp_path, str(tmp_path / "unused-python"), _POLICY_HOOK_MODULE, wrap_python=_wrapped
    )

    result = _run(hook["command"], env={"PATH": _recording_curl(tmp_path), "HOME": str(home)})

    assert _TOKEN not in (tmp_path / "curl-argv").read_text()
    assert _TOKEN not in result.stderr
    assert not trace.exists()
    assert (tmp_path / "curl-wrapped").read_text() == "unset"
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert received == _DELIVERED


@requires_curl
def test_posts_the_event_directly_when_all_proxy_is_set(
    tmp_path: Path,
    relay: tuple[str, list[dict[str, object]]],
    proxy: tuple[str, list[dict[str, object]]],
) -> None:
    url, received = relay
    proxy_url, proxied = proxy
    _write_relay_env(tmp_path, url)
    hook = hook_settings(tmp_path, str(tmp_path / "unused-python"), _POLICY_HOOK_MODULE)

    result = _run(hook["command"], env={"PATH": os.environ["PATH"], **_proxy_env(proxy_url)})

    assert result.returncode == 0, result.stderr
    assert received == _DELIVERED
    assert proxied == []


def test_runs_the_hook_module_until_the_relay_writes_its_env_file(tmp_path: Path) -> None:
    python = _fake_python(tmp_path)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    hook = hook_settings(bridge_dir, str(python), _POLICY_HOOK_MODULE, wrap_python=_wrapped)

    result = _run(hook["command"])

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "argv").read_text().splitlines() == [
        "-I",
        "-m",
        _POLICY_HOOK_MODULE,
        "observe-tool",
        "--bridge-dir",
        str(bridge_dir),
    ]
    assert (tmp_path / "stdin").read_text() == _PAYLOAD
    assert (tmp_path / "wrapped").read_text() == "1"


def test_python_fallback_posts_the_event_directly_to_the_relay(
    tmp_path: Path,
    relay: tuple[str, list[dict[str, object]]],
    proxy: tuple[str, list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, received = relay
    proxy_url, proxied = proxy
    (tmp_path / "tool_relay.json").write_text(
        json.dumps({"url": url, "token": _TOKEN}), encoding="utf-8"
    )
    for name, value in _proxy_env(proxy_url).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_PAYLOAD))

    assert main(["--bridge-dir", str(tmp_path)]) == 0
    assert received == _DELIVERED
    assert proxied == []


def test_runs_the_hook_module_on_a_host_without_curl(
    tmp_path: Path, relay: tuple[str, list[dict[str, object]]]
) -> None:
    url, received = relay
    python = _fake_python(tmp_path)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _write_relay_env(bridge_dir, url)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    hook = hook_settings(bridge_dir, str(python), _POLICY_HOOK_MODULE)

    result = _run(hook["command"], env={"PATH": str(empty_path)})

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "stdin").read_text() == _PAYLOAD
    assert received == []


@requires_curl
def test_reports_a_missing_relay_on_stderr_and_still_exits_0(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    _write_relay_env(tmp_path, f"http://127.0.0.1:{closed_port}")
    hook = hook_settings(tmp_path, str(tmp_path / "unused-python"), _POLICY_HOOK_MODULE)

    result = _run(hook["command"])

    assert result.returncode == 0
    assert result.stdout == ""
    assert "curl:" in result.stderr


def test_codex_still_trusts_the_hook(tmp_path: Path) -> None:
    hook = hook_settings(tmp_path, "/venv/bin/python", _POLICY_HOOK_MODULE)
    listed = {
        "result": {"data": [{"cwd": "/w", "hooks": [{"key": "k", "command": hook["command"]}]}]}
    }

    assert _our_hooks_from_list(listed, "/w", _POLICY_HOOK_MODULE) == [
        {"key": "k", "command": hook["command"]}
    ]
