"""Tests for :mod:`omnigent.onboarding.sandboxes.sbx`."""

from __future__ import annotations

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
    env: dict[str, str] | None = None


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
        env: dict[str, str] | None = None,
        **_: object,
    ) -> _FakeCompleted:
        self.calls.append(_SbxCall(args=list(argv), capture=capture_output, env=env))
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


def test_resolve_kits_constructor_wins(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    create, _policy, setup = fake_sbx.calls
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
    assert "opencode-ai@~1.17.7" in setup.args[-1]


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


def test_provision_allows_claude_auth_domains(
    fake_sbx: _FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Discovery finding: sbx's default-deny network policy blocks Claude
    Code's OAuth endpoints (console.anthropic.com / platform.claude.ai),
    so a subscription token refresh fails and Claude Code wipes the
    stored credentials. provision adds a sandbox-scoped allow rule for
    those domains so subscription auth keeps working.
    """
    monkeypatch.chdir(tmp_path)
    SbxSandboxLauncher().provision("box")

    policy = next(c for c in fake_sbx.calls if c.args[1] == "policy")
    assert policy.args[1:6] == ["policy", "allow", "network", "--sandbox", "box"]
    domains = policy.args[6]
    assert "console.anthropic.com:443" in domains
    assert "platform.claude.ai:443" in domains
    assert "claude.ai:443" in domains


def test_provision_survives_policy_allow_failure(
    fake_sbx: _FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An sbx build without `policy allow --sandbox` must not break
    provisioning — the rule is a best-effort enabler for subscription
    auth, and API-key users don't need it.
    """
    monkeypatch.chdir(tmp_path)
    fake_sbx.responses["policy"] = _FakeCompleted(
        args=[], returncode=1, stderr="unknown flag: --sandbox"
    )
    assert SbxSandboxLauncher().provision("box") == "box"


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
    """A permanent `sbx cp` failure surfaces stderr and fails fast (no retry)."""
    fake_sbx.responses["cp"] = _FakeCompleted(args=[], returncode=1, stderr="no such sandbox")
    with pytest.raises(click.ClickException, match="no such sandbox"):
        SbxSandboxLauncher().put("box", Path("/tmp/x"), "/tmp/x")
    # A non-transient error is not retried — one attempt, no 2s sleeps.
    assert len(fake_sbx.calls) == 1


def test_put_retries_transient_failure_then_succeeds(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A fresh sandbox's `sbx cp` transport can flake once right after
    `provision` (confirmed live: the first copy fails with a truncated
    tar extract, an immediate manual retry always succeeds). put()
    retries transiently-failed copies instead of giving up outright.
    """
    monkeypatch.setattr(sbxmod.time, "sleep", lambda seconds: None)
    attempts: list[int] = []

    def flaky_run(argv: list[str], **kwargs: object) -> _FakeCompleted:
        attempts.append(1)
        if len(attempts) == 1:
            return _FakeCompleted(
                args=argv,
                returncode=1,
                stderr="ERROR: tar extract failed: tar: Unexpected EOF in archive",
            )
        return _FakeCompleted(args=argv, returncode=0)

    monkeypatch.setattr(sbxmod.subprocess, "run", flaky_run)
    SbxSandboxLauncher().put("box", Path("/tmp/oa-wheels.tgz"), "/tmp/oa-wheels.tgz")
    assert len(attempts) == 2


def test_put_gives_up_after_retries_exhausted(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy that keeps failing still raises, naming the last error."""
    monkeypatch.setattr(sbxmod.time, "sleep", lambda seconds: None)
    fake_sbx.responses["cp"] = _FakeCompleted(
        args=[], returncode=1, stderr="tar: Unexpected EOF in archive"
    )
    with pytest.raises(click.ClickException, match="Unexpected EOF"):
        SbxSandboxLauncher().put("box", Path("/tmp/x"), "/tmp/x")
    assert len(fake_sbx.calls) == 6


# ── wheel_install_command ───────────────────────────────────


def test_wheel_install_is_full_install(fake_sbx: _FakeSbx) -> None:
    """
    The default sbx image has no baked omnigent, so the install is a
    FULL dependency install into a dedicated venv — NOT the host-image
    overlay (no --no-deps / --force-reinstall), and NOT a --user
    install: native-harness hook commands run under `python3 -I`,
    which disables user site-packages, so a --user install is
    invisible to them (confirmed against a real sbx sandbox).
    """
    cmd = SbxSandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")
    assert "tar xzf /tmp/oa-wheels.tgz" in cmd
    assert "python3 -m venv" in cmd
    assert "pip" in cmd
    assert "install" in cmd
    assert "--user" not in cmd
    # Required: opentelemetry-instrumentation-fastapi has never cut a
    # non-beta release, so a plain install excludes every version
    # matching the pin and fails outright (confirmed against a real
    # sandbox — the prebaked-image launchers' --no-deps install never
    # exercises this dependency).
    assert "--pre" in cmd
    assert "--no-deps" not in cmd
    assert "--force-reinstall" not in cmd
    # PATH persistence for the venv's entry points, mirroring the
    # generic bootstrap's ~/.local/bin persistence step.
    assert ".omnigent-venv/bin" in cmd
    assert "PATH" in cmd


# ── exec_foreground ─────────────────────────────────────────


def test_exec_foreground_argv_and_env(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Foreground attach uses an interactive TTY, injects passthrough env via
    `-e`, forces TERM, and `exec`s the command so its exit code is returned.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_sbx.responses["exec"] = _FakeCompleted(args=[], returncode=7)
    rc = SbxSandboxLauncher(env=["ANTHROPIC_API_KEY"]).exec_foreground(
        "box", "omnigent host --server u"
    )
    assert rc == 7
    [call] = fake_sbx.calls
    assert call.args[1:4] == ["exec", "-it", "-e"]
    assert call.args[4] == "ANTHROPIC_API_KEY=sk-test"
    assert call.args[5] == "box"
    assert call.args[6:8] == ["bash", "-lc"]
    assert call.args[8].endswith("TERM=xterm-256color exec omnigent host --server u")
    # Foreground inherits the terminal — output is NOT captured.
    assert call.capture is False


def test_exec_foreground_strips_proxy_managed_placeholders(fake_sbx: _FakeSbx) -> None:
    """
    Discovery finding: Docker Sandboxes injects placeholder provider
    keys (ANTHROPIC_API_KEY=proxy-managed, …) into every exec session.
    Unless a real key is registered with sbx, the gateway never
    substitutes a value, so harnesses that prefer env keys over their
    own login (Claude Code) fail with "Invalid API key". The remote
    command must unset any var still holding the literal sentinel
    before the host starts; explicitly passed `-e` values are real and
    survive the value check.
    """
    SbxSandboxLauncher(env=[]).exec_foreground("box", "omnigent host --server u")
    [call] = fake_sbx.calls
    remote = call.args[-1]
    assert '"proxy-managed"' in remote
    assert "unset" in remote
    # The strip runs before the host process replaces the shell.
    assert remote.index("unset") < remote.index("exec omnigent host")


def test_exec_foreground_reraises_keyboard_interrupt(fake_sbx: _FakeSbx) -> None:
    """Ctrl-C during the attach tears down and re-raises."""
    fake_sbx.raise_on["exec"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        SbxSandboxLauncher().exec_foreground("box", "omnigent host --server u")


# ── attach / keep_alive / terminate ─────────────────────────


def _ls_json(*names: str) -> str:
    import json

    return json.dumps([{"name": n} for n in names])


def test_attach_ok_when_present(fake_sbx: _FakeSbx) -> None:
    """attach succeeds when the sandbox appears in `sbx ls --json`."""
    fake_sbx.responses["ls"] = _FakeCompleted(args=[], stdout=_ls_json("box"))
    SbxSandboxLauncher().attach("box")  # must not raise


def test_attach_unknown_fails_with_hint(fake_sbx: _FakeSbx) -> None:
    """attach to a missing sandbox names the id."""
    fake_sbx.responses["ls"] = _FakeCompleted(args=[], stdout=_ls_json("other"))
    with pytest.raises(click.ClickException, match="box"):
        SbxSandboxLauncher().attach("box")


def test_keep_alive_issues_no_sbx_call(fake_sbx: _FakeSbx) -> None:
    """
    sbx DOES idle-stop (~30s after the last session disconnects), but
    there is no disable knob — the foreground `connect` session is what
    holds the host up — so keep_alive is informational and calls no sbx.
    """
    SbxSandboxLauncher().keep_alive("box")
    assert fake_sbx.calls == []


def test_terminate_removes_when_present(fake_sbx: _FakeSbx) -> None:
    """terminate removes an existing sandbox via `sbx rm -f`."""
    fake_sbx.responses["ls"] = _FakeCompleted(args=[], stdout=_ls_json("box"))
    SbxSandboxLauncher().terminate("box")
    rm = fake_sbx.calls[-1]
    assert rm.args[1:] == ["rm", "-f", "box"]


def test_terminate_idempotent_when_absent(fake_sbx: _FakeSbx) -> None:
    """terminate is a no-op success when the sandbox is already gone."""
    fake_sbx.responses["ls"] = _FakeCompleted(args=[], stdout=_ls_json())
    SbxSandboxLauncher().terminate("box")
    assert [c.args[1] for c in fake_sbx.calls] == ["ls"]


# ── registration / capability surface ───────────────────────


def test_provider_is_registered() -> None:
    """The sbx provider is listed and resolvable."""
    from omnigent.onboarding.sandboxes import available_providers, get_launcher

    assert "sbx" in available_providers()
    assert isinstance(get_launcher("sbx"), SbxSandboxLauncher)


def test_login_primitives_are_capability_gated(fake_sbx: _FakeSbx) -> None:
    """No local->sandbox forward path, so the OAuth primitives stay gated."""
    from omnigent.onboarding.sandboxes.base import SandboxCapabilityError

    launcher = SbxSandboxLauncher()
    with pytest.raises(SandboxCapabilityError):
        launcher.forward_local_port("box", 8022)
    with pytest.raises(SandboxCapabilityError):
        launcher.stream_exec("box", "echo hi")


# ── CLI / get_launcher kit threading ────────────────────────


def test_get_launcher_threads_kits() -> None:
    """get_launcher passes kits into the sbx launcher constructor."""
    from omnigent.onboarding.sandboxes import get_launcher

    launcher = get_launcher("sbx", kits=("/tmp/sbxkit/claude",))
    assert isinstance(launcher, SbxSandboxLauncher)
    assert launcher._resolve_kits() == ["/tmp/sbxkit/claude"]


def test_get_launcher_empty_kits_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Click's empty-tuple default must not shadow the OMNIGENT_SBX_KITS fallback."""
    from omnigent.onboarding.sandboxes import get_launcher

    monkeypatch.setenv(KITS_ENV_VAR, "/tmp/sbxkit/claude")
    launcher = get_launcher("sbx", kits=())
    assert isinstance(launcher, SbxSandboxLauncher)
    assert launcher._resolve_kits() == ["/tmp/sbxkit/claude"]


def test_create_command_forwards_kit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`omnigent sandbox create --provider sbx --kit X` forwards X to get_launcher."""
    from click.testing import CliRunner

    import omnigent.cli_sandbox as cli_sandbox

    recorded: dict[str, object] = {}

    def fake_get_launcher(provider: str, *, workspace_host=None, kits=None):
        recorded["provider"] = provider
        recorded["kits"] = kits
        return SbxSandboxLauncher(kits=kits)

    monkeypatch.setattr(cli_sandbox, "get_launcher", fake_get_launcher)
    monkeypatch.setattr(cli_sandbox, "_resolve_repo_root", lambda r: Path("/repo"))
    # Keep the command offline: stub workspace derivation + the bootstrap.
    monkeypatch.setattr("omnigent.onboarding.sandboxes.derive_workspace", lambda url: None)
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.bootstrap_sandbox_host",
        lambda *a, **k: "omnigent-host",
    )

    result = CliRunner().invoke(
        cli_sandbox.sandbox_create,
        [
            "--provider",
            "sbx",
            "--server",
            "https://example.com",
            "--kit",
            "/tmp/sbxkit/claude",
        ],
    )
    assert result.exit_code == 0, result.output
    assert recorded["provider"] == "sbx"
    assert recorded["kits"] == ("/tmp/sbxkit/claude",)


def test_resolve_env_opencode_key_falls_back_to_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(
        sbxmod, "resolve_opencode_zen_key", lambda environ=None: ("keychain", "sk-vault")
    )
    resolved = SbxSandboxLauncher(env=["OPENCODE_API_KEY"])._resolve_env()
    assert resolved == {"OPENCODE_API_KEY": "sk-vault"}


def test_resolve_env_opencode_key_prefers_local_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-local")
    monkeypatch.setattr(
        sbxmod, "resolve_opencode_zen_key", lambda environ=None: ("keychain", "sk-vault")
    )
    resolved = SbxSandboxLauncher(env=["OPENCODE_API_KEY"])._resolve_env()
    assert resolved == {"OPENCODE_API_KEY": "sk-local"}


def test_resolve_env_opencode_key_missing_everywhere_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(sbxmod, "resolve_opencode_zen_key", lambda environ=None: None)
    with pytest.raises(click.ClickException):
        SbxSandboxLauncher(env=["OPENCODE_API_KEY"])._resolve_env()


# ── managed mode ────────────────────────────────────────────


def test_managed_mode_class_vars() -> None:
    """Managed sbx hosts can resume in place."""
    assert SbxSandboxLauncher.can_resume is True


def test_managed_provision_argv_and_egress(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Managed provision creates from the template, bind-mounts a throwaway
    empty directory (the CLI requires a PATH), skips the setup step, and
    opens egress to the server + Claude domains.
    """
    empty = tmp_path / "empty"
    monkeypatch.setattr(sbxmod, "_MANAGED_EMPTY_WORKSPACE", empty)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    launcher = SbxSandboxLauncher(
        template="ghcr.io/me/omnigent-host-sbx:latest",
        env=["ANTHROPIC_API_KEY"],
        kits=["/opt/sbxkits/claude"],
        server_url="http://172.17.0.1:6767",
    )
    sandbox_id = launcher.provision("managed-box")

    assert sandbox_id == "managed-box"
    assert empty.exists()
    create, policy = fake_sbx.calls[:2]
    assert create.args[1:] == [
        "create",
        "--name",
        "managed-box",
        "--kit",
        "/opt/sbxkits/claude",
        "-t",
        "ghcr.io/me/omnigent-host-sbx:latest",
        "shell",
        str(empty),
    ]
    assert policy.args[1:5] == ["policy", "allow", "network", "--sandbox"]
    assert policy.args[5] == "managed-box"
    allowed = policy.args[6]
    assert "172.17.0.1:6767" in allowed
    assert "console.anthropic.com:443" in allowed
    # No root setup exec.
    assert [c.args[1:4] for c in fake_sbx.calls] != [["exec", "-u", "root"]]


def test_managed_run_forwards_env_off_argv(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Managed run() forwards configured env by NAME (`sbx exec -e NAME`) with
    the VALUE riding the subprocess environment — the secret value must never
    appear in argv (which is visible in the server host's process table).
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    launcher = SbxSandboxLauncher(
        template="ghcr.io/me/omnigent-host-sbx:latest",
        env=["OPENAI_API_KEY"],
        server_url="http://172.17.0.1:6767",
    )
    result = launcher.run("box", 'printf %s "$HOME"')
    assert result.returncode == 0
    [call] = fake_sbx.calls
    assert call.args[1] == "exec"
    # Name reaches argv via `-e NAME`; the value must not appear anywhere in argv.
    assert call.args.count("-e") == 1
    assert "OPENAI_API_KEY" in call.args
    assert "sk-secret" not in " ".join(call.args)
    # The value is carried in the subprocess environment instead.
    assert call.env is not None
    assert call.env["OPENAI_API_KEY"] == "sk-secret"


def test_managed_run_missing_env_fails_loud(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured env name that is unset in the server env fails loud."""
    monkeypatch.delenv("GIT_TOKEN", raising=False)
    launcher = SbxSandboxLauncher(
        template="ghcr.io/me/omnigent-host-sbx:latest",
        env=["GIT_TOKEN"],
        server_url="http://172.17.0.1:6767",
    )
    with pytest.raises(click.ClickException, match="GIT_TOKEN"):
        launcher.run("box", "echo hi")


def test_cli_run_does_not_inject_env(fake_sbx: _FakeSbx) -> None:
    """CLI-bootstrap mode keeps the existing run() argv (no -e injection)."""
    fake_sbx.responses["exec"] = _FakeCompleted(args=[], returncode=0, stdout="/root\n")
    result = SbxSandboxLauncher().run("box", 'printf %s "$HOME"')
    assert result.stdout == "/root\n"
    assert fake_sbx.calls[0].args[1:] == [
        "exec",
        "box",
        "bash",
        "-lc",
        'printf %s "$HOME"',
    ]


def test_managed_run_background_strips_proxy_managed(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The managed background host launch unsets Docker's proxy-managed
    placeholder env vars in the bash shell BEFORE spawning the detached
    host, so a placeholder for a key not in sandbox.sbx.env can't shadow the
    host's real harness credentials (e.g. Claude Code subscription auth).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    launcher = SbxSandboxLauncher(
        template="ghcr.io/me/omnigent-host-sbx:latest",
        env=["ANTHROPIC_API_KEY"],
        server_url="http://172.17.0.1:6767",
    )
    launcher.run_background("box", "OMNIGENT_HOST_TOKEN=t omnigent host --server u")
    [call] = fake_sbx.calls
    remote = call.args[-1]
    assert "proxy-managed" in remote
    assert "unset" in remote
    # The strip runs before the host is backgrounded / exec'd.
    assert remote.index("unset") < remote.index("setsid nohup")
    assert remote.index("unset") < remote.index("omnigent host")


def test_cli_run_background_is_unstripped(fake_sbx: _FakeSbx) -> None:
    """CLI-bootstrap mode keeps the base background command (no strip prefix)."""
    SbxSandboxLauncher().run_background("box", "cmd")
    [call] = fake_sbx.calls
    remote = call.args[-1]
    assert "proxy-managed" not in remote
    assert remote.startswith("setsid nohup")


def test_sandbox_exists_parses_object_wrapper(fake_sbx: _FakeSbx) -> None:
    """Current `sbx ls --json` returns {"sandboxes": [...]}."""
    fake_sbx.responses["ls"] = _FakeCompleted(
        args=[], stdout='{"sandboxes": [{"name": "box", "status": "running"}]}'
    )
    assert SbxSandboxLauncher()._sandbox_exists("box") is True
    assert SbxSandboxLauncher()._sandbox_exists("other") is False


def test_resume_verifies_existence(fake_sbx: _FakeSbx) -> None:
    """resume() checks that the sandbox still exists; exec auto-starts it."""
    fake_sbx.responses["ls"] = _FakeCompleted(
        args=[], stdout='{"sandboxes": [{"name": "box", "status": "stopped"}]}'
    )
    SbxSandboxLauncher().resume("box")  # must not raise
    assert [c.args[1] for c in fake_sbx.calls] == ["ls"]
