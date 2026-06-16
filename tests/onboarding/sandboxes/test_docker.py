"""Tests for :mod:`omnigent.onboarding.sandboxes.docker`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import click
import pytest

from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE


@dataclass
class _ExecResult:
    exit_code: int = 0
    output: tuple[bytes | None, bytes | None] = (b"", b"")


@dataclass
class _State:
    ping_called: bool = False
    run_kwargs: dict = field(default_factory=dict)
    run_image: str | None = None
    run_command: list[str] | None = None
    removed: list[str] = field(default_factory=list)
    exec_result: _ExecResult = field(default_factory=_ExecResult)
    get_missing: bool = False
    run_raises: Exception | None = None


class _DockerException(Exception):
    pass


class _DockerAPIError(_DockerException):
    pass


class _DockerNotFound(_DockerAPIError):
    pass


class _FakeContainer:
    def __init__(self, state: _State, container_id: str = "container-123") -> None:
        self.id = container_id
        self._state = state

    def exec_run(self, command, demux: bool = False):
        assert demux is True
        assert command[:2] == ["bash", "-lc"]
        return self._state.exec_result

    def remove(self, force: bool = False) -> None:
        assert force is True
        self._state.removed.append(self.id)


class _FakeContainers:
    def __init__(self, state: _State) -> None:
        self._state = state

    def run(self, image, command, **kwargs):
        if self._state.run_raises is not None:
            raise self._state.run_raises
        self._state.run_image = image
        self._state.run_command = command
        self._state.run_kwargs = kwargs
        return _FakeContainer(self._state)

    def get(self, container_id: str):
        if self._state.get_missing:
            raise _DockerNotFound(container_id)
        return _FakeContainer(self._state, container_id)


class _FakeClient:
    def __init__(self, state: _State) -> None:
        self._state = state
        self.containers = _FakeContainers(state)

    def ping(self) -> bool:
        self._state.ping_called = True
        return True


@pytest.fixture()
def docker_sdk(monkeypatch: pytest.MonkeyPatch) -> _State:
    state = _State()
    docker_mod = types.ModuleType("docker")
    errors_mod = types.ModuleType("docker.errors")

    docker_mod.from_env = lambda: _FakeClient(state)  # type: ignore[attr-defined]
    docker_mod.errors = errors_mod  # type: ignore[attr-defined]
    errors_mod.APIError = _DockerAPIError  # type: ignore[attr-defined]
    errors_mod.NotFound = _DockerNotFound  # type: ignore[attr-defined]
    errors_mod.DockerException = _DockerException  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "docker", docker_mod)
    monkeypatch.setitem(sys.modules, "docker.errors", errors_mod)
    monkeypatch.delenv("OMNIGENT_DOCKER_HOST_IMAGE", raising=False)
    monkeypatch.delenv("OMNIGENT_DOCKER_SANDBOX_ENV", raising=False)
    return state


def test_prepare_pings_daemon(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    DockerSandboxLauncher(network="omnigent-sbx").prepare()
    assert docker_sdk.ping_called is True


def test_prepare_raises_when_daemon_unreachable(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    def _boom() -> bool:
        raise _DockerAPIError("no daemon")

    launcher = DockerSandboxLauncher(network="omnigent-sbx")
    # Replace ping with a failing one via the fake client factory.
    import docker

    docker.from_env = lambda: type("C", (), {"ping": staticmethod(_boom)})()  # type: ignore[attr-defined]
    with pytest.raises(click.ClickException, match="Docker daemon"):
        launcher.prepare()


def test_provision_builds_expected_container_run_args(
    docker_sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    launcher = DockerSandboxLauncher(
        network="omnigent-sbx",
        env=["OPENAI_API_KEY"],
        resources={"mem_limit": "4g", "nano_cpus": 2_000_000_000, "pids_limit": 512},
        security={"security_opt": ["no-new-privileges:true"]},
    )
    assert launcher.provision("managed-abcd") == "container-123"
    assert docker_sdk.run_image == DEFAULT_HOST_IMAGE
    assert docker_sdk.run_command == ["sleep", "infinity"]
    assert docker_sdk.run_kwargs["detach"] is True
    assert docker_sdk.run_kwargs["network"] == "omnigent-sbx"
    assert docker_sdk.run_kwargs["environment"] == {"OPENAI_API_KEY": "sk-test"}
    assert docker_sdk.run_kwargs["mem_limit"] == "4g"
    assert docker_sdk.run_kwargs["nano_cpus"] == 2_000_000_000
    assert docker_sdk.run_kwargs["pids_limit"] == 512
    assert docker_sdk.run_kwargs["security_opt"] == ["no-new-privileges:true"]
    assert docker_sdk.run_kwargs["init"] is True
    labels = docker_sdk.run_kwargs["labels"]
    assert labels["omnigent.managed"] == "1"
    assert labels["omnigent.provider"] == "docker"
    assert labels["omnigent.host_name"] == "managed-abcd"
    assert int(labels["omnigent.created_at"]) > 0


def test_provision_defaults_image_and_resolves_env_fallback(
    docker_sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    launcher = DockerSandboxLauncher()
    launcher.provision("managed-x")
    assert docker_sdk.run_image == DEFAULT_HOST_IMAGE
    # No env names configured → no environment passed.
    assert docker_sdk.run_kwargs["environment"] is None


def test_provision_missing_env_var_raises(
    docker_sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    monkeypatch.delenv("GIT_TOKEN", raising=False)
    launcher = DockerSandboxLauncher(env=["GIT_TOKEN"])
    with pytest.raises(click.ClickException, match="GIT_TOKEN"):
        launcher.provision("managed-x")


def test_provision_wraps_run_error(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    docker_sdk.run_raises = _DockerAPIError("boom")
    with pytest.raises(click.ClickException, match="creation failed"):
        DockerSandboxLauncher().provision("managed-x")


def test_run_decodes_demuxed_output_and_check(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    docker_sdk.exec_result = _ExecResult(exit_code=3, output=(b"out\xff", b"err\xff"))
    launcher = DockerSandboxLauncher(network="omnigent-sbx")
    unchecked = launcher.run("container-123", "false", check=False)
    assert unchecked.returncode == 3
    assert unchecked.stdout.startswith("out")
    assert unchecked.stderr.startswith("err")
    with pytest.raises(click.ClickException, match="exit 3"):
        launcher.run("container-123", "false")


def test_run_raises_when_container_missing(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    docker_sdk.get_missing = True
    with pytest.raises(click.ClickException, match="not found"):
        DockerSandboxLauncher().run("gone", "echo hi")


def test_terminate_removes_container(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    DockerSandboxLauncher().terminate("container-123")
    assert docker_sdk.removed == ["container-123"]


def test_terminate_is_idempotent_on_missing_container(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    docker_sdk.get_missing = True
    DockerSandboxLauncher(network="omnigent-sbx").terminate("gone")
    assert docker_sdk.removed == []
