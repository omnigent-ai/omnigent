"""Tests for :mod:`omnigent.onboarding.sandboxes.boxd`."""

from __future__ import annotations

import re
import subprocess
import sys
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.boxd import (
    MANAGED_TOKEN_TTL_S,
    ORG_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    SNAPSHOT_ENV_VAR,
    BoxdSandboxLauncher,
    boxd_wheel_install_command,
    default_omnigent_spec,
    host_bootstrap_command,
    machine_name_for,
)

# ── Fake boxd SDK ───────────────────────────────────────────
#
# The boxd SDK is an optional dependency the test environment does not
# install, so these hand-rolled recorders stand in for it (never MagicMock:
# every attribute the launcher touches must be defined here). The fake module
# is injected via sys.modules so the launcher's function-local `import boxd`
# resolves to it.


class _FakeBoxdError(Exception):
    """Stands in for ``boxd.BoxdError`` (the SDK error base)."""


class _FakeAuthenticationError(_FakeBoxdError):
    """Stands in for ``boxd.AuthenticationError``."""


class _FakeNotFoundError(_FakeBoxdError):
    """Stands in for ``boxd.NotFoundError``."""


class _FakeConflictError(_FakeBoxdError):
    """Stands in for ``boxd.ConflictError``."""


class _FakePermissionDeniedError(_FakeBoxdError):
    """Stands in for ``boxd.PermissionDeniedError``."""


@dataclass
class _FakeMachine:
    """The ``Machine`` fields the launcher reads."""

    id: str
    name: str
    status: str = "running"


@dataclass
class _FakeExecResult:
    """Canned stand-in for ``boxd.ExecResult``."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass
class _ExecCall:
    """One recorded ``machines.exec`` invocation."""

    machine_id: str
    command: list[str]
    env: dict[str, str] | None


class _FakeExecStream:
    """
    Recording stand-in for ``boxd.ExecStream``.

    :param chunks: Byte chunks iteration yields.
    :param exit_code: Exit code exposed once iteration finishes.
    :param raises: Exception raised after the chunks, or ``None``.
    """

    def __init__(
        self,
        chunks: list[bytes],
        exit_code: int | None = 0,
        raises: Exception | None = None,
    ) -> None:
        self._chunks = chunks
        self._final_exit_code = exit_code
        self._raises = raises
        self.exit_code: int | None = None
        self.close_calls = 0

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        if self._raises is not None:
            raise self._raises
        self.exit_code = self._final_exit_code

    def close(self) -> None:
        """Record the close request."""
        self.close_calls += 1


class _FakeFiles:
    """Recording stand-in for ``machines.files``."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes]] = []
        self.upload_error: Exception | None = None

    def upload(self, machine_id: str, path: str, data: bytes) -> int:
        """Record the upload, or raise the configured error."""
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append((machine_id, path, data))
        return len(data)


@dataclass
class _FakeMachines:
    """Recording stand-in for the ``machines`` namespace."""

    files: _FakeFiles = field(default_factory=_FakeFiles)
    machines: dict[str, _FakeMachine] = field(default_factory=dict)
    created: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    exec_calls: list[_ExecCall] = field(default_factory=list)
    stream_calls: list[dict[str, object]] = field(default_factory=list)
    lifecycle: list[tuple[str, str]] = field(default_factory=list)
    exec_handler: Callable[[list[str]], _FakeExecResult] = lambda _command: _FakeExecResult()
    streams: list[_FakeExecStream] = field(default_factory=list)
    create_error: Exception | None = None
    ready_error: Exception | None = None
    exec_error: Exception | None = None
    get_error: Exception | None = None
    delete_error: Exception | None = None
    suspend_error: Exception | None = None

    def create(self, name: str, **kwargs: object) -> _FakeMachine:
        """Record the create and return a running machine."""
        if self.create_error is not None:
            raise self.create_error
        self.created.append((name, kwargs))
        machine = _FakeMachine(id=f"vm-{len(self.created)}", name=name)
        self.machines[machine.id] = machine
        return machine

    def wait_until_ready(self, machine_id: str, *, timeout: float = 90.0) -> _FakeMachine:
        """Record the wait, or raise the configured error."""
        self.lifecycle.append(("wait_until_ready", machine_id))
        if self.ready_error is not None:
            raise self.ready_error
        return self.machines[machine_id]

    def exec(
        self, machine_id: str, command: list[str], *, env: dict[str, str] | None = None
    ) -> _FakeExecResult:
        """Record the exec and answer it with the configured handler."""
        self.exec_calls.append(_ExecCall(machine_id, command, env))
        if self.exec_error is not None:
            raise self.exec_error
        return self.exec_handler(command)

    def stream_exec(self, machine_id: str, **kwargs: object) -> _FakeExecStream:
        """Record the call and hand out the next queued stream."""
        self.stream_calls.append({"machine_id": machine_id, **kwargs})
        return self.streams.pop(0)

    def get(self, machine_id: str) -> _FakeMachine:
        """Return the machine, or raise ``NotFoundError`` / the configured error."""
        if self.get_error is not None:
            raise self.get_error
        if machine_id not in self.machines:
            raise _FakeNotFoundError(f"VM not found: {machine_id}")
        return self.machines[machine_id]

    def delete(self, machine_id: str) -> None:
        """Record the delete, or raise the configured error."""
        self.lifecycle.append(("delete", machine_id))
        if self.delete_error is not None:
            raise self.delete_error
        self.machines.pop(machine_id, None)

    def wake(self, machine_id: str) -> None:
        """Record the wake and mark the machine running."""
        self.lifecycle.append(("wake", machine_id))
        self.machines[machine_id].status = "running"

    def start(self, machine_id: str) -> None:
        """Record the start and mark the machine running."""
        self.lifecycle.append(("start", machine_id))
        self.machines[machine_id].status = "running"

    def set_auto_suspend_timeout(self, machine_id: str, seconds: int) -> None:
        """Record the setting, or raise the configured error."""
        if self.suspend_error is not None:
            raise self.suspend_error
        self.lifecycle.append((f"auto_suspend={seconds}", machine_id))


class _FakeBoxd:
    """Stands in for the ``boxd.Boxd`` client."""

    def __init__(self) -> None:
        self.machines = _FakeMachines()


@pytest.fixture
def fake_boxd(monkeypatch: pytest.MonkeyPatch) -> _FakeBoxd:
    """Install a fake ``boxd`` module whose client is shared across calls."""
    client = _FakeBoxd()
    module = types.ModuleType("boxd")
    module.Boxd = lambda: client  # type: ignore[attr-defined]
    module.BoxdError = _FakeBoxdError  # type: ignore[attr-defined]
    module.AuthenticationError = _FakeAuthenticationError  # type: ignore[attr-defined]
    module.NotFoundError = _FakeNotFoundError  # type: ignore[attr-defined]
    module.ConflictError = _FakeConflictError  # type: ignore[attr-defined]
    module.PermissionDeniedError = _FakePermissionDeniedError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boxd", module)
    for name in (ORG_ENV_VAR, SNAPSHOT_ENV_VAR, SANDBOX_ENV_PASSTHROUGH_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    return client


def _running_machine(client: _FakeBoxd, status: str = "running") -> str:
    """Seed one machine in *status* and return its id."""
    client.machines.machines["vm-seeded"] = _FakeMachine("vm-seeded", "omnigent-x", status)
    return "vm-seeded"


# ── Construction and capabilities ───────────────────────────


def test_capabilities_declare_resume_and_cli_bootstrap() -> None:
    """boxd resumes machines in place and supports the CLI bootstrap."""
    caps = BoxdSandboxLauncher().capabilities
    assert caps.resume_stopped is True
    assert caps.managed_launch is True
    assert caps.cli_bootstrap is True
    assert caps.local_port_forward is False
    assert caps.streaming_exec is True
    assert caps.foreground_exec is True
    assert caps.file_copy is True
    assert caps.git_clone_options is True
    assert BoxdSandboxLauncher.supports_local_port_forward is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"vcpu": 3}, "vcpu must be one of 1, 2, 4, 8"),
        ({"vcpu": 2, "snapshot": "golden"}, "cannot be combined with snapshot"),
        ({"omnigent_spec": " "}, "omnigent_spec"),
    ],
)
def test_constructor_rejects_invalid_config(kwargs: dict[str, object], message: str) -> None:
    """Invalid sizes and size-with-snapshot fail before any machine exists."""
    with pytest.raises(ValueError, match=message):
        BoxdSandboxLauncher(**kwargs)  # type: ignore[arg-type]


def test_managed_token_ttl_is_seven_days() -> None:
    """Machines have no lifetime cap, so the token bound is the 7-day policy."""
    assert MANAGED_TOKEN_TTL_S == 7 * 24 * 3600


# ── prepare ─────────────────────────────────────────────────


def test_prepare_without_sdk_gives_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing SDK names the extra to install."""
    monkeypatch.setitem(sys.modules, "boxd", None)
    with pytest.raises(click.ClickException, match=r"omnigent\[boxd\]"):
        BoxdSandboxLauncher().prepare()


def test_prepare_requires_credentials(
    fake_boxd: _FakeBoxd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without BOXD_API_KEY or BOXD_TOKEN, prepare fails with a hint."""
    monkeypatch.delenv("BOXD_API_KEY", raising=False)
    monkeypatch.delenv("BOXD_TOKEN", raising=False)
    with pytest.raises(click.ClickException, match="BOXD_API_KEY"):
        BoxdSandboxLauncher().prepare()


@pytest.mark.parametrize("variable", ["BOXD_API_KEY", "BOXD_TOKEN"])
def test_prepare_accepts_either_credential(
    fake_boxd: _FakeBoxd, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Either credential the SDK reads satisfies the preflight."""
    monkeypatch.delenv("BOXD_API_KEY", raising=False)
    monkeypatch.delenv("BOXD_TOKEN", raising=False)
    monkeypatch.setenv(variable, "bxd_test")
    BoxdSandboxLauncher().prepare()


# ── provision ───────────────────────────────────────────────


def test_provision_creates_isolated_machine_and_bootstraps(fake_boxd: _FakeBoxd) -> None:
    """A fresh machine is isolated, never auto-suspends, and runs the bootstrap."""
    launcher = BoxdSandboxLauncher(org="acme", vcpu=4, omnigent_spec="omnigent==9.9.9")

    machine_id = launcher.provision("managed-a1b2c3d4")

    assert machine_id == "vm-1"
    name, kwargs = fake_boxd.machines.created[0]
    assert re.fullmatch(r"omnigent-managed-a1b2c3d4-[0-9a-f]{6}", name)
    assert kwargs == {"org": "acme", "isolated": True, "auto_suspend_timeout": 0, "vcpu": 4}
    assert fake_boxd.machines.lifecycle == [("wait_until_ready", "vm-1")]
    probe, bootstrap = (call.command for call in fake_boxd.machines.exec_calls)
    assert probe == ["true"]
    assert bootstrap[:2] == ["bash", "-lc"]
    assert "uv tool install --quiet --python 3.12 omnigent==9.9.9" in bootstrap[2]


def test_provision_from_snapshot_omits_size(
    fake_boxd: _FakeBoxd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot machine keeps the snapshot's size; org and snapshot come from env."""
    monkeypatch.setenv(SNAPSHOT_ENV_VAR, "omnigent-host")
    monkeypatch.setenv(ORG_ENV_VAR, "from-env")

    BoxdSandboxLauncher().provision("omnigent-host")

    name, kwargs = fake_boxd.machines.created[0]
    assert re.fullmatch(r"omnigent-host-[0-9a-f]{6}", name)
    assert kwargs == {
        "from_snapshot": "omnigent-host",
        "org": "from-env",
        "isolated": True,
        "auto_suspend_timeout": 0,
    }


def test_provision_never_creates_unisolated_machines(fake_boxd: _FakeBoxd) -> None:
    """Agents run untrusted code, so isolation is not configurable."""
    BoxdSandboxLauncher(snapshot="golden").provision("a")
    BoxdSandboxLauncher(vcpu=1).provision("b")
    assert [kwargs["isolated"] for _, kwargs in fake_boxd.machines.created] == [True, True]


def test_provision_deletes_machine_when_bootstrap_fails(fake_boxd: _FakeBoxd) -> None:
    """A machine nobody holds the id of yet is deleted before the error propagates."""
    fake_boxd.machines.exec_handler = lambda _command: _FakeExecResult(stderr="no uv", exit_code=1)

    with pytest.raises(click.ClickException, match="exit 1"):
        BoxdSandboxLauncher().provision("managed-x")

    assert ("delete", "vm-1") in fake_boxd.machines.lifecycle


def test_provision_deletes_machine_when_readiness_times_out(fake_boxd: _FakeBoxd) -> None:
    """A machine that never becomes ready is not leaked."""
    fake_boxd.machines.ready_error = TimeoutError("not ready after 120s")

    with pytest.raises(click.ClickException, match="did not become ready"):
        BoxdSandboxLauncher().provision("managed-x")

    assert ("delete", "vm-1") in fake_boxd.machines.lifecycle


def test_provision_deletes_machine_on_interrupt(fake_boxd: _FakeBoxd) -> None:
    """Ctrl-C during the bootstrap still deletes the machine, then re-raises."""

    def _interrupt(_command: list[str]) -> _FakeExecResult:
        raise KeyboardInterrupt

    fake_boxd.machines.exec_handler = _interrupt

    with pytest.raises(KeyboardInterrupt):
        BoxdSandboxLauncher().provision("managed-x")

    assert ("delete", "vm-1") in fake_boxd.machines.lifecycle


def test_provision_surfaces_authentication_failure(fake_boxd: _FakeBoxd) -> None:
    """A rejected key points at BOXD_API_KEY."""
    fake_boxd.machines.create_error = _FakeAuthenticationError("invalid api key")

    with pytest.raises(click.ClickException, match="check BOXD_API_KEY"):
        BoxdSandboxLauncher().provision("managed-x")


def test_provision_surfaces_quota_errors(fake_boxd: _FakeBoxd) -> None:
    """Provider errors keep their reason through the launcher contract."""
    fake_boxd.machines.create_error = _FakeBoxdError("machine quota reached")

    with pytest.raises(click.ClickException, match="machine quota reached"):
        BoxdSandboxLauncher().provision("managed-x")


# ── names, specs and scripts ────────────────────────────────


@pytest.mark.parametrize(
    ("label", "pattern"),
    [
        ("managed-a1b2c3d4", r"omnigent-managed-a1b2c3d4-[0-9a-f]{6}"),
        ("My Sandbox_01", r"omnigent-my-sandbox-01-[0-9a-f]{6}"),
        ("omnigent-host", r"omnigent-host-[0-9a-f]{6}"),
        ("!!!", r"omnigent-[0-9a-f]{6}"),
    ],
)
def test_machine_name_is_a_valid_boxd_name(label: str, pattern: str) -> None:
    """Names are lowercase/digits/hyphens, prefixed, and suffixed for uniqueness."""
    assert re.fullmatch(pattern, machine_name_for(label))


def test_machine_name_fits_the_hostname_limit() -> None:
    """boxd names double as hostnames and are capped at 63 characters."""
    name = machine_name_for("x" * 200)
    assert len(name) <= 63
    assert re.fullmatch(r"[a-z0-9-]+", name)
    assert not name.endswith("-")


def test_default_spec_pins_release_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release server installs its own version in the machine."""
    monkeypatch.setattr("omnigent.version.VERSION", "0.19.1")
    assert default_omnigent_spec() == "omnigent==0.19.1"


def test_default_spec_takes_latest_release_for_dev_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev build has no published version to pin, so it takes the latest."""
    monkeypatch.setattr("omnigent.version.VERSION", "0.20.0.dev0")
    assert default_omnigent_spec() == "omnigent"


def test_bootstrap_is_idempotent_valid_shell() -> None:
    """Each install is guarded by a presence check and the script parses."""
    script = host_bootstrap_command("omnigent==0.19.1; rm -rf /")
    assert "command -v omnigent" in script
    assert "command -v bwrap" in script
    assert "'omnigent==0.19.1; rm -rf /'" in script
    assert "credential.helper" in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_wheel_install_resolves_sibling_wheels() -> None:
    """Shipped wheels install into a fresh uv tool env with their siblings."""
    command = boxd_wheel_install_command("/tmp/oa-wheels.tgz")
    assert "--find-links oa-wheels" in command
    assert "uv tool install" in command
    assert (
        BoxdSandboxLauncher()
        .wheel_install_command("/tmp/x.tgz")
        .endswith("oa-wheels/omnigent-*.whl")
    )


# ── run / put ───────────────────────────────────────────────


def test_run_sends_passthrough_env_with_every_command(
    fake_boxd: _FakeBoxd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env set at creation does not reach exec sessions, so each exec carries it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    machine_id = _running_machine(fake_boxd)
    fake_boxd.machines.exec_handler = lambda _command: _FakeExecResult(stdout="ok\n")

    result = BoxdSandboxLauncher(env=["ANTHROPIC_API_KEY"]).run(machine_id, "echo ok")

    call = fake_boxd.machines.exec_calls[0]
    assert call.command == ["bash", "-lc", "echo ok"]
    assert call.env == {"IS_SANDBOX": "1", "ANTHROPIC_API_KEY": "sk-test"}
    assert (result.returncode, result.stdout) == (0, "ok\n")


def test_run_reads_passthrough_names_from_env_var(
    fake_boxd: _FakeBoxd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The comma-separated fallback applies when no names are configured."""
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, " GIT_TOKEN , ")
    monkeypatch.setenv("GIT_TOKEN", "ghp_test")
    machine_id = _running_machine(fake_boxd)

    BoxdSandboxLauncher().run(machine_id, "true")

    assert fake_boxd.machines.exec_calls[0].env == {"IS_SANDBOX": "1", "GIT_TOKEN": "ghp_test"}


def test_run_fails_loud_on_unset_passthrough_name(
    fake_boxd: _FakeBoxd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured credential the server never had is a startup error."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    machine_id = _running_machine(fake_boxd)

    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        BoxdSandboxLauncher(env=["OPENAI_API_KEY"]).run(machine_id, "true")
    assert fake_boxd.machines.exec_calls == []


def test_run_keeps_streams_apart_and_checks_exit(fake_boxd: _FakeBoxd) -> None:
    """stderr stays separate; check=True raises and check=False returns the code."""
    machine_id = _running_machine(fake_boxd)
    fake_boxd.machines.exec_handler = lambda _command: _FakeExecResult(
        stdout="out\n", stderr="err\n", exit_code=42
    )
    launcher = BoxdSandboxLauncher()

    result = launcher.run(machine_id, "false", check=False)
    assert (result.returncode, result.stdout, result.stderr) == (42, "out\n", "err\n")
    with pytest.raises(click.ClickException, match="exit 42"):
        launcher.run(machine_id, "false")


def test_run_wraps_provider_errors(fake_boxd: _FakeBoxd) -> None:
    """A stopped machine's ConflictError surfaces through the launcher contract."""
    machine_id = _running_machine(fake_boxd)
    fake_boxd.machines.exec_error = _FakeConflictError("VM is stopped")

    with pytest.raises(click.ClickException, match="VM is stopped"):
        BoxdSandboxLauncher().run(machine_id, "true")


def test_put_uploads_file_bytes(fake_boxd: _FakeBoxd, tmp_path: Path) -> None:
    """put ships the local file's exact bytes to the remote path."""
    machine_id = _running_machine(fake_boxd)
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"\x00binary\xff")

    BoxdSandboxLauncher().put(machine_id, local, "/tmp/oa-wheels.tgz")

    assert fake_boxd.machines.files.uploads == [
        (machine_id, "/tmp/oa-wheels.tgz", b"\x00binary\xff")
    ]


def test_put_wraps_provider_errors(fake_boxd: _FakeBoxd, tmp_path: Path) -> None:
    """A failed upload becomes a launcher-contract error."""
    machine_id = _running_machine(fake_boxd)
    fake_boxd.machines.files.upload_error = _FakeBoxdError("disk full")
    local = tmp_path / "f"
    local.write_bytes(b"x")

    with pytest.raises(click.ClickException, match="disk full"):
        BoxdSandboxLauncher().put(machine_id, local, "/tmp/f")


# ── streaming and foreground ────────────────────────────────


def test_stream_exec_yields_lines_across_chunk_boundaries(fake_boxd: _FakeBoxd) -> None:
    """Lines and multi-byte characters split across chunks come out whole."""
    machine_id = _running_machine(fake_boxd)
    snowman = "☃".encode()
    fake_boxd.machines.streams.append(
        _FakeExecStream([b"fir", b"st\nsec", snowman[:1], snowman[1:] + b"\nlast"], exit_code=3)
    )

    process = BoxdSandboxLauncher().stream_exec(machine_id, "cmd")

    assert process.lines is process.lines
    assert next(process.lines) == "first\n"
    assert list(process.lines) == ["sec☃\n", "last\n"]
    assert process.wait() == 3
    call = fake_boxd.machines.stream_calls[0]
    assert call["command"] == ["bash", "-lc", "cmd"]
    assert (call["tty"], call["cols"], call["rows"]) == (False, 0, 0)


def test_stream_exec_pty_sets_terminal_size(fake_boxd: _FakeBoxd) -> None:
    """A PTY stream gets real geometry; tmux-spawning harnesses need it."""
    machine_id = _running_machine(fake_boxd)
    fake_boxd.machines.streams.append(_FakeExecStream([]))

    BoxdSandboxLauncher().stream_exec(machine_id, "cmd", pty=True)

    call = fake_boxd.machines.stream_calls[0]
    assert call["tty"] is True
    assert call["cols"] > 0 and call["rows"] > 0


def test_stream_transport_error_raises_on_wait(fake_boxd: _FakeBoxd) -> None:
    """A dropped stream is a transport error, not an exit code."""
    machine_id = _running_machine(fake_boxd)
    stream = _FakeExecStream([b"partial"], raises=_FakeBoxdError("stream reset"))
    fake_boxd.machines.streams.append(stream)

    process = BoxdSandboxLauncher().stream_exec(machine_id, "cmd")

    assert list(process.lines) == ["partial\n"]
    with pytest.raises(click.ClickException, match="stream reset"):
        process.wait()
    process.close()
    process.close()
    assert stream.close_calls == 2


def test_exec_foreground_records_pid_and_cleans_up(
    fake_boxd: _FakeBoxd, capsys: pytest.CaptureFixture[str]
) -> None:
    """The foreground command records its pid, forces TERM, and drops its run dir."""
    machine_id = _running_machine(fake_boxd)
    fake_boxd.machines.streams.append(_FakeExecStream([b"host up\n"], exit_code=0))

    assert BoxdSandboxLauncher().exec_foreground(machine_id, "omnigent host --server x") == 0

    remote = fake_boxd.machines.stream_calls[0]["command"][2]  # type: ignore[index]
    assert "mkdir -m 700 /tmp/oa-foreground-" in remote
    assert remote.endswith("TERM=xterm-256color exec omnigent host --server x")
    assert fake_boxd.machines.stream_calls[0]["tty"] is True
    assert fake_boxd.machines.exec_calls[-1].command[2].startswith("rm -rf /tmp/oa-foreground-")
    assert "host up" in capsys.readouterr().out


def test_exec_foreground_interrupt_kills_remote_process(fake_boxd: _FakeBoxd) -> None:
    """Ctrl-C closes the stream and signals the recorded pid before re-raising."""
    machine_id = _running_machine(fake_boxd)

    class _InterruptedStream(_FakeExecStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"working\n"
            raise KeyboardInterrupt

    stream = _InterruptedStream([])
    fake_boxd.machines.streams.append(stream)

    with pytest.raises(KeyboardInterrupt):
        BoxdSandboxLauncher().exec_foreground(machine_id, "omnigent host")

    assert stream.close_calls == 1
    kill = fake_boxd.machines.exec_calls[-1].command[2]
    assert 'kill "$pid"' in kill


# ── resume / attach / status / keep-alive ───────────────────


@pytest.mark.parametrize(
    ("status", "action"),
    [("suspended", "wake"), ("hibernated", "wake"), ("stopped", "start")],
)
def test_resume_wakes_or_starts_in_place(fake_boxd: _FakeBoxd, status: str, action: str) -> None:
    """Sleeping machines wake, stopped ones start; the id never changes."""
    machine_id = _running_machine(fake_boxd, status)

    BoxdSandboxLauncher().resume(machine_id)

    assert fake_boxd.machines.lifecycle == [(action, machine_id), ("wait_until_ready", machine_id)]
    assert fake_boxd.machines.created == []


def test_resume_waits_until_a_started_machine_accepts_exec(
    fake_boxd: _FakeBoxd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold start reports ready before exec works; resume probes until it does."""
    monkeypatch.setattr("omnigent.onboarding.sandboxes.boxd._EXEC_READY_POLL_S", 0.0)
    machine_id = _running_machine(fake_boxd, "stopped")
    failures = iter([_FakeBoxdError("agent session failed: transport error")] * 2)

    def _flaky(_command: list[str]) -> _FakeExecResult:
        error = next(failures, None)
        if error is not None:
            raise error
        return _FakeExecResult()

    fake_boxd.machines.exec_handler = _flaky

    BoxdSandboxLauncher().resume(machine_id)

    assert [call.command for call in fake_boxd.machines.exec_calls] == [["true"]] * 3


def test_resume_gives_up_when_exec_never_works(
    fake_boxd: _FakeBoxd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine whose agent never answers fails the resume instead of hanging."""
    monkeypatch.setattr("omnigent.onboarding.sandboxes.boxd._EXEC_READY_TIMEOUT_S", 0.0)
    machine_id = _running_machine(fake_boxd, "stopped")
    fake_boxd.machines.exec_error = _FakeBoxdError("agent session failed: transport error")

    with pytest.raises(click.ClickException, match="transport error"):
        BoxdSandboxLauncher().resume(machine_id)


def test_resume_running_machine_is_a_no_op(fake_boxd: _FakeBoxd) -> None:
    """A running machine needs nothing."""
    machine_id = _running_machine(fake_boxd)
    BoxdSandboxLauncher().resume(machine_id)
    assert fake_boxd.machines.lifecycle == []


@pytest.mark.parametrize("status", ["destroyed", None])
def test_resume_reports_gone_machines(fake_boxd: _FakeBoxd, status: str | None) -> None:
    """A deleted machine raises SandboxGoneError so the server relaunches."""
    machine_id = _running_machine(fake_boxd, status) if status else "vm-missing"
    with pytest.raises(SandboxGoneError):
        BoxdSandboxLauncher().resume(machine_id)


def test_resume_refuses_failed_machines(fake_boxd: _FakeBoxd) -> None:
    """A failed machine cannot be resumed in place and is not replaced silently."""
    machine_id = _running_machine(fake_boxd, "failed")
    with pytest.raises(click.ClickException, match="'failed'"):
        BoxdSandboxLauncher().resume(machine_id)


def test_resume_wraps_transport_errors(fake_boxd: _FakeBoxd) -> None:
    """An API outage is a resume failure, not a gone machine."""
    fake_boxd.machines.get_error = _FakeBoxdError("unavailable")
    with pytest.raises(click.ClickException, match="unavailable") as excinfo:
        BoxdSandboxLauncher().resume("vm-1")
    assert not isinstance(excinfo.value, SandboxGoneError)


def test_attach_starts_a_stopped_machine(fake_boxd: _FakeBoxd) -> None:
    """The CLI attach path brings a stopped machine back."""
    machine_id = _running_machine(fake_boxd, "stopped")
    BoxdSandboxLauncher().attach(machine_id)
    assert fake_boxd.machines.lifecycle[0] == ("start", machine_id)


def test_attach_missing_machine_is_a_click_error(fake_boxd: _FakeBoxd) -> None:
    """Attaching to a deleted machine explains what happened."""
    with pytest.raises(click.ClickException, match="not found"):
        BoxdSandboxLauncher().attach("vm-missing")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("running", True),
        ("suspended", False),
        ("hibernated", False),
        ("stopped", False),
        ("failed", False),
        ("starting", None),
    ],
)
def test_is_running_maps_machine_status(
    fake_boxd: _FakeBoxd, status: str, expected: bool | None
) -> None:
    """Only a running machine has a live host tunnel."""
    machine_id = _running_machine(fake_boxd, status)
    assert BoxdSandboxLauncher().is_running(machine_id) is expected


def test_is_running_missing_machine_is_false(fake_boxd: _FakeBoxd) -> None:
    """A deleted machine is not running."""
    assert BoxdSandboxLauncher().is_running("vm-missing") is False


def test_is_running_transport_error_is_unknown(fake_boxd: _FakeBoxd) -> None:
    """A provider blip reports unknown rather than failing the message path."""
    fake_boxd.machines.get_error = _FakeBoxdError("unavailable")
    assert BoxdSandboxLauncher().is_running("vm-1") is None


def test_keep_alive_disables_auto_suspend(fake_boxd: _FakeBoxd) -> None:
    """keep_alive re-asserts auto-suspend off and is safe to repeat."""
    machine_id = _running_machine(fake_boxd)
    launcher = BoxdSandboxLauncher()
    launcher.keep_alive(machine_id)
    launcher.keep_alive(machine_id)
    assert fake_boxd.machines.lifecycle == [("auto_suspend=0", machine_id)] * 2


def test_keep_alive_soft_fails(fake_boxd: _FakeBoxd, capsys: pytest.CaptureFixture[str]) -> None:
    """A rejected setting warns instead of raising."""
    fake_boxd.machines.suspend_error = _FakeBoxdError("hibernated")
    BoxdSandboxLauncher().keep_alive("vm-1")
    assert "could not disable auto-suspend" in capsys.readouterr().err


# ── start_host / terminate ──────────────────────────────────


def test_start_host_stops_preserved_daemon_before_launch(fake_boxd: _FakeBoxd) -> None:
    """A woken machine's old host is stopped before the fresh one starts."""
    machine_id = _running_machine(fake_boxd)
    fake_boxd.machines.exec_handler = lambda command: _FakeExecResult(
        stdout="/home/boxd" if "$HOME" in command[2] else ""
    )

    workspace = BoxdSandboxLauncher().start_host(
        machine_id,
        token="tok-secret",
        host_id="host_1",
        host_name="managed-1",
        server_url="https://omnigent.example.com",
    )

    commands = [call.command[2] for call in fake_boxd.machines.exec_calls]
    stop_index = next(i for i, cmd in enumerate(commands) if "preserved omnigent host" in cmd)
    launch_index = next(i for i, cmd in enumerate(commands) if "omnigent host --server" in cmd)
    assert stop_index < launch_index
    assert workspace == "/home/boxd/workspace"
    # resume_stopped providers always rewrite the injected host config.
    assert any("injected_host_config" in cmd for cmd in commands)


def test_terminate_is_idempotent(fake_boxd: _FakeBoxd) -> None:
    """Deleting an already-deleted machine succeeds."""
    machine_id = _running_machine(fake_boxd)
    launcher = BoxdSandboxLauncher()
    launcher.terminate(machine_id)
    fake_boxd.machines.delete_error = _FakeNotFoundError("VM not found")
    launcher.terminate(machine_id)
    assert fake_boxd.machines.lifecycle == [("delete", machine_id), ("delete", machine_id)]


def test_terminate_surfaces_other_errors(fake_boxd: _FakeBoxd) -> None:
    """Any other delete failure reaches the best-effort teardown caller."""
    fake_boxd.machines.delete_error = _FakeConflictError("delete in progress")
    with pytest.raises(click.ClickException, match="delete in progress"):
        BoxdSandboxLauncher().terminate("vm-1")
