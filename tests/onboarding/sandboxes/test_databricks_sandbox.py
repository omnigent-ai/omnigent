"""Unit tests for the Databricks Sandbox launcher.

The launcher drives the SDK's real ``SandboxAPI``; only the transport beneath
it (``ApiClient.do``) is an in-memory fake that records every request and
models the Sandbox lifecycle. These tests therefore pin the exact wire shapes
the SDK sends for the launcher and how it maps responses and errors.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import click
import pytest
from databricks.sdk.errors import NotFound
from databricks.sdk.service.sandbox import SandboxAPI

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes import databricks_sandbox as mod
from omnigent.onboarding.sandboxes.base import SandboxCapabilityError, SandboxGoneError
from omnigent.onboarding.sandboxes.databricks_sandbox import DatabricksSandboxLauncher
from omnigent.util.proxy_bearer import PROXY_BEARER_FILE_ENV_VAR, PROXY_BEARER_SERVER_URL_ENV_VAR

_EXEC_PREFIX = "/api/2.0/sandbox-exec/sandboxes/"


class _FakeApi:
    """In-memory stand-in for ``WorkspaceClient().api_client``."""

    def __init__(self) -> None:
        self._cfg = SimpleNamespace(workspace_id=None)
        self.calls: list[dict[str, Any]] = []
        self.states: dict[str, list[str]] = {}
        self.exec_results: list[dict[str, Any]] = []
        self.errors: dict[tuple[str, str], Exception] = {}

    def set_states(self, sandbox_id: str, *states: str) -> None:
        """Queue the states successive GETs report (the last one sticks)."""
        self.states[sandbox_id] = list(states)

    def do(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"method": method, "path": path, "query": query, "body": body})
        error = self.errors.get((method, path))
        if error is not None:
            raise error
        if method == "POST" and path == "/api/2.0/sandboxes":
            assert query is not None
            sid = query["sandbox_id"]
            self.states.setdefault(sid, ["SANDBOX_STATE_PENDING", "SANDBOX_STATE_RUNNING"])
            return {"name": f"sandboxes/{sid}", "status": {"state": "SANDBOX_STATE_PENDING"}}
        if method == "GET" and path.startswith("/api/2.0/sandboxes/"):
            sid = path.rsplit("/", 1)[1]
            if sid not in self.states:
                raise NotFound(f"Sandbox '{sid}' not found")
            queue = self.states[sid]
            state = queue.pop(0) if len(queue) > 1 else queue[0]
            return {"name": f"sandboxes/{sid}", "status": {"state": state}}
        if method == "POST" and path.startswith(_EXEC_PREFIX):
            if self.exec_results:
                return self.exec_results.pop(0)
            return {"status": "EXECUTE_COMMAND_STATUS_COMPLETED", "exit_code": 0, "stdout": ""}
        return {}

    def exec_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"].startswith(_EXEC_PREFIX)]


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_STATE_POLL_S", 0.0)


def _launcher(api: _FakeApi, **kwargs: Any) -> DatabricksSandboxLauncher:
    launcher = DatabricksSandboxLauncher(**kwargs)
    launcher._client = SimpleNamespace(
        api_client=api,
        sandbox=SandboxAPI(api),
        config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer launcher-token"}),
    )
    return launcher


def test_capabilities_declare_managed_resumable_terminable() -> None:
    caps = DatabricksSandboxLauncher().capabilities
    assert caps.managed_launch and caps.resume_stopped and caps.programmatic_terminate
    assert caps.streaming_exec and caps.foreground_exec
    assert not caps.cli_bootstrap and not caps.local_port_forward
    assert DatabricksSandboxLauncher.provider == "databricks"


def test_provision_creates_with_prefix_label_and_idle_timeout_then_waits() -> None:
    api = _FakeApi()
    launcher = _launcher(api, sandbox_id_prefix="team", inactivity_timeout_s=900)

    sandbox_id = launcher.provision("managed-abc")

    create = api.calls[0]
    assert create["method"] == "POST" and create["path"] == "/api/2.0/sandboxes"
    assert create["query"] == {"sandbox_id": sandbox_id}
    assert sandbox_id.startswith("team-")
    assert create["body"] == {
        "display_name": "managed-abc",
        "spec": {"compute": {"inactivity_timeout": "900s"}},
    }
    assert [c["method"] for c in api.calls[1:]] == ["GET", "GET"]


def test_provision_without_idle_timeout_omits_spec() -> None:
    api = _FakeApi()
    _launcher(api).provision("managed-abc")
    assert api.calls[0]["body"] == {"display_name": "managed-abc"}


def test_provision_terminates_when_sandbox_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeApi()
    monkeypatch.setattr(mod, "_STATE_TIMEOUT_S", 0.0)
    launcher = _launcher(api)
    original = api.do

    def do(method: str, path: str, **kw: Any) -> dict[str, Any]:
        if method == "POST" and path == "/api/2.0/sandboxes":
            api.set_states(kw["query"]["sandbox_id"], "SANDBOX_STATE_PENDING")
        return original(method, path, **kw)

    api.do = do  # type: ignore[method-assign]
    with pytest.raises(click.ClickException, match="did not reach"):
        launcher.provision("managed-abc")
    assert api.calls[-1]["method"] == "DELETE"


def test_provision_keeps_the_original_error_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi()
    monkeypatch.setattr(mod, "_STATE_TIMEOUT_S", 0.0)
    launcher = _launcher(api)
    original = api.do

    def do(method: str, path: str, **kw: Any) -> dict[str, Any]:
        if method == "POST" and path == "/api/2.0/sandboxes":
            api.set_states(kw["query"]["sandbox_id"], "SANDBOX_STATE_PENDING")
        if method == "DELETE":
            raise RuntimeError("delete failed")
        return original(method, path, **kw)

    api.do = do  # type: ignore[method-assign]
    with pytest.raises(click.ClickException, match="did not reach"):
        launcher.provision("managed-abc")


def test_run_sends_bash_with_home_prelude_timeout_and_envs() -> None:
    api = _FakeApi()
    api.exec_results = [
        {"status": "EXECUTE_COMMAND_STATUS_COMPLETED", "exit_code": 0, "stdout": "hi\n"}
    ]
    result = _launcher(api).run("sb-1", "echo hi", envs={"SECRET": "s3cr3t"})

    (call,) = api.exec_calls()
    assert call["path"] == "/api/2.0/sandbox-exec/sandboxes/sb-1/exec-sync"
    body = call["body"]
    assert body["cmd"] == "/bin/bash" and body["args"][0] == "-c"
    assert body["args"][1].startswith(mod._HOME_PRELUDE) and body["args"][1].endswith("echo hi")
    assert body["execution_timeout"] == f"{mod._EXEC_TIMEOUT_S}s"
    assert body["envs"] == {"SECRET": "s3cr3t"}
    assert "s3cr3t" not in " ".join(body["args"])
    assert (result.returncode, result.stdout) == (0, "hi\n")


def test_run_omits_envs_when_none() -> None:
    api = _FakeApi()
    _launcher(api).run("sb-1", "true")
    assert "envs" not in api.exec_calls()[0]["body"]


@pytest.mark.parametrize(
    ("response", "returncode", "stderr_note"),
    [
        ({"status": "EXECUTE_COMMAND_STATUS_COMPLETED", "exit_code": 5}, 5, None),
        ({"status": "EXECUTE_COMMAND_STATUS_TIMED_OUT"}, 124, "timed out"),
        ({"status": "EXECUTE_COMMAND_STATUS_FAILED"}, 1, None),
        # A status this SDK does not know, or none at all, must not read as success.
        ({"status": "EXECUTE_COMMAND_STATUS_START_FAILED"}, 1, None),
        ({}, 1, None),
        (
            {"status": "EXECUTE_COMMAND_STATUS_COMPLETED", "exit_code": 0, "truncated": True},
            0,
            "truncated",
        ),
    ],
)
def test_run_maps_exec_status(
    response: dict[str, Any], returncode: int, stderr_note: str | None
) -> None:
    api = _FakeApi()
    api.exec_results = [response]
    result = _launcher(api).run("sb-1", "cmd", check=False)
    assert result.returncode == returncode
    if stderr_note:
        assert stderr_note in result.stderr


def test_run_check_raises_with_detail() -> None:
    api = _FakeApi()
    api.exec_results = [
        {"status": "EXECUTE_COMMAND_STATUS_COMPLETED", "exit_code": 2, "stderr": "boom"}
    ]
    with pytest.raises(click.ClickException, match=r"exit 2.*boom"):
        _launcher(api).run("sb-1", "false")


def test_run_on_missing_sandbox_raises_gone() -> None:
    api = _FakeApi()
    api.errors[("POST", f"{_EXEC_PREFIX}sb-x/exec-sync")] = NotFound("gone")
    with pytest.raises(SandboxGoneError):
        _launcher(api).run("sb-x", "true")


def test_resume_running_is_a_no_op() -> None:
    api = _FakeApi()
    api.set_states("sb-1", "SANDBOX_STATE_RUNNING")
    _launcher(api).resume("sb-1")
    assert [c["method"] for c in api.calls] == ["GET"]
    assert not any(c["path"].endswith("/start") for c in api.calls)


def test_resume_stopping_waits_for_stopped_then_starts() -> None:
    api = _FakeApi()
    api.set_states(
        "sb-1",
        "SANDBOX_STATE_STOPPING",
        "SANDBOX_STATE_STOPPING",
        "SANDBOX_STATE_STOPPED",
        "SANDBOX_STATE_RUNNING",
    )
    _launcher(api).resume("sb-1")
    starts = [c for c in api.calls if c["path"] == "/api/2.0/sandboxes/sb-1/start"]
    assert len(starts) == 1 and starts[0]["method"] == "POST"


def test_resume_tolerates_concurrent_start() -> None:
    api = _FakeApi()
    api.set_states("sb-1", "SANDBOX_STATE_STOPPED", "SANDBOX_STATE_RUNNING")
    api.errors[("POST", "/api/2.0/sandboxes/sb-1/start")] = RuntimeError(
        "Sandbox 'sb-1' is already active (state: RUNNING)"
    )
    _launcher(api).resume("sb-1")


def test_resume_missing_sandbox_raises_gone() -> None:
    with pytest.raises(SandboxGoneError):
        _launcher(_FakeApi()).resume("sb-missing")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("SANDBOX_STATE_RUNNING", True),
        ("SANDBOX_STATE_STOPPED", False),
        ("SANDBOX_STATE_STOPPING", False),
        ("SANDBOX_STATE_PENDING", True),
        ("SANDBOX_STATE_SOMETHING_NEW", None),
    ],
)
def test_is_running_maps_states(state: str, expected: bool | None) -> None:
    api = _FakeApi()
    api.set_states("sb-1", state)
    assert _launcher(api).is_running("sb-1") is expected


def test_is_running_missing_is_false() -> None:
    assert _launcher(_FakeApi()).is_running("sb-missing") is False


def test_terminate_is_idempotent_and_surfaces_other_errors() -> None:
    api = _FakeApi()
    launcher = _launcher(api)
    api.errors[("DELETE", "/api/2.0/sandboxes/sb-1")] = NotFound("gone")
    launcher.terminate("sb-1")
    api.errors[("DELETE", "/api/2.0/sandboxes/sb-1")] = RuntimeError("permission denied")
    with pytest.raises(click.ClickException, match="permission denied"):
        launcher.terminate("sb-1")


def test_keep_alive_without_proxy_bearer_still_runs_a_command() -> None:
    """Any exec resets the platform's idle timer while the session is live."""
    api = _FakeApi()
    api.set_states("sb-1", "SANDBOX_STATE_RUNNING")
    assert _launcher(api).keep_alive("sb-1") is True
    (call,) = api.exec_calls()
    assert call["body"]["args"][1].endswith("true")


def test_keep_alive_refreshes_bearer_via_envs_only_when_running() -> None:
    api = _FakeApi()
    api.set_states("sb-1", "SANDBOX_STATE_RUNNING")
    api.exec_results = [
        {
            "status": "EXECUTE_COMMAND_STATUS_COMPLETED",
            "exit_code": 0,
            "stdout": "/home/u/.omnigent/proxy-bearer",
        }
    ]
    assert _launcher(api, proxy_bearer=True).keep_alive("sb-1") is True
    (call,) = api.exec_calls()
    assert call["body"]["envs"] == {"OMNIGENT_PROXY_BEARER_VALUE": "launcher-token"}
    assert "launcher-token" not in " ".join(call["body"]["args"])
    assert "umask 077" in call["body"]["args"][1]


def test_keep_alive_soft_fails_when_the_bearer_write_fails() -> None:
    api = _FakeApi()
    api.set_states("sb-1", "SANDBOX_STATE_RUNNING")
    api.exec_results = [
        {"status": "EXECUTE_COMMAND_STATUS_COMPLETED", "exit_code": 1, "stderr": "disk full"}
    ]
    assert _launcher(api, proxy_bearer=True).keep_alive("sb-1") is False


def test_keep_alive_soft_fails_without_writing_when_no_bearer_can_be_minted() -> None:
    api = _FakeApi()
    api.set_states("sb-1", "SANDBOX_STATE_RUNNING")
    launcher = _launcher(api, proxy_bearer=True)
    launcher._client.config = SimpleNamespace(authenticate=dict)
    assert launcher.keep_alive("sb-1") is False
    assert api.exec_calls() == []


def test_keep_alive_never_execs_into_a_stopped_sandbox() -> None:
    """exec-sync auto-starts a stopped sandbox; keepalive must not wake it."""
    api = _FakeApi()
    api.set_states("sb-1", "SANDBOX_STATE_STOPPED")
    assert _launcher(api, proxy_bearer=True).keep_alive("sb-1") is None
    assert api.exec_calls() == []


def _stdout(text: str) -> dict[str, Any]:
    return {"status": "EXECUTE_COMMAND_STATUS_COMPLETED", "exit_code": 0, "stdout": text}


def test_start_host_bootstraps_and_launches_with_secrets_in_envs() -> None:
    api = _FakeApi()
    api.exec_results = [
        _stdout(""),  # pkill any previous host
        _stdout("/home/u/.omnigent/proxy-bearer"),  # proxy bearer write
        _stdout(""),  # bootstrap command
        _stdout("/home/u"),  # $HOME probe
        _stdout(""),  # mkdir workspace
        _stdout(""),  # host config write
        _stdout("launched\n"),  # detached host launch
    ]
    launcher = _launcher(api, proxy_bearer=True, bootstrap_command="echo bootstrap")
    stages: list[str] = []

    workspace = launcher.start_host(
        "sb-1",
        token="launch-token",
        host_id="host-1",
        host_name="managed-host-1",
        server_url="https://omnigent.example.com",
        host_config={},
        on_stage=stages.append,
    )

    assert workspace == "/home/u/workspace"
    calls = api.exec_calls()
    assert "pkill" in calls[0]["body"]["args"][1]
    assert calls[2]["body"]["args"][1].endswith("echo bootstrap")
    launch = calls[-1]["body"]
    assert "setsid nohup" in launch["args"][1] and "omnigent host --server" in launch["args"][1]
    assert launch["envs"] == {
        HOST_TOKEN_ENV_VAR: "launch-token",
        HOST_ID_ENV_VAR: "host-1",
        HOST_NAME_ENV_VAR: "managed-host-1",
        PROXY_BEARER_FILE_ENV_VAR: "/home/u/.omnigent/proxy-bearer",
        PROXY_BEARER_SERVER_URL_ENV_VAR: "https://omnigent.example.com",
    }
    for call in calls:
        argv = " ".join(call["body"]["args"])
        assert "launch-token" not in argv and "launcher-token" not in argv
    assert "starting" in stages


def test_start_host_without_proxy_bearer_sets_no_bearer_env() -> None:
    api = _FakeApi()
    api.exec_results = [_stdout(""), _stdout("/home/u"), _stdout(""), _stdout(""), _stdout("")]
    _launcher(api).start_host(
        "sb-1",
        token="launch-token",
        host_id="host-1",
        host_name="managed-host-1",
        server_url="https://omnigent.example.com",
    )
    launch = api.exec_calls()[-1]["body"]
    assert PROXY_BEARER_FILE_ENV_VAR not in launch["envs"]
    assert PROXY_BEARER_SERVER_URL_ENV_VAR not in launch["envs"]
    assert not any("proxy-bearer" in " ".join(c["body"]["args"]) for c in api.exec_calls())


def test_host_launch_logs_under_home_and_holds_a_lock() -> None:
    """$HOME survives a stop; the lock makes a replayed launch exit, not double-start."""
    api = _FakeApi()
    api.exec_results = [_stdout(""), _stdout("/home/u")]  # pkill, $HOME probe
    _launcher(api).start_host(
        "sb-1",
        token="launch-token",
        host_id="host-1",
        host_name="managed-host-1",
        server_url="https://omnigent.example.com",
    )
    launch = api.exec_calls()[-1]["body"]["args"][1]
    assert 'mkdir -p "$HOME/.omnigent";' in launch
    assert 'setsid nohup flock -n "$HOME/.omnigent/host.lock" sh -c ' in launch
    assert '>> "$HOME/.omnigent/host.log"' in launch


@pytest.mark.parametrize(
    "call",
    [
        lambda launcher: launcher.prepare(),
        lambda launcher: launcher.is_running("sb-1"),
        lambda launcher: launcher.resume("sb-1"),
        lambda launcher: launcher.run("sb-1", "true"),
    ],
    ids=["prepare", "is_running", "resume", "run"],
)
def test_sdk_without_the_sandbox_service_asks_for_an_upgrade(
    monkeypatch: pytest.MonkeyPatch, call: Any
) -> None:
    import databricks.sdk.service

    # Hide the module both ways `from databricks.sdk.service import sandbox` finds it.
    monkeypatch.delattr(databricks.sdk.service, "sandbox")
    monkeypatch.setitem(sys.modules, "databricks.sdk.service.sandbox", None)
    with pytest.raises(click.ClickException, match=r"databricks-sdk\[sandbox\]>=0\.153"):
        call(DatabricksSandboxLauncher())


# ── Streaming exec ──────────────────────────────────────────────────────────


class _FakeCommand:
    """Stand-in for the SDK's ``CommandProcess``: lines, a pid, an exit code."""

    def __init__(self, lines: list[str], *, exit_code: int | None = 0) -> None:
        self.stdout = iter(lines)
        self.pid = 4242
        self.exit_code = exit_code
        self.closed = 0

    def wait(self) -> int:
        assert self.exit_code is not None
        return self.exit_code

    def close(self) -> None:
        self.closed += 1


def _streaming(api: _FakeApi, command: _FakeCommand | Exception) -> tuple[Any, list[dict]]:
    launcher = _launcher(api)
    started: list[dict[str, Any]] = []

    def execute_command(name: str, cmd: str, **kwargs: Any) -> _FakeCommand:
        started.append({"name": name, "cmd": cmd, **kwargs})
        if isinstance(command, Exception):
            raise command
        return command

    launcher._client.sandbox.execute_command = execute_command
    return launcher, started


def test_stream_exec_streams_merged_output_and_exit_code() -> None:
    from databricks.sdk.mixins.sandbox import StreamMode

    api = _FakeApi()
    command = _FakeCommand(["Visit https://login\n", "done\n"], exit_code=3)
    launcher, started = _streaming(api, command)

    process = launcher.stream_exec("sb-1", "omnigent login https://srv")
    assert list(process.lines) == ["Visit https://login\n", "done\n"]
    assert process.wait() == 3
    process.close()

    (call,) = started
    assert call["name"] == "sandboxes/sb-1"
    assert call["args"][1].endswith("omnigent login https://srv")
    assert call["stderr"] is StreamMode.STDOUT
    assert command.closed == 1
    assert api.exec_calls() == []  # finished: nothing to kill


def test_stream_exec_close_stops_a_running_command_once() -> None:
    api = _FakeApi()
    command = _FakeCommand([], exit_code=None)
    launcher, _ = _streaming(api, command)

    process = launcher.stream_exec("sb-1", "sleep 600")
    process.close()
    process.close()

    (kill,) = api.exec_calls()
    assert kill["body"]["args"][1].endswith("pkill -TERM -P 4242; kill -TERM 4242")
    assert command.closed == 1


def test_stream_exec_close_drops_the_stream_when_the_kill_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = _FakeCommand([], exit_code=None)
    launcher, _ = _streaming(_FakeApi(), command)

    def _failing_kill(sandbox_id: str, pid: int) -> None:
        raise click.ClickException("owner disconnected")

    launcher._kill_tree = _failing_kill
    process = launcher.stream_exec("sb-1", "sleep 600")
    process.close()
    process.close()

    assert command.closed == 1
    assert "owner disconnected" in caplog.text


def test_stream_exec_refuses_a_pty() -> None:
    launcher, started = _streaming(_FakeApi(), _FakeCommand([]))
    with pytest.raises(SandboxCapabilityError):
        launcher.stream_exec("sb-1", "omnigent login https://srv", pty=True)
    assert started == []


class _GrpcNotFound(Exception):
    """Shape of a ``grpc.RpcError`` for a missing sandbox."""

    def code(self) -> Any:
        return SimpleNamespace(name="NOT_FOUND")


@pytest.mark.parametrize("error", [NotFound("gone"), _GrpcNotFound()], ids=["rest", "grpc"])
def test_stream_exec_on_missing_sandbox_raises_gone(error: Exception) -> None:
    launcher, _ = _streaming(_FakeApi(), error)
    with pytest.raises(SandboxGoneError):
        launcher.stream_exec("sb-1", "true")


def test_exec_foreground_echoes_output_and_returns_the_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher, started = _streaming(_FakeApi(), _FakeCommand(["host up\n"], exit_code=0))

    assert launcher.exec_foreground("sb-1", "omnigent host --server https://srv") == 0

    assert "host up" in capsys.readouterr().out
    (call,) = started
    assert call["args"][1].endswith("exec omnigent host --server https://srv")
    assert call["envs"] == {"TERM": "xterm-256color"}


def test_exec_foreground_ctrl_c_stops_the_remote_command() -> None:
    api = _FakeApi()
    command = _FakeCommand([], exit_code=None)

    def interrupted() -> Any:
        raise KeyboardInterrupt
        yield

    command.stdout = interrupted()
    launcher, _ = _streaming(api, command)

    with pytest.raises(KeyboardInterrupt):
        launcher.exec_foreground("sb-1", "omnigent host --server https://srv")

    (kill,) = api.exec_calls()
    assert "kill -TERM 4242" in kill["body"]["args"][1]
