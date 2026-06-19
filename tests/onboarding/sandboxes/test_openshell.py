"""Tests for :mod:`omnigent.onboarding.sandboxes.openshell`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest

from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.openshell import (
    HOST_IMAGE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    OpenShellSandboxLauncher,
    _OpenShellClient,
)

# ── Shared fakes ────────────────────────────────────────────


@dataclass
class _FakeExecResult:
    """Stands in for ``openshell.ExecResult``."""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _FakeExecChunk:
    """Stands in for ``openshell.ExecChunk`` (one stdout/stderr fragment)."""

    stream: str
    data: bytes


# ── Launcher-level fake (the _OpenShellClient wrapper) ──────
#
# The launcher tests monkeypatch ``launcher._openshell`` to return this
# recorder, exercising the launcher's logic without the SDK.


@dataclass
class _FakeOpenShellAPI:
    """Recorder for the launcher-facing OpenShell wrapper."""

    create_kwargs: list[dict[str, Any]] = field(default_factory=list)
    exec_calls: list[tuple[str, list[str], bytes | None]] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    closed: bool = False
    exec_result: _FakeExecResult = field(default_factory=_FakeExecResult)
    created_name: str = "petname-test"
    background_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    foreground_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    foreground_exit_code: int = 0
    foreground_raises: BaseException | None = None

    def exec_background(self, name: str, command: list[str], *, timeout: int) -> None:
        self.background_calls.append((name, list(command)))

    def create_sandbox(self, *, image: str, env: dict[str, str]) -> str:
        self.create_kwargs.append({"image": image, "env": env})
        return self.created_name

    def run_foreground(self, name: str, command: list[str], *, timeout: int) -> int:
        self.foreground_calls.append((name, list(command)))
        if self.foreground_raises is not None:
            raise self.foreground_raises
        return self.foreground_exit_code

    def execute(
        self,
        name: str,
        command: list[str],
        *,
        stdin: bytes | None = None,
        timeout: int = 300,
    ) -> _FakeExecResult:
        self.exec_calls.append((name, list(command), stdin))
        return self.exec_result

    def get_status(self, name: str) -> None:
        self.statuses.append(name)

    def delete_sandbox(self, name: str) -> None:
        self.deleted.append(name)

    def close(self) -> None:
        self.closed = True


# ── Launcher behavior ───────────────────────────────────────


def test_provision_creates_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provisioning passes the image + env to create and returns the name."""
    fake = _FakeOpenShellAPI(created_name="petname-abc")
    launcher = OpenShellSandboxLauncher(image="custom-image:latest")
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    sandbox_name = launcher.provision("test-host")

    assert sandbox_name == "petname-abc"
    assert fake.create_kwargs == [{"image": "custom-image:latest", "env": {}}]


def test_provision_uses_default_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit image, the official host image is used."""
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.provision("host")

    [created] = fake.create_kwargs
    assert created["image"] == DEFAULT_HOST_IMAGE


def test_provision_uses_image_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host image can be overridden via environment variable."""
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "docker.io/custom/host:1")
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.provision("host")

    [created] = fake.create_kwargs
    assert created["image"] == "docker.io/custom/host:1"


def test_provision_with_env_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured env vars are injected into the sandbox create spec."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test")
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(env=["OPENAI_API_KEY", "GIT_TOKEN"])
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.provision("host")

    [created] = fake.create_kwargs
    assert created["env"] == {"OPENAI_API_KEY": "sk-test", "GIT_TOKEN": "ghp-test"}


def test_provision_env_passthrough_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env passthrough names can come from the process environment."""
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.provision("host")

    [created] = fake.create_kwargs
    assert created["env"] == {"OPENAI_API_KEY": "sk-test"}


def test_provision_env_passthrough_missing_var_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured but unset env name aborts before creating a sandbox."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher(env=["OPENAI_API_KEY"])
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        launcher.provision("host")
    assert fake.create_kwargs == []


def test_run_captures_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` wraps the command in ``bash -lc`` and captures output."""
    fake = _FakeOpenShellAPI(
        exec_result=_FakeExecResult(exit_code=0, stdout="out\n", stderr="err\n")
    )
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    result = launcher.run("sb-1", "echo hello")

    assert fake.exec_calls == [("sb-1", ["bash", "-lc", "echo hello"], None)]
    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_run_check_raises_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` with ``check=True`` raises on non-zero exit."""
    fake = _FakeOpenShellAPI(exec_result=_FakeExecResult(exit_code=1, stderr="boom\n"))
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    with pytest.raises(click.ClickException, match="exit 1"):
        launcher.run("sb-1", "false")


def test_run_no_check_allows_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` with ``check=False`` returns the result even on non-zero exit."""
    fake = _FakeOpenShellAPI(exec_result=_FakeExecResult(exit_code=1, stderr="boom\n"))
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    result = launcher.run("sb-1", "false", check=False)

    assert result.returncode == 1
    assert result.stderr == "boom\n"


def test_run_background_uses_exec_background(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_background`` routes through ``exec_background`` instead of detaching."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    result = launcher.run_background(
        "sb-1", "ENV=val omnigent host --server https://s", log_path="/tmp/host.log"
    )

    assert result.returncode == 0
    assert result.stdout == "launched\n"
    [(name, command)] = fake.background_calls
    assert name == "sb-1"
    assert command[:2] == ["bash", "-lc"]
    assert "omnigent host --server https://s" in command[2]
    assert "> /tmp/host.log 2>&1 < /dev/null" in command[2]
    assert fake.exec_calls == []


def test_put_uploads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``put`` streams the file's bytes to ``cat`` over the exec channel."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    local_file = tmp_path / "wheels.tgz"
    local_file.write_bytes(b"fake-tarball")

    launcher.put("sb-1", local_file, "/tmp/oa/wheels.tgz")

    [(name, command, stdin)] = fake.exec_calls
    assert name == "sb-1"
    assert command[:2] == ["bash", "-c"]
    assert "mkdir -p /tmp/oa &&" in command[2]
    assert "cat > /tmp/oa/wheels.tgz" in command[2]
    assert stdin == b"fake-tarball"


def test_put_raises_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``put`` raises when the remote ``cat`` exits non-zero."""
    fake = _FakeOpenShellAPI(exec_result=_FakeExecResult(exit_code=1, stderr="denied"))
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    local_file = tmp_path / "wheels.tgz"
    local_file.write_bytes(b"data")

    with pytest.raises(click.ClickException, match="File upload"):
        launcher.put("sb-1", local_file, "/tmp/wheels.tgz")


def test_attach_validates_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """``attach`` checks sandbox status via the API."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    launcher.attach("sb-1")

    assert fake.statuses == ["sb-1"]


def test_terminate_deletes_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """``terminate`` calls delete and cleans up the client."""
    fake = _FakeOpenShellAPI()
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)
    monkeypatch.setattr(launcher, "_client", fake)  # so terminate can close it

    launcher.terminate("sb-1")

    assert fake.deleted == ["sb-1"]
    assert fake.closed is True


def test_wheel_install_command() -> None:
    """``wheel_install_command`` delegates to the shared helper."""
    launcher = OpenShellSandboxLauncher()
    cmd = launcher.wheel_install_command("/tmp/oa-wheels.tgz")
    assert "pip install" in cmd
    assert "/tmp/oa-wheels.tgz" in cmd


def test_exec_foreground_returns_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """``exec_foreground`` records the pid and runs the command under ``exec``."""
    fake = _FakeOpenShellAPI(foreground_exit_code=0)
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    rc = launcher.exec_foreground("sb-1", "omnigent host --server https://s")

    assert rc == 0
    [(name, command)] = fake.foreground_calls
    assert name == "sb-1"
    assert command[:2] == ["bash", "-lc"]
    assert command[2].startswith("echo $$ >")
    assert "exec omnigent host --server https://s" in command[2]


def test_exec_foreground_ctrl_c_kills_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C during a foreground host kills the remote process and re-raises."""
    fake = _FakeOpenShellAPI(foreground_raises=KeyboardInterrupt())
    launcher = OpenShellSandboxLauncher()
    monkeypatch.setattr(launcher, "_openshell", lambda: fake)

    with pytest.raises(KeyboardInterrupt):
        launcher.exec_foreground("sb-1", "omnigent host --server https://s")

    # The interrupt handler issued a best-effort kill of the recorded pid.
    assert any("kill $(cat" in command[2] for _name, command, _stdin in fake.exec_calls)


# ── _OpenShellClient wrapper against a faked SDK ────────────
#
# These exercise the real wrapper (spec building, name->id mapping,
# error translation) by injecting a stub `openshell` SDK — the SDK is
# an optional dependency the test env does not install, and real
# sandboxes only exist behind a live gateway.


@dataclass
class _SDKState:
    """Shared recorder for the faked SDK."""

    connect_error: bool = False
    delete_not_found: bool = False
    delete_not_found_via_sandbox_error: bool = False
    created_spec: Any = None
    waited: tuple[str, int | None] | None = None
    got: list[str] = field(default_factory=list)
    execs: list[tuple[str, list[str], bytes | None]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    closed: bool = False
    exec_result: _FakeExecResult = field(default_factory=_FakeExecResult)
    stream_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    stream_chunks: list[tuple[str, bytes]] = field(default_factory=list)
    stream_exit_code: int = 0
    last_workdir: str | None = None
    last_env: dict[str, str] | None = None


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> _SDKState:
    """Inject a fake ``openshell`` SDK (+ ``openshell._proto`` and ``grpc``)."""
    state = _SDKState()

    class _SandboxError(RuntimeError):
        pass

    @dataclass
    class _SandboxRef:
        id: str
        name: str

    # Fake grpc surface used by delete_sandbox / _guard.
    class _RpcError(Exception):
        pass

    class _NotFound(_RpcError):
        def __init__(self, code: str) -> None:
            super().__init__("not found")
            self._code = code

        def code(self) -> str:
            return self._code

    class _StatusCode:
        NOT_FOUND = "NOT_FOUND"

    class _SandboxClient:
        @classmethod
        def from_active_cluster(cls, *, cluster: str | None = None, **_: Any) -> _SandboxClient:
            if state.connect_error:
                raise _SandboxError("no active gateway configured")
            return cls()

        def create(self, *, spec: Any) -> _SandboxRef:
            state.created_spec = spec
            return _SandboxRef(id="id-1", name="petname-new")

        def wait_ready(self, name: str, *, timeout_seconds: int | None = None) -> _SandboxRef:
            state.waited = (name, timeout_seconds)
            return _SandboxRef(id="id-1", name=name)

        def get(self, name: str) -> _SandboxRef:
            state.got.append(name)
            return _SandboxRef(id=f"id-for-{name}", name=name)

        def exec(
            self,
            sandbox_id: str,
            command: Any,
            *,
            stdin: bytes | None = None,
            timeout_seconds: int | None = None,
            workdir: str | None = None,
            env: dict[str, str] | None = None,
        ) -> _FakeExecResult:
            state.execs.append((sandbox_id, list(command), stdin))
            state.last_workdir = workdir
            state.last_env = env
            return state.exec_result

        def exec_stream(
            self,
            sandbox_id: str,
            command: Any,
            *,
            timeout_seconds: int | None = None,
            workdir: str | None = None,
            env: dict[str, str] | None = None,
        ) -> Any:
            state.stream_calls.append((sandbox_id, list(command)))
            state.last_workdir = workdir
            state.last_env = env
            for stream, data in state.stream_chunks:
                yield _FakeExecChunk(stream=stream, data=data)
            yield _FakeExecResult(exit_code=state.stream_exit_code)

        def delete(self, name: str) -> None:
            state.deleted.append(name)
            if state.delete_not_found:
                raise _NotFound(_StatusCode.NOT_FOUND)
            if state.delete_not_found_via_sandbox_error:
                raise _SandboxError("sandbox not found")

        def close(self) -> None:
            state.closed = True

    fake_grpc = SimpleNamespace(RpcError=_RpcError, Call=_NotFound, StatusCode=_StatusCode)

    # Fake spec protobufs: record kwargs as attributes for assertions.
    class _Template:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    class _Spec:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    fake_pb2 = SimpleNamespace(SandboxSpec=_Spec, SandboxTemplate=_Template)

    openshell_mod = types.ModuleType("openshell")
    openshell_mod.SandboxClient = _SandboxClient  # type: ignore[attr-defined]
    openshell_mod.SandboxError = _SandboxError  # type: ignore[attr-defined]
    openshell_mod.ExecResult = _FakeExecResult  # type: ignore[attr-defined]
    openshell_mod.ExecChunk = _FakeExecChunk  # type: ignore[attr-defined]
    proto_mod = types.ModuleType("openshell._proto")
    proto_mod.openshell_pb2 = fake_pb2  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "openshell", openshell_mod)
    monkeypatch.setitem(sys.modules, "openshell._proto", proto_mod)
    monkeypatch.setitem(sys.modules, "grpc", fake_grpc)
    return state


def test_prepare_requires_gateway(sdk: _SDKState) -> None:
    """Preflight surfaces a remediation hint when no gateway resolves."""
    sdk.connect_error = True
    with pytest.raises(click.ClickException, match="openshell gateway select"):
        OpenShellSandboxLauncher().prepare()


def test_prepare_succeeds_with_gateway(sdk: _SDKState) -> None:
    """Preflight passes when the SDK resolves an active gateway."""
    OpenShellSandboxLauncher().prepare()


def test_client_create_sandbox(sdk: _SDKState) -> None:
    """create_sandbox builds a spec with the image, waits ready, returns name."""
    client = _OpenShellClient()
    name = client.create_sandbox(image="ghcr.io/x/host:1", env={"A": "1"})

    assert name == "petname-new"
    assert sdk.created_spec.template.image == "ghcr.io/x/host:1"
    assert sdk.created_spec.environment == {"A": "1"}
    assert sdk.waited == ("petname-new", 300)


def test_client_execute_maps_name_to_id(sdk: _SDKState) -> None:
    """exec uses the opaque sandbox id, resolved from the petname."""
    client = _OpenShellClient()
    client.create_sandbox(image="img", env={})

    client.execute("petname-new", ["echo", "hi"])

    # Cached from create — no extra get() needed; exec keyed by id.
    assert sdk.execs[-1][0] == "id-1"
    assert sdk.got == []


def test_client_execute_resolves_unknown_name(sdk: _SDKState) -> None:
    """An un-cached name is resolved to its id via get() before exec."""
    client = _OpenShellClient()

    client.execute("other-box", ["ls"])

    assert sdk.got == ["other-box"]
    assert sdk.execs[-1][0] == "id-for-other-box"


def test_client_execute_pins_sandbox_home(sdk: _SDKState) -> None:
    """
    Execs run with cwd + ``$HOME`` set to the non-root sandbox user's home.

    OpenShell runs as the ``sandbox`` user; without this the host image's
    ``/root`` cwd/home is unreadable to it and ``omnigent host`` crashes.
    """
    client = _OpenShellClient()

    client.execute("box", ["printf", "%s", "$HOME"])

    assert sdk.last_workdir == "/home/sandbox"
    assert sdk.last_env == {"HOME": "/home/sandbox"}


def test_client_delete_ignores_not_found(sdk: _SDKState) -> None:
    """Deleting a missing sandbox is treated as success (idempotent)."""
    sdk.delete_not_found = True
    client = _OpenShellClient()

    client.delete_sandbox("gone-sandbox")

    assert sdk.deleted == ["gone-sandbox"]


def test_client_delete_ignores_sandbox_error_not_found(sdk: _SDKState) -> None:
    """Deleting via SandboxError with 'not found' is treated as success."""
    sdk.delete_not_found_via_sandbox_error = True
    client = _OpenShellClient()

    client.delete_sandbox("gone-sandbox")

    assert sdk.deleted == ["gone-sandbox"]


def test_client_connect_error_raises(sdk: _SDKState) -> None:
    """A gateway-resolution failure surfaces as a ClickException."""
    sdk.connect_error = True
    with pytest.raises(click.ClickException, match="OpenShell gateway"):
        _OpenShellClient()


def test_client_run_foreground_streams_and_returns_exit(
    sdk: _SDKState, capsys: pytest.CaptureFixture[str]
) -> None:
    """run_foreground streams stdout/stderr chunks and returns the exit code."""
    sdk.stream_chunks = [("stdout", b"hello\n"), ("stderr", b"warn\n")]
    sdk.stream_exit_code = 3
    client = _OpenShellClient()

    code = client.run_foreground("other-box", ["bash", "-lc", "echo hi"], timeout=60)

    assert code == 3
    assert sdk.stream_calls[-1] == ("id-for-other-box", ["bash", "-lc", "echo hi"])
    out, err = capsys.readouterr()
    assert "hello" in out
    assert "warn" in err
