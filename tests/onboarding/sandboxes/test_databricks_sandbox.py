"""Unit tests for the Databricks Sandbox launcher (REST command execution).

The SDK's generic ``ApiClient.do`` is replaced by an in-memory fake that
records every request and models the Sandbox lifecycle, so these tests pin the
exact REST shapes the launcher sends and how it maps responses and errors.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import click
import pytest
from databricks.sdk.errors import NotFound

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes import databricks_sandbox as mod
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.databricks_sandbox import DatabricksSandboxLauncher
from omnigent.util.proxy_bearer import PROXY_BEARER_FILE_ENV_VAR

_EXEC_PREFIX = "/api/2.0/sandbox-exec/sandboxes/"


class _FakeApi:
    """In-memory stand-in for ``WorkspaceClient().api_client``."""

    def __init__(self) -> None:
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
        config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer launcher-token"}),
    )
    return launcher


def test_capabilities_declare_managed_resumable_terminable() -> None:
    caps = DatabricksSandboxLauncher().capabilities
    assert caps.managed_launch and caps.resume_stopped and caps.programmatic_terminate
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
        ("SANDBOX_STATE_PENDING", False),
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


def test_keep_alive_without_proxy_bearer_does_nothing() -> None:
    api = _FakeApi()
    assert _launcher(api).keep_alive("sb-1") is None
    assert api.calls == []


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
    assert not any("proxy-bearer" in " ".join(c["body"]["args"]) for c in api.exec_calls())
