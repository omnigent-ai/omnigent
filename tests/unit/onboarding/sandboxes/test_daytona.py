"""Tests for :mod:`omnigent.onboarding.sandboxes.daytona`."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    SandboxCapabilityError,
)
from omnigent.onboarding.sandboxes.daytona import (
    HOST_IMAGE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    DaytonaSandboxLauncher,
)

# ── Fake Daytona SDK ────────────────────────────────────────
#
# The Daytona SDK is an optional dependency the test environment may
# not install, and real Sandbox objects only exist server-side anyway —
# so these are hand-rolled stub classes (never MagicMock: the
# launcher's attribute access must hit explicitly defined recorders,
# not silently succeed). The fake module is injected via sys.modules so
# the launcher's function-local `import daytona` resolves to it.


class _FakeDaytonaError(Exception):
    """Stands in for ``daytona.DaytonaError`` (the SDK error base)."""


class _FakeNotFoundError(_FakeDaytonaError):
    """Stands in for ``daytona.DaytonaNotFoundError``."""


class _FakeConflictError(_FakeDaytonaError):
    """Stands in for ``daytona.DaytonaConflictError``."""


class _FakeSandboxState(Enum):
    """Stands in for ``daytona.SandboxState`` (the subset attach reads)."""

    STARTED = "started"
    STOPPED = "stopped"


@dataclass
class _ExecCall:
    """
    One recorded ``process.exec`` invocation.

    :param command: The shell command passed.
    """

    command: str


@dataclass
class _ExecResponse:
    """
    Canned stand-in for ``daytona.ExecuteResponse``.

    :param exit_code: Exit status the toolbox reports.
    :param result: Combined stdout+stderr output.
    """

    exit_code: int
    result: str


@dataclass
class _FakePtyResult:
    """
    Canned stand-in for ``daytona.common.pty.PtyResult``.

    :param exit_code: Exit code the PTY close frame carried, or
        ``None`` when the session ended without one.
    :param error: Error message from the PTY daemon, or ``None``.
    """

    exit_code: int | None
    error: str | None = None


class _FakePtyHandle:
    """
    Recording stand-in for the SDK's ``PtyHandle``.

    :param result: The canned result ``wait`` returns.
    :param output_chunks: Byte chunks ``wait`` feeds to its
        ``on_data`` callback before returning.
    :param wait_raises: Exception ``wait`` raises instead of
        returning (models Ctrl-C during the blocking websocket read),
        or ``None`` for normal completion.
    """

    def __init__(
        self,
        result: _FakePtyResult,
        output_chunks: list[bytes] | None = None,
        wait_raises: BaseException | None = None,
    ) -> None:
        self._result = result
        self._output_chunks = output_chunks or []
        self._wait_raises = wait_raises
        self.sent_inputs: list[str] = []
        self.kill_calls: int = 0
        self.disconnect_calls: int = 0

    def send_input(self, data: str) -> None:
        """Record the input line sent to the PTY."""
        self.sent_inputs.append(data)

    def wait(self, on_data: object = None) -> _FakePtyResult:
        """Feed canned output to *on_data*, then return/raise."""
        for chunk in self._output_chunks:
            if on_data is not None:
                on_data(chunk)
        if self._wait_raises is not None:
            raise self._wait_raises
        return self._result

    def kill(self) -> None:
        """Record the kill request."""
        self.kill_calls += 1

    def disconnect(self) -> None:
        """Record the websocket teardown."""
        self.disconnect_calls += 1


class _FakeProcess:
    """Recorder for the sandbox ``process`` namespace."""

    def __init__(self) -> None:
        self.exec_calls: list[_ExecCall] = []
        # Responses handed back by successive exec() calls, in order;
        # an empty queue yields a default success response.
        self.exec_queue: list[_ExecResponse] = []
        # Exception ``exec`` raises instead of responding (models a
        # toolbox outage / stopped sandbox), or ``None`` for normal
        # execution.
        self.exec_raises: Exception | None = None
        # PTY session ids created, and the handle handed back (tests
        # configure ``pty_handle`` before calling exec_foreground).
        self.pty_session_ids: list[str] = []
        self.pty_handle: _FakePtyHandle | None = None
        # Exception ``create_pty_session`` raises (models a stopped
        # sandbox), or ``None`` for normal creation.
        self.create_pty_raises: Exception | None = None

    def exec(self, command: str) -> _ExecResponse:
        """Record the call and pop the next canned response."""
        self.exec_calls.append(_ExecCall(command=command))
        if self.exec_raises is not None:
            raise self.exec_raises
        return self.exec_queue.pop(0) if self.exec_queue else _ExecResponse(0, "")

    def create_pty_session(self, id: str) -> _FakePtyHandle:
        """Record the session id and hand back the configured handle."""
        if self.create_pty_raises is not None:
            raise self.create_pty_raises
        self.pty_session_ids.append(id)
        if self.pty_handle is None:
            self.pty_handle = _FakePtyHandle(_FakePtyResult(exit_code=0), [])
        return self.pty_handle


class _FakeFileSystem:
    """Recorder for the sandbox ``fs`` namespace."""

    def __init__(self) -> None:
        self.uploads: list[_UploadCall] = []
        # Exception ``upload_file`` raises (models a stopped sandbox /
        # toolbox outage), or ``None`` for normal upload.
        self.upload_raises: Exception | None = None

    def upload_file(self, src: str, dst: str) -> None:
        """Record the transfer."""
        if self.upload_raises is not None:
            raise self.upload_raises
        self.uploads.append(_UploadCall(src=src, dst=dst))


@dataclass
class _UploadCall:
    """
    One recorded ``fs.upload_file`` invocation.

    :param src: Local source path passed.
    :param dst: Remote destination path passed.
    """

    src: str
    dst: str


class _FakeSandbox:
    """
    Recording stand-in for a Daytona ``Sandbox`` handle.

    :param sandbox_id: The sandbox id, e.g. ``"dt-1"``.
    """

    def __init__(self, sandbox_id: str) -> None:
        self.id = sandbox_id
        self.process = _FakeProcess()
        self.fs = _FakeFileSystem()
        self.state = _FakeSandboxState.STARTED
        self.refresh_data_calls: int = 0
        self.start_calls: int = 0
        self.autostop_intervals: list[int] = []
        # Exception ``set_autostop_interval`` raises (models a
        # provider rejection), or ``None`` for normal configuration.
        self.autostop_raises: Exception | None = None

    def refresh_data(self) -> None:
        """Record the state refresh."""
        self.refresh_data_calls += 1

    def start(self) -> None:
        """Record the start and transition to STARTED."""
        self.start_calls += 1
        self.state = _FakeSandboxState.STARTED

    def set_autostop_interval(self, interval: int) -> None:
        """Record the configured idle auto-stop interval."""
        if self.autostop_raises is not None:
            raise self.autostop_raises
        self.autostop_intervals.append(interval)


@dataclass
class _CreateParams:
    """
    Recorded ``CreateSandboxFromImageParams`` construction.

    Field-for-field mirror of the kwargs the launcher passes — the
    real class is a Pydantic model, but the launcher only constructs
    and forwards it, so a recording dataclass keeps every value
    observable.

    :param image: Registry image reference.
    :param env_vars: Injected workload env, or ``None``.
    :param labels: Sandbox labels.
    :param auto_stop_interval: Idle auto-stop minutes (0 = disabled).
    :param resources: The ``Resources`` instance passed.
    """

    image: str
    env_vars: dict[str, str] | None
    labels: dict[str, str]
    auto_stop_interval: int
    resources: _FakeResources


@dataclass
class _FakeResources:
    """
    Stands in for ``daytona.Resources``.

    :param cpu: vCPU count.
    :param memory: Memory in GiB.
    """

    cpu: int
    memory: int


@dataclass
class _CreateCall:
    """
    One recorded ``Daytona.create`` invocation.

    :param params: The construction params passed.
    :param timeout: Creation timeout in seconds.
    :param has_log_callback: Whether snapshot-create log streaming was
        requested.
    """

    params: _CreateParams
    timeout: float
    has_log_callback: bool


@dataclass
class _FakeDaytonaState:
    """
    Shared recorder the fake module writes into.

    :param create_calls: Every ``Daytona.create`` invocation.
    :param sandboxes: Live sandboxes by id (``get`` resolves here;
        absent ids raise the fake not-found error).
    :param deleted: Ids passed to ``Daytona.delete``.
    :param client_count: How many ``Daytona()`` clients were built —
        the launcher must build exactly one and reuse it.
    :param create_raises: Exception ``create`` raises instead of
        provisioning (e.g. a canned SDK authorization error), or
        ``None`` for normal creation.
    :param delete_raises: Exceptions successive ``delete`` calls raise
        before succeeding (popped front-first) — models the live
        "Sandbox state change in progress" conflict window.
    """

    create_calls: list[_CreateCall] = field(default_factory=list)
    sandboxes: dict[str, _FakeSandbox] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    client_count: int = 0
    create_raises: Exception | None = None
    delete_raises: list[Exception] = field(default_factory=list)


def _install_fake_daytona(monkeypatch: pytest.MonkeyPatch) -> _FakeDaytonaState:
    """
    Inject a fake ``daytona`` module into ``sys.modules`` and return
    its recorder state.

    :param monkeypatch: pytest monkeypatch (restores sys.modules after
        the test).
    :returns: The state object the fake records into.
    """
    import types

    state = _FakeDaytonaState()

    class _Client:
        """Fake ``daytona.Daytona`` API client."""

        def __init__(self) -> None:
            state.client_count += 1

        def create(
            self,
            params: _CreateParams,
            *,
            timeout: float,
            on_snapshot_create_logs: object = None,
        ) -> _FakeSandbox:
            """Record creation and register the new sandbox."""
            if state.create_raises is not None:
                raise state.create_raises
            state.create_calls.append(
                _CreateCall(
                    params=params,
                    timeout=timeout,
                    has_log_callback=on_snapshot_create_logs is not None,
                )
            )
            sandbox = _FakeSandbox(f"dt-new-{len(state.create_calls)}")
            state.sandboxes[sandbox.id] = sandbox
            return sandbox

        def get(self, sandbox_id: str) -> _FakeSandbox:
            """Resolve a live sandbox or raise the not-found error."""
            sandbox = state.sandboxes.get(sandbox_id)
            if sandbox is None:
                raise _FakeNotFoundError(sandbox_id)
            return sandbox

        def delete(self, sandbox: _FakeSandbox) -> None:
            """Record the deletion and drop the sandbox."""
            if state.delete_raises:
                raise state.delete_raises.pop(0)
            state.deleted.append(sandbox.id)
            state.sandboxes.pop(sandbox.id, None)

    fake = types.ModuleType("daytona")
    fake.Daytona = _Client  # type: ignore[attr-defined]
    fake.CreateSandboxFromImageParams = _CreateParams  # type: ignore[attr-defined]
    fake.Resources = _FakeResources  # type: ignore[attr-defined]
    fake.DaytonaError = _FakeDaytonaError  # type: ignore[attr-defined]
    fake.DaytonaNotFoundError = _FakeNotFoundError  # type: ignore[attr-defined]
    fake.DaytonaConflictError = _FakeConflictError  # type: ignore[attr-defined]
    fake.SandboxState = _FakeSandboxState  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "daytona", fake)
    return state


@pytest.fixture()
def fake_daytona(monkeypatch: pytest.MonkeyPatch) -> _FakeDaytonaState:
    """
    Install the fake SDK with credentials present.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The fake's recorder state.
    """
    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_test_key")
    # A developer's ambient passthrough config must not leak into tests
    # that assert the no-injection default.
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    return _install_fake_daytona(monkeypatch)


# ── prepare ─────────────────────────────────────────────────


def test_prepare_requires_api_key(
    fake_daytona: _FakeDaytonaState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Preflight fails loud without ``DAYTONA_API_KEY`` — otherwise the
    failure surfaces later as an opaque SDK auth error mid-provision.
    """
    monkeypatch.delenv("DAYTONA_API_KEY")
    with pytest.raises(click.ClickException, match="DAYTONA_API_KEY"):
        DaytonaSandboxLauncher().prepare()


def test_prepare_passes_with_api_key(fake_daytona: _FakeDaytonaState) -> None:
    """SDK importable + key set → preflight succeeds."""
    DaytonaSandboxLauncher().prepare()


# ── provision ───────────────────────────────────────────────


def test_provision_defaults_official_image_and_disables_autostop(
    fake_daytona: _FakeDaytonaState,
) -> None:
    """
    A bare provision uses the official host image, DISABLES idle
    auto-stop (Daytona's 15-minute default would kill a host sitting
    between turns), injects no env, and sizes the sandbox like the
    Modal launcher.
    """
    sandbox_id = DaytonaSandboxLauncher().provision("managed-abc")

    assert sandbox_id == "dt-new-1"
    [create] = fake_daytona.create_calls
    assert create.params.image == DEFAULT_HOST_IMAGE
    # 0 = disabled; any other value re-enables the idle reaper that
    # would stop the session host mid-conversation.
    assert create.params.auto_stop_interval == 0
    assert create.params.env_vars is None
    assert create.params.labels == {"omnigent-name": "managed-abc"}
    assert create.params.resources == _FakeResources(cpu=2, memory=4)
    # Cold creates pull + snapshot the image (minutes); the SDK's 60s
    # default only covers the warm path.
    assert create.timeout > 60
    assert create.has_log_callback is True


def test_provision_image_resolution_order(
    fake_daytona: _FakeDaytonaState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Explicit constructor image wins over the env override, which wins
    over the official default — the same precedence the server's
    ``sandbox.daytona.image`` config relies on.
    """
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "docker.io/env/override:1")

    DaytonaSandboxLauncher(image="docker.io/explicit/img:2").provision("a")
    DaytonaSandboxLauncher().provision("b")

    first, second = fake_daytona.create_calls
    assert first.params.image == "docker.io/explicit/img:2"
    assert second.params.image == "docker.io/env/override:1"


def test_provision_env_passthrough_resolves_from_server_env(
    fake_daytona: _FakeDaytonaState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Constructor env NAMES resolve to values from the server process
    environment at provision time — the config carries names only, so
    secret values never live in the config file.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test-456")

    DaytonaSandboxLauncher(env=["OPENAI_API_KEY", "GIT_TOKEN"]).provision("a")

    [create] = fake_daytona.create_calls
    assert create.params.env_vars == {
        "OPENAI_API_KEY": "sk-test-123",
        "GIT_TOKEN": "ghp-test-456",
    }


def test_provision_env_passthrough_env_var_fallback(
    fake_daytona: _FakeDaytonaState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Without constructor names, the comma-separated
    ``OMNIGENT_DAYTONA_SANDBOX_ENV`` names apply (whitespace around
    commas tolerated).
    """
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "OPENAI_API_KEY , GIT_TOKEN")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test-456")

    DaytonaSandboxLauncher().provision("a")

    [create] = fake_daytona.create_calls
    assert create.params.env_vars == {
        "OPENAI_API_KEY": "sk-test-123",
        "GIT_TOKEN": "ghp-test-456",
    }


def test_provision_env_passthrough_missing_var_fails_loud(
    fake_daytona: _FakeDaytonaState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A configured name unset in the server environment is an operator
    error — launching without it would surface much later as an opaque
    harness auth failure inside the sandbox.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        DaytonaSandboxLauncher(env=["OPENAI_API_KEY"]).provision("a")
    # Failing the env resolution must not have created a sandbox.
    assert fake_daytona.create_calls == []


def test_provision_wraps_sdk_errors_with_provider_reason(
    fake_daytona: _FakeDaytonaState,
) -> None:
    """
    SDK failures surface as launcher-contract ClickExceptions carrying
    the provider's reason. Raw SDK exceptions would fall through the
    managed launch's generic except and report an opaque "internal
    error" — this is exactly how a real "Organization is suspended:
    Please verify your email address" rejection reached a user as
    noise during live verification.
    """
    fake_daytona.create_raises = _FakeDaytonaError(
        "Failed to create sandbox: Organization is suspended"
    )

    with pytest.raises(click.ClickException, match="Organization is suspended"):
        DaytonaSandboxLauncher().provision("a")


def test_launcher_reuses_one_client(fake_daytona: _FakeDaytonaState) -> None:
    """
    One API client per launcher: provision twice, still one client —
    per-call clients would re-handshake auth on every primitive.
    """
    launcher = DaytonaSandboxLauncher()
    launcher.provision("a")
    launcher.provision("b")
    assert fake_daytona.client_count == 1


# ── run ─────────────────────────────────────────────────────


def test_run_returns_combined_output_as_stdout(fake_daytona: _FakeDaytonaState) -> None:
    """
    Daytona merges the output streams; the combined text lands in
    ``stdout`` with ``stderr`` empty (the documented merged-streams
    convention of ``RemoteCommandResult``).
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    fake_daytona.sandboxes[sandbox_id].process.exec_queue.append(_ExecResponse(0, "/root\n"))

    result = launcher.run(sandbox_id, 'printf %s "$HOME"')

    assert result.returncode == 0
    assert result.stdout == "/root\n"
    assert result.stderr == ""
    [call] = fake_daytona.sandboxes[sandbox_id].process.exec_calls
    assert call.command == 'printf %s "$HOME"'


def test_run_check_raises_on_nonzero_exit(fake_daytona: _FakeDaytonaState) -> None:
    """
    ``check=True`` (the managed flow's default) raises with the
    command named; ``check=False`` returns the failure for the caller
    to inspect.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    sandbox = fake_daytona.sandboxes[sandbox_id]
    sandbox.process.exec_queue.append(_ExecResponse(1, "boom"))
    sandbox.process.exec_queue.append(_ExecResponse(1, "boom"))

    with pytest.raises(click.ClickException, match="exit 1"):
        launcher.run(sandbox_id, "false")
    result = launcher.run(sandbox_id, "false", check=False)
    assert result.returncode == 1
    assert result.stdout == "boom"


def test_run_wraps_sdk_errors_with_provider_reason(
    fake_daytona: _FakeDaytonaState,
) -> None:
    """
    ``run`` wraps SDK exec failures as launcher-contract
    ClickExceptions carrying the provider's reason — same posture as
    the (tested) provision wrap. A raw SDK exception here would fall
    through the managed flow's generic except as an opaque "internal
    error" when, e.g., the toolbox is down or the sandbox stopped
    mid-command.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    fake_daytona.sandboxes[sandbox_id].process.exec_raises = _FakeDaytonaError(
        "toolbox unavailable"
    )

    with pytest.raises(click.ClickException, match="toolbox unavailable"):
        launcher.run(sandbox_id, "true")


def test_run_unknown_sandbox_fails_with_hint(fake_daytona: _FakeDaytonaState) -> None:
    """
    A vanished sandbox surfaces as a clear error naming the id — the
    shape the managed-relaunch machinery logs when generation N died.
    """
    with pytest.raises(click.ClickException, match="dt-gone"):
        DaytonaSandboxLauncher().run("dt-gone", "true")


# ── terminate ───────────────────────────────────────────────


def test_terminate_deletes_and_is_idempotent(fake_daytona: _FakeDaytonaState) -> None:
    """
    Terminate deletes the sandbox; terminating an unknown id is a
    no-op success (cleanup paths race the provider's own deletion).
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")

    launcher.terminate(sandbox_id)
    assert fake_daytona.deleted == [sandbox_id]

    # Second terminate: already gone → swallow, not raise.
    launcher.terminate(sandbox_id)
    assert fake_daytona.deleted == [sandbox_id]


def test_terminate_retries_state_change_conflicts(
    fake_daytona: _FakeDaytonaState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A delete racing another state change retries until it lands.

    Observed live: a second terminate while the first deletion was
    still settling raised ``DaytonaConflictError: Sandbox state change
    in progress``. Two cleanup paths can genuinely overlap
    (launch-failure cleanup vs session delete), so terminate must ride
    out the conflict window rather than surface it.
    """
    monkeypatch.setattr("omnigent.onboarding.sandboxes.daytona._TERMINATE_CONFLICT_BACKOFF_S", 0.0)
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    # First two attempts conflict; the third (final allowed attempt)
    # succeeds — exercising the full retry budget.
    fake_daytona.delete_raises = [_FakeConflictError("in progress"), _FakeConflictError("x")]

    launcher.terminate(sandbox_id)

    assert fake_daytona.deleted == [sandbox_id]


def test_terminate_conflict_exhaustion_raises(
    fake_daytona: _FakeDaytonaState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A conflict that outlives the retry budget surfaces — the managed
    teardown callers are best-effort and log it; swallowing forever
    could hide a wedged sandbox that never gets reaped.
    """
    monkeypatch.setattr("omnigent.onboarding.sandboxes.daytona._TERMINATE_CONFLICT_BACKOFF_S", 0.0)
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    fake_daytona.delete_raises = [_FakeConflictError("stuck")] * 3

    with pytest.raises(_FakeConflictError):
        launcher.terminate(sandbox_id)
    # Nothing recorded as deleted — every attempt conflicted.
    assert fake_daytona.deleted == []


# ── attach / keep_alive ─────────────────────────────────────


def test_attach_starts_stopped_sandbox(fake_daytona: _FakeDaytonaState) -> None:
    """
    Attaching to a stopped sandbox starts it — unlike Modal, a stopped
    Daytona sandbox is restartable, so attach must recover it instead
    of rejecting it.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    sandbox = fake_daytona.sandboxes[sandbox_id]
    sandbox.state = _FakeSandboxState.STOPPED

    launcher.attach(sandbox_id)

    # State was refreshed before the decision (a cached handle's state
    # is stale) and exactly one start was issued.
    assert sandbox.refresh_data_calls == 1
    assert sandbox.start_calls == 1


def test_attach_running_sandbox_skips_start(fake_daytona: _FakeDaytonaState) -> None:
    """
    A sandbox already STARTED attaches without a start call — issuing
    one anyway would be a pointless state-change request the provider
    may reject as a conflict.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")

    launcher.attach(sandbox_id)

    assert fake_daytona.sandboxes[sandbox_id].start_calls == 0


def test_attach_unknown_sandbox_fails_with_hint(fake_daytona: _FakeDaytonaState) -> None:
    """A vanished sandbox surfaces as a clear error naming the id."""
    with pytest.raises(click.ClickException, match="dt-gone"):
        DaytonaSandboxLauncher().attach("dt-gone")


def test_keep_alive_disables_autostop(fake_daytona: _FakeDaytonaState) -> None:
    """
    keep_alive re-asserts the disabled idle auto-stop (interval 0) —
    it matters for attached sandboxes created outside this flow, whose
    15-minute default would kill a host sitting between turns.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")

    launcher.keep_alive(sandbox_id)

    # 0 = disabled; any other value re-enables the idle reaper.
    assert fake_daytona.sandboxes[sandbox_id].autostop_intervals == [0]


def test_keep_alive_soft_fails_on_provider_rejection(
    fake_daytona: _FakeDaytonaState, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    A rejected auto-stop setting warns instead of aborting — the base
    contract says keep_alive is soft-fail, since the bootstrap can
    proceed (the host just risks idle-stop later).
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    fake_daytona.sandboxes[sandbox_id].autostop_raises = _FakeDaytonaError("nope")

    launcher.keep_alive(sandbox_id)  # must not raise

    # The warning names the failure so the user knows the idle-stop
    # risk exists; silence would hide it until the host vanished.
    assert "could not disable idle auto-stop" in capsys.readouterr().out


# ── put / wheel_install_command ─────────────────────────────


def test_put_ships_file_via_filesystem_api(fake_daytona: _FakeDaytonaState) -> None:
    """
    ``put`` rides the SDK's filesystem upload with the exact local
    source and remote destination the bootstrap passed.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")

    launcher.put(sandbox_id, Path("/tmp/oa-wheels.tgz"), "/tmp/oa-wheels.tgz")

    assert fake_daytona.sandboxes[sandbox_id].fs.uploads == [
        _UploadCall(src="/tmp/oa-wheels.tgz", dst="/tmp/oa-wheels.tgz")
    ]


def test_put_wraps_sdk_errors_with_provider_reason(fake_daytona: _FakeDaytonaState) -> None:
    """
    Upload failures surface as launcher-contract ClickExceptions
    carrying the provider's reason — same posture as run/provision.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    fake_daytona.sandboxes[sandbox_id].fs.upload_raises = _FakeDaytonaError("toolbox down")

    with pytest.raises(click.ClickException, match="toolbox down"):
        launcher.put(sandbox_id, Path("/tmp/x"), "/tmp/x")


def test_wheel_install_command_overlays_baked_install(fake_daytona: _FakeDaytonaState) -> None:
    """
    The install command must force-reinstall without deps: the host
    image bakes omnigent at the same version, so plain pip would
    silently skip the freshly-shipped wheels.
    """
    command = DaytonaSandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")

    assert "tar xzf /tmp/oa-wheels.tgz" in command
    assert "--force-reinstall" in command
    assert "--no-deps" in command


# ── exec_foreground ─────────────────────────────────────────


def test_exec_foreground_runs_command_over_pty(
    fake_daytona: _FakeDaytonaState, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    The foreground attach sends the command into a fresh PTY session
    (TERM forced for tmux, ``exec`` so the PTY's exit code is the
    command's), echoes its output locally, and returns the exit code.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    process = fake_daytona.sandboxes[sandbox_id].process
    process.pty_handle = _FakePtyHandle(
        _FakePtyResult(exit_code=7), output_chunks=[b"host registered\r\n"]
    )

    returncode = launcher.exec_foreground(sandbox_id, "omnigent host --server u")

    assert returncode == 7
    # One fresh session per call — a fixed id would collide with the
    # provider's "session id already in use" error on reconnect.
    assert len(process.pty_session_ids) == 1
    # exec replaces the PTY shell so the close frame carries the
    # command's own exit code; TERM is required for tmux-spawning
    # harnesses downstream.
    assert process.pty_handle.sent_inputs == [
        "TERM=xterm-256color exec omnigent host --server u\n"
    ]
    # Remote output reached the local terminal — a silent foreground
    # attach would hide the host's registration banner and errors.
    assert "host registered" in capsys.readouterr().out
    # The websocket is released even on the happy path.
    assert process.pty_handle.disconnect_calls == 1


def test_exec_foreground_kills_remote_on_interrupt(fake_daytona: _FakeDaytonaState) -> None:
    """
    Ctrl-C during the blocking PTY read kills the remote process and
    re-raises — otherwise a detached `omnigent host` would keep the
    sandbox registered with a dead terminal.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    process = fake_daytona.sandboxes[sandbox_id].process
    process.pty_handle = _FakePtyHandle(
        _FakePtyResult(exit_code=None), output_chunks=[], wait_raises=KeyboardInterrupt()
    )

    with pytest.raises(KeyboardInterrupt):
        launcher.exec_foreground(sandbox_id, "omnigent host --server u")

    # The remote process was killed AND the websocket released — a
    # missing kill leaves the host running headless; a missing
    # disconnect leaks the pooled connection.
    assert process.pty_handle.kill_calls == 1
    assert process.pty_handle.disconnect_calls == 1


def test_exec_foreground_missing_exit_code_fails_loud(
    fake_daytona: _FakeDaytonaState,
) -> None:
    """
    A PTY that ends without an exit code (websocket drop) raises with
    the daemon's error instead of inventing a status — connect treats
    the return value as the remote command's outcome.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    fake_daytona.sandboxes[sandbox_id].process.pty_handle = _FakePtyHandle(
        _FakePtyResult(exit_code=None, error="connection reset")
    )

    with pytest.raises(click.ClickException, match="connection reset"):
        launcher.exec_foreground(sandbox_id, "omnigent host --server u")


def test_exec_foreground_wraps_pty_create_errors(fake_daytona: _FakeDaytonaState) -> None:
    """
    A PTY session that can't be created (stopped sandbox) surfaces the
    provider's reason through the launcher contract.
    """
    launcher = DaytonaSandboxLauncher()
    sandbox_id = launcher.provision("a")
    fake_daytona.sandboxes[sandbox_id].process.create_pty_raises = _FakeDaytonaError(
        "sandbox stopped"
    )

    with pytest.raises(click.ClickException, match="sandbox stopped"):
        launcher.exec_foreground(sandbox_id, "true")


# ── capability surface ──────────────────────────────────────


def test_only_login_primitives_are_capability_gated(
    fake_daytona: _FakeDaytonaState,
) -> None:
    """
    Daytona supports the CLI bootstrap, so only the in-sandbox OAuth
    login's primitives stay gated: there is no local→sandbox port
    forwarding path, and ``stream_exec``'s sole consumer (that login)
    fails fast on the port-forward flag before ever reaching it.
    """
    launcher = DaytonaSandboxLauncher()
    assert launcher.supports_cli_bootstrap is True
    assert launcher.supports_local_port_forward is False
    with pytest.raises(SandboxCapabilityError):
        launcher.stream_exec("dt-1", "echo hi")
    with pytest.raises(SandboxCapabilityError):
        launcher.forward_local_port("dt-1", 8022)
