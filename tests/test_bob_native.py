"""Tests for the bob-native (IBM Bob Shell ``bob chat``) native TUI harness."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import click
import httpx
import pytest
import yaml

from omnigent.harnesses.bob_native import bridge as bob_bridge
from omnigent.harnesses.bob_native import main as bob_main
from omnigent.harnesses.bob_native.bridge import BobPaneState, classify_bob_pane
from omnigent.harnesses.bob_native.launch_args import (
    BobLaunchArgsError,
    build_bob_chat_argv,
    validate_bob_chat_args,
)

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance
    from omnigent.runner.resource_registry import SessionResourceRegistry

_RULE = "─" * 60

# Trimmed pane captures from Bob Shell 2.0.5 (no credentials or user data).
_READY_PANE = f"""
                Users should independently verify accuracy of AI-generated content.
  (i) Using API key authentication
 {_RULE}
  ❯   Build Anything, @ for context, / for commands, $ for skills
 {_RULE}
  Agent Mode
"""
_MID_TURN_PANE = f"""
 ❯ Reply with exactly: ok
 ⠼ Processing… (Enter to steer, Tab to queue)
 {_RULE}
  ❯   Build Anything, @ for context, / for commands, $ for skills
 {_RULE}
  Agent Mode · 8.2k / 270.0k (3%)
"""
_TRUST_PANE = f"""
                /private/tmp/ws
   {_RULE}
   Do you trust this folder?
   Trusting a folder allows Bob Shell to execute commands it suggests.
   {_RULE}
   → 1. Trust folder (ws)
     2. Trust parent folder (tmp)
     3. Don't trust
     ↑↓ (1/3)
   Press ESC or CTRL+C to exit
   {_RULE}
"""
_APPROVAL_PANE = f"""
  (i) Using API key authentication
 ❯ Run this exact shell command: echo hi
  {_RULE}
  Execute Command
  {_RULE}
  Command:          echo hi
  → Approve Once
    Always Allow Command for task
    Reject
    ↑↓ (1/3)
  Press Enter to confirm
  {_RULE}
"""
_SIGN_IN_PANE = """
                /private/tmp/ws
                ∙∙● Complete sign-in in your browser…
                   (Press ESC or Ctrl+C to exit)
"""


# ── identity / aliases ──────────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", ["bob", "bob-native", "native-bob"])
def test_bob_spellings_canonicalize_to_the_native_harness(spelling: str) -> None:
    from omnigent.harness_aliases import (
        canonicalize_harness,
        is_native_harness,
        native_terminal_name,
    )

    assert canonicalize_harness(spelling) == "bob-native"
    assert is_native_harness(spelling)
    assert native_terminal_name(spelling) == "bob"


def test_bob_native_agent_identity() -> None:
    from omnigent._wrapper_labels import BOB_NATIVE_WRAPPER_VALUE
    from omnigent.native.native_coding_agents import (
        native_coding_agent_for_harness,
        native_coding_agent_for_wrapper_label,
    )

    agent = native_coding_agent_for_harness("bob-native")
    assert agent is not None
    assert (agent.key, agent.agent_name, agent.terminal_name) == ("bob", "bob-native-ui", "bob")
    assert agent.wrapper_label == BOB_NATIVE_WRAPPER_VALUE == "bob-native-ui"
    assert native_coding_agent_for_wrapper_label("bob-native-ui") is agent
    assert agent.subagent_wrapper_label is None


def test_bob_native_capabilities_advertise_only_what_is_supported() -> None:
    from omnigent.harness_capabilities import (
        EffortFamily,
        Elicitation,
        ForkHistory,
        IntegrationMode,
        Resume,
    )
    from omnigent.harness_plugins import harness_capabilities

    caps = harness_capabilities()["bob-native"]
    assert caps.integration_mode is IntegrationMode.NATIVE_TUI
    # No Omnigent approval cards / policy integration: Bob's own dialog gates tools.
    assert caps.elicitation is Elicitation.NONE
    assert caps.effort is EffortFamily.NONE
    assert caps.fork_history is ForkHistory.NONE
    assert caps.resume is Resume.WARM_REATTACH
    assert caps.subagents is False
    assert caps.streaming is False


# ── install / readiness ─────────────────────────────────────────────────────


def test_bob_install_spec_declares_the_2x_floor_and_ibm_installer() -> None:
    from omnigent.onboarding.harness_install import required_cli_for_harness

    for spelling in ("bob-native", "native-bob"):
        spec = required_cli_for_harness(spelling)
        assert spec is not None
        assert spec.binary == "bob"
        assert spec.package is None  # not on the npm registry; IBM's script installs it
        assert spec.min_version == "2.0.0"
        assert spec.install_hint == "curl -fsSL https://bob.ibm.com/download/bobshell.sh | bash"
        # Bob signs in from its own TUI: no Omnigent-driven login command.
        assert spec.login_args is None


def test_bob_readiness_gates_on_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.onboarding import harness_readiness

    monkeypatch.setattr(harness_readiness, "_installer_only_availability", lambda key: key == "x")
    assert harness_readiness.harness_is_configured("bob-native") is False
    monkeypatch.setattr(
        harness_readiness, "_installer_only_availability", lambda key: key == "bob"
    )
    assert harness_readiness.harness_is_configured("bob-native") is True
    assert harness_readiness.harness_is_configured("native-bob") is True


@pytest.mark.parametrize(
    ("version_output", "satisfied"),
    [("2.0.5\ncommit: 2dc180906\n", True), ("2.0.0\n", True), ("1.0.6\n", False)],
)
def test_bob_version_floor_parses_bob_version_output(
    monkeypatch: pytest.MonkeyPatch, version_output: str, satisfied: bool
) -> None:
    from omnigent.onboarding import harness_install

    spec = harness_install.required_cli_for_harness("bob-native")
    assert spec is not None
    monkeypatch.setattr(
        harness_install,
        "_harness_cli_version_string",
        lambda _spec, _binary, _timeout: harness_install._parse_harness_cli_version(
            version_output
        ),
    )
    assert harness_install._harness_cli_version_satisfies(spec, "/usr/bin/bob") is satisfied


def test_bob_setup_steps_are_in_the_harness_catalog() -> None:
    from omnigent.harness_plugins import harness_catalog, harness_setup_steps_by_spelling

    steps = harness_setup_steps_by_spelling()["bob-native"]
    assert steps and steps[0]["kind"] == "install"
    # Like the other native TUIs, Bob reaches the picker through its seeded
    # bob-native-ui agent, not as a brain-harness row for bundle agents.
    assert "bob-native" not in {row["id"] for row in harness_catalog()}


# ── built-in seeding ────────────────────────────────────────────────────────


def test_materialized_agent_spec_is_terminal_first(tmp_path: Path) -> None:
    spec_path = bob_main._materialize_bob_agent_spec(tmp_path)
    raw = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert raw["name"] == "bob-native-ui"
    assert raw["executor"] == {"harness": "bob-native"}
    assert raw["os_env"]["type"] == "caller_process"
    assert "auto-approve" not in spec_path.read_text(encoding="utf-8")


def test_bob_is_seeded_as_a_builtin_native_bundle() -> None:
    from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS

    assert "bob-native-ui" in {agent.agent_name for agent in NATIVE_CODING_AGENTS}


# ── argv / env allowlisting ─────────────────────────────────────────────────


def test_default_argv_is_bare_bob_chat() -> None:
    assert build_bob_chat_argv([]) == ["chat"]


@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "plan"],
        ["--mode=ask"],
        ["--resume"],
        ["--resume", "latest"],
        ["-r", "1f0c-task"],
        ["--trust", "--accept-license"],
        ["--disable-mcp", "--disable-subagents", "--disable-tool-groups", "execute,mcp"],
        ["--max-turns", "3", "--max-cost", "0.5", "--log-level", "warn"],
        ["--instance-id", "my-project", "--team-id", "t1"],
    ],
)
def test_documented_bob_chat_options_pass_through(args: list[str]) -> None:
    assert build_bob_chat_argv(args) == ["chat", *args]


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--auto-approve"], "--auto-approve"),
        (["--workspace", "/elsewhere"], "workspace"),
        (["-w", "/elsewhere"], "-w"),
        (["--model", "x"], "no model flag"),
        (["--yolo"], "Unsupported"),
        (["hello"], "Unsupported"),
        (["--mode"], "requires a value"),
        (["--mode", "--trust"], "requires a value"),
        (["--trust=yes"], "does not take a value"),
        (["--mode="], "empty value"),
        (["--resume="], "empty value"),
        (["-r="], "empty value"),
        (["--team-id="], "empty value"),
    ],
)
def test_bob_chat_args_outside_the_allowlist_are_rejected(args: list[str], needle: str) -> None:
    with pytest.raises(BobLaunchArgsError, match=needle):
        validate_bob_chat_args(args)


def test_terminal_env_is_allowlisted_and_drops_secrets() -> None:
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "NODE_EXTRA_CA_CERTS": "/etc/ca.pem",
        "HTTPS_PROXY": "http://proxy:8080",
        "BOB_API_KEY": "should-not-be-copied",
        "BOBSHELL_API_KEY": "should-not-be-copied",
        "OPENAI_API_KEY": "should-not-be-copied",
        "RUNNER_AUTH_TOKEN": "should-not-be-copied",
    }
    env = bob_bridge.build_bob_native_terminal_env(source)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "NODE_EXTRA_CA_CERTS": "/etc/ca.pem",
        "HTTPS_PROXY": "http://proxy:8080",
    }


def test_spawn_env_carries_only_the_bridge_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOB_API_KEY", "should-not-be-copied")
    monkeypatch.setenv("BOBSHELL_API_KEY", "should-not-be-copied")
    env = bob_bridge.build_bob_native_spawn_env("conv_bob_env")
    assert set(env) == {bob_bridge.BRIDGE_DIR_ENV_VAR}
    assert "should-not-be-copied" not in json.dumps(env)


def test_cli_rejects_unsupported_args_before_starting_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    import omnigent.cli as cli_module

    def _no_backend(*args: object, **kwargs: object) -> str:
        raise AssertionError("backend must not start for rejected Bob args")

    monkeypatch.setattr(cli_module, "_ensure_backend", _no_backend)
    result = CliRunner().invoke(cli_module.cli, ["bob", "--", "--auto-approve"])
    assert result.exit_code != 0
    assert "--auto-approve" in result.output


def test_run_harness_dispatch_routes_bob_to_the_native_wrapper() -> None:
    from omnigent.cli import _NATIVE_TERMINAL_DISPATCH_SPECS

    spec = _NATIVE_TERMINAL_DISPATCH_SPECS["bob"]
    assert (spec.module, spec.function) == ("omnigent.harnesses.bob_native.main", "run_bob_native")
    # A --model reaches the argv allowlist only when given explicitly, and is rejected there.
    assert spec.model_strategy == "explicit_passthrough"


def test_runner_launch_uses_validated_bob_chat_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.runner.native import orchestration

    launched: dict[str, Any] = {}

    async def _launch_config(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            workspace=str(tmp_path),
            terminal_launch_args=["--mode", "plan"],
            model_override=None,
            reasoning_effort=None,
        )

    class _Registry:
        terminal_registry = None

        async def launch_required_terminal(self, **kwargs: Any) -> SimpleNamespace:
            launched.update(kwargs)
            return SimpleNamespace(id="terminal_bob_main")

    monkeypatch.setattr(orchestration, "_pi_native_launch_config", _launch_config)
    monkeypatch.setattr(bob_main, "resolve_bob_executable", lambda: "/usr/local/bin/bob")
    monkeypatch.setattr(orchestration, "session_resource_view_to_dict", lambda view: {})
    asyncio.run(
        orchestration._auto_create_bob_terminal(
            "conv_bob_launch",
            cast("SessionResourceRegistry", _Registry()),
            lambda *_: None,
            server_client=None,
        )
    )
    spec = launched["spec"]
    assert launched["terminal_name"] == "bob"
    assert launched["resource_role"] == "bob-native"
    assert spec.command == "/usr/local/bin/bob"
    assert spec.args == ["chat", "--mode", "plan"]
    # The pane gets an allowlisted env, not the runner's (no ambient secrets).
    assert spec.inherit_env is False
    assert "PATH" in spec.env
    assert set(spec.env) <= set(bob_bridge._CHILD_ENV_ALLOWLIST)
    assert spec.os_env.cwd == str(tmp_path.resolve())


def test_runner_launch_rejects_persisted_auto_approve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.runner.native import orchestration

    async def _launch_config(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            workspace=str(tmp_path),
            terminal_launch_args=["--auto-approve"],
            model_override=None,
            reasoning_effort=None,
        )

    monkeypatch.setattr(orchestration, "_pi_native_launch_config", _launch_config)
    with pytest.raises(RuntimeError, match="auto-approve"):
        asyncio.run(
            orchestration._auto_create_bob_terminal(
                "conv_bob_bad",
                cast("SessionResourceRegistry", SimpleNamespace()),
                lambda *_: None,
                server_client=None,
            )
        )


# ── input readiness ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pane", "state"),
    [
        (_READY_PANE, BobPaneState.READY),
        (_MID_TURN_PANE, BobPaneState.READY),
        (_TRUST_PANE, BobPaneState.DIALOG),
        (_APPROVAL_PANE, BobPaneState.DIALOG),
        (_SIGN_IN_PANE, BobPaneState.STARTING),
        ("", BobPaneState.STARTING),
        (None, BobPaneState.STARTING),
    ],
)
def test_classify_bob_pane(pane: str | None, state: BobPaneState) -> None:
    assert classify_bob_pane(pane) is state


def test_user_message_echo_is_not_mistaken_for_the_composer() -> None:
    # A ``❯`` prompt echo not directly under a rule must not read as ready.
    assert classify_bob_pane(" (i) note\n ❯ an earlier prompt\n") is BobPaneState.STARTING


def test_input_ready_probe_reports_composer_only() -> None:
    def _instance(pane: str) -> TerminalInstance:
        return cast("TerminalInstance", SimpleNamespace(last_pane_text=lambda: pane))

    assert bob_bridge.native_input_ready("conv", _instance(_READY_PANE)) is True
    assert bob_bridge.native_input_ready("conv", _instance(_TRUST_PANE)) is False


_INPUT_VERBS = frozenset({"paste-buffer", "send-keys"})


def _input_calls(pane: _FakePane) -> list[tuple[str, ...]]:
    """tmux calls that deliver input to Bob (staging a buffer delivers none)."""
    return [call for call in pane.calls if call[0] in _INPUT_VERBS]


class _FakePane:
    """Records tmux calls and serves scripted captures for the bridge."""

    def __init__(self, panes: list[str]) -> None:
        self.panes = panes
        self.calls: list[tuple[str, ...]] = []

    def capture(self, socket_path: str, target: str) -> str:
        return self.panes.pop(0) if len(self.panes) > 1 else self.panes[0]

    def run(self, socket_path: str, *args: str) -> None:
        self.calls.append(args)


@pytest.fixture
def fake_pane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[_FakePane, Path]:
    pane = _FakePane([_READY_PANE])
    bob_bridge.write_tmux_target(tmp_path, socket_path=tmp_path / "sock", tmux_target="bob:0")
    monkeypatch.setattr(bob_bridge, "_session_alive", lambda *_: True)
    monkeypatch.setattr(bob_bridge, "_capture_pane", pane.capture)
    monkeypatch.setattr(bob_bridge, "_run_tmux", pane.run)
    monkeypatch.setattr(bob_bridge, "_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(bob_bridge, "_PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr(bob_bridge, "_PASTE_COMMIT_TIMEOUT_S", 0.0)
    return pane, tmp_path


def test_inject_pastes_then_submits_with_one_enter(fake_pane: tuple[_FakePane, Path]) -> None:
    pane, bridge_dir = fake_pane
    bob_bridge.inject_user_message(bridge_dir, content="line one\nline two", timeout_s=0.1)
    verbs = [call[0] for call in pane.calls]
    assert verbs == ["load-buffer", "paste-buffer", "send-keys"]
    assert pane.calls[-1][-1] == "Enter"
    assert "-p" in pane.calls[1]  # bracketed paste keeps interior newlines as data


@pytest.mark.parametrize("dialog", [_TRUST_PANE, _APPROVAL_PANE])
def test_inject_never_types_into_a_bob_dialog(
    fake_pane: tuple[_FakePane, Path], dialog: str
) -> None:
    pane, bridge_dir = fake_pane
    pane.panes = [dialog]
    with pytest.raises(RuntimeError, match="waiting on a prompt"):
        bob_bridge.inject_user_message(bridge_dir, content="hello", timeout_s=0.05)
    assert _input_calls(pane) == []
    assert pane.calls[-1][0] == "delete-buffer"


def test_inject_refuses_while_signing_in(fake_pane: tuple[_FakePane, Path]) -> None:
    pane, bridge_dir = fake_pane
    pane.panes = [_SIGN_IN_PANE]
    with pytest.raises(RuntimeError, match="not ready"):
        bob_bridge.inject_user_message(bridge_dir, content="hello", timeout_s=0.05)
    assert _input_calls(pane) == []
    assert pane.calls[-1][0] == "delete-buffer"


def test_inject_withholds_enter_when_a_dialog_opens_mid_paste(
    fake_pane: tuple[_FakePane, Path],
) -> None:
    pane, bridge_dir = fake_pane
    # Composer wait and pre-paste check see READY; a tool approval opens after.
    pane.panes = [_READY_PANE, _READY_PANE, _APPROVAL_PANE]
    with pytest.raises(RuntimeError, match="waiting on a prompt"):
        bob_bridge.inject_user_message(bridge_dir, content="hello", timeout_s=0.05)
    assert [call[0] for call in _input_calls(pane)] == ["paste-buffer"]


def test_interrupt_sends_escape_only_outside_dialogs(fake_pane: tuple[_FakePane, Path]) -> None:
    pane, bridge_dir = fake_pane
    bob_bridge.inject_interrupt(bridge_dir, timeout_s=0.1)
    assert pane.calls == [("send-keys", "-t", "bob:0", "Escape")]
    pane.calls.clear()
    pane.panes = [_TRUST_PANE]  # Escape here would exit Bob
    with pytest.raises(RuntimeError):
        bob_bridge.inject_interrupt(bridge_dir, timeout_s=0.1)
    assert pane.calls == []


def test_inject_fails_fast_when_bob_exited(
    fake_pane: tuple[_FakePane, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _pane, bridge_dir = fake_pane
    monkeypatch.setattr(bob_bridge, "_session_alive", lambda *_: False)
    with pytest.raises(RuntimeError, match="no longer running"):
        bob_bridge.inject_user_message(bridge_dir, content="hello", timeout_s=0.1)


def test_paste_payload_drops_escape_bytes() -> None:
    assert bob_bridge._paste_payload_bytes("a\x1b[201~b\nc") == b"a[201~b\rc"


# ── executor ────────────────────────────────────────────────────────────────


def test_executor_surfaces_a_dialog_refusal_as_a_turn_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.inner import bob_native_executor
    from omnigent.inner.executor import ExecutorError

    def _refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Bob is waiting on a prompt in its terminal")

    monkeypatch.setattr(bob_native_executor, "inject_user_message", _refuse)
    executor = bob_native_executor.BobNativeExecutor(bridge_dir=tmp_path)

    async def _collect() -> list[object]:
        messages = [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
        return [event async for event in executor.run_turn(messages, [], "")]

    events = asyncio.run(_collect())
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert "waiting on a prompt" in events[0].message


# ── resume ──────────────────────────────────────────────────────────────────


def _daemon_client(handler: Any) -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _open(*args: object, **kwargs: object) -> Any:
        async with httpx.AsyncClient(
            base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
        ) as client:
            yield client

    return _open


def test_resume_reattaches_a_running_bob_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_bob":
            return httpx.Response(200, json={"labels": {"omnigent.wrapper": "bob-native-ui"}})
        if request.url.path.endswith("/resources/terminals/terminal_bob_main"):
            return httpx.Response(
                200,
                json={
                    "id": "terminal_bob_main",
                    "metadata": {"running": True, "tmux_socket": "/tmp/s", "tmux_target": "t"},
                },
            )
        return httpx.Response(500, json={"error": "unexpected request"})

    monkeypatch.setattr(bob_main, "open_daemon_client", _daemon_client(_handler))
    prepared = asyncio.run(
        bob_main._prepare_bob_terminal_via_daemon(
            base_url="http://omnigent.test",
            headers={},
            session_id="conv_bob",
            session_bundle=None,
            bob_args=(),
            host_id="host",
            workspace="/tmp",
        )
    )
    assert prepared.reattached is True
    assert prepared.terminal_id == "terminal_bob_main"


def test_resume_rejects_a_session_from_another_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labels": {"omnigent.wrapper": "goose-native-ui"}})

    monkeypatch.setattr(bob_main, "open_daemon_client", _daemon_client(_handler))
    with pytest.raises(click.ClickException, match="not a bob-native session"):
        asyncio.run(
            bob_main._prepare_bob_terminal_via_daemon(
                base_url="http://omnigent.test",
                headers={},
                session_id="conv_goose",
                session_bundle=None,
                bob_args=(),
                host_id="host",
                workspace="/tmp",
            )
        )


# ── Codex review follow-ups: fail-closed input, model override, path ───────


def test_inject_pastes_nothing_when_a_dialog_opens_before_the_paste(
    fake_pane: tuple[_FakePane, Path],
) -> None:
    pane, bridge_dir = fake_pane
    # The composer wait sees READY, then the last pre-paste capture sees a dialog.
    pane.panes = [_READY_PANE, _APPROVAL_PANE]
    with pytest.raises(RuntimeError, match="waiting on a prompt"):
        bob_bridge.inject_user_message(bridge_dir, content="hello", timeout_s=0.05)
    assert [call[0] for call in pane.calls] == ["load-buffer", "delete-buffer"]


def test_interrupt_is_refused_on_the_startup_or_sign_in_screen(
    fake_pane: tuple[_FakePane, Path],
) -> None:
    pane, bridge_dir = fake_pane
    pane.panes = [_SIGN_IN_PANE]  # "Press ESC ... to exit": Escape would quit Bob
    with pytest.raises(RuntimeError, match="not ready"):
        bob_bridge.inject_interrupt(bridge_dir, timeout_s=0.1)
    assert pane.calls == []


@pytest.mark.parametrize("spelling", ["bob", "bob-native", "native-bob"])
def test_bob_rejects_model_overrides(spelling: str) -> None:
    from omnigent.models.model_override import harness_supports_model_override

    assert harness_supports_model_override(spelling) is False
    assert harness_supports_model_override("goose-native") is True


def test_configured_bob_path_survives_the_daemon_hop() -> None:
    from omnigent.host import connect

    assert "OMNIGENT_BOB_PATH" in connect._RUNNER_ENV_ALLOWLIST


def test_bench_declares_no_model_or_omnigent_tool_support_for_bob() -> None:
    from tests.harness_bench.manifest import _declared_from_capabilities
    from tests.harness_bench.verdict import Verdict

    declared = _declared_from_capabilities("bob-native")
    assert declared["model_override"] is Verdict.UNSUPPORTED
    assert declared["tool_calling"] is Verdict.UNSUPPORTED
    assert declared["policy_deny"] is Verdict.UNSUPPORTED
    # Other natives keep their model-override declaration.
    assert _declared_from_capabilities("goose-native")["model_override"] is Verdict.SUPPORTED


@pytest.mark.parametrize("override", [{"model_override": "x"}, {"reasoning_effort": "high"}])
def test_runner_launch_refuses_a_persisted_model_or_effort_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: dict[str, str]
) -> None:
    from omnigent.runner.native import orchestration

    async def _launch_config(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            workspace=str(tmp_path),
            terminal_launch_args=[],
            model_override=override.get("model_override"),
            reasoning_effort=override.get("reasoning_effort"),
        )

    monkeypatch.setattr(orchestration, "_pi_native_launch_config", _launch_config)
    with pytest.raises(RuntimeError, match="picks its model inside the TUI"):
        asyncio.run(
            orchestration._auto_create_bob_terminal(
                "conv_bob_model",
                cast("SessionResourceRegistry", SimpleNamespace()),
                lambda *_: None,
                server_client=None,
            )
        )


@pytest.mark.parametrize("harness", ["bob", "bob-native", "native-bob"])
def test_run_harness_rejects_bob_model_before_starting_a_backend(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    from click.testing import CliRunner

    import omnigent.cli as cli_module

    def _no_backend(*args: object, **kwargs: object) -> str:
        raise AssertionError("backend must not start for a rejected Bob --model")

    monkeypatch.setattr(cli_module, "_ensure_backend", _no_backend)
    result = CliRunner().invoke(cli_module.cli, ["run", "--harness", harness, "--model", "x"])
    assert result.exit_code != 0
    assert not isinstance(result.exception, AssertionError), result.output
    assert "Bob Shell 2.x has no model flag" in result.output


def test_run_help_lists_bob_as_a_harness_choice() -> None:
    from click.testing import CliRunner

    import omnigent.cli as cli_module

    result = CliRunner().invoke(cli_module.cli, ["run", "--help"], terminal_width=200)
    assert result.exit_code == 0
    assert "'bob' (alias for 'bob-native')" in " ".join(result.output.split())
