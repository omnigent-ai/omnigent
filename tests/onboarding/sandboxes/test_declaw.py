"""Tests for :mod:`omnigent.onboarding.sandboxes.declaw`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes import available_providers, get_launcher
from omnigent.onboarding.sandboxes.declaw import (
    DEFAULT_DECLAW_TEMPLATE,
    MAX_LIFETIME_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    SECURITY_MODE_ENV_VAR,
    TEMPLATE_ENV_VAR,
    DeclawSandboxLauncher,
    managed_token_ttl_s,
    resolve_max_lifetime_s,
)

# ── Fake declaw SDK ─────────────────────────────────────────
#
# The SDK is an optional dependency the test env does not install, and real
# Sandbox/SecurityPolicy objects only exist server-side — so these are
# hand-rolled stubs injected via sys.modules, resolving the launcher's
# function-local `from declaw import ...` / `from declaw.exceptions import ...`.


class _SandboxException(Exception):
    pass


class _NotFoundException(_SandboxException):
    pass


class _TemplateException(_SandboxException):
    pass


class _FileUploadException(_SandboxException):
    pass


class _CommandExitException(_SandboxException):
    pass


class _AuthenticationException(Exception):
    # Mirrors the real class: extends Exception, NOT SandboxException.
    pass


# -- security config stubs: record constructor kwargs / from_dict payload --


class _Recorder:
    """Generic recording config: stores constructor kwargs and from_dict data."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.from_dict_data: dict | None = None

    @classmethod
    def from_dict(cls, data: dict) -> _Recorder:
        obj = cls()
        obj.from_dict_data = dict(data)
        return obj


class _FakePIIConfig(_Recorder):
    pass


class _FakeInjectionDefenseConfig(_Recorder):
    pass


class _FakeNetworkPolicy(_Recorder):
    pass


class _FakeAuditConfig(_Recorder):
    pass


class _FakeToxicityConfig(_Recorder):
    pass


class _FakeCodeSecurityConfig(_Recorder):
    pass


class _FakeContentGateConfig(_Recorder):
    pass


class _FakeCustomPolicyConfig:
    def __init__(
        self,
        enabled: bool = False,
        inline_rego: str | None = None,
        inline_modules: list | None = None,
        policy_ref: str | None = None,
        default_deny: bool = False,
    ) -> None:
        self.enabled = enabled
        self.inline_rego = inline_rego
        self.inline_modules = inline_modules
        self.policy_ref = policy_ref
        self.default_deny = default_deny


class _FakeSecurityPolicy:
    """Holder mirroring declaw's SecurityPolicy surface for mapping assertions."""

    def __init__(self) -> None:
        self.pii: object = None
        self.injection_defense: object = False
        self.network: object = None
        self.audit: object = True
        self.toxicity: object = None
        self.code_security: object = None
        self.content_gate: object = None
        self.custom_policy: object = None
        self.full_kwargs: dict | None = None
        self.from_dict_data: dict | None = None

    @classmethod
    def full_injection_defense(cls, **kwargs: object) -> _FakeSecurityPolicy:
        obj = cls()
        obj.full_kwargs = dict(kwargs)
        # Mirror the real factory: it also enables the prompt-injection OPA pack.
        obj.custom_policy = _FakeCustomPolicyConfig(enabled=True, policy_ref="prompt-injection@v3")
        return obj

    @classmethod
    def from_dict(cls, data: dict) -> _FakeSecurityPolicy:
        obj = cls()
        obj.from_dict_data = dict(data)
        return obj


@dataclass
class _FakeCommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass
class _State:
    """Shared recorder for assertions."""

    create_kwargs: dict = field(default_factory=dict)
    run_calls: list[dict] = field(default_factory=list)
    written: list[tuple[str, bytes]] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    set_timeouts: list[int] = field(default_factory=list)
    connect_calls: list[str] = field(default_factory=list)
    exec_result: _FakeCommandResult = field(default_factory=_FakeCommandResult)
    stream_text: str = ""
    stream_exit: int = 0
    running: bool = True
    connect_missing: bool = False
    kill_missing: bool = False
    create_raises: BaseException | None = None


class _FakeCommands:
    def __init__(self, state: _State) -> None:
        self._state = state

    def run(self, cmd: str, timeout=None, request_timeout=None, **kwargs):
        self._state.run_calls.append({"cmd": cmd, "timeout": timeout})
        return self._state.exec_result

    def run_stream(self, cmd: str, on_stdout=None, on_stderr=None, timeout=None, **kwargs):
        if on_stdout is not None and self._state.stream_text:
            on_stdout(self._state.stream_text)
        return _FakeCommandResult(exit_code=self._state.stream_exit)


class _FakeFiles:
    def __init__(self, state: _State) -> None:
        self._state = state

    def write(self, path: str, data: bytes, **kwargs):
        self._state.written.append((path, data))
        return object()  # WriteInfo stand-in


class _FakeSandbox:
    _state: _State

    def __init__(self, sandbox_id: str = "sb-declaw-1") -> None:
        self._sandbox_id = sandbox_id
        self.commands = _FakeCommands(self._state)
        self.files = _FakeFiles(self._state)

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @classmethod
    def create(cls, **kwargs) -> _FakeSandbox:
        cls._state.create_kwargs = kwargs
        if cls._state.create_raises is not None:
            raise cls._state.create_raises
        return cls()

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs) -> _FakeSandbox:
        cls._state.connect_calls.append(sandbox_id)
        if cls._state.connect_missing:
            raise _NotFoundException(sandbox_id)
        return cls(sandbox_id)

    def kill(self, request_timeout=None, *, wait: bool = False) -> bool:
        if self._state.kill_missing:
            raise _NotFoundException(self._sandbox_id)
        self._state.killed.append(self._sandbox_id)
        return True

    def is_running(self, request_timeout=None) -> bool:
        return self._state.running

    def set_timeout(self, timeout: int, **kwargs) -> None:
        self._state.set_timeouts.append(timeout)


def _install_fake_declaw(monkeypatch: pytest.MonkeyPatch, state: _State) -> None:
    """Inject the fake ``declaw`` package + ``declaw.exceptions`` submodule."""
    _FakeSandbox._state = state

    mod = types.ModuleType("declaw")
    mod.Sandbox = _FakeSandbox  # type: ignore[attr-defined]
    mod.SecurityPolicy = _FakeSecurityPolicy  # type: ignore[attr-defined]
    mod.PIIConfig = _FakePIIConfig  # type: ignore[attr-defined]
    mod.InjectionDefenseConfig = _FakeInjectionDefenseConfig  # type: ignore[attr-defined]
    mod.NetworkPolicy = _FakeNetworkPolicy  # type: ignore[attr-defined]
    mod.AuditConfig = _FakeAuditConfig  # type: ignore[attr-defined]
    mod.ToxicityConfig = _FakeToxicityConfig  # type: ignore[attr-defined]
    mod.CodeSecurityConfig = _FakeCodeSecurityConfig  # type: ignore[attr-defined]
    mod.ContentGateConfig = _FakeContentGateConfig  # type: ignore[attr-defined]
    mod.CustomPolicyConfig = _FakeCustomPolicyConfig  # type: ignore[attr-defined]

    exc = types.ModuleType("declaw.exceptions")
    exc.SandboxException = _SandboxException  # type: ignore[attr-defined]
    exc.NotFoundException = _NotFoundException  # type: ignore[attr-defined]
    exc.TemplateException = _TemplateException  # type: ignore[attr-defined]
    exc.AuthenticationException = _AuthenticationException  # type: ignore[attr-defined]
    exc.FileUploadException = _FileUploadException  # type: ignore[attr-defined]
    exc.CommandExitException = _CommandExitException  # type: ignore[attr-defined]
    mod.exceptions = exc  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "declaw", mod)
    monkeypatch.setitem(sys.modules, "declaw.exceptions", exc)


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _State:
    state = _State()
    _install_fake_declaw(monkeypatch, state)
    monkeypatch.setenv("DECLAW_API_KEY", "declaw-test-key")
    monkeypatch.delenv(TEMPLATE_ENV_VAR, raising=False)
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    monkeypatch.delenv(SECURITY_MODE_ENV_VAR, raising=False)
    monkeypatch.delenv(MAX_LIFETIME_ENV_VAR, raising=False)
    return state


# ── registration / capabilities ─────────────────────────────


def test_declaw_is_a_registered_available_provider() -> None:
    # The launcher module ships in the package, so the provider is available
    # regardless of whether the optional declaw SDK is installed.
    assert "declaw" in available_providers()
    assert isinstance(get_launcher("declaw"), DeclawSandboxLauncher)


def test_capability_flags() -> None:
    launcher = DeclawSandboxLauncher()
    assert launcher.provider == "declaw"
    assert launcher.supports_cli_bootstrap is True
    assert launcher.supports_local_port_forward is False


def test_constructs_without_sdk() -> None:
    # __init__ must not import the SDK (the CLI builds the launcher no-arg and
    # the managed factory builds it with kwargs, both before any SDK call).
    launcher = DeclawSandboxLauncher(
        template="t", env=["X"], security={"mode": "strict"}, vault_refs={"K": "v"}
    )
    assert launcher._template_ref == "t"
    assert launcher._env_names == ("X",)
    assert launcher._security == {"mode": "strict"}
    assert launcher._vault_refs == {"K": "v"}


# ── prepare ──────────────────────────────────────────────────


def test_prepare_requires_api_key(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECLAW_API_KEY")
    with pytest.raises(click.ClickException, match="DECLAW_API_KEY"):
        DeclawSandboxLauncher().prepare()


def test_prepare_raises_install_hint_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry in sys.modules makes `import declaw` raise ImportError.
    monkeypatch.setitem(sys.modules, "declaw", None)
    monkeypatch.setenv("DECLAW_API_KEY", "k")
    with pytest.raises(click.ClickException, match=r"pip install 'omnigent\[declaw\]'"):
        DeclawSandboxLauncher().prepare()


# ── _build_security_policy (the declaw-specific mapping) ─────


def test_security_secure_by_default_is_balanced_with_pii_redact(sdk: _State) -> None:
    policy = DeclawSandboxLauncher()._build_security_policy()
    # Balanced injection cascade + PII redaction on, no injection domains yet.
    assert policy.full_kwargs == {"mode": "balanced"}
    assert isinstance(policy.pii, _FakePIIConfig)
    assert policy.pii.kwargs == {"enabled": True, "action": "redact"}


def test_security_default_mode_overridable_via_env(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SECURITY_MODE_ENV_VAR, "strict")
    policy = DeclawSandboxLauncher()._build_security_policy()
    assert policy.full_kwargs == {"mode": "strict"}


def test_security_curated_mode_enables_full_cascade(sdk: _State) -> None:
    policy = DeclawSandboxLauncher(
        security={
            "mode": "agentic-tool",
            "agent_policy": "summarize docs",
            "injection_defense": {"action": "block", "domains": ["api.github.com"]},
        }
    )._build_security_policy()
    assert policy.full_kwargs["mode"] == "agentic-tool"
    assert policy.full_kwargs["agent_policy"] == "summarize docs"
    assert policy.full_kwargs["action"] == "block"
    assert policy.full_kwargs["domains"] == ["api.github.com"]


def test_security_pii_block_maps_to_pii_config(sdk: _State) -> None:
    policy = DeclawSandboxLauncher(
        security={"pii": {"enabled": True, "action": "block"}}
    )._build_security_policy()
    assert isinstance(policy.pii, _FakePIIConfig)
    assert policy.pii.from_dict_data == {"enabled": True, "action": "block"}
    # No mode → no full cascade.
    assert policy.full_kwargs is None


def test_security_governance_pack_maps_to_custom_policy(sdk: _State) -> None:
    policy = DeclawSandboxLauncher(
        security={"governance_pack": "owasp-llm-top10"}
    )._build_security_policy()
    assert policy.custom_policy.enabled is True
    assert policy.custom_policy.policy_ref == "owasp-llm-top10"


def test_security_escape_hatch_inline_rego(sdk: _State) -> None:
    policy = DeclawSandboxLauncher(
        security={"inline_rego": "deny_command contains m if { true }"}
    )._build_security_policy()
    assert policy.custom_policy.enabled is True
    assert policy.custom_policy.inline_rego.startswith("deny_command")


def test_security_network_allow_deny_aliases_map_to_out(sdk: _State) -> None:
    policy = DeclawSandboxLauncher(
        security={"network": {"allow": ["api.github.com"], "deny": ["169.254.169.254"]}}
    )._build_security_policy()
    assert isinstance(policy.network, _FakeNetworkPolicy)
    assert policy.network.from_dict_data == {
        "allow_out": ["api.github.com"],
        "deny_out": ["169.254.169.254"],
    }


def test_security_audit_bool_passthrough(sdk: _State) -> None:
    policy = DeclawSandboxLauncher(security={"audit": False})._build_security_policy()
    assert policy.audit is False


# ── provision ────────────────────────────────────────────────


def test_provision_passes_template_lifetime_and_security(sdk: _State) -> None:
    assert DeclawSandboxLauncher().provision("managed-x") == "sb-declaw-1"
    assert sdk.create_kwargs["template"] == DEFAULT_DECLAW_TEMPLATE
    assert sdk.create_kwargs["timeout"] == resolve_max_lifetime_s()
    assert sdk.create_kwargs["metadata"] == {"omnigent-name": "managed-x"}
    assert sdk.create_kwargs["allow_internet_access"] is True
    assert isinstance(sdk.create_kwargs["security"], _FakeSecurityPolicy)


def test_provision_forwards_vault_refs(sdk: _State) -> None:
    DeclawSandboxLauncher(vault_refs={"OPENAI_API_KEY": "openai-prod"}).provision("x")
    assert sdk.create_kwargs["vault_refs"] == {"OPENAI_API_KEY": "openai-prod"}


def test_provision_env_passthrough_missing_var_fails_loud(sdk: _State) -> None:
    with pytest.raises(click.ClickException, match="NOT_SET_ANYWHERE"):
        DeclawSandboxLauncher(env=["NOT_SET_ANYWHERE"]).provision("x")


def test_provision_missing_template_points_at_build(sdk: _State) -> None:
    sdk.create_raises = _TemplateException("alias 'omnigent-host' not found")
    with pytest.raises(click.ClickException, match="deploy/declaw/README"):
        DeclawSandboxLauncher().provision("x")


# ── run / put / lifecycle ────────────────────────────────────


def test_run_maps_exit_code_to_returncode(sdk: _State) -> None:
    sdk.exec_result = _FakeCommandResult(stdout="hi", stderr="", exit_code=0)
    result = DeclawSandboxLauncher().run("sb", "echo hi")
    assert result.returncode == 0
    assert result.stdout == "hi"
    # Command is bash -lc wrapped.
    assert sdk.run_calls[-1]["cmd"].startswith("bash -lc ")


def test_run_check_raises_on_nonzero(sdk: _State) -> None:
    sdk.exec_result = _FakeCommandResult(exit_code=3)
    with pytest.raises(click.ClickException, match="exit 3"):
        DeclawSandboxLauncher().run("sb", "false")


def test_run_check_false_returns_nonzero(sdk: _State) -> None:
    sdk.exec_result = _FakeCommandResult(exit_code=3)
    result = DeclawSandboxLauncher().run("sb", "false", check=False)
    assert result.returncode == 3


def test_put_writes_local_bytes(sdk: _State, tmp_path: Path) -> None:
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"payload")
    DeclawSandboxLauncher().put("sb", local, "/tmp/oa-wheels.tgz")
    assert sdk.written == [("/tmp/oa-wheels.tgz", b"payload")]


def test_attach_running_ok_then_stopped_raises(sdk: _State) -> None:
    DeclawSandboxLauncher().attach("sb")  # running=True default
    sdk.running = False
    with pytest.raises(click.ClickException, match="not running"):
        DeclawSandboxLauncher().attach("sb")


def test_keep_alive_extends_timeout(sdk: _State) -> None:
    DeclawSandboxLauncher().keep_alive("sb")
    assert sdk.set_timeouts == [resolve_max_lifetime_s()]


def test_terminate_kills(sdk: _State) -> None:
    DeclawSandboxLauncher().terminate("sb-declaw-1")
    assert sdk.killed == ["sb-declaw-1"]


def test_terminate_already_gone_is_success(sdk: _State) -> None:
    sdk.connect_missing = True
    # Not cached → connect raises NotFound → swallowed (desired end state holds).
    DeclawSandboxLauncher().terminate("sb-gone")
    assert sdk.killed == []


def test_wheel_install_command_uses_host_image_overlay(sdk: _State) -> None:
    cmd = DeclawSandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")
    assert "pip install" in cmd
    assert "/tmp/oa-wheels.tgz" in cmd


# ── stream_exec (threaded run_stream wrapper) ────────────────


def test_stream_exec_streams_lines_and_exit_code(sdk: _State) -> None:
    sdk.stream_text = "hello\n"
    sdk.stream_exit = 0
    process = DeclawSandboxLauncher().stream_exec("sb", "omnigent host")
    lines = list(process.lines)
    assert "".join(lines) == "hello\n"
    assert process.wait() == 0


def test_managed_token_ttl_exceeds_lifetime(sdk: _State) -> None:
    assert managed_token_ttl_s() > resolve_max_lifetime_s()
