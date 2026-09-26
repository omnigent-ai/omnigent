"""Tests for the opencode serve process manager + arg/env builders."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.opencode_native import app_server as appsrv
from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeCliNotFoundError,
    OpenCodeNativeServer,
    OpenCodeVersionError,
    build_opencode_serve_args,
    build_tui_command,
    check_opencode_version,
    filtered_server_env,
    find_opencode_cli,
    opencode_terminal_env,
    parse_opencode_version,
)


class _FakeStdin:
    """Stdin pipe stand-in; closing it can end the fake process."""

    def __init__(self, proc: _FakeProc) -> None:
        self._proc = proc
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self._proc.exits_on_stdin_close:
            self._proc.returncode = 0


class _FakeProc:
    """``Popen`` stand-in for a ``--stdio`` server."""

    pid = 4242

    def __init__(self, *, exits_on_stdin_close: bool = True) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.exits_on_stdin_close = exits_on_stdin_close
        self.stdin = _FakeStdin(self)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("opencode", timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Route the readiness probe's ``httpx.AsyncClient`` to *handler*."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        appsrv.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),  # type: ignore[arg-type]
    )


def test_parse_opencode_version() -> None:
    assert parse_opencode_version("opencode v2.0.18") == "2.0.18"
    assert parse_opencode_version("2.0.18") == "2.0.18"
    assert parse_opencode_version("v2.1.0-beta.1") == "2.1.0-beta.1"
    assert parse_opencode_version("no version here") is None


def test_check_version_in_range() -> None:
    check_opencode_version("2.0.0")
    check_opencode_version("2.0.18")
    check_opencode_version("2.9.99")


@pytest.mark.parametrize("version", ["1.17.7", "1.18.16", "1.99.0", "3.0.0"])
def test_check_version_out_of_range_raises(version: str) -> None:
    with pytest.raises(OpenCodeVersionError):
        check_opencode_version(version)


def test_check_version_unparsable_raises() -> None:
    with pytest.raises(OpenCodeVersionError):
        check_opencode_version("not-a-version")


def test_find_opencode_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda _name: None)
    with pytest.raises(OpenCodeCliNotFoundError):
        find_opencode_cli()


def test_find_opencode_cli_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert find_opencode_cli() == "/usr/bin/opencode"


def test_build_serve_args_uses_stdio() -> None:
    args = build_opencode_serve_args(hostname="127.0.0.1", port=49231)
    assert args == ["serve", "--hostname", "127.0.0.1", "--port", "49231", "--stdio"]


def test_build_tui_command() -> None:
    assert build_tui_command(
        "/usr/bin/opencode",
        base_url="http://127.0.0.1:49231",
        session_id="ses_1",
        workspace="/repo",
        extra_args=("--log-level", "debug"),
    ) == [
        "/usr/bin/opencode",
        "--server",
        "http://127.0.0.1:49231",
        "--session",
        "ses_1",
        "/repo",
        "--log-level",
        "debug",
    ]


def test_filtered_server_env_sets_xdg_and_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-key")
    monkeypatch.setenv("RANDOM_UNRELATED", "nope")
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert env["XDG_DATA_HOME"] == str(tmp_path / "xdg-data")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")
    assert env["OPENCODE_PASSWORD"] == "pw"
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw"
    assert "OPENCODE_SERVER_USERNAME" not in env
    assert env["ANTHROPIC_API_KEY"] == "secret-key"  # provider env passes through
    assert "RANDOM_UNRELATED" not in env  # unrelated env filtered out


def test_filtered_server_env_honors_runner_passthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "GH_TOKEN, GH_CONFIG_DIR, MISSING")
    monkeypatch.setenv("GH_TOKEN", "ghp_example")
    monkeypatch.setenv("GH_CONFIG_DIR", "/home/user/.config/gh")
    monkeypatch.setenv("UNLISTED_SECRET", "nope")

    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")

    assert env["GH_TOKEN"] == "ghp_example"
    assert env["GH_CONFIG_DIR"] == "/home/user/.config/gh"
    assert "MISSING" not in env
    assert "UNLISTED_SECRET" not in env


def test_filtered_server_env_drops_global_opencode_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Global OpenCode config env never leaks into the isolated session.

    ``OPENCODE_CONFIG`` / ``OPENCODE_CONFIG_CONTENT`` would re-introduce the
    parent shell's config/model/permission settings, defeating the
    per-session XDG isolation — so they are dropped even though they match
    the ``OPENCODE_`` passthrough prefix. Other ``OPENCODE_`` vars (and the
    server password we set) are unaffected.
    """
    monkeypatch.setenv("OPENCODE_CONFIG", "/home/user/.config/opencode/opencode.json")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"model": "evil/model"}')
    monkeypatch.setenv("OPENCODE_DISABLE_AUTOUPDATE", "1")
    monkeypatch.setenv(
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH", "OPENCODE_CONFIG,OPENCODE_CONFIG_CONTENT"
    )
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert "OPENCODE_CONFIG" not in env
    assert "OPENCODE_CONFIG_CONTENT" not in env
    # An unrelated OPENCODE_ var is still passed through (not config leakage).
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    # The per-session XDG dirs remain the only config source.
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")


def test_filtered_server_env_sets_per_session_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert env["OPENCODE_DB"] == str(tmp_path / "opencode.db")


def test_filtered_server_env_drops_inherited_opencode_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A parent's config dir, DB, or password never reach the isolated server."""
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/home/user/.config/opencode")
    monkeypatch.setenv("OPENCODE_DB", "/home/user/.local/share/opencode/opencode.db")
    monkeypatch.setenv("OPENCODE_PASSWORD", "parent-secret")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "parent-secret")
    monkeypatch.setenv(
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH", "OPENCODE_CONFIG_DIR,OPENCODE_DB,OPENCODE_PASSWORD"
    )
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert "OPENCODE_CONFIG_DIR" not in env
    assert env["OPENCODE_DB"] == str(tmp_path / "opencode.db")
    assert env["OPENCODE_PASSWORD"] == "pw"
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw"


def test_filtered_server_env_extra_env_may_set_opencode_config(tmp_path: Path) -> None:
    """Launcher-supplied OpenCode env is applied after the parent filter."""
    env = filtered_server_env(
        bridge_dir=tmp_path,
        auth_secret="pw",
        extra_env={"OPENCODE_CONFIG": str(tmp_path / "opencode.json")},
    )
    assert env["OPENCODE_CONFIG"] == str(tmp_path / "opencode.json")


def _server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> OpenCodeNativeServer:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    return OpenCodeNativeServer(
        bridge_dir=tmp_path,
        workspace=tmp_path,
        port=49231,
        verify_version=False,
    )


def test_build_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    server.port = 49231
    argv = server.build_argv()
    assert argv[0] == "/usr/bin/opencode"
    assert argv[1:] == ["serve", "--hostname", "127.0.0.1", "--port", "49231", "--stdio"]


def test_base_url_and_auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    assert server.base_url == "http://127.0.0.1:49231"
    assert server.auth_headers["Authorization"].startswith("Basic ")


def test_terminal_env_carries_password_under_both_names(tmp_path: Path) -> None:
    env = opencode_terminal_env(
        "pw", xdg_data_home=tmp_path / "data", xdg_config_home=tmp_path / "config"
    )
    assert env == {
        "OPENCODE_PASSWORD": "pw",
        "OPENCODE_SERVER_PASSWORD": "pw",
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }


def test_terminal_env_without_xdg_dirs() -> None:
    assert opencode_terminal_env("pw") == {
        "OPENCODE_PASSWORD": "pw",
        "OPENCODE_SERVER_PASSWORD": "pw",
    }


async def test_start_launches_stdio_server_with_stdin_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    started: dict[str, object] = {}

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        started["argv"] = argv
        started["stdin"] = kwargs.get("stdin")
        started["env"] = kwargs.get("env")
        return _FakeProc()

    async def fake_wait(self: OpenCodeNativeServer) -> None:
        started["ready"] = True

    monkeypatch.setattr(appsrv.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", fake_wait)
    await server.start()
    assert started["ready"] is True
    assert started["argv"][1] == "serve"
    assert "--stdio" in started["argv"]
    assert started["stdin"] == subprocess.PIPE
    assert started["env"]["OPENCODE_DB"] == str(tmp_path / "opencode.db")
    assert server.process is not None
    assert server.process.pid == 4242


async def test_start_closes_process_when_readiness_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelling the readiness wait must reap the OpenCode server."""
    server = _server(monkeypatch, tmp_path)

    process = _FakeProc(exits_on_stdin_close=False)
    readiness_started = asyncio.Event()

    async def parked_wait(self: OpenCodeNativeServer) -> None:
        readiness_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(appsrv.subprocess, "Popen", lambda argv, **kwargs: process)
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", parked_wait)

    start_task = asyncio.create_task(server.start())
    await readiness_started.wait()
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert process.terminated is True
    assert process.stdin.closed is True
    assert server.process is None


async def test_start_closes_process_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A readiness probe that gives up must not leave ``opencode serve`` running."""
    server = _server(monkeypatch, tmp_path)

    process = _FakeProc(exits_on_stdin_close=False)

    async def failing_wait(self: OpenCodeNativeServer) -> None:
        raise RuntimeError("opencode serve did not become ready")

    monkeypatch.setattr(appsrv.subprocess, "Popen", lambda argv, **kwargs: process)
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", failing_wait)

    with pytest.raises(RuntimeError, match="did not become ready"):
        await server.start()

    assert process.terminated is True
    assert process.stdin.closed is True
    assert server.process is None


async def test_start_raises_on_unsupported_version_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(appsrv, "resolve_opencode_version", lambda _path: "1.18.16")
    monkeypatch.delenv("OMNIGENT_OPENCODE_SKIP_VERSION_CHECK", raising=False)
    server = OpenCodeNativeServer(
        bridge_dir=tmp_path,
        workspace=tmp_path,
        port=49231,
        verify_version=True,
    )

    class _FakeProc:
        pid = 4242

        def poll(self) -> None:
            return None

    async def fake_wait(self: OpenCodeNativeServer) -> None:
        return None

    monkeypatch.setattr(appsrv.subprocess, "Popen", lambda argv, **kwargs: _FakeProc())
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", fake_wait)
    with pytest.raises(OpenCodeVersionError):
        await server.start()


async def test_start_skips_version_gate_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(appsrv, "resolve_opencode_version", lambda _path: "1.18.16")
    monkeypatch.setenv("OMNIGENT_OPENCODE_SKIP_VERSION_CHECK", "1")
    server = OpenCodeNativeServer(
        bridge_dir=tmp_path,
        workspace=tmp_path,
        port=49231,
        verify_version=True,
    )

    class _FakeProc:
        pid = 4242

        def poll(self) -> None:
            return None

    async def fake_wait(self: OpenCodeNativeServer) -> None:
        return None

    monkeypatch.setattr(appsrv.subprocess, "Popen", lambda argv, **kwargs: _FakeProc())
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", fake_wait)
    await server.start()
    assert server.version == "1.18.16"
    assert server.process is not None


def test_find_opencode_cli_absolute_executable(tmp_path: Path) -> None:
    exe = tmp_path / "opencode"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert appsrv.find_opencode_cli(str(exe)) == str(exe)


def test_resolve_opencode_version_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(
        appsrv.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="opencode v2.0.18\n", stderr=""),
    )
    assert appsrv.resolve_opencode_version("/x/opencode") == "2.0.18"


def test_resolve_opencode_version_run_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("cannot exec")

    monkeypatch.setattr(appsrv.subprocess, "run", _boom)
    with pytest.raises(appsrv.OpenCodeVersionError):
        appsrv.resolve_opencode_version("/x/opencode")


def test_resolve_opencode_version_unparseable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(
        appsrv.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="no version here", stderr=""),
    )
    with pytest.raises(appsrv.OpenCodeVersionError):
        appsrv.resolve_opencode_version("/x/opencode")


async def test_wait_until_ready_polls_api_info_and_records_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    server.process = _FakeProc()  # type: ignore[assignment]
    seen: list[tuple[str, str]] = []
    statuses = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization", "")))
        status = next(statuses)
        if status != 200:
            return httpx.Response(status, json={"code": "service_starting"})
        return httpx.Response(
            200, json={"version": "2.0.18", "pid": 1, "urls": [], "paths": {"tmp": "/tmp"}}
        )

    _mock_http(monkeypatch, handler)
    await server._wait_until_ready(attempts=3, delay=0)
    assert [path for path, _ in seen] == ["/api/info", "/api/info"]
    assert seen[0][1].startswith("Basic ")
    assert server.version == "2.0.18"


async def test_wait_until_ready_fails_fast_on_rejected_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    server.process = _FakeProc()  # type: ignore[assignment]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"_tag": "UnauthorizedError", "message": "no"})

    _mock_http(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="rejected the per-session password"):
        await server._wait_until_ready(attempts=5, delay=0)
    assert calls == ["/api/info"]


async def test_close_stops_server_by_closing_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    process = _FakeProc()
    server.process = process  # type: ignore[assignment]
    await server.close()
    assert process.stdin.closed is True
    assert process.terminated is False
    assert process.returncode == 0
    assert server.process is None


async def test_close_terminates_when_stdin_close_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    process = _FakeProc(exits_on_stdin_close=False)
    server.process = process  # type: ignore[assignment]
    await server.close()
    assert process.stdin.closed is True
    assert process.terminated is True
    assert server.process is None
