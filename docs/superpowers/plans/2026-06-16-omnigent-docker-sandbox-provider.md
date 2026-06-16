# Omnigent Docker Sandbox Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Sub-project A: a self-contained Docker Compose managed-sandbox path where the server provisions one sibling `omnigent-host` container per managed session.

**Architecture:** Reuse Omnigent's existing managed-host launch flow and add a managed-only `DockerSandboxLauncher` behind the existing `SandboxLauncher` seam. The server remains the control plane; spawned Docker containers are execution hosts on a segmented sandbox network. Compose supplies Docker socket access, sandbox config, single-user auth, and stable network names.

**Tech Stack:** Python 3.12, FastAPI lifespan tasks, SQLAlchemy-backed `HostStore`, Docker Python SDK `docker>=7,<8`, Docker Compose, pytest.

**Spec:** `docs/superpowers/specs/2026-06-15-omnigent-docker-sandbox-provider-design.md`

---

## Merge notes (this is the combined plan; supersedes `2026-06-15-docker-sandbox-provider.md`)

This plan merges the Codex draft with the earlier plan. Corrections verified against the codebase and applied while implementing:

1. **`install_fake_docker_launcher` lives in `tests/server/helpers.py`** (next to `FakeSandboxLauncher` and the `install_fake_{modal,daytona,islo}_launcher` shims), imported into `tests/server/test_managed_hosts.py`. Use the existing `monkeypatch.setattr(docker_mod, "DockerSandboxLauncher", _ctor)` pattern (as `install_fake_daytona_launcher` does), not a `sys.modules` fake module.
2. **Opt-in tests live under `tests/onboarding/sandboxes/`, NOT `tests/integration/`** (this supersedes the `tests/integration/docker_sandbox/` paths in the Task 1/8/10 bodies below). `tests/integration/` is reserved for real-LLM tests: its `conftest.py` force-gates the whole directory on both `--integration` and `--harness`, which the Docker tests must not require. The Docker spike/integration tests instead live in the normal suite and **auto-skip** without a Docker daemon (and, for the spike, without the host image), selected with `-m docker_sandbox`. Final filenames: `tests/onboarding/sandboxes/test_docker_bwrap_spike.py` and `tests/onboarding/sandboxes/test_docker_provider_integration.py`; the daemon/image guards live in `tests/onboarding/sandboxes/_docker_it.py`. No `addopts` override is needed. Run: `pytest -m docker_sandbox tests/onboarding/sandboxes`.
3. **Verified APIs:** `resolve_sandbox(spec, cwd)` / `create_exec_launcher(target_path, sandbox)` (`omnigent/inner/sandbox.py`); `OSEnvSpec(type="caller_process", cwd=..., sandbox=...)` + `OSEnvSandboxSpec(type=..., allow_network=...)` (`omnigent/inner/datamodel.py`); `HostStore.set_offline(host_id)`; generic parsers `_parse_provider_{section,image,env,string,positive_int}` exist (`_parse_provider_mapping` is new, added in Task 4); `now_epoch` is already imported in `tests/stores/test_host_store.py`; the `host_store` / `db_uri` fixtures exist (`tests/stores/test_host_store.py`, `tests/conftest.py`).
4. **Environment:** a Docker daemon is available, so Tasks 1 and 8 run here. The local venv has docker-py 5.0.3 (works for our API usage); the package pin stays `docker>=7,<8` for the server image.
5. `pytest.ini_options` markers are non-strict (no `--strict-markers`), so registering `docker_sandbox` is good practice but not load-bearing.

---

## Scope And File Map

- Create `omnigent/onboarding/sandboxes/docker.py`: Docker SDK launcher implementing only `prepare`, `provision`, `run`, and `terminate`.
- Create `tests/onboarding/sandboxes/test_docker.py`: mocked Docker SDK tests for launcher behavior and error mapping.
- Modify `omnigent/server/managed_hosts.py`: add provider registration, config parsing, Docker launcher factory, and token TTL.
- Modify `tests/server/test_managed_hosts.py`: parser/factory tests for `sandbox.provider: docker`.
- Modify `omnigent/stores/host_store.py`: add `list_managed_sandbox_ids(provider: str) -> set[str]`.
- Modify `tests/stores/test_host_store.py`: store query tests across owners and providers.
- Create `omnigent/server/docker_sandbox_reaper.py`: Docker-specific orphan reaper with startup and periodic sweep helpers.
- Modify `omnigent/server/app.py`: start/stop the Docker reaper from lifespan only when `sandbox_config.provider == "docker"`.
- Modify `pyproject.toml`: add `docker` optional dependency and any needed test marker.
- Modify `deploy/docker/Dockerfile`: install Docker SDK into the server image only.
- Modify `deploy/docker/docker-compose.yaml`: add stable app/DB and sandbox networks without putting Postgres on the sandbox network.
- Create `deploy/docker/docker-compose.managed.yaml`: managed-Docker overlay with Docker socket mount, `DOCKER_GID`, `OMNIGENT_AUTH_ENABLED=0`, empty auth provider, `OMNIGENT_CONFIG`, and sandbox config mount.
- Modify `deploy/docker/.env.example`, `deploy/docker/README.md`: document `DOCKER_GID`, managed overlay usage, socket risk, auth caveat, and coarse egress.
- Add opt-in Docker integration/spike tests under `tests/onboarding/sandboxes/` (see merge note 2 — the `tests/integration/docker_sandbox/` paths in the Task bodies below are superseded): `test_docker_provider_integration.py`, `test_docker_bwrap_spike.py`, and the `_docker_it.py` daemon/image guards.

---

### Task 1: A2 Docker/bwrap Spike Harness

**Files:**
- Create: `tests/integration/docker_sandbox/test_bwrap_spike.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add an opt-in pytest marker**

In `pyproject.toml`, add a marker entry to `[tool.pytest.ini_options].markers`:

```toml
"docker_sandbox: opt-in tests requiring a local Docker daemon and Omnigent host image",
```

- [ ] **Step 2: Write the opt-in spike test file**

Create `tests/integration/docker_sandbox/test_bwrap_spike.py` with this structure:

```python
"""Docker sandbox provider spike tests.

These are opt-in because they require a Docker daemon and the host image.
They validate the execution boundary before the provider hardening is finalized.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

HOST_IMAGE = os.environ.get("OMNIGENT_DOCKER_HOST_IMAGE", "ghcr.io/omnigent-ai/omnigent-host:latest")


pytestmark = pytest.mark.docker_sandbox


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.returncode == 0


pytestmark = [
    pytest.mark.docker_sandbox,
    pytest.mark.skipif(not _docker_available(), reason="Docker daemon is not available"),
]


def _run_host_image(command: str) -> subprocess.CompletedProcess[str]:
    name = f"omnigent-bwrap-spike-{uuid.uuid4().hex[:10]}"
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            HOST_IMAGE,
            "bash",
            "-lc",
            command,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )


def test_host_image_can_run_os_env_none_smoke() -> None:
    result = _run_host_image(
        "python - <<'PY'\n"
        "from omnigent.inner.datamodel import OSEnvSpec, OSEnvSandboxSpec\n"
        "from omnigent.inner.sandbox import resolve_sandbox\n"
        "from pathlib import Path\n"
        "spec = OSEnvSpec(type='caller_process', cwd='/tmp', sandbox=OSEnvSandboxSpec(type='none'))\n"
        "policy = resolve_sandbox(spec, Path('/tmp'))\n"
        "print(policy.backend_type, policy.active)\n"
        "PY"
    )
    assert result.returncode == 0, result.stderr
    assert "none False" in result.stdout


def test_host_image_can_activate_linux_bwrap_full_path() -> None:
    result = _run_host_image(
        "tmp=$(mktemp -d) && "
        "python - <<'PY'\n"
        "from omnigent.inner.datamodel import OSEnvSpec, OSEnvSandboxSpec\n"
        "from omnigent.inner.sandbox import create_exec_launcher, resolve_sandbox\n"
        "from pathlib import Path\n"
        "import os, subprocess, sys\n"
        "cwd = Path('/tmp').resolve()\n"
        "spec = OSEnvSpec(type='caller_process', cwd=str(cwd), sandbox=OSEnvSandboxSpec(type='linux_bwrap', allow_network=True))\n"
        "policy = resolve_sandbox(spec, cwd)\n"
        "launcher = create_exec_launcher(sys.executable, policy)\n"
        "proc = subprocess.run([launcher, '-c', 'print(\"BWRAP_FULL_PATH_OK\")'], text=True, capture_output=True, check=False)\n"
        "print(proc.stdout, end='')\n"
        "print(proc.stderr, end='', file=sys.stderr)\n"
        "raise SystemExit(proc.returncode)\n"
        "PY"
    )
    assert result.returncode == 0, result.stderr
    assert "BWRAP_FULL_PATH_OK" in result.stdout
```

- [ ] **Step 3: Run the spike explicitly**

Run:

```bash
pytest tests/integration/docker_sandbox/test_bwrap_spike.py -m docker_sandbox -v
```

Expected:

- If both tests pass, keep platform defaults and treat bwrap as supported inside the stock unprivileged host container.
- If the `linux_bwrap` test fails, copy the failure text into `deploy/docker/README.md` during Task 8 and leave A5 hardening conservative.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/integration/docker_sandbox/test_bwrap_spike.py
git commit -m "test: add docker sandbox bwrap spike"
```

---

### Task 2: Docker Launcher Unit Tests

**Files:**
- Create: `tests/onboarding/sandboxes/test_docker.py`

- [ ] **Step 1: Write mocked Docker SDK fixtures**

Create `tests/onboarding/sandboxes/test_docker.py` with fake SDK modules injected via `sys.modules`, mirroring `tests/onboarding/sandboxes/test_cwsandbox.py`:

```python
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


class _DockerAPIError(Exception):
    pass


class _DockerNotFound(_DockerAPIError):
    pass


class _DockerException(Exception):
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
```

- [ ] **Step 2: Add tests for preflight, provision, run, and terminate**

Append tests with these names and assertions:

```python
def test_prepare_pings_daemon(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    DockerSandboxLauncher(network="omnigent-sbx").prepare()
    assert docker_sdk.ping_called is True


def test_provision_builds_expected_container_run_args(
    docker_sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    launcher = DockerSandboxLauncher(
        network="omnigent-sbx",
        env=["OPENAI_API_KEY"],
        resources={"mem_limit": "4g", "nano_cpus": 2_000_000_000, "pids_limit": 512},
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
    assert docker_sdk.run_kwargs["init"] is True
    assert docker_sdk.run_kwargs["labels"]["omnigent.managed"] == "1"
    assert docker_sdk.run_kwargs["labels"]["omnigent.provider"] == "docker"
    assert docker_sdk.run_kwargs["labels"]["omnigent.host_name"] == "managed-abcd"
    assert int(docker_sdk.run_kwargs["labels"]["omnigent.created_at"]) > 0


def test_run_decodes_demuxed_output_and_check(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    docker_sdk.exec_result = _ExecResult(exit_code=3, output=(b"out\\xff", b"err\\xff"))
    launcher = DockerSandboxLauncher(network="omnigent-sbx")
    unchecked = launcher.run("container-123", "false", check=False)
    assert unchecked.returncode == 3
    assert "out" in unchecked.stdout
    assert "err" in unchecked.stderr
    with pytest.raises(click.ClickException, match="exit 3"):
        launcher.run("container-123", "false")


def test_terminate_is_idempotent_on_missing_container(docker_sdk: _State) -> None:
    from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

    launcher = DockerSandboxLauncher(network="omnigent-sbx")
    docker_sdk.get_missing = True
    launcher.terminate("gone")
    assert docker_sdk.removed == []
```

- [ ] **Step 3: Run tests and verify they fail before implementation**

Run:

```bash
pytest tests/onboarding/sandboxes/test_docker.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'omnigent.onboarding.sandboxes.docker'`.

- [ ] **Step 4: Commit the failing tests**

```bash
git add tests/onboarding/sandboxes/test_docker.py
git commit -m "test: specify docker sandbox launcher"
```

---

### Task 3: DockerSandboxLauncher Implementation

**Files:**
- Create: `omnigent/onboarding/sandboxes/docker.py`
- Test: `tests/onboarding/sandboxes/test_docker.py`

- [ ] **Step 1: Implement launcher constants and constructor**

Create `omnigent/onboarding/sandboxes/docker.py`:

```python
"""Docker sandbox launcher for server-managed Omnigent hosts."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
)

if TYPE_CHECKING:
    from docker.models.containers import Container

HOST_IMAGE_ENV_VAR = "OMNIGENT_DOCKER_HOST_IMAGE"
SANDBOX_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_DOCKER_SANDBOX_ENV"
DEFAULT_NETWORK = "omnigent-sbx"
DOCKER_MANAGED_LABEL = "omnigent.managed"
DOCKER_PROVIDER_LABEL = "omnigent.provider"
DOCKER_HOST_NAME_LABEL = "omnigent.host_name"
DOCKER_CREATED_AT_LABEL = "omnigent.created_at"


def _ensure_sdk() -> None:
    try:
        import docker  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The Docker SDK is required for the 'docker' sandbox provider. "
            "Install it with `pip install 'omnigent[docker]'`, or use the "
            "managed Docker Compose image."
        ) from exc


def docker_client():
    """Return a Docker SDK client, converting setup failures to ClickException."""
    _ensure_sdk()
    import docker

    try:
        return docker.from_env()
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"Docker client setup failed: {exc}") from exc


class DockerSandboxLauncher(SandboxLauncher):
    provider: ClassVar[str] = "docker"
    supports_cli_bootstrap: ClassVar[bool] = False
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        network: str | None = None,
        resources: Mapping[str, object] | None = None,
        security: Mapping[str, object] | None = None,
    ) -> None:
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._network = network or DEFAULT_NETWORK
        self._resources = dict(resources or {})
        self._security = dict(security or {})
```

- [ ] **Step 2: Implement SDK access and env resolution**

Add helper methods:

```python
    def _client(self):
        return docker_client()

    def _resolve_sandbox_env(self) -> dict[str, str]:
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set in the "
                    "server's environment — set it or remove it from sandbox.docker.env "
                    f"/ {SANDBOX_ENV_PASSTHROUGH_ENV_VAR}."
                )
            resolved[name] = value
        return resolved
```

- [ ] **Step 3: Implement managed methods**

Add:

```python
    def prepare(self) -> None:
        client = self._client()
        try:
            client.ping()
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(
                "Docker daemon is not reachable from the Omnigent server. "
                "For Compose, mount /var/run/docker.sock and set DOCKER_GID "
                "so the non-root server user can access it."
            ) from exc

    def provision(self, name: str) -> str:
        image = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        labels = {
            DOCKER_MANAGED_LABEL: "1",
            DOCKER_PROVIDER_LABEL: "docker",
            DOCKER_HOST_NAME_LABEL: name,
            DOCKER_CREATED_AT_LABEL: str(int(time.time())),
        }
        kwargs = {
            "detach": True,
            "network": self._network,
            "environment": self._resolve_sandbox_env() or None,
            "labels": labels,
            "init": True,
            **self._resources,
            **self._security,
        }
        try:
            container = self._client().containers.run(image, ["sleep", "infinity"], **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(f"Docker container creation failed: {exc}") from exc
        container_id = getattr(container, "id", None)
        if not isinstance(container_id, str) or not container_id:
            raise click.ClickException("Docker container creation returned no container id")
        return container_id

    def _get_container(self, container_id: str) -> Container:
        try:
            return self._client().containers.get(container_id)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(f"Docker container '{container_id}' not found: {exc}") from exc

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        container = self._get_container(sandbox_id)
        try:
            result = container.exec_run(["bash", "-lc", command], demux=True)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(
                f"Remote command failed to execute in Docker container '{sandbox_id}': {exc}"
            ) from exc
        stdout_b, stderr_b = result.output if isinstance(result.output, tuple) else (result.output, b"")
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        if check and result.exit_code != 0:
            raise click.ClickException(
                f"Remote command failed in Docker container '{sandbox_id}' "
                f"(exit {result.exit_code}): {command}"
            )
        return RemoteCommandResult(returncode=result.exit_code, stdout=stdout, stderr=stderr)

    def terminate(self, sandbox_id: str) -> None:
        _ensure_sdk()
        import docker

        try:
            self._client().containers.get(sandbox_id).remove(force=True)
        except docker.errors.NotFound:
            return
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(
                f"Failed to remove Docker container '{sandbox_id}': {exc}"
            ) from exc
```

- [ ] **Step 4: Run launcher tests**

Run:

```bash
pytest tests/onboarding/sandboxes/test_docker.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/docker.py tests/onboarding/sandboxes/test_docker.py
git commit -m "feat: add docker sandbox launcher"
```

---

### Task 4: Managed Config Parsing

**Files:**
- Modify: `omnigent/server/managed_hosts.py`
- Modify: `tests/server/test_managed_hosts.py`

- [ ] **Step 1: Add parser tests**

In `tests/server/test_managed_hosts.py`, add a fake installer similar to the existing provider installers:

```python
def install_fake_docker_launcher(monkeypatch: pytest.MonkeyPatch, fake: FakeSandboxLauncher) -> None:
    mod = types.ModuleType("omnigent.onboarding.sandboxes.docker")

    class FakeDockerSandboxLauncher:
        def __new__(cls, **kwargs):
            fake.image = kwargs.get("image")
            fake.env = kwargs.get("env")
            fake.network = kwargs.get("network")
            fake.resources = kwargs.get("resources")
            fake.security = kwargs.get("security")
            return fake

    mod.DockerSandboxLauncher = FakeDockerSandboxLauncher  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omnigent.onboarding.sandboxes.docker", mod)
```

Add tests:

```python
def test_parse_valid_docker_config_builds_parameterized_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = parse_sandbox_config(
        {
            "provider": "docker",
            "server_url": "http://omnigent:8000/",
            "docker": {
                "image": "ghcr.io/acme/omnigent-host:sha",
                "network": "omnigent-sbx",
                "env": ["OPENAI_API_KEY", "GIT_TOKEN"],
                "resources": {"mem_limit": "4g", "nano_cpus": 2000000000, "pids_limit": 512},
                "security": {"security_opt": ["no-new-privileges:true"]},
            },
        }
    )
    assert cfg is not None
    assert cfg.server_url == "http://omnigent:8000"
    assert cfg.managed_launch_supported is True
    assert cfg.provider == "docker"
    fake = FakeSandboxLauncher()
    install_fake_docker_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image == "ghcr.io/acme/omnigent-host:sha"
    assert fake.env == ["OPENAI_API_KEY", "GIT_TOKEN"]
    assert fake.network == "omnigent-sbx"
    assert fake.resources == {"mem_limit": "4g", "nano_cpus": 2000000000, "pids_limit": 512}
    assert fake.security == {"security_opt": ["no-new-privileges:true"]}


def test_parse_docker_without_section_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = parse_sandbox_config({"provider": "docker", "server_url": "http://omnigent:8000"})
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_docker_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image is None
    assert fake.env is None
    assert fake.network is None
```

Extend the invalid-config parametrization with:

```python
({"provider": "docker", "server_url": "https://s", "docker": "x"}, "sandbox.docker"),
({"provider": "docker", "server_url": "https://s", "docker": {"image": "  "}}, "sandbox.docker.image"),
({"provider": "docker", "server_url": "https://s", "docker": {"network": "  "}}, "sandbox.docker.network"),
({"provider": "docker", "server_url": "https://s", "docker": {"env": "OPENAI"}}, "sandbox.docker.env"),
```

- [ ] **Step 2: Run parser tests and verify failure**

Run:

```bash
pytest tests/server/test_managed_hosts.py -k "docker or invalid_config" -q
```

Expected: FAIL because `docker` is not yet supported.

- [ ] **Step 3: Implement config parsing**

In `omnigent/server/managed_hosts.py`:

- Add `"docker"` to `SUPPORTED_SANDBOX_PROVIDERS` and `PROVIDERS_WITH_MANAGED_LAUNCH`.
- Add `DOCKER_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600`.
- Add parse branch:

```python
    elif provider == "docker":
        launcher_factory = _docker_launcher_factory(
            image=_parse_provider_image(raw, "docker"),
            env=_parse_provider_env(raw, "docker"),
            network=_parse_provider_string(raw, "docker", "network"),
            resources=_parse_provider_mapping(raw, "docker", "resources"),
            security=_parse_provider_mapping(raw, "docker", "security"),
        )
        token_ttl_s = DOCKER_MANAGED_TOKEN_TTL_S
```

Add factory and mapping parser:

```python
def _docker_launcher_factory(
    *,
    image: str | None,
    env: list[str] | None,
    network: str | None,
    resources: dict[str, object] | None,
    security: dict[str, object] | None,
) -> Callable[[], SandboxLauncher]:
    def _build() -> SandboxLauncher:
        from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

        return DockerSandboxLauncher(
            image=image,
            env=env,
            network=network,
            resources=resources,
            security=security,
        )

    return _build


def _parse_provider_mapping(
    raw: dict[str, object],
    provider: str,
    key: str,
) -> dict[str, object] | None:
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"server config 'sandbox.{provider}.{key}' must be a mapping")
    return dict(value)
```

- [ ] **Step 4: Run parser tests**

Run:

```bash
pytest tests/server/test_managed_hosts.py -k "docker or invalid_config" -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
git commit -m "feat: wire docker managed sandbox config"
```

---

### Task 5: HostStore API For Reaper

**Files:**
- Modify: `omnigent/stores/host_store.py`
- Modify: `tests/stores/test_host_store.py`

- [ ] **Step 1: Write store tests**

Add tests near existing managed-host tests:

```python
def test_list_managed_sandbox_ids_filters_by_provider_across_owners(
    host_store: HostStore,
) -> None:
    host_store.register_managed_host(
        host_id="host_docker_1",
        name="managed-docker-1",
        owner="alice@example.com",
        token="token-1",
        provider="docker",
        sandbox_id="container-1",
        token_expires_at=now_epoch() + 3600,
    )
    host_store.register_managed_host(
        host_id="host_docker_2",
        name="managed-docker-2",
        owner="bob@example.com",
        token="token-2",
        provider="docker",
        sandbox_id="container-2",
        token_expires_at=now_epoch() + 3600,
    )
    host_store.register_managed_host(
        host_id="host_modal",
        name="managed-modal",
        owner="alice@example.com",
        token="token-3",
        provider="modal",
        sandbox_id="modal-1",
        token_expires_at=now_epoch() + 3600,
    )

    assert host_store.list_managed_sandbox_ids("docker") == {"container-1", "container-2"}


def test_list_managed_sandbox_ids_preserves_offline_rows(host_store: HostStore) -> None:
    host_store.register_managed_host(
        host_id="host_offline",
        name="managed-offline",
        owner="alice@example.com",
        token="token-1",
        provider="docker",
        sandbox_id="container-offline",
        token_expires_at=now_epoch() + 3600,
    )
    host_store.set_offline("host_offline")

    assert host_store.list_managed_sandbox_ids("docker") == {"container-offline"}
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/stores/test_host_store.py -k list_managed_sandbox_ids -q
```

Expected: FAIL with missing method.

- [ ] **Step 3: Implement store method**

Add to `HostStore`:

```python
    def list_managed_sandbox_ids(self, provider: str) -> set[str]:
        """
        Return sandbox/container ids recorded for managed hosts of *provider*.

        Includes online and offline rows. The Docker orphan reaper uses row
        presence, not liveness, to decide whether a container is backed by a
        current managed-host identity.
        """
        with self._session() as session:
            rows = session.execute(
                select(SqlHost.sandbox_id).where(
                    SqlHost.sandbox_provider == provider,
                    SqlHost.sandbox_id.is_not(None),
                )
            ).all()
        return {row.sandbox_id for row in rows if row.sandbox_id}
```

- [ ] **Step 4: Run store tests**

Run:

```bash
pytest tests/stores/test_host_store.py -k list_managed_sandbox_ids -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/stores/host_store.py tests/stores/test_host_store.py
git commit -m "feat: expose managed sandbox ids from host store"
```

---

### Task 6: Docker Orphan Reaper

**Files:**
- Create: `omnigent/server/docker_sandbox_reaper.py`
- Create: `tests/server/test_docker_sandbox_reaper.py`
- Modify: `omnigent/server/app.py`

- [ ] **Step 1: Write reaper unit tests**

Create `tests/server/test_docker_sandbox_reaper.py`:

```python
"""Tests for Docker managed-sandbox orphan reaping."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from omnigent.server.docker_sandbox_reaper import reap_docker_sandboxes_once


@dataclass
class _Container:
    id: str
    labels: dict[str, str]
    removed: bool = False

    def remove(self, force: bool = False) -> None:
        assert force is True
        self.removed = True


@dataclass
class _Containers:
    containers: list[_Container]

    def list(self, filters):
        assert filters["label"] == ["omnigent.managed=1", "omnigent.provider=docker"]
        return self.containers


@dataclass
class _Client:
    containers: _Containers


@dataclass
class _HostStore:
    ids: set[str] = field(default_factory=set)

    def list_managed_sandbox_ids(self, provider: str) -> set[str]:
        assert provider == "docker"
        return set(self.ids)


def test_reaper_removes_only_unbacked_containers_past_grace() -> None:
    old = str(int(time.time()) - 3600)
    backed = _Container("backed", {"omnigent.created_at": old})
    orphan = _Container("orphan", {"omnigent.created_at": old})
    recent = _Container("recent", {"omnigent.created_at": str(int(time.time()))})
    client = _Client(_Containers([backed, orphan, recent]))
    store = _HostStore({"backed"})

    removed = reap_docker_sandboxes_once(client=client, host_store=store, grace_s=60)

    assert removed == ["orphan"]
    assert backed.removed is False
    assert orphan.removed is True
    assert recent.removed is False
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/server/test_docker_sandbox_reaper.py -q
```

Expected: FAIL with missing module.

- [ ] **Step 3: Implement reaper module**

Create `omnigent/server/docker_sandbox_reaper.py`:

```python
"""Docker managed-sandbox orphan reaper."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Any

from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)
DEFAULT_REAPER_INTERVAL_S = 300
DEFAULT_REAPER_GRACE_S = 180


def _container_created_at(container: Any) -> int:
    labels = getattr(container, "labels", {}) or {}
    raw = labels.get("omnigent.created_at")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def reap_docker_sandboxes_once(*, client: Any, host_store: HostStore, grace_s: int) -> list[str]:
    backed = host_store.list_managed_sandbox_ids("docker")
    now = int(time.time())
    removed: list[str] = []
    containers = client.containers.list(
        filters={"label": ["omnigent.managed=1", "omnigent.provider=docker"]}
    )
    for container in containers:
        container_id = getattr(container, "id", "")
        if not container_id or container_id in backed:
            continue
        if now - _container_created_at(container) < grace_s:
            continue
        try:
            container.remove(force=True)
            removed.append(container_id)
        except Exception:  # noqa: BLE001
            _logger.warning("Failed to reap Docker sandbox container %s", container_id, exc_info=True)
    return removed


async def run_docker_sandbox_reaper(
    *,
    client: Any,
    host_store: HostStore,
    interval_s: int = DEFAULT_REAPER_INTERVAL_S,
    grace_s: int = DEFAULT_REAPER_GRACE_S,
) -> None:
    while True:
        await asyncio.to_thread(reap_docker_sandboxes_once, client=client, host_store=host_store, grace_s=grace_s)
        await asyncio.sleep(interval_s)


async def cancel_reaper_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
```

- [ ] **Step 4: Wire reaper into FastAPI lifespan**

In `omnigent/server/app.py`, inside `_lifespan`, after metrics task creation:

```python
        docker_reaper_task = None
        if sandbox_config is not None and sandbox_config.provider == "docker" and host_store is not None:
            from omnigent.onboarding.sandboxes.docker import docker_client
            from omnigent.server.docker_sandbox_reaper import (
                reap_docker_sandboxes_once,
                run_docker_sandbox_reaper,
            )

            client = docker_client()
            await asyncio.to_thread(
                reap_docker_sandboxes_once,
                client=client,
                host_store=host_store,
                grace_s=180,
            )
            docker_reaper_task = asyncio.create_task(
                run_docker_sandbox_reaper(client=client, host_store=host_store)
            )
```

In shutdown, before closing router resources:

```python
            from omnigent.server.docker_sandbox_reaper import cancel_reaper_task

            await cancel_reaper_task(docker_reaper_task)
```

- [ ] **Step 5: Run reaper tests and a focused app import check**

Run:

```bash
pytest tests/server/test_docker_sandbox_reaper.py -q
python -m compileall omnigent/server/docker_sandbox_reaper.py omnigent/server/app.py
```

Expected: PASS and compileall succeeds.

- [ ] **Step 6: Commit**

```bash
git add omnigent/server/docker_sandbox_reaper.py tests/server/test_docker_sandbox_reaper.py omnigent/server/app.py
git commit -m "feat: reap orphaned docker sandboxes"
```

---

### Task 7: Packaging And Compose Wiring

**Files:**
- Modify: `pyproject.toml`
- Modify: `deploy/docker/Dockerfile`
- Modify: `deploy/docker/docker-compose.yaml`
- Create: `deploy/docker/docker-compose.managed.yaml`
- Modify: `deploy/docker/.env.example`

- [ ] **Step 1: Add Docker SDK extra and server-image install**

In `pyproject.toml`:

```toml
docker = ["docker>=7,<8"]
```

In `deploy/docker/Dockerfile`, add after the existing psycopg install in the `server-builder` stage:

```dockerfile
# Docker managed-sandbox provider: server launches sibling host containers
# through /var/run/docker.sock. Kept out of the shared builder so the host
# image stays lean.
RUN uv pip install --no-cache-dir --index-url ${PYPI_INDEX_URL} 'docker>=7,<8'
```

- [ ] **Step 2: Segment Compose networks**

Modify `deploy/docker/docker-compose.yaml` so:

```yaml
services:
  postgres:
    networks:
      - omnigent-app

  omnigent:
    networks:
      - omnigent-app
      - omnigent-sbx

networks:
  omnigent-app:
    name: omnigent-app
  omnigent-sbx:
    name: omnigent-sbx
```

Do not attach `postgres` to `omnigent-sbx`.

- [ ] **Step 3: Add managed overlay**

Create `deploy/docker/docker-compose.managed.yaml`:

```yaml
services:
  omnigent:
    group_add:
      - "${DOCKER_GID:?set DOCKER_GID to your host docker group id}"
    environment:
      OMNIGENT_AUTH_ENABLED: "0"
      OMNIGENT_AUTH_PROVIDER: ""
      OMNIGENT_CONFIG: /etc/omnigent/config.yaml
      OMNIGENT_DOCKER_HOST_IMAGE: "${OMNIGENT_DOCKER_HOST_IMAGE:-ghcr.io/omnigent-ai/omnigent-host:latest}"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./config.managed.yaml:/etc/omnigent/config.yaml:ro
```

Create `deploy/docker/config.managed.example.yaml` as the checked-in template. Operators copy it to `deploy/docker/config.managed.yaml` before running the managed overlay:

```yaml
sandbox:
  provider: docker
  server_url: http://omnigent:8000
  docker:
    image: ghcr.io/omnigent-ai/omnigent-host:latest
    network: omnigent-sbx
    env:
      - ANTHROPIC_API_KEY
      - OPENAI_API_KEY
      - GIT_TOKEN
    resources:
      mem_limit: 4g
      nano_cpus: 2000000000
      pids_limit: 512
```

- [ ] **Step 4: Document `.env.example` knobs**

Add:

```dotenv
# Managed Docker sandboxes. Required only when using docker-compose.managed.yaml.
# Get this with: getent group docker | cut -d: -f3
DOCKER_GID=
OMNIGENT_DOCKER_HOST_IMAGE=ghcr.io/omnigent-ai/omnigent-host:latest
```

- [ ] **Step 5: Validate Compose rendering**

Run:

```bash
POSTGRES_PASSWORD=dummy DOCKER_GID=999 docker compose \
  -f deploy/docker/docker-compose.yaml \
  -f deploy/docker/docker-compose.managed.yaml \
  config --quiet
```

Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml deploy/docker/Dockerfile deploy/docker/docker-compose.yaml deploy/docker/docker-compose.managed.yaml deploy/docker/.env.example deploy/docker/config.managed.example.yaml
git commit -m "feat: package managed docker compose stack"
```

---

### Task 8: Docker Provider Integration Test

**Files:**
- Create: `tests/integration/docker_sandbox/test_docker_provider.py`

- [ ] **Step 1: Write real Docker provision/run/terminate test**

Create `tests/integration/docker_sandbox/test_docker_provider.py`:

```python
"""Opt-in integration tests for the Docker sandbox provider."""

from __future__ import annotations

import os

import pytest

from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

pytestmark = pytest.mark.docker_sandbox


def test_real_docker_provision_run_terminate() -> None:
    launcher = DockerSandboxLauncher(
        image=os.environ.get("OMNIGENT_DOCKER_HOST_IMAGE"),
        network=os.environ.get("OMNIGENT_DOCKER_TEST_NETWORK", "bridge"),
        resources={"mem_limit": "512m", "nano_cpus": 500000000, "pids_limit": 128},
    )
    launcher.prepare()
    sandbox_id = launcher.provision("managed-integration")
    try:
        result = launcher.run(sandbox_id, "printf DOCKER_PROVIDER_OK")
        assert result.returncode == 0
        assert result.stdout == "DOCKER_PROVIDER_OK"
    finally:
        launcher.terminate(sandbox_id)
```

- [ ] **Step 2: Run integration test explicitly**

Run:

```bash
pytest tests/integration/docker_sandbox/test_docker_provider.py -m docker_sandbox -v
```

Expected: PASS when Docker daemon and image are available. If the host image pull fails due to network restrictions, rerun after `docker pull ghcr.io/omnigent-ai/omnigent-host:latest`.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/docker_sandbox/test_docker_provider.py
git commit -m "test: add docker sandbox provider integration"
```

---

### Task 9: Documentation And Operator Workflow

**Files:**
- Modify: `deploy/docker/README.md`
- Modify: `docs/superpowers/specs/2026-06-15-omnigent-docker-sandbox-provider-design.md` if the A2 spike changes the final hardening decision.

- [ ] **Step 1: Document managed Docker quickstart**

Add a section to `deploy/docker/README.md`:

```markdown
## Managed Docker sandboxes

The managed Docker overlay lets the server launch one sibling
`omnigent-host` container per managed session.

```bash
cd deploy/docker
cp .env.example .env
printf "DOCKER_GID=%s\n" "$(getent group docker | cut -d: -f3)" >> .env
cp config.managed.example.yaml config.managed.yaml
docker compose -f docker-compose.yaml -f docker-compose.managed.yaml up -d
docker compose -f docker-compose.yaml -f docker-compose.managed.yaml logs -f omnigent
```

The overlay sets `OMNIGENT_AUTH_ENABLED=0` and leaves
`OMNIGENT_AUTH_PROVIDER` empty. This is a single-user trusted deployment.
For multi-user use, put Omnigent behind a header/OIDC proxy; do not use
plain accounts mode for managed sandboxes until runner-tunnel identity is
threaded through the launch token.
```

- [ ] **Step 2: Document socket and network posture**

Add:

```markdown
### Security posture

Mounting `/var/run/docker.sock` gives the Omnigent server host-root-equivalent
control over Docker. The MVP accepts this for internal deployments. Use a
Docker socket proxy as the hardening follow-up.

Sandbox containers join `omnigent-sbx` only. They can reach
`http://omnigent:8000` and outbound internet through Docker NAT, but they are
not attached to the app/DB network and cannot reach Postgres by service name.
Egress is coarse in this MVP; fine-grained egress requires a future internal
network plus egress router/proxy.
```

- [ ] **Step 3: Record A2 result**

If Task 1 proved bwrap works unprivileged, add:

```markdown
### Inner sandbox result

The stock host image can activate Omnigent's Linux bwrap sandbox inside the
managed Docker container without extra privileges. The provider therefore keeps
platform defaults; `os_env.sandbox.type: none` remains an explicit agent-spec
choice, not a provider default.
```

If Task 1 failed, add the exact failure and the supported posture:

```markdown
### Inner sandbox result

The stock host image could not activate the full bwrap path inside the default
managed Docker container. Non-native agents must use explicit
`os_env.sandbox.type: none`; native harnesses require the documented Docker
security options from the spike result.
```

- [ ] **Step 4: Commit**

```bash
git add deploy/docker/README.md docs/superpowers/specs/2026-06-15-omnigent-docker-sandbox-provider-design.md
git commit -m "docs: document managed docker sandboxes"
```

---

### Task 10: Full Verification Pass

**Files:**
- No new files unless fixes are needed.

- [ ] **Step 1: Run focused unit suites**

Run:

```bash
pytest tests/onboarding/sandboxes/test_docker.py tests/server/test_docker_sandbox_reaper.py -q
pytest tests/server/test_managed_hosts.py -k "docker or invalid_config" -q
pytest tests/stores/test_host_store.py -k list_managed_sandbox_ids -q
```

Expected: all PASS.

- [ ] **Step 2: Validate Compose config**

Run:

```bash
POSTGRES_PASSWORD=dummy DOCKER_GID=999 docker compose \
  -f deploy/docker/docker-compose.yaml \
  -f deploy/docker/docker-compose.managed.yaml \
  config --quiet
```

Expected: exits 0.

- [ ] **Step 3: Run opt-in Docker checks when daemon is available**

Run:

```bash
pytest tests/integration/docker_sandbox/ -m docker_sandbox -v
```

Expected: all PASS on a machine with Docker daemon and host image access. If Docker is not available, record that these were skipped and include the local reason.

- [ ] **Step 4: Run static sanity checks**

Run:

```bash
python -m compileall omnigent/onboarding/sandboxes/docker.py omnigent/server/docker_sandbox_reaper.py omnigent/server/managed_hosts.py omnigent/stores/host_store.py
git diff --check
```

Expected: compileall succeeds and `git diff --check` prints no whitespace errors.

- [ ] **Step 5: Final commit if verification fixes were needed**

If any fixes were made during verification:

```bash
git add <fixed-files>
git commit -m "fix: stabilize docker sandbox provider verification"
```

---

## Self-Review

- Spec coverage: A1 is covered by Tasks 2-3; A2 by Task 1 and Task 9; A3 by Task 4; A4/A5 by Task 7; A6 by Tasks 5-6; A7 by Task 8; docs and verification by Tasks 9-10.
- Placeholder scan: no forbidden placeholder tokens or shortcut task references remain.
- Type consistency: provider name is consistently `docker`; network is consistently `omnigent-sbx`; reaper store API is consistently `list_managed_sandbox_ids(provider: str) -> set[str]`; session creation is consistently `host_type: managed` with provider selected by server config.
