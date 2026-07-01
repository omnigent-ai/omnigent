"""Tests for :mod:`omnigent.onboarding.sandboxes.sbx`."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

import omnigent.onboarding.sandboxes.sbx as sbxmod
from omnigent.onboarding.sandboxes.sbx import (
    KITS_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    TEMPLATE_ENV_VAR,
    SbxSandboxLauncher,
)


# ── Fake sbx CLI ────────────────────────────────────────────
#
# All transport is `subprocess.run([sbx, ...])`. The fake records every
# invocation's argv and hands back canned CompletedProcess results keyed
# by the sbx subcommand (argv index 1: "create"/"exec"/"cp"/"ls"/"rm").
# Hand-rolled (never MagicMock) so attribute access is explicit.


@dataclass
class _SbxCall:
    """One recorded subprocess.run invocation."""

    args: list[str]
    capture: bool


@dataclass
class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    args: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _FakeSbx:
    """Recorder injected in place of subprocess.run."""

    calls: list[_SbxCall] = field(default_factory=list)
    responses: dict[str, _FakeCompleted] = field(default_factory=dict)
    raise_on: dict[str, BaseException] = field(default_factory=dict)

    def run(
        self,
        argv: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_: object,
    ) -> _FakeCompleted:
        self.calls.append(_SbxCall(args=list(argv), capture=capture_output))
        sub = argv[1] if len(argv) > 1 else ""
        if sub in self.raise_on:
            raise self.raise_on[sub]
        resp = self.responses.get(sub, _FakeCompleted(args=list(argv)))
        return _FakeCompleted(list(argv), resp.returncode, resp.stdout, resp.stderr)


@pytest.fixture()
def fake_sbx(monkeypatch: pytest.MonkeyPatch) -> _FakeSbx:
    """Install the fake sbx with the binary present and clean ambient config."""
    monkeypatch.setattr(sbxmod.shutil, "which", lambda name: "/usr/bin/sbx")
    fake = _FakeSbx()
    monkeypatch.setattr(sbxmod.subprocess, "run", fake.run)
    for var in (TEMPLATE_ENV_VAR, SANDBOX_ENV_PASSTHROUGH_ENV_VAR, KITS_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    return fake


# ── class vars / config resolution ──────────────────────────


def test_capability_class_vars() -> None:
    """sbx is a local CLI-bootstrap provider with no port-forward path."""
    assert SbxSandboxLauncher.provider == "sbx"
    assert SbxSandboxLauncher.supports_cli_bootstrap is True
    assert SbxSandboxLauncher.supports_local_port_forward is False


def test_resolve_kits_constructor_wins(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructor kits win over the env-var fallback."""
    monkeypatch.setenv(KITS_ENV_VAR, "env-kit")
    launcher = SbxSandboxLauncher(kits=["a", "b"])
    assert launcher._resolve_kits() == ["a", "b"]


def test_resolve_kits_env_fallback(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without constructor kits, comma/whitespace-separated env names apply."""
    monkeypatch.setenv(KITS_ENV_VAR, "kit-one , kit-two")
    assert SbxSandboxLauncher()._resolve_kits() == ["kit-one", "kit-two"]


def test_resolve_env_missing_var_fails_loud(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured passthrough name unset locally is a loud error."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        SbxSandboxLauncher(env=["OPENAI_API_KEY"])._resolve_env()


def test_resolve_env_resolves_values(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passthrough names resolve to values from the local environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert SbxSandboxLauncher(env=["ANTHROPIC_API_KEY"])._resolve_env() == {
        "ANTHROPIC_API_KEY": "sk-test"
    }


# ── prepare ─────────────────────────────────────────────────


def test_prepare_requires_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `sbx` on PATH → loud error with an install hint."""
    monkeypatch.setattr(sbxmod.shutil, "which", lambda name: None)
    with pytest.raises(click.ClickException, match="sbx"):
        SbxSandboxLauncher().prepare()


def test_prepare_requires_login(fake_sbx: _FakeSbx) -> None:
    """`sbx ls` failing (not authenticated) → error pointing to `sbx login`."""
    fake_sbx.responses["ls"] = _FakeCompleted(
        args=[], returncode=1, stderr="ERROR: Not authenticated to Docker"
    )
    with pytest.raises(click.ClickException, match="sbx login"):
        SbxSandboxLauncher().prepare()


def test_prepare_passes_when_logged_in(fake_sbx: _FakeSbx) -> None:
    """Binary present + `sbx ls` succeeds → preflight passes; ls was probed."""
    SbxSandboxLauncher().prepare()
    assert [c.args[1] for c in fake_sbx.calls] == ["ls"]


# ── provision ───────────────────────────────────────────────


def test_provision_create_argv_and_setup(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Provision creates a `shell` sandbox bind-mounting cwd, returns the
    name as the id, then runs the one-time root setup step.
    """
    monkeypatch.chdir(tmp_path)
    sandbox_id = SbxSandboxLauncher().provision("omnigent-host")

    assert sandbox_id == "omnigent-host"
    create, setup = fake_sbx.calls
    assert create.args[1:] == [
        "create",
        "--name",
        "omnigent-host",
        "shell",
        str(tmp_path),
    ]
    # Setup runs as root via exec.
    assert setup.args[1:4] == ["exec", "-u", "root"]
    assert setup.args[4] == "omnigent-host"
    assert "@anthropic-ai/claude-code" in setup.args[-1]


def test_provision_includes_kits_and_template(
    fake_sbx: _FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each kit gets a `--kit`, and the template image rides `-t`, before `shell`."""
    monkeypatch.chdir(tmp_path)
    SbxSandboxLauncher(
        kits=["/tmp/sbxkit/claude", "ghcr.io/me/kit:1"], template="me/img:2"
    ).provision("box")

    create = fake_sbx.calls[0]
    assert create.args[1:] == [
        "create",
        "--name",
        "box",
        "--kit",
        "/tmp/sbxkit/claude",
        "--kit",
        "ghcr.io/me/kit:1",
        "-t",
        "me/img:2",
        "shell",
        str(tmp_path),
    ]


def test_provision_wraps_create_failure(
    fake_sbx: _FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing `sbx create` surfaces stderr as a ClickException; no setup runs."""
    monkeypatch.chdir(tmp_path)
    fake_sbx.responses["create"] = _FakeCompleted(
        args=[], returncode=1, stderr="Docker is not running"
    )
    with pytest.raises(click.ClickException, match="Docker is not running"):
        SbxSandboxLauncher().provision("box")
    assert [c.args[1] for c in fake_sbx.calls] == ["create"]


def test_provision_missing_network_policy_gives_remediation(
    fake_sbx: _FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Discovery finding: sbx refuses to start a sandbox until a default
    network policy is set. provision rewrites that into a `sbx policy
    set-default` remediation instead of echoing the raw error.
    """
    monkeypatch.chdir(tmp_path)
    fake_sbx.responses["create"] = _FakeCompleted(
        args=[],
        returncode=1,
        stderr="ERROR: default network policy has not been configured",
    )
    with pytest.raises(click.ClickException, match="sbx policy set-default"):
        SbxSandboxLauncher().provision("box")


# ── run / put ───────────────────────────────────────────────


def test_run_execs_via_bash_lc(fake_sbx: _FakeSbx) -> None:
    """run wraps the command in `sbx exec <id> bash -lc <cmd>` and returns output."""
    fake_sbx.responses["exec"] = _FakeCompleted(args=[], returncode=0, stdout="/root\n")
    result = SbxSandboxLauncher().run("box", 'printf %s "$HOME"')
    assert result.returncode == 0
    assert result.stdout == "/root\n"
    [call] = fake_sbx.calls
    assert call.args[1:] == ["exec", "box", "bash", "-lc", 'printf %s "$HOME"']


def test_run_check_raises_on_nonzero(fake_sbx: _FakeSbx) -> None:
    """check=True raises naming the command; check=False returns the failure."""
    fake_sbx.responses["exec"] = _FakeCompleted(args=[], returncode=2, stderr="boom")
    with pytest.raises(click.ClickException, match="exit 2"):
        SbxSandboxLauncher().run("box", "false")
    result = SbxSandboxLauncher().run("box", "false", check=False)
    assert result.returncode == 2
    assert result.stderr == "boom"


def test_put_uses_sbx_cp(fake_sbx: _FakeSbx) -> None:
    """put copies a local file to `<id>:<remote>` via `sbx cp`."""
    SbxSandboxLauncher().put("box", Path("/tmp/oa-wheels.tgz"), "/tmp/oa-wheels.tgz")
    [call] = fake_sbx.calls
    assert call.args[1:] == ["cp", "/tmp/oa-wheels.tgz", "box:/tmp/oa-wheels.tgz"]


def test_put_wraps_failure(fake_sbx: _FakeSbx) -> None:
    """A failing `sbx cp` surfaces stderr as a ClickException."""
    fake_sbx.responses["cp"] = _FakeCompleted(args=[], returncode=1, stderr="no such sandbox")
    with pytest.raises(click.ClickException, match="no such sandbox"):
        SbxSandboxLauncher().put("box", Path("/tmp/x"), "/tmp/x")


# ── wheel_install_command ───────────────────────────────────


def test_wheel_install_is_full_install(fake_sbx: _FakeSbx) -> None:
    """
    The default sbx image has no baked omnigent, so the install is a
    FULL dependency install into the user site — NOT the host-image
    overlay (no --no-deps / --force-reinstall).
    """
    cmd = SbxSandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")
    assert "tar xzf /tmp/oa-wheels.tgz" in cmd
    assert "pip install" in cmd
    assert "--user" in cmd
    assert "--no-deps" not in cmd
    assert "--force-reinstall" not in cmd
