"""Tests for the managed Sprites sandbox launcher."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any

import click
import pytest

from omnigent.claude_native_bridge import CLAUDE_BYPASS_WARNING_ACCEPT_ENV_VAR
from omnigent.onboarding.sandboxes import sprites as sprites_sandbox
from omnigent.onboarding.sandboxes.sprites import (
    API_URL_ENV_VAR,
    TOKEN_ENV_VAR,
    SpritesSandboxLauncher,
    _managed_sprite_name,
    render_bootstrap_command,
)
from omnigent.runner.activity_lease import ACTIVITY_LEASE_PROVIDER_ENV_VAR


class FakeSpriteError(Exception):
    """Fake SDK provider error."""


class FakeNotFoundError(FakeSpriteError):
    """Fake SDK missing-resource error."""


@dataclass
class FakeResult:
    """Minimal subprocess-like SDK result."""

    returncode: int = 0
    stdout: bytes = b""
    stderr: bytes = b""


class FakeSprite:
    """Recording Sprite handle."""

    def __init__(self, name: str, *, status: str = "running") -> None:
        self.name = name
        self.status = status
        self.run_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.service_calls: list[dict[str, object]] = []
        self.services: list[object] = []
        self.deleted_services: list[str] = []
        self.destroyed = False
        self.fail_commands_containing: str | None = None
        self.service_events: list[object] = []

    def run(self, *args: str, **kwargs: object) -> FakeResult:
        """Record and answer shell commands used by the launcher."""
        self.run_calls.append((args, kwargs))
        command = args[-1]
        if self.fail_commands_containing and self.fail_commands_containing in command:
            return FakeResult(returncode=12, stderr=b"boom")
        if 'printf %s "$HOME"' in command:
            return FakeResult(stdout=b"/home/sprite")
        if 'printf %s "$PATH"' in command:
            return FakeResult(stdout=b"/usr/local/bin:/usr/bin:/bin")
        return FakeResult()

    def create_service(
        self,
        service_name: str,
        *,
        cmd: str,
        args: list[str],
        env: dict[str, str],
        dir: str,
    ) -> list[object]:
        """Record the merged service definition and return its event stream."""
        self.service_calls.append(
            {
                "service_name": service_name,
                "cmd": cmd,
                "args": args,
                "env": env,
                "dir": dir,
            }
        )
        return self.service_events

    def list_services(self) -> list[object]:
        """Return the configured fake service definitions."""
        return self.services

    def delete_service(self, service_name: str) -> None:
        """Record removal of a stale service definition."""
        self.deleted_services.append(service_name)

    def destroy(self) -> None:
        """Record direct cleanup after failed provisioning."""
        self.destroyed = True


class FakeClient:
    """Recording Sprites client."""

    def __init__(self) -> None:
        self.sprites: dict[str, FakeSprite] = {}
        self.create_calls: list[dict[str, object]] = []
        self.destroy_calls: list[str] = []
        self.missing: set[str] = set()

    def create_sprite(self, name: str, **kwargs: object) -> FakeSprite:
        """Create a recording Sprite."""
        self.create_calls.append({"name": name, **kwargs})
        sprite = FakeSprite(name)
        self.sprites[name] = sprite
        return sprite

    def sprite(self, name: str) -> FakeSprite:
        """Return a handle without a provider lookup."""
        return self.sprites[name]

    def get_sprite(self, name: str) -> FakeSprite:
        """Return status or simulate a missing Sprite."""
        if name in self.missing:
            raise FakeNotFoundError(name)
        return self.sprites[name]

    def destroy_sprite(self, name: str) -> None:
        """Record deletion or simulate an already-missing Sprite."""
        if name in self.missing:
            raise FakeNotFoundError(name)
        self.destroy_calls.append(name)


@pytest.fixture(autouse=True)
def fake_sprites_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a minimal SDK module tree for lazy imports."""
    sprites_module = ModuleType("sprites")
    sprites_module.SpritesClient = object  # type: ignore[attr-defined]
    exceptions_module = ModuleType("sprites.exceptions")
    exceptions_module.SpriteError = FakeSpriteError  # type: ignore[attr-defined]
    exceptions_module.NotFoundError = FakeNotFoundError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sprites", sprites_module)
    monkeypatch.setitem(sys.modules, "sprites.exceptions", exceptions_module)


def launcher_with(client: FakeClient, **kwargs: Any) -> SpritesSandboxLauncher:
    """Construct a launcher with its network client replaced by a fake."""
    launcher = SpritesSandboxLauncher(**kwargs)
    launcher._client = client
    return launcher


def test_prepare_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing organization token fails before provisioning."""
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with pytest.raises(click.ClickException, match=TOKEN_ENV_VAR):
        SpritesSandboxLauncher().prepare()


def test_prepare_validates_env_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Named sandbox secrets must exist in the server environment."""
    monkeypatch.setenv(TOKEN_ENV_VAR, "sprite-token")
    monkeypatch.delenv("MISSING_API_KEY", raising=False)
    with pytest.raises(click.ClickException, match="MISSING_API_KEY"):
        SpritesSandboxLauncher(
            env=["MISSING_API_KEY"],
            allow_control_plane_credentials=True,
        ).prepare()


def test_prepare_requires_explicit_control_plane_credential_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming secrets is insufficient without acknowledging their transport."""
    monkeypatch.setenv(TOKEN_ENV_VAR, "sprite-token")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    with pytest.raises(click.ClickException, match="allow_control_plane_credentials"):
        SpritesSandboxLauncher(env=["OPENAI_API_KEY"]).prepare()


def test_prepare_rejects_invalid_env_passthrough_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environment names cannot inject shell syntax into remote commands."""
    monkeypatch.setenv(TOKEN_ENV_VAR, "sprite-token")
    with pytest.raises(click.ClickException, match="invalid environment variable name"):
        SpritesSandboxLauncher(
            env=["SAFE; touch /tmp/injected"],
            allow_control_plane_credentials=True,
        ).prepare()


def test_sdk_client_honors_api_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server token and configured API endpoint reach the SDK constructor."""
    calls: list[tuple[str, dict[str, object]]] = []

    class RecordingClient:
        def __init__(self, token: str, **kwargs: object) -> None:
            calls.append((token, kwargs))

    sys.modules["sprites"].SpritesClient = RecordingClient  # type: ignore[attr-defined]
    monkeypatch.setenv(TOKEN_ENV_VAR, "sprite-token")
    monkeypatch.setenv(API_URL_ENV_VAR, "https://env.example.test")
    launcher = SpritesSandboxLauncher(api_url="https://config.example.test")
    launcher._sprites()
    assert calls == [("sprite-token", {"base_url": "https://config.example.test"})]


def test_provision_bootstraps_persistent_sprite() -> None:
    """Provisioning creates the expected Sprite and installs the native runtime."""
    client = FakeClient()
    launcher = launcher_with(
        client,
        runtime="dev",
        install_spec="omnigent @ https://example.test/omnigent.whl",
    )
    sandbox_id = launcher.provision("Alice's Feature Branch")
    assert sandbox_id == "omnigent-alice-s-feature-branch"
    assert client.create_calls == [
        {
            "name": sandbox_id,
            "labels": ["omnigent-managed"],
            "wait_for_capacity": True,
            "runtime": "dev",
        }
    ]
    command = client.sprites[sandbox_id].run_calls[0][0][-1]
    assert "bootstrap-version" in command
    assert "python3 -m venv" in command
    assert "https://example.test/omnigent.whl" in command
    assert "@anthropic-ai/claude-code@2.1.212" in command
    assert "@openai/codex@0.139.0" in command
    assert "@earendil-works/pi-coding-agent@0.84.2" in command
    assert '[ -x "$HOME/.local/bin/$binary" ] && return' not in command
    assert "exit 0" not in command


def test_agent_cli_pin_change_invalidates_bootstrap_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing a pinned CLI spec changes both installation and cache identity."""
    original = render_bootstrap_command("omnigent==1.2.3")
    monkeypatch.setitem(
        sprites_sandbox._AGENT_CLI_SPECS,
        "codex",
        "@openai/codex@0.140.0",
    )
    updated = render_bootstrap_command("omnigent==1.2.3")

    assert original != updated
    assert "@openai/codex@0.139.0" in original
    assert "@openai/codex@0.140.0" in updated


def test_provision_destroys_sprite_when_bootstrap_fails() -> None:
    """A partial native install never strands a billable managed Sprite."""
    client = FakeClient()
    sprite = FakeSprite("omnigent-broken")
    sprite.fail_commands_containing = "bootstrap-version"

    def create_sprite(name: str, **_: object) -> FakeSprite:
        assert name == sprite.name
        client.sprites[name] = sprite
        return sprite

    client.create_sprite = create_sprite  # type: ignore[method-assign]
    launcher = launcher_with(client)
    with pytest.raises(click.ClickException, match="exit 12"):
        launcher.provision("broken")
    assert sprite.destroyed is True


def test_run_keeps_workload_credentials_out_of_command_execs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routine provider commands never contain or inherit workload secrets."""
    monkeypatch.setenv("OPENAI_API_KEY", "secret with spaces")
    client = FakeClient()
    sprite = FakeSprite("sb")
    client.sprites["sb"] = sprite
    launcher = launcher_with(
        client,
        env=["OPENAI_API_KEY"],
        allow_control_plane_credentials=True,
    )

    result = launcher.run("sb", "printf ok", check=False)
    assert result.returncode == 0
    args, kwargs = sprite.run_calls[-1]
    assert args[-1] == "printf ok"
    assert kwargs["env"] is None
    assert "secret with spaces" not in args[-1]

    sprite.fail_commands_containing = "false"
    with pytest.raises(click.ClickException, match="exit 12"):
        launcher.run("sb", "false")


def test_run_failure_includes_remote_output_tail() -> None:
    """Managed launch errors retain the actionable end of remote output."""
    client = FakeClient()
    sprite = FakeSprite("sb")
    sprite.fail_commands_containing = "bootstrap"
    client.sprites["sb"] = sprite
    launcher = launcher_with(client)

    with pytest.raises(click.ClickException, match="boom"):
        launcher.run("sb", "bootstrap")


def test_repository_clone_receives_only_git_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clone execs do not receive unrelated model credentials."""
    monkeypatch.setenv("GIT_TOKEN", "git-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "model-secret")
    client = FakeClient()
    sprite = FakeSprite("sb")
    client.sprites["sb"] = sprite
    launcher = launcher_with(
        client,
        env=["GIT_TOKEN", "OPENAI_API_KEY"],
        allow_control_plane_credentials=True,
    )

    clone_dir = launcher.materialize_workspace(
        "sb",
        workspace="/home/sprite/workspace",
        repo_url="https://github.com/example/project",
        repo_branch="main",
        repo_name="project",
    )

    assert clone_dir == "/home/sprite/workspace/project"
    args, kwargs = sprite.run_calls[-1]
    assert "git-secret" not in args[-1]
    assert "model-secret" not in args[-1]
    assert kwargs["env"] == {"GIT_TOKEN": "git-secret"}


def test_start_host_creates_cold_wake_safe_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repository, host identity, credentials, and cwd reach the service API."""
    monkeypatch.setenv("OPENAI_API_KEY", "llm-secret")
    client = FakeClient()
    sprite = FakeSprite("sb")
    sprite.service_events = [SimpleNamespace(type="info", data="service updated")]
    client.sprites["sb"] = sprite
    launcher = launcher_with(
        client,
        env=["OPENAI_API_KEY"],
        allow_control_plane_credentials=True,
    )
    stages: list[str] = []

    workspace = launcher.start_host(
        "sb",
        token="host-token",
        host_id="host-123",
        host_name="managed-123",
        server_url="https://server.example.test",
        repo_url="https://github.com/example/project",
        repo_branch="main",
        repo_name="project",
        host_config={"providers": {}},
        on_stage=stages.append,
    )

    assert workspace == "/home/sprite/workspace/project"
    assert stages == ["cloning", "starting"]
    commands = [call[0][-1] for call in sprite.run_calls]
    assert any(
        "git clone --branch main --single-branch -- "
        "https://github.com/example/project /home/sprite/workspace/project" in command
        for command in commands
    )
    assert any(
        "/home/sprite/.local/share/omnigent-host/venv/bin/python -c" in command
        for command in commands
    )
    service = sprite.service_calls[0]
    assert service["service_name"] == "omnigent-host"
    assert service["cmd"] == ("/home/sprite/.local/share/omnigent-host/venv/bin/omnigent")
    assert service["args"] == [
        "host",
        "--server",
        "https://server.example.test",
    ]
    assert service["dir"] == "/home/sprite/workspace/project"
    assert service["env"] == {
        "OPENAI_API_KEY": "llm-secret",
        "PATH": "/home/sprite/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "IS_SANDBOX": "1",
        ACTIVITY_LEASE_PROVIDER_ENV_VAR: "sprites",
        CLAUDE_BYPASS_WARNING_ACCEPT_ENV_VAR: "1",
        "OMNIGENT_HOST_TOKEN": "host-token",
        "OMNIGENT_HOST_ID": "host-123",
        "OMNIGENT_HOST_NAME": "managed-123",
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("running", True),
        ("warm", False),
        ("cold", False),
        ("starting", None),
    ],
)
def test_is_running_maps_sprite_status(status: str, expected: bool | None) -> None:
    """Provider lifecycle states map to the launcher tri-state contract."""
    client = FakeClient()
    client.sprites["sb"] = FakeSprite("sb", status=status)
    assert launcher_with(client).is_running("sb") is expected


def test_missing_sprite_is_stopped_and_termination_is_idempotent() -> None:
    """A provider 404 behaves like an already-terminated sandbox."""
    client = FakeClient()
    client.missing.add("gone")
    launcher = launcher_with(client)
    assert launcher.is_running("gone") is False
    launcher.terminate("gone")
    assert client.destroy_calls == []


def test_resume_wakes_sprite_with_exec() -> None:
    """Wake removes the stale auto-started host before token refresh."""
    client = FakeClient()
    sprite = FakeSprite("sb", status="cold")
    sprite.services = [SimpleNamespace(name="omnigent-host")]
    client.sprites["sb"] = sprite
    launcher_with(client).resume("sb")
    assert sprite.run_calls[-1][0][-1] == "true"
    assert sprite.deleted_services == ["omnigent-host"]


def test_resume_tolerates_missing_host_service() -> None:
    """A partial first launch can wake without a persisted service."""
    client = FakeClient()
    sprite = FakeSprite("sb", status="cold")
    client.sprites["sb"] = sprite
    launcher_with(client).resume("sb")
    assert sprite.deleted_services == []


def test_bootstrap_and_names_are_shell_safe() -> None:
    """Install specs are quoted and provider names remain bounded/DNS-safe."""
    command = render_bootstrap_command("omnigent @ https://e.test/a b.whl")
    assert "'omnigent @ https://e.test/a b.whl'" in command
    name = _managed_sprite_name(" !!! ")
    assert name == "omnigent-host"
    assert len(_managed_sprite_name("X" * 100)) <= 63
