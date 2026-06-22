"""Tests for :mod:`omnigent.onboarding.sandboxes.modal`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.onboarding.sandboxes.modal import (
    DEFAULT_HOST_IMAGE,
    HOST_IMAGE_ENV_VAR,
    MAX_SANDBOX_LIFETIME_S,
    MODAL_APP_NAME,
    REGISTRY_SECRET_ENV_VAR,
    SANDBOX_SECRETS_ENV_VAR,
    ModalSandboxLauncher,
)

# ── Fake Modal SDK ──────────────────────────────────────────
#
# The Modal SDK is an optional dependency the test environment doesn't
# install, and real Sandbox objects only exist server-side anyway — so
# these are hand-rolled stub classes (never MagicMock: the launcher's
# attribute access must hit explicitly defined recorders, not silently
# succeed). The fake module is injected via sys.modules so the
# launcher's function-local `import modal` resolves to it.


class _FakeNotFoundError(Exception):
    """Stands in for ``modal.exception.NotFoundError``."""


@dataclass
class _ExecCall:
    """
    One recorded ``Sandbox.exec`` invocation.

    :param argv: The argv passed (e.g. ``["bash", "-lc", "echo hi"]``).
    :param pty: Whether a PTY was requested.
    """

    argv: list[str]
    pty: bool


@dataclass
class _CopyCall:
    """
    One recorded ``filesystem.copy_from_local`` invocation.

    :param local_path: Local source path (stringified).
    :param remote_path: Sandbox destination path.
    """

    local_path: str
    remote_path: str


@dataclass
class _LookupCall:
    """
    One recorded ``App.lookup`` invocation.

    :param name: App name looked up.
    :param create_if_missing: Whether auto-create was requested.
    """

    name: str
    create_if_missing: bool


@dataclass
class _CreateCall:
    """
    One recorded ``Sandbox.create`` invocation.

    :param entrypoint: Positional entrypoint argv.
    :param app: The app handle passed.
    :param image: The image definition passed.
    :param timeout: Requested lifetime in seconds.
    :param cpu: Requested vCPU count.
    :param memory: Requested memory in MiB.
    :param secrets: Workload secrets passed, or ``None`` when no
        sandbox env injection was configured.
    """

    entrypoint: list[str]
    app: object
    image: _FakeImage
    timeout: int
    cpu: float
    memory: int
    secrets: list[_FakeSecret] | None


class _FakeStream:
    """
    Canned stand-in for a Modal ``StreamReader``.

    :param text: Full output the stream carries.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    def __iter__(self) -> object:
        """Yield output lines as the SDK's line iterator does."""
        return iter(self._text.splitlines(keepends=True))

    def read(self) -> str:
        """Return the full output (SDK semantics: blocks until EOF)."""
        return self._text


class _FakeProcess:
    """
    Canned stand-in for a Modal ``ContainerProcess``.

    :param stdout: Text the stdout stream carries.
    :param stderr: Text the stderr stream carries.
    :param returncode: Exit code ``wait()`` reports.
    :param wait_raises: Exception ``wait()`` raises instead of
        returning (e.g. ``KeyboardInterrupt()`` for the Ctrl-C path).
    """

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        wait_raises: BaseException | None = None,
    ) -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._returncode = returncode
        self._wait_raises = wait_raises
        self.waited = False

    def wait(self) -> int:
        """Reap the process (or raise the canned interrupt)."""
        if self._wait_raises is not None:
            raise self._wait_raises
        self.waited = True
        return self._returncode

    def poll(self) -> int | None:
        """Exit code once reaped, else ``None`` (still running)."""
        return self._returncode if self.waited else None


class _FakeFilesystem:
    """Recorder for the sandbox ``filesystem`` namespace."""

    def __init__(self) -> None:
        self.copy_calls: list[_CopyCall] = []

    def copy_from_local(self, local_path: str, remote_path: str) -> None:
        """Record the upload (no real transfer)."""
        self.copy_calls.append(_CopyCall(local_path=local_path, remote_path=remote_path))


class _FakeSandbox:
    """
    Recording stand-in for a ``modal.Sandbox`` handle.

    :param object_id: The sandbox id, e.g. ``"sb-1"``.
    """

    def __init__(self, object_id: str) -> None:
        self.object_id = object_id
        self.exec_calls: list[_ExecCall] = []
        # Processes handed back by successive exec() calls, in order;
        # an empty queue yields a default success process.
        self.exec_queue: list[_FakeProcess] = []
        self.tags: dict[str, str] = {}
        self.poll_result: int | None = None
        self.filesystem = _FakeFilesystem()

    def exec(self, *argv: str, pty: bool = False) -> _FakeProcess:
        """Record the call and pop the next canned process."""
        self.exec_calls.append(_ExecCall(argv=list(argv), pty=pty))
        return self.exec_queue.pop(0) if self.exec_queue else _FakeProcess()

    def set_tags(self, tags: dict[str, str]) -> None:
        """Record tag assignment."""
        self.tags.update(tags)

    def poll(self) -> int | None:
        """Canned liveness: ``None`` means still running."""
        return self.poll_result


@dataclass
class _FakeSecret:
    """
    Recorder for a ``modal.Secret`` reference.

    :param name: The workspace secret name passed to ``from_name``,
        e.g. ``"ghcr-pull"``.
    """

    name: str


@dataclass
class _FakeImage:
    """
    Recorder for a ``modal.Image.from_registry`` definition.

    :param tag: The registry image reference, e.g.
        ``"ghcr.io/omnigent-ai/omnigent-host:latest"``.
    :param secret: Registry-credentials secret, or ``None`` for an
        anonymous pull.
    """

    tag: str
    secret: _FakeSecret | None


@dataclass
class _FakeModalState:
    """
    Recorders shared by one test's fake modal SDK.

    :param lookup_calls: Every ``App.lookup`` call.
    :param create_calls: Every ``Sandbox.create`` call.
    :param sandboxes: Sandboxes resolvable via ``Sandbox.from_id``,
        keyed by object id. Pre-seed to simulate existing sandboxes.
    :param app: Sentinel object ``App.lookup`` returns.
    """

    lookup_calls: list[_LookupCall] = field(default_factory=list)
    create_calls: list[_CreateCall] = field(default_factory=list)
    sandboxes: dict[str, _FakeSandbox] = field(default_factory=dict)
    app: object = field(default_factory=object)


def _install_fake_modal(monkeypatch: pytest.MonkeyPatch) -> _FakeModalState:
    """
    Inject a fake ``modal`` module into ``sys.modules`` and return its
    recorder state.

    :param monkeypatch: pytest monkeypatch (restores sys.modules after
        the test).
    :returns: The state object the fake records into.
    """
    state = _FakeModalState()

    class _App:
        """Fake ``modal.App`` namespace."""

        @staticmethod
        def lookup(name: str, *, create_if_missing: bool = False) -> object:
            """Record the lookup and hand back the app sentinel."""
            state.lookup_calls.append(_LookupCall(name=name, create_if_missing=create_if_missing))
            return state.app

    class _Sandbox:
        """Fake ``modal.Sandbox`` namespace."""

        @staticmethod
        def create(
            *entrypoint: str,
            app: object,
            image: _FakeImage,
            timeout: int,
            cpu: float,
            memory: int,
            secrets: list[_FakeSecret] | None = None,
        ) -> _FakeSandbox:
            """Record creation and register the new sandbox."""
            state.create_calls.append(
                _CreateCall(
                    entrypoint=list(entrypoint),
                    app=app,
                    image=image,
                    timeout=timeout,
                    cpu=cpu,
                    memory=memory,
                    secrets=secrets,
                )
            )
            sandbox = _FakeSandbox(f"sb-new-{len(state.create_calls)}")
            state.sandboxes[sandbox.object_id] = sandbox
            return sandbox

        @staticmethod
        def from_id(sandbox_id: str) -> _FakeSandbox:
            """Resolve a pre-seeded sandbox or raise NotFound."""
            if sandbox_id not in state.sandboxes:
                raise _FakeNotFoundError(sandbox_id)
            return state.sandboxes[sandbox_id]

    class _Image:
        """Fake ``modal.Image`` namespace."""

        @staticmethod
        def from_registry(tag: str, secret: _FakeSecret | None = None) -> _FakeImage:
            """Record the registry pull definition."""
            return _FakeImage(tag=tag, secret=secret)

    class _Secret:
        """Fake ``modal.Secret`` namespace."""

        @staticmethod
        def from_name(name: str) -> _FakeSecret:
            """Record the workspace secret reference."""
            return _FakeSecret(name=name)

    fake = types.ModuleType("modal")
    # Module attribute assignments below define the fake SDK's public
    # surface; the launcher accesses exactly these names.
    fake.App = _App  # type: ignore[attr-defined]
    fake.Sandbox = _Sandbox  # type: ignore[attr-defined]
    fake.Image = _Image  # type: ignore[attr-defined]
    fake.Secret = _Secret  # type: ignore[attr-defined]
    exception_mod = types.ModuleType("modal.exception")
    exception_mod.NotFoundError = _FakeNotFoundError  # type: ignore[attr-defined]
    fake.exception = exception_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modal", fake)
    monkeypatch.setitem(sys.modules, "modal.exception", exception_mod)
    return state


def _seed_sandbox(state: _FakeModalState, sandbox_id: str = "sb-1") -> _FakeSandbox:
    """
    Register an existing running sandbox in the fake SDK.

    :param state: The fake SDK's recorder state.
    :param sandbox_id: Id to register under.
    :returns: The seeded sandbox recorder.
    """
    sandbox = _FakeSandbox(sandbox_id)
    state.sandboxes[sandbox_id] = sandbox
    return sandbox


# ── prepare ─────────────────────────────────────────────────


def test_prepare_raises_with_install_hint_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without the optional SDK, prepare must fail with the exact install
    remediation (extras name + auth command) — not a raw ImportError.
    ``sys.modules[name] = None`` makes ``import modal`` raise
    ImportError regardless of whether the package is installed.
    """
    monkeypatch.setitem(sys.modules, "modal", None)
    with pytest.raises(click.ClickException) as exc:
        ModalSandboxLauncher().prepare()
    assert "omnigent[modal]" in str(exc.value)
    assert "modal token new" in str(exc.value)


def test_prepare_raises_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No env token pair and no token file → fail loud with the fix."""
    _install_fake_modal(monkeypatch)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    # Point the documented config-path override at a missing file so
    # the developer's real ~/.modal.toml can't satisfy the check.
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(tmp_path / "absent.toml"))
    with pytest.raises(click.ClickException) as exc:
        ModalSandboxLauncher().prepare()
    assert "modal token new" in str(exc.value)


def test_prepare_accepts_env_token_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The MODAL_TOKEN_ID/SECRET pair alone satisfies the preflight."""
    _install_fake_modal(monkeypatch)
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-test")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-test")
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(tmp_path / "absent.toml"))
    ModalSandboxLauncher().prepare()


def test_prepare_accepts_token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A token file at MODAL_CONFIG_PATH satisfies the preflight."""
    _install_fake_modal(monkeypatch)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    token_file = tmp_path / "modal.toml"
    token_file.write_text('[default]\ntoken_id = "ak-x"\n', encoding="utf-8")
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(token_file))
    ModalSandboxLauncher().prepare()


# ── provision / attach / keep_alive ─────────────────────────


def test_provision_creates_max_lifetime_sandbox_under_shared_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Provision must create the sandbox under the shared auto-created
    App, at Modal's 24h maximum lifetime, with a hold-open entrypoint
    and the official prebaked host image — and label it via tags (names
    must be unique per App, so the label can't be the sandbox name).
    """
    state = _install_fake_modal(monkeypatch)
    # The ambient environment must not leak overrides into the
    # default-image assertion below.
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    monkeypatch.delenv(REGISTRY_SECRET_ENV_VAR, raising=False)
    sandbox_id = ModalSandboxLauncher().provision("my-host")

    assert state.lookup_calls == [_LookupCall(name=MODAL_APP_NAME, create_if_missing=True)]
    create = state.create_calls[0]
    # sleep infinity holds the sandbox open for the full window — with
    # no entrypoint, liveness would depend on exec activity.
    assert create.entrypoint == ["sleep", "infinity"]
    assert create.timeout == MAX_SANDBOX_LIFETIME_S
    # The app handle from lookup must flow into create.
    assert create.app is state.app
    # Image: the official prebaked host image (omnigent + git/tmux
    # baked in — sandbox creation must not pay an in-sandbox dependency
    # install), pulled anonymously by default.
    assert create.image.tag == DEFAULT_HOST_IMAGE
    assert create.image.secret is None
    assert sandbox_id == "sb-new-1"
    assert state.sandboxes[sandbox_id].tags == {"omnigent-name": "my-host"}


def test_provision_honors_image_override_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    OMNIGENT_MODAL_HOST_IMAGE must replace the default image ref —
    it's the escape hatch for org-internal copies of the host image.
    """
    state = _install_fake_modal(monkeypatch)
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "ghcr.io/acme/omnigent-host:sha-abc1234")
    monkeypatch.delenv(REGISTRY_SECRET_ENV_VAR, raising=False)
    ModalSandboxLauncher().provision("my-host")

    assert state.create_calls[0].image.tag == "ghcr.io/acme/omnigent-host:sha-abc1234"


def test_provision_passes_registry_secret_for_private_pulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    OMNIGENT_MODAL_REGISTRY_SECRET must thread the named Modal secret
    into the image pull — without it, a private host image fails with
    an unauthorized pull at sandbox start.
    """
    state = _install_fake_modal(monkeypatch)
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    monkeypatch.setenv(REGISTRY_SECRET_ENV_VAR, "ghcr-pull")
    ModalSandboxLauncher().provision("my-host")

    assert state.create_calls[0].image.secret == _FakeSecret(name="ghcr-pull")


def test_attach_accepts_running_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """A running sandbox (poll → None) attaches without error."""
    state = _install_fake_modal(monkeypatch)
    _seed_sandbox(state)
    ModalSandboxLauncher().attach("sb-1")


def test_attach_rejects_terminated_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A terminated sandbox must be rejected with the 24h-cap explanation
    — silently proceeding would fail later with opaque exec errors.
    """
    state = _install_fake_modal(monkeypatch)
    _seed_sandbox(state).poll_result = 137
    with pytest.raises(click.ClickException) as exc:
        ModalSandboxLauncher().attach("sb-1")
    assert "24 hours" in str(exc.value)


def test_attach_rejects_unknown_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The SDK's NotFoundError must surface as a ClickException naming the
    id and the recreate command (expired sandboxes vanish entirely).
    """
    _install_fake_modal(monkeypatch)
    with pytest.raises(click.ClickException) as exc:
        ModalSandboxLauncher().attach("sb-gone")
    assert "sb-gone" in str(exc.value)
    assert "sandbox create --provider modal" in str(exc.value)


def test_keep_alive_surfaces_lifetime_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    keep_alive can't extend anything (lifetime is fixed at creation);
    it must tell the user about the 24h cap instead of staying silent.
    """
    state = _install_fake_modal(monkeypatch)
    _seed_sandbox(state)
    ModalSandboxLauncher().keep_alive("sb-1")
    assert "24 hours" in capsys.readouterr().out


# ── run / put / stream_exec ─────────────────────────────────


def test_run_wraps_bash_lc_and_captures_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` execs via bash -lc and returns the full captured streams."""
    state = _install_fake_modal(monkeypatch)
    sandbox = _seed_sandbox(state)
    sandbox.exec_queue.append(_FakeProcess(stdout="remote-out\n", stderr="remote-err\n"))

    result = ModalSandboxLauncher().run("sb-1", "echo hi")

    assert sandbox.exec_calls == [_ExecCall(argv=["bash", "-lc", "echo hi"], pty=False)]
    # Content assertions prove the streams traversed the adapter, not
    # just that a result object came back.
    assert result.returncode == 0
    assert result.stdout == "remote-out\n"
    assert result.stderr == "remote-err\n"


def test_run_raises_on_failure_when_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """check=True turns a non-zero exit into a ClickException with id."""
    state = _install_fake_modal(monkeypatch)
    _seed_sandbox(state).exec_queue.append(_FakeProcess(returncode=2))
    with pytest.raises(click.ClickException) as exc:
        ModalSandboxLauncher().run("sb-1", "false")
    assert "sb-1" in str(exc.value)


def test_put_ships_file_via_filesystem_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``put`` rides the SDK filesystem API with both paths intact."""
    state = _install_fake_modal(monkeypatch)
    sandbox = _seed_sandbox(state)
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"payload")

    ModalSandboxLauncher().put("sb-1", local, "/tmp/oa-wheels.tgz")

    assert sandbox.filesystem.copy_calls == [
        _CopyCall(local_path=str(local), remote_path="/tmp/oa-wheels.tgz")
    ]


def test_stream_exec_merges_stderr_without_pty(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Without a PTY, stdout/stderr are separate SDK streams but the
    RemoteProcess contract promises combined output — the launcher must
    merge in-shell (2>&1).
    """
    state = _install_fake_modal(monkeypatch)
    sandbox = _seed_sandbox(state)
    sandbox.exec_queue.append(_FakeProcess(stdout="line-1\nline-2\n"))

    process = ModalSandboxLauncher().stream_exec("sb-1", "echo hi")

    assert sandbox.exec_calls == [_ExecCall(argv=["bash", "-lc", "echo hi 2>&1"], pty=False)]
    # The same iterator resumes across reads (contract used by the
    # OAuth URL scraper).
    assert next(process.lines) == "line-1\n"
    assert list(process.lines) == ["line-2\n"]
    assert process.wait() == 0
    # close() after exit is a safe no-op.
    process.close()


def test_stream_exec_pty_skips_shell_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PTY already interleaves the streams — no 2>&1 rewrite."""
    state = _install_fake_modal(monkeypatch)
    sandbox = _seed_sandbox(state)
    ModalSandboxLauncher().stream_exec("sb-1", "databricks auth login", pty=True)
    assert sandbox.exec_calls == [
        _ExecCall(argv=["bash", "-lc", "databricks auth login"], pty=True)
    ]


# ── forward_local_port (unsupported) ────────────────────────


def test_forward_local_port_raises_capability_error() -> None:
    """
    Modal has no local→sandbox path; the launcher must keep the base
    default (capability error naming --no-auth) and advertise the gap
    via the flag the bootstrap fail-fasts on.
    """
    launcher = ModalSandboxLauncher()
    assert ModalSandboxLauncher.supports_local_port_forward is False
    with pytest.raises(SandboxCapabilityError) as exc:
        launcher.forward_local_port("sb-1", 8022)
    assert "App auth" in str(exc.value)
    assert "modal" in str(exc.value)


# ── exec_foreground ─────────────────────────────────────────


def test_exec_foreground_records_pid_and_streams_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    The foreground command must run under a PTY with the pid recorded
    (the kill path needs it — Modal has no kill handle) and TERM forced
    (in-sandbox tmux refuses to start under a dumb terminal).
    """
    state = _install_fake_modal(monkeypatch)
    sandbox = _seed_sandbox(state)
    sandbox.exec_queue.append(_FakeProcess(stdout="host-output\n"))

    returncode = ModalSandboxLauncher().exec_foreground("sb-1", "omnigent host --server u")

    assert returncode == 0
    call = sandbox.exec_calls[0]
    assert call.pty is True
    remote = call.argv[-1]
    assert "echo $$ > /tmp/oa-foreground.pid" in remote
    assert "TERM=xterm-256color" in remote
    # `exec` keeps the recorded pid across the swap to the real command.
    assert "exec omnigent host --server u" in remote
    # Output reached the local terminal.
    assert "host-output" in capsys.readouterr().out


def test_exec_foreground_kills_remote_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Ctrl-C must kill the remote process (via the pidfile) and re-raise
    — without the kill, the host would keep running headless in the
    sandbox for up to 24 hours.
    """
    state = _install_fake_modal(monkeypatch)
    sandbox = _seed_sandbox(state)
    sandbox.exec_queue.append(_FakeProcess(wait_raises=KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        ModalSandboxLauncher().exec_foreground("sb-1", "omnigent host --server u")

    # Second exec is the kill, addressed via the recorded pidfile.
    assert len(sandbox.exec_calls) == 2
    assert "kill $(cat /tmp/oa-foreground.pid)" in sandbox.exec_calls[1].argv[-1]


# ── wheel_install_command ───────────────────────────────────


def test_wheel_install_command_overlays_baked_install() -> None:
    """
    The prebaked host image carries omnigent at the same 0.1.0
    version, so the overlay must --force-reinstall (pip would otherwise
    see the version satisfied and silently keep the baked code) and
    --no-deps (the dependency tree is baked; reinstalling it would
    defeat the prebaked image's fast boot).
    """
    command = ModalSandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")
    assert "tar xzf /tmp/oa-wheels.tgz" in command
    assert "pip install" in command
    assert "--force-reinstall" in command
    assert "--no-deps" in command


def test_provision_explicit_image_overrides_env_and_gets_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An explicit constructor image (the server's managed-host
    ``sandbox.modal.image`` config) wins over the env override AND
    still threads the registry secret — a private custom image must
    not silently fall back to an anonymous pull.
    """
    state = _install_fake_modal(monkeypatch)
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "ghcr.io/acme/omnigent-host:env")
    monkeypatch.setenv(REGISTRY_SECRET_ENV_VAR, "ghcr-pull")
    ModalSandboxLauncher(image="docker.io/me/custom-host:latest").provision("my-host")

    create = state.create_calls[0]
    assert create.image.tag == "docker.io/me/custom-host:latest"
    assert create.image.secret == _FakeSecret(name="ghcr-pull")


def test_provision_injects_configured_sandbox_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Constructor secret names (the server's managed-host
    ``sandbox.modal.secrets`` config) become workload secrets on
    ``Sandbox.create`` — this is how harness LLM credentials reach the
    sandbox env without transiting our config/DB.
    """
    state = _install_fake_modal(monkeypatch)
    monkeypatch.delenv(SANDBOX_SECRETS_ENV_VAR, raising=False)
    ModalSandboxLauncher(secrets=["omnigent-llm", "gateway-extras"]).provision("my-host")

    assert state.create_calls[0].secrets == [
        _FakeSecret(name="omnigent-llm"),
        _FakeSecret(name="gateway-extras"),
    ]


def test_provision_resolves_sandbox_secrets_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without constructor names, OMNIGENT_MODAL_SANDBOX_SECRETS
    (comma-separated, whitespace tolerated) supplies the workload
    secrets — the CLI flow's path to the same feature.
    """
    state = _install_fake_modal(monkeypatch)
    monkeypatch.setenv(SANDBOX_SECRETS_ENV_VAR, "omnigent-llm, extra-creds")
    ModalSandboxLauncher().provision("my-host")

    assert state.create_calls[0].secrets == [
        _FakeSecret(name="omnigent-llm"),
        _FakeSecret(name="extra-creds"),
    ]


def test_provision_constructor_secrets_override_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Explicit constructor names win over the env var — server config
    must not be silently widened by ambient environment.
    """
    state = _install_fake_modal(monkeypatch)
    monkeypatch.setenv(SANDBOX_SECRETS_ENV_VAR, "ambient-secret")
    ModalSandboxLauncher(secrets=["configured-secret"]).provision("my-host")

    assert state.create_calls[0].secrets == [_FakeSecret(name="configured-secret")]


def test_provision_without_secrets_passes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    No configured secrets → ``secrets=None`` reaches the SDK (its
    default), not an empty list with different semantics.
    """
    state = _install_fake_modal(monkeypatch)
    monkeypatch.delenv(SANDBOX_SECRETS_ENV_VAR, raising=False)
    ModalSandboxLauncher().provision("my-host")

    assert state.create_calls[0].secrets is None
