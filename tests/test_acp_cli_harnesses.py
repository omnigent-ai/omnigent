"""The declarative builtin ACP CLI harness catalog (omnigent/acp_cli_harnesses.py).

Two halves:

- Mechanism tests drive a fake catalog row through the shared spawn-env builder
  and the runner dispatch, so the machinery stays covered even while the
  catalog is small.
- Per-row tests parametrize over the real catalog and assert every derived
  registration a row relies on, so adding a row is one dict entry and this
  module proves the wiring end to end.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
from pathlib import Path

import pytest

from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES, HERMES_MIN_VERSION, AcpCliHarness
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_install_spec import HarnessInstallSpec
from omnigent.harness_plugins import (
    harness_capabilities,
    harness_install_keys,
    harness_labels,
    harness_modules,
    install_specs,
    valid_harnesses,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.onboarding.harness_install import ui_setup_steps
from omnigent.runtime.workflow import _build_acp_cli_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec, LLMConfig

_FAKE_ROW = AcpCliHarness(
    install=HarnessInstallSpec(
        "Fake CLI",
        "fakecli",
        None,
        login_args=("login", "--device"),
        install_hint="curl -fsSL https://fake.example/install.sh | bash",
    ),
    args=("agent", "stdio"),
    aliases=("fake-cli",),
)


def _spec(
    harness: str,
    os_env: OSEnvSpec | None = None,
    *,
    permission_mode: str | None = None,
    model: str | None = None,
    skills_filter: str | list[str] = "all",
) -> AgentSpec:
    config: dict[str, object] = {"harness": harness}
    if permission_mode is not None:
        config["permission_mode"] = permission_mode
    return AgentSpec(
        spec_version=1,
        name=f"test-{harness}",
        instructions="Test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model),
        llm=LLMConfig(model=model) if model is not None else None,
        skills_filter=skills_filter,
        os_env=os_env,
    )


# ---------------------------------------------------------------------------
# Mechanism (fake row)
# ---------------------------------------------------------------------------


def test_spawn_env_forwards_cwd_sandbox_and_quotes_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared builder forwards session cwd + os_env and shell-quotes argv0.

    These are exactly the fields a hand-rolled thin wrap historically dropped
    (a spec sandbox silently ignored, the session folder falling back to the
    runner workspace), so the catalog path must prove them.
    """
    monkeypatch.setitem(ACP_CLI_HARNESSES, "fakecli", _FAKE_ROW)
    monkeypatch.delenv("OMNIGENT_FAKECLI_PATH", raising=False)
    # A resolved binary path containing a space must survive the round-trip
    # through the shlex-split command string.
    monkeypatch.setattr(
        "omnigent._platform.resolve_cli_binary",
        lambda name, **k: "/opt/fake cli/fakecli" if name == "fakecli" else None,
    )
    os_env = OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="omnibox"),
        fork=False,
    )
    env = _build_acp_cli_spawn_env(
        _spec("fakecli", os_env=os_env), harness="fakecli", cwd=Path("/work/space")
    )

    assert shlex.split(env["HARNESS_ACP_COMMAND"]) == [
        "/opt/fake cli/fakecli",
        "agent",
        "stdio",
    ]
    assert env["HARNESS_ACP_NAME"] == "Fake CLI"
    assert env["HARNESS_ACP_CWD"] == "/work/space"
    assert json.loads(env["HARNESS_ACP_OS_ENV"]) == dataclasses.asdict(os_env)
    # Rows own their model selection: no model var may ride along.
    assert "HARNESS_ACP_MODEL" not in env


def test_spawn_env_honors_path_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ACP_CLI_HARNESSES, "fakecli", _FAKE_ROW)
    monkeypatch.setenv("OMNIGENT_FAKECLI_PATH", "/custom/fakecli")
    env = _build_acp_cli_spawn_env(_spec("fakecli"), harness="fakecli")
    assert shlex.split(env["HARNESS_ACP_COMMAND"])[0] == "/custom/fakecli"
    # No session cwd and no os_env on the spec: neither var may be emitted, so
    # the wrap falls back to OMNIGENT_RUNNER_WORKSPACE / its own default.
    assert "HARNESS_ACP_CWD" not in env
    assert "HARNESS_ACP_OS_ENV" not in env


def test_runner_dispatch_routes_catalog_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_spawn_env_from_spec picks up any catalog row without new wiring."""
    from omnigent.runner.app import _build_spawn_env_from_spec

    monkeypatch.setitem(ACP_CLI_HARNESSES, "fakecli", _FAKE_ROW)
    monkeypatch.delenv("OMNIGENT_FAKECLI_PATH", raising=False)
    env = _build_spawn_env_from_spec(_spec("fakecli"), "fakecli")
    assert env is not None
    assert env["HARNESS_ACP_NAME"] == "Fake CLI"
    assert shlex.split(env["HARNESS_ACP_COMMAND"])[-2:] == ["agent", "stdio"]


def test_runner_dispatch_passes_session_context_to_hermes_acp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent.runner.app import _build_spawn_env_from_spec

    bridge_dir = tmp_path / "bridge"
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://server.example")
    monkeypatch.setattr(
        "omnigent.hermes_native_bridge.bridge_dir_for_session_id",
        lambda session_id: bridge_dir if session_id == "conv_runner" else None,
    )
    monkeypatch.setattr(
        "omnigent.hermes_native_bridge.write_policy_hook_config",
        lambda path, _server_url, _session_id, **_kwargs: path / "home",
    )

    env = _build_spawn_env_from_spec(
        _spec("hermes-acp"),
        "hermes-acp",
        session_id="conv_runner",
    )

    assert env is not None
    assert env["HERMES_HOME"] == str(bridge_dir / "home")


def test_fake_row_login_command() -> None:
    assert _FAKE_ROW.login_command == "fakecli login --device"
    assert _FAKE_ROW.label == "Fake CLI"
    assert _FAKE_ROW.binary == "fakecli"


def test_hermes_acp_is_a_catalog_row_without_replacing_batch_hermes() -> None:
    row = ACP_CLI_HARNESSES["hermes-acp"]

    assert row.args == ("acp", "--accept-hooks")
    assert row.binary == "hermes"
    assert HERMES_MIN_VERSION == "0.19.1"
    assert row.install.min_version == HERMES_MIN_VERSION
    assert row.policy_hook_authoritative is True
    assert harness_modules()["hermes-acp"] == "omnigent.inner.acp_harness"
    assert harness_modules()["hermes"] == "omnigent.inner.hermes_harness"


def test_hermes_acp_spawn_env_prepares_policy_home_and_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str, str, str | None, bool]] = []
    bridge_dir = tmp_path / "bridge"
    hermes_home = bridge_dir / "hermes_home"

    monkeypatch.setenv("RUNNER_SERVER_URL", "https://server.example")
    monkeypatch.setenv("OMNIGENT_HERMES_MANAGED_DIR", "/etc/omnigent/hermes-managed")
    monkeypatch.setattr(
        "omnigent.hermes_native_bridge.bridge_dir_for_session_id",
        lambda session_id: bridge_dir,
    )

    def _write_policy_hook_config(
        path: Path,
        server_url: str,
        session_id: str,
        *,
        model: str | None = None,
        include_omnigent_mcp: bool = True,
    ) -> Path:
        calls.append((path, server_url, session_id, model, include_omnigent_mcp))
        return hermes_home

    monkeypatch.setattr(
        "omnigent.hermes_native_bridge.write_policy_hook_config",
        _write_policy_hook_config,
    )

    env = _build_acp_cli_spawn_env(
        _spec(
            "hermes-acp",
            os_env=OSEnvSpec(),
            model="hermes-model",
        ),
        harness="hermes-acp",
        session_id="conv_123",
    )

    assert calls == [(bridge_dir, "https://server.example", "conv_123", "hermes-model", False)]
    assert env["HERMES_HOME"] == str(hermes_home)
    assert env["HERMES_MANAGED_DIR"] == "/etc/omnigent/hermes-managed"
    assert env["HERMES_ACP_SKIP_CONFIGURED_MCP"] == "1"
    assert env["HARNESS_ACP_POLICY_HOOK_AUTHORITATIVE"] == "1"
    assert "HARNESS_ACP_SESSION_NEW_EXTRAS" not in env
    sandbox = json.loads(env["HARNESS_ACP_OS_ENV"])["sandbox"]
    assert sandbox["type"] == OSEnvSandboxSpec().type
    assert set(sandbox["env_passthrough"]) >= {
        "HERMES_HOME",
        "HERMES_MANAGED_DIR",
        "HERMES_ACP_SKIP_CONFIGURED_MCP",
        "_OMNIGENT_SERVER_URL",
        "_OMNIGENT_SESSION_ID",
    }


def test_hermes_managed_dir_reaches_the_filtered_acp_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The managed wrapper carries the policy context into the ACP child."""
    from omnigent.inner.acp_harness import _build_acp_executor

    monkeypatch.setenv("OMNIGENT_HERMES_MANAGED_DIR", "/etc/omnigent/hermes-managed")
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://server.example")
    monkeypatch.setattr(
        "omnigent.hermes_native_bridge.bridge_dir_for_session_id",
        lambda _session_id: tmp_path / "bridge",
    )
    monkeypatch.setattr(
        "omnigent.hermes_native_bridge.write_policy_hook_config",
        lambda bridge_dir, _server_url, _session_id, **_kwargs: bridge_dir / "home",
    )

    wrapper_env = _build_acp_cli_spawn_env(
        _spec("hermes-acp"),
        harness="hermes-acp",
        session_id="conv_managed",
    )
    for name, value in wrapper_env.items():
        monkeypatch.setenv(name, value)

    child_env = _build_acp_executor()._build_spawn_env()

    assert child_env["_OMNIGENT_SERVER_URL"] == "https://server.example"
    assert child_env["_OMNIGENT_SESSION_ID"] == "conv_managed"
    assert child_env["HERMES_ACP_SKIP_CONFIGURED_MCP"] == "1"


@pytest.mark.parametrize("model", ["databricks-hermes", "databricks/hermes"])
def test_hermes_acp_does_not_write_gateway_model_into_vendor_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model: str,
) -> None:
    captured: list[str | None] = []
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://server.example")
    monkeypatch.setattr(
        "omnigent.hermes_native_bridge.bridge_dir_for_session_id",
        lambda _session_id: tmp_path / "bridge",
    )

    def _write(
        bridge_dir: Path,
        _server_url: str,
        _session_id: str,
        *,
        model: str | None = None,
        include_omnigent_mcp: bool = True,
    ) -> Path:
        assert include_omnigent_mcp is False
        captured.append(model)
        return bridge_dir / "home"

    monkeypatch.setattr("omnigent.hermes_native_bridge.write_policy_hook_config", _write)

    _build_acp_cli_spawn_env(
        _spec("hermes-acp", model=model),
        harness="hermes-acp",
        session_id="conv_gateway_model",
    )

    assert captured == [None]


@pytest.mark.parametrize("skills_filter", [["safe-skill"], []])
def test_hermes_acp_rejects_restrictive_skill_filter_before_writing_home(
    monkeypatch: pytest.MonkeyPatch,
    skills_filter: list[str],
) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://server.example")
    called = False

    def _unexpected_write(*_args: object, **_kwargs: object) -> Path:
        nonlocal called
        called = True
        return Path("/unused")

    monkeypatch.setattr(
        "omnigent.hermes_native_bridge.write_policy_hook_config",
        _unexpected_write,
    )

    with pytest.raises(RuntimeError, match="does not support restrictive skills_filter"):
        _build_acp_cli_spawn_env(
            _spec("hermes-acp", skills_filter=skills_filter),
            harness="hermes-acp",
            session_id="conv_123",
        )

    assert called is False


def test_hermes_acp_spawn_env_requires_session_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)

    with pytest.raises(RuntimeError, match="session id and RUNNER_SERVER_URL"):
        _build_acp_cli_spawn_env(_spec("hermes-acp"), harness="hermes-acp")


# ---------------------------------------------------------------------------
# Per-row registration (parametrized over the real catalog)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ACP_CLI_HARNESSES))
def test_catalog_row_is_fully_registered(name: str) -> None:
    """One catalog row must yield every registration a harness needs."""
    row = ACP_CLI_HARNESSES[name]

    assert name in valid_harnesses()
    assert harness_modules()[name] == "omnigent.inner.acp_harness"
    assert harness_labels()[name] == row.label
    # Same declared profile as the generic "acp" harness they run through.
    assert harness_capabilities()[name] == harness_capabilities()["acp"]
    assert install_specs()[name] == row.install
    for spelling in (name, *row.aliases):
        assert harness_install_keys()[spelling] == name
    for alias in row.aliases:
        assert canonicalize_harness(alias) == name
    # The setup checklist must exist, and rows with a vendor login must show it.
    steps = ui_setup_steps(name)
    assert steps
    if row.login_command is not None and row.install.package is not None:
        assert any(step.command == row.login_command for step in steps)
    # `omni setup` renders a row per catalog entry and needs somewhere to point a
    # user who hasn't installed the CLI. Without one the row would say "Not
    # installed" with no way to fix it.
    assert row.install.install_hint or row.install.package


@pytest.mark.parametrize("name", sorted(ACP_CLI_HARNESSES))
def test_catalog_row_spawn_env_builds(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shared builder produces a launchable command for every real row."""
    row = ACP_CLI_HARNESSES[name]
    if name == "hermes-acp":
        monkeypatch.setenv("RUNNER_SERVER_URL", "https://server.example")
        monkeypatch.setattr(
            "omnigent.hermes_native_bridge.bridge_dir_for_session_id",
            lambda _session_id: tmp_path / "bridge",
        )
        monkeypatch.setattr(
            "omnigent.hermes_native_bridge.write_policy_hook_config",
            lambda bridge_dir, _server_url, _session_id, **_kwargs: bridge_dir / "home",
        )
    env = _build_acp_cli_spawn_env(
        _spec(name),
        harness=name,
        session_id="conv_catalog",
    )
    argv = shlex.split(env["HARNESS_ACP_COMMAND"])
    assert argv[0], "argv[0] must resolve to a non-empty binary"
    if row.args:
        assert argv[-len(row.args) :] == list(row.args)
    assert env["HARNESS_ACP_NAME"] == row.label


# ---------------------------------------------------------------------------
# `omni setup` drill-in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ACP_CLI_HARNESSES))
def test_setup_drill_in_names_install_and_login(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Selecting a builtin ACP row in ``omni setup`` names how to install and sign in.

    These rows own their auth and install out-of-band, so the drill-in is the only
    place a user learns the two commands. Without it the row is a dead end — which
    is what shipped before: ``grok`` was addressable via ``--harness grok`` but
    absent from setup entirely, making a builtin *less* discoverable than a
    user-configured ``acp:`` entry.

    **What breaks if this fails**: a user picks the harness in setup and is told
    nothing about how to make it work.
    """
    from omnigent import cli_config

    row = ACP_CLI_HARNESSES[name]
    # Force the "not installed" branch so the install hint has to be shown.
    monkeypatch.setattr("omnigent._platform.resolve_cli_binary", lambda _binary: None)
    cli_config._show_acp_cli_harness(name)

    out = capsys.readouterr().out
    assert row.label in out
    assert (row.install.install_hint or row.binary) in out
    if row.login_command:
        assert row.login_command in out
    # Tells the user how to actually launch it.
    assert f"--harness {name}" in out


def test_setup_drill_in_ignores_unknown_row() -> None:
    """A stale key (concurrent config change) must not raise."""
    from omnigent import cli_config

    cli_config._show_acp_cli_harness("definitely-not-a-row")


def test_spawn_env_forwards_permission_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A builtin row honors ``permission_mode`` too, not just configured agents.

    Devin and Grok Build are builtin rows, so they take this builder rather than
    ``_build_acp_spawn_env``. Missing it here would leave the option working for
    a self-registered ``acp:devin`` but silently inert for the builtin ``devin``
    the picker offers.
    """
    monkeypatch.setitem(ACP_CLI_HARNESSES, "fakecli", _FAKE_ROW)
    monkeypatch.setattr(
        "omnigent._platform.resolve_cli_binary", lambda _b, **k: "/usr/bin/fakecli"
    )

    env = _build_acp_cli_spawn_env(
        _spec("fakecli", permission_mode="bypassPermissions"), harness="fakecli"
    )
    assert env["HARNESS_ACP_PERMISSION_MODE"] == "bypassPermissions"
    # Absent -> unset, so the wrap keeps its prompting default.
    assert "HARNESS_ACP_PERMISSION_MODE" not in _build_acp_cli_spawn_env(
        _spec("fakecli"), harness="fakecli"
    )
