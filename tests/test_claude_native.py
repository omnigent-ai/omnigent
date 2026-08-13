"""Tests for the native Claude Code terminal wrapper helpers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import importlib.metadata
import io
import json
import os
import re
import shlex
import ssl
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest
import websockets
import yaml
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from omnigent import claude_native
from omnigent._runner_startup import RunnerStartupProgress
from omnigent._startup_profile import StartupProfiler
from omnigent._terminal_picker_theme import PICKER_ACCENT, PICKER_MUTED
from omnigent.databricks_model_discovery import DatabricksClaudeCatalog
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.runtime import tool_result_replay as trc
from omnigent.spec import load_omnigent_yaml
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_DETACHED,
    WS_CLOSE_TERMINAL_NOT_FOUND,
)
from tests._image_fixtures import (
    _TINY_CMYK_JPEG_BASE64,
    _TINY_GIF_BASE64,
    _TINY_JPEG_BASE64,
    _TINY_PNG_BASE64,
    _TINY_PROGRESSIVE_GRAY_JPEG_BASE64,
    _TINY_PROGRESSIVE_JPEG_BASE64,
    _TINY_WEBP_BASE64,
)
from tests._image_fixtures import (
    mcp_call_output as _mcp_call_output,
)


@pytest.fixture(autouse=True)
def _stub_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.model_catalog.resolve_catalog_model",
        lambda provider_name, *, family, **kwargs: SimpleNamespace(
            model_id=f"catalog-{provider_name}-{family}-default"
        ),
    )


def _test_bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bridge_root = tmp_path / "claude-native"
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", bridge_root)
    return bridge_root / "session"


def _load_invocation_settings(args: list[str]) -> dict[str, Any]:
    settings_path = Path(args[args.index("--settings") + 1])
    return json.loads(settings_path.read_text(encoding="utf-8"))


def test_claude_terminal_request_pins_launch_cwd(tmp_path, monkeypatch) -> None:
    """
    The terminal launch body pins ``cwd`` to the user's launch dir.

    Regression for the interaction where the wrapper runs on the same
    host as the runner subprocess, so ``Path.cwd()`` here equals the
    runner's ``RUNNER_WORKSPACE`` env. If the wrapper omits ``cwd`` /
    sends the placeholder ``"."``, the runner falls through to
    ``compute_default_env_root`` which (under
    ``per_session_workspace=True``) returns
    ``<workspace>/<conversation_id>`` -- a directory the runner never
    creates. tmux then silently launches in ``$HOME``.

    Test also guards: ``bridge_inject_dir`` stays a boolean opt-in
    (sending the path string would resurrect a directory-traversal
    vector the runner now ignores) and the experimental Claude
    Channels flag is not snuck in.
    """
    monkeypatch.chdir(tmp_path)
    bridge_dir = _test_bridge_dir(tmp_path, monkeypatch)
    body = claude_native._claude_terminal_request(
        ("--resume", "claude-session", "-p", "hi"),
        command="claude",
        bridge_dir=bridge_dir,
    )

    assert body["terminal"] == "claude"
    assert body["session_key"] == "main"
    # Boolean opt-in only — sending the path string would resurrect the
    # directory-traversal vector the runner now ignores.
    assert body["bridge_inject_dir"] is True
    spec = body["spec"]
    assert spec["command"] == "claude"
    assert spec["env"] == {
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    }
    assert spec["os_env_type"] == "caller_process"
    # Claude Code emits long interactive transcripts; this value is
    # the tmux history limit for the native terminal.
    assert spec["scrollback"] == 100000
    # The wrapper pins cwd explicitly to the launch directory; assert
    # the literal value, not just "some path is set", so a regression
    # back to a placeholder like ``"."`` is caught.
    assert spec["cwd"] == str(tmp_path.resolve())
    assert "log_file" not in spec

    args = spec["args"]
    assert args[:4] == ["--resume", "claude-session", "-p", "hi"]
    mcp_index = args.index("--mcp-config")
    mcp_config = json.loads(args[mcp_index + 1])
    assert mcp_config["mcpServers"]["omnigent"]["args"] == [
        "-I",
        "-m",
        "omnigent.claude_native_bridge",
        "serve-mcp",
        "--bridge-dir",
        str(bridge_dir),
    ]
    # The experimental Claude Channels flag is blocked at the org
    # policy layer — the wrapper must not pass it. Web-UI input now
    # goes through tmux send-keys.
    assert "--dangerously-load-development-channels" not in args
    settings = _load_invocation_settings(args)
    assert sorted(settings["hooks"]) == [
        "MessageDisplay",
        "PostToolUse",
        "PreCompact",
        "SessionStart",
        "Stop",
        "StopFailure",
        "TaskCompleted",
        "TaskCreated",
        "UserPromptSubmit",
    ]


def test_claude_terminal_request_default_launch_is_unwrapped(tmp_path, monkeypatch) -> None:
    """Without ``OMNIGENT_CLAUDE_LAUNCHER`` the command/args are unchanged."""
    monkeypatch.delenv("OMNIGENT_CLAUDE_LAUNCHER", raising=False)
    monkeypatch.chdir(tmp_path)
    body = claude_native._claude_terminal_request(
        ("--resume", "s"),
        command="claude",
        bridge_dir=_test_bridge_dir(tmp_path, monkeypatch),
    )
    spec = body["spec"]
    assert spec["command"] == "claude"
    assert spec["args"][:2] == ["--resume", "s"]


def test_claude_terminal_request_launcher_plugin_wraps(tmp_path, monkeypatch) -> None:
    """
    A registered launcher plugin rewrites the spawn command, keeping the bridge.

    Exercises the local-CLI wiring of :func:`resolve_claude_launch`: with a
    launcher plugin selected, the terminal spec runs the wrapped command
    (here ``isaac -- <augmented args>``) while the Omnigent bridge
    (``--mcp-config`` / ``--settings``) survives intact in the passed-through
    argv.
    """

    from omnigent.claude_launcher import ClaudeLauncher

    class _IsaacLauncher(ClaudeLauncher):
        def launch(self, command, args):
            return "isaac", ["--", *args]

    entry_point = SimpleNamespace(name="isaac", load=lambda: _IsaacLauncher)
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda *, group: [entry_point])
    monkeypatch.setenv("OMNIGENT_CLAUDE_LAUNCHER", "isaac")
    monkeypatch.chdir(tmp_path)
    body = claude_native._claude_terminal_request(
        ("--resume", "s"),
        command="claude",
        bridge_dir=_test_bridge_dir(tmp_path, monkeypatch),
    )
    spec = body["spec"]
    assert spec["command"] == "isaac"
    # Claude's argv is now passed through isaac after the ``--`` separator.
    assert spec["args"][0] == "--"
    assert spec["args"][1:3] == ["--resume", "s"]
    # The bridge MCP + hook injection still rides along.
    assert "--mcp-config" in spec["args"]
    assert "--settings" in spec["args"]


def test_claude_terminal_request_injects_claude_config(tmp_path, monkeypatch) -> None:
    """
    Ucode config reaches the terminal env, settings, and model argv.

    This test pins the native ``omnigent claude`` launch boundary:
    a regression that reads ucode but forgets to pass the resulting
    Databricks gateway values to the terminal resource would leave
    Claude Code on its default provider path.
    """
    config = claude_native.ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "900000",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        },
        api_key_helper="printf %s sk-sentinel-do-not-use",
        model="databricks-claude-opus-test",
    )

    body = claude_native._claude_terminal_request(
        ("--print", "hi"),
        command="claude",
        bridge_dir=_test_bridge_dir(tmp_path, monkeypatch),
        claude_config=config,
    )

    spec = body["spec"]
    assert spec["command"] == "env"
    assert spec["env"] == {
        "ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic",
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "900000",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    }
    args = spec["args"]
    assert args[:9] == [
        "-u",
        "ANTHROPIC_API_KEY",
        "-u",
        "CLAUDECODE",
        "claude",
        "--print",
        "hi",
        "--model",
        "databricks-claude-opus-test",
    ]
    settings = _load_invocation_settings(args)
    assert all("sk-sentinel-do-not-use" not in arg for arg in args)
    assert settings["apiKeyHelper"] == "printf %s sk-sentinel-do-not-use"
    assert "hooks" in settings


def test_claude_terminal_request_preserves_user_model_arg(tmp_path, monkeypatch) -> None:
    """
    User-selected Claude model wins over the ucode default.

    The ucode model is a default, not a forced override. If this
    regresses, users who pass ``--model`` would silently get the
    workspace default instead of the model they explicitly requested.
    """
    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic"},
        api_key_helper="printf token",
        model="databricks-claude-opus-test",
    )

    body = claude_native._claude_terminal_request(
        ("--model", "user-model", "--print", "hi"),
        command="claude",
        bridge_dir=_test_bridge_dir(tmp_path, monkeypatch),
        claude_config=config,
    )

    args = body["spec"]["args"]
    assert args[:9] == [
        "-u",
        "ANTHROPIC_API_KEY",
        "-u",
        "CLAUDECODE",
        "claude",
        "--model",
        "user-model",
        "--print",
        "hi",
    ]
    assert args.count("--model") == 1


def test_ucode_config_for_profile_reads_allowlisted_claude_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Profile-backed native Claude config reads only required ucode fields.

    The extra ``ANTHROPIC_AUTH_TOKEN`` in fake ucode env is deliberate:
    the native wrapper must not blindly forward arbitrary state-file
    environment values into the terminal launch body.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        agents={
            "claude": UcodeAgentState(
                model="databricks-claude-opus-test",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
                auth_refresh_interval_ms=123456,
                env={
                    "ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "must-not-leak",
                },
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )

    config = claude_native._ucode_config_for_profile("test-profile", refresh_models=False)

    assert config == claude_native.ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "123456",
            "CLAUDE_CODE_USE_GATEWAY": "1",
            "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
            # No CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS: this path launches in
            # gateway mode (CLAUDE_CODE_USE_GATEWAY=1), where Claude Code
            # negotiates the anthropic-beta set with the gateway and keeps MCP
            # tool search on (it rides on the advanced-tool-use beta).
        },
        api_key_helper="printf token",
        model="databricks-claude-opus-test",
    )


def _ucode_state_with_auth_command(auth_command: str) -> Any:
    """Build a one-agent ucode workspace state carrying *auth_command*."""
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    return UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        agents={
            "claude": UcodeAgentState(
                model="databricks-claude-opus-test",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command=auth_command,
            )
        },
    )


def test_ucode_config_pins_the_token_helper_to_the_named_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The profile the config names is the identity the pane must authenticate as.

    ucode records its own token command, and it selects the workspace however
    ucode was configured — often by host. The server's router client instead
    uses the ``kind: databricks`` provider's profile. Two profiles on one host
    are two identities, so host selection is how a pane and the router end up
    disagreeing about whether the workspace is reachable.

    The preference is not exclusive: the recorded command survives as the
    helper's last resort, for the config that names a credential-less profile.
    """
    recorded = (
        'databricks auth token --host "https://example.databricks.com" '
        "--output json | jq -r '.access_token'"
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: _ucode_state_with_auth_command(recorded),
    )

    config = claude_native._ucode_config_for_profile("agent", refresh_models=False)

    assert config is not None
    helper = config.api_key_helper
    assert helper is not None
    # The mint selects by profile; the only --host left is the quoted fallback.
    assert '--profile "agent"' in helper
    mint, _, fallback = helper.partition("eval ")
    assert "--host" not in mint
    assert fallback.startswith(shlex.quote(recorded))


def test_ucode_config_leaves_a_non_databricks_token_command_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enterprise deployment's own token command has a selector we don't know."""
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: _ucode_state_with_auth_command("corp-auth print-token --scope llm"),
    )

    config = claude_native._ucode_config_for_profile("agent", refresh_models=False)

    assert config is not None
    assert config.api_key_helper == "corp-auth print-token --scope llm"


def test_ucode_config_for_profile_sets_model_tier_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ANTHROPIC_DEFAULT_*_MODEL env vars are set from workspace claude_models.

    When ``claude_models`` lists all four tiers the corresponding
    ``ANTHROPIC_DEFAULT_FABLE_MODEL``, ``ANTHROPIC_DEFAULT_OPUS_MODEL``,
    ``ANTHROPIC_DEFAULT_SONNET_MODEL``, and ``ANTHROPIC_DEFAULT_HAIKU_MODEL``
    vars are injected into the terminal env so that Claude Code's ``/model``
    picker natively shows Databricks gateway model IDs instead of normalising
    them to canonical Anthropic names.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={
            "fable": "databricks-claude-fable-5",
            "opus": "databricks-claude-opus-4-7",
            "sonnet": "databricks-claude-sonnet-4-6",
            "haiku": "databricks-claude-haiku-4-5",
        },
        agents={
            "claude": UcodeAgentState(
                model="databricks-claude-opus-4-7",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )

    config = claude_native._ucode_config_for_profile("test-profile", refresh_models=False)

    assert config is not None
    assert config.env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "databricks-claude-fable-5"
    assert config.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "databricks-claude-opus-4-7"
    assert config.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "databricks-claude-sonnet-4-6"
    assert config.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "databricks-claude-haiku-4-5"


def test_ucode_config_for_profile_sets_only_present_tier_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Only tiers present in claude_models get ANTHROPIC_DEFAULT_* env vars.

    If ``claude_models`` only has one tier (e.g. ``"sonnet"``), only
    ``ANTHROPIC_DEFAULT_SONNET_MODEL`` is set — the other three are absent.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={"sonnet": "databricks-claude-sonnet-4-6"},
        agents={
            "claude": UcodeAgentState(
                model="databricks-claude-sonnet-4-6",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )

    config = claude_native._ucode_config_for_profile("test-profile", refresh_models=False)

    assert config is not None
    assert config.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "databricks-claude-sonnet-4-6"
    assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in config.env
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in config.env
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in config.env


def test_ucode_config_for_profile_sets_custom_model_option_for_second_sonnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``claude_models["sonnet_5"]`` entry pins Claude Code's one custom
    ``/model`` slot (``ANTHROPIC_CUSTOM_MODEL_OPTION``) to the newer Sonnet,
    offered as an opt-in *alongside* the ``sonnet`` tier alias, which stays
    on the workspace's existing default Sonnet (4.6). The default is
    unchanged; Sonnet 5 is an additional, explicit choice.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={
            "sonnet": "databricks-claude-sonnet-4-6",
            "sonnet_5": "databricks-claude-sonnet-5",
        },
        agents={
            "claude": UcodeAgentState(
                model="databricks-claude-sonnet-4-6",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )

    config = claude_native._ucode_config_for_profile("test-profile", refresh_models=False)

    assert config is not None
    assert config.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "databricks-claude-sonnet-4-6"
    assert config.env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "databricks-claude-sonnet-5"
    assert config.env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] == "Sonnet 5"


def test_ucode_config_for_profile_omits_model_tier_vars_when_no_claude_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    No ANTHROPIC_DEFAULT_* env vars are set when claude_models is empty.

    Older ucode state files may not include ``claude_models``.  In that
    case the env dict must not gain any spurious default model overrides.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={},
        agents={
            "claude": UcodeAgentState(
                model="databricks-claude-opus-4-7",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )

    config = claude_native._ucode_config_for_profile("test-profile", refresh_models=False)

    assert config is not None
    for key in config.env:
        assert not key.startswith("ANTHROPIC_DEFAULT_"), (
            f"Unexpected model-tier env var {key!r} when claude_models is empty"
        )


def test_ucode_config_for_profile_defaults_model_when_ucode_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ucode state with no model defaults to the Databricks gateway model.

    Some workspaces (e.g. the OSS integration gateway) cache the gateway
    URL + auth command but neither a per-agent ``model`` nor any
    ``claude_models`` tiers. Without a default the native Claude CLI falls
    back to its host-config model (an Anthropic-direct id like ``opus[1m]``)
    that the Databricks gateway rejects with "model ... may not exist".
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={},
        agents={
            "claude": UcodeAgentState(
                model=None,
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )

    config = claude_native._ucode_config_for_profile("test-profile", refresh_models=False)

    assert config is not None
    # The verified routable gateway endpoint name, not the CLI's own default.
    assert config.model == "catalog-databricks-claude-default"


def test_ucode_config_refreshes_live_models_and_builds_picker_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each launch replaces stale ucode versions with the live workspace catalog."""
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={
            "fable": "system.ai.claude-fable-5",
            "opus": "system.ai.claude-opus-4-7",
            "sonnet": "system.ai.claude-sonnet-4-6",
        },
        fable_enabled=False,
        agents={
            "claude": UcodeAgentState(
                model="databricks-claude-4-6-sonnet",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host="https://example.databricks.com", token="token"),
    )
    calls: list[tuple[str, str]] = []

    def _discover(host: str, token: str) -> DatabricksClaudeCatalog:
        calls.append((host, token))
        opus_version = "4-9" if len(calls) == 1 else "4-10"
        families = {
            "fable": "system.ai.claude-fable-5",
            "opus": f"system.ai.claude-opus-{opus_version}",
            "sonnet": "system.ai.claude-sonnet-5",
        }
        return DatabricksClaudeCatalog(
            families=families,
            model_ids=(*families.values(), "system.ai.claude-sonnet-4-6"),
        )

    monkeypatch.setattr(
        "omnigent.databricks_model_discovery.discover_databricks_claude_catalog",
        _discover,
    )

    first_config = claude_native._ucode_config_for_profile("test-profile")
    config = claude_native._ucode_config_for_profile("test-profile")

    assert config is not None
    assert first_config is not None
    assert first_config.model == "system.ai.claude-sonnet-5"
    assert calls == [
        ("https://example.databricks.com", "token"),
        ("https://example.databricks.com", "token"),
    ]
    assert config.model == "system.ai.claude-sonnet-5"
    assert config.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-10"
    assert config.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "system.ai.claude-sonnet-5"
    assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in config.env
    assert claude_native.claude_native_model_options(config) == [
        {
            "id": "opus",
            "model": "system.ai.claude-opus-4-10",
            "displayName": "Opus 4.10",
            "isDefault": False,
        },
        {
            "id": "sonnet",
            "model": "system.ai.claude-sonnet-5",
            "displayName": "Sonnet 5",
            "isDefault": True,
        },
    ]


def test_claude_native_static_model_options_keep_alias_as_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct Claude auth rows preserve the alias/model/label contract."""
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", ())
    options = claude_native.claude_native_model_options(None)

    assert options
    assert all(option["model"] == option["id"] for option in options)


def test_claude_native_model_options_follow_managed_claude_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed Claude model overrides replace the generic fallback rows."""
    managed_settings = tmp_path / "managed-settings.json"
    managed_settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-4-8[1m]",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "system.ai.claude-sonnet-4-6[1m]",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "system.ai.claude-haiku-4-5",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claude_native,
        "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS",
        (managed_settings,),
    )

    assert claude_native.claude_native_model_options(None) == [
        {
            "id": "opus",
            "model": "system.ai.claude-opus-4-8[1m]",
            "displayName": "Opus 4.8",
            "isDefault": False,
        },
        {
            "id": "sonnet",
            "model": "system.ai.claude-sonnet-4-6[1m]",
            "displayName": "Sonnet 4.6",
            "isDefault": False,
        },
        {
            "id": "haiku",
            "model": "system.ai.claude-haiku-4-5",
            "displayName": "Haiku 4.5",
            "isDefault": False,
        },
    ]


def test_unpinned_family_alias_resolves_to_the_provider_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tier alias with no env pin cannot reach the gateway as a canonical id."""
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", ())
    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic"},
        model="databricks-claude-sonnet-4-5",
    )
    assert (
        claude_native.resolve_claude_native_model_selection("opus", config)
        == "databricks-claude-sonnet-4-5"
    )


def test_unpinned_family_alias_passes_through_on_the_anthropic_api() -> None:
    """The Anthropic API resolves aliases natively; never rewrite them there."""
    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
        model="claude-sonnet-5",
    )
    assert claude_native.resolve_claude_native_model_selection("opus", config) == "opus"


def test_launch_model_takes_the_custom_slot_when_no_alias_names_it() -> None:
    """A routed older generation gets its own spelling for later ``/model``."""
    from omnigent.claude_model_vocabulary import claude_model_command_arg

    config = claude_native.ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5",
        },
        model="databricks-claude-opus-5",
    )

    pinned = claude_native.claude_config_with_launch_model_pinned(
        config, "databricks-claude-opus-4-8"
    )

    assert pinned is not None
    assert pinned.env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "databricks-claude-opus-4-8"
    assert pinned.env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] == "Opus 4.8"
    assert config.env.get("ANTHROPIC_CUSTOM_MODEL_OPTION") is None
    assert (
        claude_model_command_arg("databricks-claude-opus-4-8", pinned.env)
        == "databricks-claude-opus-4-8"
    )


@pytest.mark.parametrize(
    "launch_model",
    ["opus", "databricks-claude-opus-5", None, ""],
)
def test_launch_model_needs_no_custom_slot_when_already_speakable(
    launch_model: str | None,
) -> None:
    """Aliases and the pinned id resolve without the extra slot."""
    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5"},
        model="databricks-claude-opus-5",
    )

    assert claude_native.claude_config_with_launch_model_pinned(config, launch_model) is config
    assert claude_native.claude_config_with_launch_model_pinned(None, "x") is None


def test_managed_settings_pin_keeps_alias_passthrough_on_a_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code applies managed-settings pins, so the alias still routes."""
    managed_settings = tmp_path / "managed-settings.json"
    managed_settings.write_text(
        json.dumps({"env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed_settings,))
    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic"},
        model="databricks-claude-sonnet-4-5",
    )
    assert claude_native.resolve_claude_native_model_selection("opus", config) == "opus"


def test_family_alias_passes_through_when_substitution_is_not_needed() -> None:
    """Pinned (env resolves it), direct login (Claude does), or no default (nothing to swap in)."""
    pinned = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8"},
        model="databricks-claude-sonnet-4-5",
    )
    no_default = claude_native.ClaudeNativeUcodeConfig(env={"ANTHROPIC_BASE_URL": "https://x"})
    assert claude_native.resolve_claude_native_model_selection("opus", pinned) == "opus"
    assert claude_native.resolve_claude_native_model_selection("opus", None) == "opus"
    assert claude_native.resolve_claude_native_model_selection("opus", no_default) == "opus"


def test_provider_config_without_pins_offers_only_the_default_model_row() -> None:
    """Gateway configs never get the subscription alias rows they cannot route."""
    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic"},
        model="databricks-claude-sonnet-4-5",
    )
    assert claude_native.claude_native_model_options(config) == [
        {
            "id": "databricks-claude-sonnet-4-5",
            "model": "databricks-claude-sonnet-4-5",
            "displayName": "databricks-claude-sonnet-4-5",
            "isDefault": True,
        }
    ]
    assert (
        claude_native.claude_native_model_options(
            claude_native.ClaudeNativeUcodeConfig(env={"ANTHROPIC_BASE_URL": "https://x"})
        )
        == []
    )


def test_anthropic_endpoint_config_without_pins_keeps_the_alias_rows() -> None:
    """API-key providers on the Anthropic API keep the alias catalog Claude resolves."""
    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
        model="claude-sonnet-5",
    )
    options = claude_native.claude_native_model_options(config)
    assert options
    assert all(option["model"] == option["id"] for option in options)


def test_sonnet_5_selection_resolves_to_the_configured_custom_model() -> None:
    """The friendly Sonnet 5 row launches the provider's routable model id."""
    config = claude_native.ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "system.ai.claude-sonnet-5[1m]",
        }
    )
    assert (
        claude_native.resolve_claude_native_model_selection("sonnet_5", config)
        == "system.ai.claude-sonnet-5[1m]"
    )


def test_sonnet_5_subscription_selection_uses_owned_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct-login custom row resolves from the central fallback record."""
    fallback = SimpleNamespace(model_ids=("future-claude-sonnet-5",))
    monkeypatch.setattr(claude_native, "static_model_fallback", lambda *_args: fallback)

    assert (
        claude_native.resolve_claude_native_model_selection("sonnet_5", None)
        == "future-claude-sonnet-5"
    )


def test_sonnet_5_subscription_selection_uses_first_sonnet_family_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renamed Sonnet release stays routable when its display label drifts."""
    fallback = SimpleNamespace(model_ids=("future-claude-opus-5", "future-claude-sonnet-5-1"))
    monkeypatch.setattr(claude_native, "static_model_fallback", lambda *_args: fallback)

    assert (
        claude_native.resolve_claude_native_model_selection("sonnet_5", None)
        == "future-claude-sonnet-5-1"
    )


@pytest.mark.parametrize(
    "fallback",
    [None, SimpleNamespace(model_ids=("future-claude-opus-5",))],
)
def test_sonnet_5_subscription_selection_fails_without_sonnet_fallback(
    fallback: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private picker id never reaches Claude when its fallback disappears."""
    monkeypatch.setattr(claude_native, "static_model_fallback", lambda *_args: fallback)

    with pytest.raises(ValueError, match="no routable Sonnet model"):
        claude_native.resolve_claude_native_model_selection("sonnet_5", None)


def test_removed_sonnet_5_selection_falls_back_to_routable_databricks_sonnet() -> None:
    """A stale Sonnet 5 override cannot launch a non-gateway Anthropic id."""
    config = claude_native.ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "system.ai.claude-sonnet-4-6",
        }
    )

    assert (
        claude_native.resolve_claude_native_model_selection("sonnet_5", config)
        == "system.ai.claude-sonnet-4-6"
    )


def test_ucode_config_retains_live_fable_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live discovery preserves Fable when the persisted opt-in is enabled."""
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={},
        fable_enabled=True,
        agents={
            "claude": UcodeAgentState(
                model="system.ai.claude-opus-4-10",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host="https://example.databricks.com", token="token"),
    )
    monkeypatch.setattr(
        "omnigent.databricks_model_discovery.discover_databricks_claude_catalog",
        lambda host, token: DatabricksClaudeCatalog(
            families={
                "fable": "system.ai.claude-fable-5",
                "opus": "system.ai.claude-opus-4-10",
            },
            model_ids=("system.ai.claude-fable-5", "system.ai.claude-opus-4-10"),
        ),
    )

    config = claude_native._ucode_config_for_profile("test-profile")

    assert config is not None
    assert config.env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "system.ai.claude-fable-5"


def test_ucode_config_uses_cached_models_when_live_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network failure preserves the previously working ucode mapping."""
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={"opus": "system.ai.claude-opus-4-8"},
        agents={
            "claude": UcodeAgentState(
                model="system.ai.claude-opus-4-8",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host="https://example.databricks.com", token="token"),
    )

    def _fail(host: str, token: str) -> DatabricksClaudeCatalog:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(
        "omnigent.databricks_model_discovery.discover_databricks_claude_catalog",
        _fail,
    )

    config = claude_native._ucode_config_for_profile("test-profile")

    assert config is not None
    assert config.model == "system.ai.claude-opus-4-8"
    assert config.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-8"


def test_ucode_config_rejects_authoritative_empty_live_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful empty listing removes stale models instead of launching them."""
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={"opus": "system.ai.claude-opus-4-8"},
        agents={
            "claude": UcodeAgentState(
                model="system.ai.claude-opus-4-8",
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host="https://example.databricks.com", token="token"),
    )
    monkeypatch.setattr(
        "omnigent.databricks_model_discovery.discover_databricks_claude_catalog",
        lambda host, token: DatabricksClaudeCatalog(families={}, model_ids=()),
    )

    with pytest.raises(click.ClickException, match="exposes no Claude model services"):
        claude_native._ucode_config_for_profile("test-profile")


def test_ucode_config_for_profile_fails_loud_on_malformed_claude_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selected malformed Claude ucode entry surfaces a setup error."""
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    workspace_state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        agents={"claude": UcodeAgentState(auth_command="printf token")},
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state",
        lambda workspace_url: workspace_state,
    )

    with pytest.raises(click.ClickException, match="missing Claude base URL"):
        claude_native._ucode_config_for_profile("test-profile", refresh_models=False)


def test_attach_url_encodes_path_components() -> None:
    """Attach URLs preserve base paths and percent-encode ids."""
    url = claude_native._attach_url(
        "https://example.com/base/",
        "conv with space",
        "terminal/odd:id",
    )

    assert (
        url == "wss://example.com/base/v1/sessions/conv%20with%20space/"
        "resources/terminals/terminal%2Fodd%3Aid/attach"
    )


def test_materialized_session_spec_is_valid_terminal_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The generated bundled agent spec validates for Omnigent session creation.

    The session agent only exists so the Sessions API can create a
    normal session row; Claude itself is launched as a terminal
    resource after creation, not through this executor block.
    """
    # Pin the host shells so the declared terminals are deterministic
    # ($SHELL=bash → the default/first terminal is ``bash``).
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("SHELL", "/bin/bash")
    path = claude_native._materialize_claude_agent_spec(tmp_path)

    raw = yaml.safe_load(path.read_text())
    assert raw["name"] == "claude-native-ui"
    assert raw["prompt"].startswith("Claude Code is running in the session terminal.")
    # ``context_window`` is the conservative pre-first-turn default;
    # the statusLine forwarder overrides it once the real number is
    # observed (see ``omnigent.claude_native_status``).
    assert raw["executor"] == {"harness": "claude-native", "context_window": 200_000}
    # os_env block is required for the runner's filesystem APIs not
    # to 404 (see _require_os_env in omnigent/runner/app.py).
    assert raw["os_env"] == {
        "type": "caller_process",
        "cwd": ".",
        "sandbox": {"type": "none"},
    }
    spec = load_omnigent_yaml(path)
    assert spec.executor.type == "omnigent"
    assert spec.executor.config["harness"] == "claude-native"
    assert spec.os_env is not None
    # The native wrapper opts into the spawn-write surface so the
    # wrapped Claude Code can author agent configs and launch them as
    # child sessions; the bridge relay derives its tool set from this
    # spec via ToolManager, so a dropped flag silently removes
    # sys_session_create/send/close from the native CLI.
    assert raw["spawn"] is True
    assert spec.spawn is True
    # The native wrapper declares one terminal per installed shell so the
    # relay advertises the sys_terminal_* family to the wrapped Claude Code
    # (the relay gate is a non-empty ``terminals:`` block on this spec); a
    # dropped block silently removes the terminal tools from the native CLI.
    assert spec.terminals is not None
    assert spec.terminals["bash"].command == "bash"


def test_remote_run_preflights_local_claude_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``--server`` mode still requires a local Claude executable.

    ``--server`` only selects the AP/web UI/control plane. Claude
    itself is launched by the local runner, so the wrapper must fail
    before contacting the server when the local binary is missing.
    """
    called_remote = False

    def fake_which(command: str) -> str | None:
        """
        Report the fake Claude command as missing and tmux as present.

        :param command: Command name passed to ``shutil.which``.
        :returns: Fake executable path or ``None``.
        """
        if command == "missing-claude":
            return None
        if command == "tmux":
            return "/usr/bin/tmux"
        return f"/usr/bin/{command}"

    def fake_remote(
        base_url: str,
        spec_path: Path,
        *,
        session_id: str | None,
        claude_args: tuple[str, ...],
        command: str,
    ) -> None:
        """Record an unexpected remote launch attempt.

        :param base_url: Remote server URL.
        :param spec_path: Generated wrapper spec path.
        :param session_id: Optional session id.
        :param claude_args: Passthrough Claude arguments.
        :param command: Claude executable name.
        :returns: None.
        """
        nonlocal called_remote
        del base_url, spec_path, session_id, claude_args, command
        called_remote = True

    monkeypatch.setattr(claude_native.shutil, "which", fake_which)
    monkeypatch.setattr(claude_native, "_run_with_remote_server", fake_remote)

    with pytest.raises(click.ClickException, match="missing-claude"):
        claude_native.run_claude_native(
            server="https://example.com/",
            session_id="conv_abc",
            claude_args=("--resume", "claude-native"),
            command="missing-claude",
        )

    assert called_remote is False


def test_local_run_preflights_local_claude_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local-server mode also requires a local Claude executable.

    The Omnigent server and web UI are local in this mode, but Claude is
    still launched by a local runner-owned terminal resource.
    """
    called_local = False

    def fake_which(command: str) -> str | None:
        """
        Report the fake Claude command as missing and tmux as present.

        :param command: Command name passed to ``shutil.which``.
        :returns: Fake executable path or ``None``.
        """
        if command == "missing-claude":
            return None
        if command == "tmux":
            return "/usr/bin/tmux"
        return f"/usr/bin/{command}"

    def fake_local(
        spec_path: Path,
        *,
        session_id: str | None,
        claude_args: tuple[str, ...],
        command: str,
    ) -> None:
        """
        Record an unexpected local launch attempt.

        :param spec_path: Generated wrapper spec path.
        :param session_id: Optional session id.
        :param claude_args: Passthrough Claude arguments.
        :param command: Claude executable name.
        :returns: None.
        """
        nonlocal called_local
        del spec_path, session_id, claude_args, command
        called_local = True

    monkeypatch.setattr(claude_native.shutil, "which", fake_which)
    monkeypatch.setattr(claude_native, "_run_with_local_server", fake_local)

    with pytest.raises(click.ClickException, match="missing-claude"):
        claude_native.run_claude_native(
            server=None,
            session_id=None,
            claude_args=(),
            command="missing-claude",
        )

    assert called_local is False


def test_run_preflights_local_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The native wrapper fails before setup when local tmux is unavailable.

    This catches regressions where the wrapper would start a server or
    runner and only fail after terminal-resource launch.
    """

    def fake_which(command: str) -> str | None:
        """
        Report Claude as present and tmux as missing.

        :param command: Command name passed to ``shutil.which``.
        :returns: Fake executable path or ``None``.
        """
        if command == "tmux":
            return None
        return f"/usr/bin/{command}"

    monkeypatch.setattr(claude_native.shutil, "which", fake_which)

    with pytest.raises(click.ClickException, match="tmux"):
        claude_native.run_claude_native(
            server=None,
            session_id=None,
            claude_args=(),
            command="claude",
        )


def test_local_run_persists_launch_state_on_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    The local-server fresh-session path persists launch state.

    Both ``_run_with_local_server`` and ``_run_with_remote_server``
    carry the same ``_record_launch_for_fresh_session`` call site, so the
    duplicated block is easy to break in only one of the two
    when one is touched and the other isn't. This test pins the
    local variant; a copy-paste regression that forgets to wire
    the call there would surface here without affecting the remote
    test (and vice versa).
    """
    from omnigent.claude_native_state import read_launch_state

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    opened: list[tuple[str, str, bool]] = []

    class _Proc:
        """Stub for the local server subprocess."""

        def poll(self) -> None:
            """Pretend the server is alive."""

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        """Return a minimal server handle without spawning anything."""
        del args, kwargs
        return SimpleNamespace(
            proc=_Proc(),
            runner_id="runner_local",
            log_path=None,
        )

    async def fake_prepare(**kwargs: object) -> claude_native.PreparedClaudeTerminal:
        """Return a prepared terminal pointing at a freshly-minted conv id."""
        del kwargs
        return claude_native.PreparedClaudeTerminal(
            session_id="conv_local_fresh",
            terminal_id=claude_native.claude_terminal_resource_id(),
            bridge_dir=tmp_path / "bridge",
            reattached=False,
        )

    async def fake_attach(
        attach_url: str,
        *,
        headers: dict[str, str],
        terminal_gone_probe: object | None = None,
    ) -> bool:
        """Exit immediately so the attach loop returns."""
        del attach_url, headers, terminal_gone_probe
        return True

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 12345)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundle")
    monkeypatch.setattr(claude_native, "_prepare_claude_terminal", fake_prepare)
    monkeypatch.setattr(claude_native, "attach_local_terminal", fake_attach)
    monkeypatch.setattr(
        claude_native,
        "open_conversation_link_if_enabled",
        lambda **kwargs: opened.append(
            (
                kwargs["base_url"],
                kwargs["conversation_id"],
                kwargs["enabled"],
            )
        ),
    )

    claude_native._run_with_local_server(
        spec_path,
        session_id=None,
        resume_picker=False,
        claude_args=(),
        command="claude",
        auto_open_conversation=True,
    )

    state = read_launch_state("conv_local_fresh")
    assert state is not None, (
        "local-server fresh-session create did not persist launch state. "
        "The local variant of the call site is broken (or missing); the "
        "remote variant is exercised by a sibling test, so failing here "
        "narrows the regression to ``_run_with_local_server``."
    )
    assert state.working_directory == str(workspace.resolve()), (
        f"recorded cwd {state.working_directory!r} does not match the "
        f"workspace the wrapper ran in ({str(workspace.resolve())!r})."
    )
    captured = capsys.readouterr()
    web_ui = "Web UI: http://127.0.0.1:12345/c/conv_local_fresh"
    resume_hint = "Resume with: omnigent claude --resume conv_local_fresh"
    assert web_ui in captured.err
    assert resume_hint in captured.err
    assert captured.err.index(web_ui) < captured.err.index(resume_hint)
    assert opened == [("http://127.0.0.1:12345", "conv_local_fresh", True)]


def test_local_resume_does_not_print_redundant_resume_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    ``omnigent claude --resume`` does not echo another resume prompt.

    The final hint is useful when a fresh launch creates a new
    conversation id. On an explicit resume, the user already supplied
    that id; printing the same prompt again creates persistent noise.
    """
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n", encoding="utf-8")

    class _Proc:
        """Stub for the local server subprocess."""

        def poll(self) -> None:
            """
            Pretend the server is alive.

            :returns: None.
            """

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        """
        Return a minimal server handle without spawning anything.

        :param args: Positional startup args.
        :param kwargs: Keyword startup args.
        :returns: Fake local server handle.
        """
        del args, kwargs
        return SimpleNamespace(proc=_Proc(), runner_id="runner_local", log_path=None)

    async def fake_prepare(**kwargs: object) -> claude_native.PreparedClaudeTerminal:
        """
        Return a prepared terminal for the resumed conversation.

        :param kwargs: Terminal preparation kwargs.
        :returns: Prepared fake terminal.
        """
        del kwargs
        return claude_native.PreparedClaudeTerminal(
            session_id="conv_existing",
            terminal_id=claude_native.claude_terminal_resource_id(),
            bridge_dir=tmp_path / "bridge",
            reattached=False,
        )

    async def fake_attach(
        attach_url: str,
        *,
        headers: dict[str, str],
        terminal_gone_probe: object | None = None,
    ) -> bool:
        """
        Exit immediately so the attach loop returns.

        :param attach_url: Terminal attach URL.
        :param headers: Auth headers.
        :param terminal_gone_probe: Optional terminal-gone callback.
        :returns: ``True`` for user-requested exit.
        """
        del attach_url, headers, terminal_gone_probe
        return True

    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 12346)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr(claude_native, "_prepare_claude_terminal", fake_prepare)
    monkeypatch.setattr(claude_native, "attach_local_terminal", fake_attach)

    claude_native._run_with_local_server(
        spec_path,
        session_id="conv_existing",
        resume_picker=False,
        claude_args=(),
        command="claude",
    )

    captured = capsys.readouterr()
    assert "Web UI: http://127.0.0.1:12346/c/conv_existing" in captured.err
    assert "Resume with:" not in captured.err


def test_remote_daemon_run_attaches_without_cli_forwarder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Daemon-routed ``omnigent claude`` leaves forwarding to the runner.

    The daemon path launches a runner, the runner auto-creates the
    Claude terminal, and that auto-create starts the transcript
    forwarder. The CLI should only attach to tmux/WebSocket. If this
    call site omits ``run_transcript_forwarder=False``, the CLI starts a
    second forwarder on the same bridge and every transcript item is
    posted to Omnigent twice.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the generated spec and bridge.
    :returns: None.
    """
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n", encoding="utf-8")
    captured_attach: dict[str, Any] = {}
    recorded_launches: list[str] = []

    async def fake_prepare(**kwargs: object) -> claude_native.PreparedClaudeTerminal:
        """
        Return a runner-owned prepared terminal.

        :param kwargs: Daemon preparation kwargs.
        :returns: Prepared fake terminal using a session-keyed bridge.
        """
        assert kwargs["host_id"] == "host_test"
        assert kwargs["workspace"] == str(Path.cwd().resolve())
        assert isinstance(kwargs["startup_progress"], RunnerStartupProgress)
        return claude_native.PreparedClaudeTerminal(
            session_id="conv_daemon",
            terminal_id=claude_native.claude_terminal_resource_id(),
            bridge_dir=tmp_path / "bridge",
            reattached=False,
            tmux_socket="/tmp/claude.sock",
            tmux_target="claude:main",
        )

    async def fake_attach_with_forwarder_switch(**kwargs: object) -> claude_native._AttachOutcome:
        """
        Capture attach-helper kwargs without starting an attach loop.

        :param kwargs: Arguments passed to
            :func:`_attach_with_transcript_forwarder`.
        :returns: ``EXITED`` so the remote runner path completes.
        """
        captured_attach.update(kwargs)
        return claude_native._AttachOutcome.EXITED

    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundle")
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda server_url=None, **_kw: {"Authorization": "Bearer tok"},
    )
    monkeypatch.setattr("omnigent.chat._server_auth", lambda server_url=None, **_kw: None)
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", lambda base_url: None)
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host_test"),
    )
    monkeypatch.setattr(claude_native, "_prepare_claude_terminal_via_daemon", fake_prepare)
    monkeypatch.setattr(
        claude_native,
        "_attach_with_transcript_forwarder",
        fake_attach_with_forwarder_switch,
    )
    monkeypatch.setattr(
        claude_native,
        "_record_launch_for_fresh_session",
        lambda session_id: recorded_launches.append(session_id),
    )
    monkeypatch.setattr(claude_native, "echo_native_resume_hint", lambda **kwargs: None)
    monkeypatch.setattr(
        claude_native,
        "open_conversation_link_if_enabled",
        lambda **kwargs: None,
    )

    claude_native._run_with_remote_server(
        "https://example.com",
        spec_path,
        session_id=None,
        resume_picker=False,
        claude_args=("--allowedTools", "Read"),
    )

    assert recorded_launches == ["conv_daemon"]
    assert captured_attach["run_transcript_forwarder"] is False, (
        "daemon-owned Claude sessions must not start a CLI transcript "
        "forwarder; the runner already owns the forwarder and a second "
        "tailer duplicates every web chat message"
    )


@pytest.mark.asyncio
async def test_prepare_daemon_terminal_reports_progress_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Daemon-routed startup surfaces each long wait to the user.

    The user-visible failure mode is silence until ``tmux attach``.
    This test pins the small set of user-facing milestones shown while
    the CLI creates the session, waits for the runner, and waits for the
    runner-owned Claude terminal. Regressing to internal labels or
    silence makes this test fail.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for fake tmux metadata.
    :returns: None.
    """
    updates: list[str] = []
    progress = RunnerStartupProgress(update=updates.append)

    async def fake_create_session(
        client: object,
        bundle: bytes,
        *,
        bridge_id: str | None,
        terminal_launch_args: list[str] | None = None,
    ) -> str:
        """
        Return a fresh session id and assert persisted launch args.

        :param client: HTTP client created by the prepare helper.
        :param bundle: Bundled agent bytes.
        :param bridge_id: Bridge label requested by the caller.
        :param terminal_launch_args: Claude launch args persisted on
            the session for the runner to apply.
        :returns: Fixed conversation id.
        """
        del client
        assert bundle == b"bundle"
        assert bridge_id is None
        assert terminal_launch_args == ["--print", "hi"]
        return "conv_daemon_progress"

    async def fake_wait_for_host_online(
        client: object,
        host_id: str,
        *,
        timeout_s: float,
    ) -> None:
        """
        Pretend the daemon host is already online.

        :param client: HTTP client created by the prepare helper.
        :param host_id: Host id under test.
        :param timeout_s: Timeout supplied by production.
        :returns: None.
        """
        del client, timeout_s
        assert host_id == "host_progress"

    async def fake_launch_or_reuse_runner(
        client: object,
        *,
        host_id: str,
        session_id: str,
        workspace: str,
        fresh: bool = False,
    ) -> str:
        """
        Return the runner id that production should wait on.

        :param client: HTTP client created by the prepare helper.
        :param host_id: Host id under test.
        :param session_id: Created conversation id.
        :param workspace: Workspace path requested for the runner.
        :returns: Fixed runner id.
        """
        del client
        assert host_id == "host_progress"
        assert session_id == "conv_daemon_progress"
        assert workspace == "/workspace"
        return "runner_progress"

    async def fake_wait_for_runner_online(
        client: object,
        runner_id: str,
        *,
        timeout_s: float,
    ) -> None:
        """
        Pretend the daemon-spawned runner is online.

        :param client: HTTP client created by the prepare helper.
        :param runner_id: Runner id under test.
        :param timeout_s: Timeout supplied by production.
        :returns: None.
        """
        del client, timeout_s
        assert runner_id == "runner_progress"

    async def fake_wait_for_terminal_ready(
        client: object,
        session_id: str,
        *,
        timeout_s: float,
    ) -> str:
        """
        Return the Claude terminal id once the wait phase is reached.

        :param client: HTTP client created by the prepare helper.
        :param session_id: Created conversation id.
        :param timeout_s: Timeout supplied by production.
        :returns: Fixed terminal id.
        """
        del client, timeout_s
        assert session_id == "conv_daemon_progress"
        return claude_native.claude_terminal_resource_id()

    async def fake_read_tmux(
        client: object,
        session_id: str,
    ) -> claude_native._ClaudeTerminalTmux:
        """
        Return local tmux metadata for direct attach.

        :param client: HTTP client created by the prepare helper.
        :param session_id: Created conversation id.
        :returns: Fake tmux coordinates.
        """
        del client
        assert session_id == "conv_daemon_progress"
        return claude_native._ClaudeTerminalTmux(
            socket=tmp_path / "tmux.sock",
            target="claude:main",
        )

    monkeypatch.setattr(claude_native, "_create_claude_session", fake_create_session)
    monkeypatch.setattr(claude_native, "wait_for_host_online", fake_wait_for_host_online)
    monkeypatch.setattr(
        claude_native,
        "launch_or_reuse_daemon_runner",
        fake_launch_or_reuse_runner,
    )
    monkeypatch.setattr(claude_native, "wait_for_runner_online", fake_wait_for_runner_online)
    monkeypatch.setattr(
        claude_native,
        "_wait_for_claude_terminal_ready",
        fake_wait_for_terminal_ready,
    )
    monkeypatch.setattr(claude_native, "_read_claude_terminal_tmux", fake_read_tmux)

    prepared = await claude_native._prepare_claude_terminal_via_daemon(
        base_url="https://example.com",
        headers={},
        session_id=None,
        session_bundle=b"bundle",
        claude_args=("--print", "hi"),
        host_id="host_progress",
        workspace="/workspace",
        startup_progress=progress,
    )

    assert prepared.session_id == "conv_daemon_progress"
    assert prepared.terminal_id == claude_native.claude_terminal_resource_id()
    assert prepared.tmux_socket == tmp_path / "tmux.sock"
    assert prepared.tmux_target == "claude:main"
    assert updates == [
        "Creating Claude session...",
        "Starting runner...",
        "Starting Claude terminal...",
        "Claude terminal ready.",
    ]


@pytest.mark.asyncio
async def test_attach_profiles_direct_tmux_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Startup profiling marks the direct tmux attach handoff.

    The user's slow-start symptom is "nothing opens until tmux attach";
    this test pins the final foreground marks before the wrapper gives
    control to tmux. If the profiler is no longer passed into
    ``_attach_with_transcript_forwarder`` or the direct-tmux branch,
    these assertions fail.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for bridge and socket paths.
    :returns: None.
    """
    stream = io.StringIO()
    clock_values = iter([0.0, 0.1, 0.3])
    profiler = StartupProfiler(
        name="omnigent claude",
        enabled=True,
        clock=lambda: next(clock_values),
        stream=stream,
    )
    socket = tmp_path / "tmux.sock"
    socket.write_text("")
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_tmux",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=True,
        tmux_socket=socket,
        tmux_target="main",
    )

    async def fake_direct_tmux(
        socket_path: Path,
        tmux_target: str,
        *,
        startup_profiler: StartupProfiler | None = None,
    ) -> claude_native._AttachOutcome:
        """
        Record the direct attach path without spawning tmux.

        :param socket_path: Tmux socket path.
        :param tmux_target: Tmux target.
        :param startup_profiler: Startup profiler passed through by
            the attach helper.
        :returns: ``DETACHED`` so no cleanup close runs.
        """
        assert socket_path == socket
        assert tmux_target == "main"
        assert startup_profiler is profiler
        return claude_native._AttachOutcome.DETACHED

    monkeypatch.setattr(claude_native, "_can_attach_direct_tmux", lambda prepared: True)
    monkeypatch.setattr(claude_native, "_attach_direct_tmux", fake_direct_tmux)

    outcome = await claude_native._attach_with_transcript_forwarder(
        base_url="https://example.com",
        headers={},
        prepared=prepared,
        agent_name=claude_native._AGENT_NAME,
        attach_url="ws://example.invalid/attach",
        attach=lambda *_args, **_kwargs: None,
        run_transcript_forwarder=False,
        startup_profiler=profiler,
    )

    assert outcome is claude_native._AttachOutcome.DETACHED
    output = stream.getvalue()
    assert "transcript forwarder skipped" in output
    assert "opening direct tmux attach - target=main" in output
    assert "opening websocket terminal attach" not in output


@pytest.mark.asyncio
async def test_attach_marks_terminal_stopped_on_exit_when_launched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Wrapper-launched Claude terminals are explicitly stopped on exit.

    Web clients otherwise see a phantom live terminal until the AP
    server's runner-disconnect signaling catches up. The wrapper issues
    a best-effort DELETE so the resource flips to stopped synchronously.
    Failure of this test would mean the DoD (Claude-exit cleanup) is
    no longer met for `--server` mode.
    """
    close_calls: list[dict[str, str]] = []

    async def fake_close(
        *, base_url: str, headers: dict[str, str], session_id: str, terminal_id: str
    ) -> None:
        """
        Record cleanup args without issuing a real DELETE.

        :param base_url: Omnigent base URL passed to the cleanup helper.
        :param headers: Auth headers passed to the cleanup helper.
        :param session_id: Session id being cleaned up.
        :param terminal_id: Terminal resource id being closed.
        :returns: None.
        """
        del headers
        close_calls.append(
            {
                "base_url": base_url,
                "session_id": session_id,
                "terminal_id": terminal_id,
            }
        )

    async def fake_attach(attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        Return immediately so the finally block runs.

        :param attach_url: Terminal WebSocket URL.
        :param headers: Auth headers.
        :returns: ``True`` to simulate a user-initiated exit.
        """
        del attach_url, headers
        return True

    async def fake_forward(**kwargs: object) -> None:
        """
        No-op transcript forwarder; cancelled in the finally block.

        :param kwargs: Forwarder kwargs.
        :returns: None.
        """
        del kwargs
        await asyncio.sleep(3600)

    monkeypatch.setattr(claude_native, "_close_claude_terminal", fake_close)
    monkeypatch.setattr(claude_native, "supervise_forwarder", fake_forward)

    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
    )
    await claude_native._attach_with_transcript_forwarder(
        base_url="https://example.com",
        headers={"Authorization": "Bearer tok"},
        prepared=prepared,
        agent_name=claude_native._AGENT_NAME,
        attach_url="wss://example.com/attach",
        attach=fake_attach,
    )

    assert close_calls == [
        {
            "base_url": "https://example.com",
            "session_id": "conv_abc",
            "terminal_id": claude_native.claude_terminal_resource_id(),
        }
    ]


@pytest.mark.asyncio
async def test_attach_runs_cleanup_even_when_forwarder_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Forwarder crashes don't skip the AP-side terminal stop call.

    The forwarder tails a JSONL transcript and POSTs items to AP;
    real-world failure modes (corrupt JSONL, file-system error, an
    uncaught exception in the parser) raise non-``CancelledError``
    exceptions out of the shutdown ``await forwarder``. If the cleanup
    block re-raised them, ``_close_claude_terminal`` would be skipped
    and the web UI would show a phantom live terminal after the wrapper
    exits — contradicting the DoD. The implementation must log the
    forwarder crash and still issue the DELETE.
    """
    close_calls: list[str] = []

    async def fake_close(
        *, base_url: str, headers: dict[str, str], session_id: str, terminal_id: str
    ) -> None:
        """
        Record that cleanup ran despite the forwarder fault.

        :param base_url: Omnigent base URL.
        :param headers: Auth headers.
        :param session_id: Session id being cleaned up.
        :param terminal_id: Terminal resource id being closed.
        :returns: None.
        """
        del base_url, headers, session_id, terminal_id
        close_calls.append("called")

    async def fake_attach(attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        Return immediately so the finally block runs.

        :param attach_url: Terminal WebSocket URL.
        :param headers: Auth headers.
        :returns: ``True`` to simulate a user-initiated exit.
        """
        del attach_url, headers
        return True

    async def crashing_forward(**kwargs: object) -> None:
        """
        Raise the kind of non-cancel exception a corrupt transcript yields.

        :param kwargs: Forwarder kwargs.
        :raises OSError: Always, to exercise the cleanup fallback.
        """
        del kwargs
        raise OSError("transcript file unreadable")

    monkeypatch.setattr(claude_native, "_close_claude_terminal", fake_close)
    monkeypatch.setattr(claude_native, "supervise_forwarder", crashing_forward)

    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
    )

    # Must not raise — the OSError from the forwarder is logged and
    # cleanup proceeds.
    await claude_native._attach_with_transcript_forwarder(
        base_url="https://example.com",
        headers={"Authorization": "Bearer tok"},
        prepared=prepared,
        agent_name=claude_native._AGENT_NAME,
        attach_url="wss://example.com/attach",
        attach=fake_attach,
    )

    assert close_calls == ["called"]


@pytest.mark.asyncio
async def test_attach_skips_terminal_close_when_reattached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Reattached terminals are owned by their launching invocation.

    A second wrapper that simply joins an existing terminal must not
    issue a stop on exit — that would tear down the terminal under
    the launcher's feet. Failure here would let `--session` reattach
    + clean exit silently kill the launcher's live Claude session.
    """
    close_calls: list[str] = []

    async def fake_close(**kwargs: object) -> None:
        """
        Record any cleanup call; reattach mode should pass none.

        :param kwargs: Cleanup kwargs.
        :returns: None.
        """
        del kwargs
        close_calls.append("called")

    async def fake_attach(attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        Return immediately so the finally block runs.

        :param attach_url: Terminal WebSocket URL.
        :param headers: Auth headers.
        :returns: ``True`` to simulate a user-initiated exit.
        """
        del attach_url, headers
        return True

    async def fake_forward(**kwargs: object) -> None:
        """
        No-op transcript forwarder; cancelled in the finally block.

        :param kwargs: Forwarder kwargs.
        :returns: None.
        """
        del kwargs
        await asyncio.sleep(3600)

    monkeypatch.setattr(claude_native, "_close_claude_terminal", fake_close)
    monkeypatch.setattr(claude_native, "supervise_forwarder", fake_forward)

    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=True,
    )
    await claude_native._attach_with_transcript_forwarder(
        base_url="https://example.com",
        headers={},
        prepared=prepared,
        agent_name=claude_native._AGENT_NAME,
        attach_url="wss://example.com/attach",
        attach=fake_attach,
    )

    assert close_calls == []


@pytest.mark.asyncio
async def test_attach_can_skip_transcript_forwarder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Runner-owned launches attach without starting a second forwarder.

    The daemon path starts the Claude terminal inside the runner, and
    ``_auto_create_claude_terminal`` starts the transcript forwarder
    there. If the CLI attach process starts another forwarder on the
    same bridge, both tailers POST the same transcript items and the web
    UI renders duplicated user and assistant messages.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for the fake bridge.
    :returns: None.
    """

    async def fail_forwarder(**kwargs: object) -> None:
        """
        Fail if this attach process tries to own transcript forwarding.

        :param kwargs: Forwarder kwargs that should never be supplied.
        :returns: Never returns when called.
        :raises AssertionError: Always, if the forbidden forwarder starts.
        """
        raise AssertionError(f"unexpected CLI transcript forwarder: {kwargs!r}")

    async def fake_attach(attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        Return immediately so the attach helper reaches cleanup.

        :param attach_url: Terminal WebSocket URL.
        :param headers: WebSocket headers.
        :returns: ``True`` to simulate a user-requested exit.
        """
        assert attach_url.endswith(
            "/v1/sessions/conv_abc/resources/terminals/terminal_claude_main/attach"
        )
        assert headers == {"Authorization": "Bearer tok"}
        return True

    monkeypatch.setattr(claude_native, "supervise_forwarder", fail_forwarder)

    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=True,
    )
    await claude_native._attach_with_transcript_forwarder(
        base_url="https://example.com",
        headers={"Authorization": "Bearer tok"},
        prepared=prepared,
        agent_name=claude_native._AGENT_NAME,
        attach_url="wss://example.com/attach",
        attach=fake_attach,
        run_transcript_forwarder=False,
    )


@pytest.mark.asyncio
async def test_prepare_reattaches_existing_claude_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Existing running ``claude/main`` terminals are reused before bind.

    If this regresses, a second ``omnigent claude --session`` can
    rebind the session to a new local runner and launch a duplicate
    terminal instead of attaching to the live one.
    """
    calls: list[str] = []

    async def fake_find(client: object, session_id: str) -> str | None:
        """
        Return the existing terminal and record lookup order.

        :param client: HTTP client created by the prepare helper.
        :param session_id: Existing session id.
        :returns: Existing terminal id.
        """
        del client
        calls.append(f"find:{session_id}")
        return claude_native.claude_terminal_resource_id()

    async def fail_bind(client: object, session_id: str, runner_id: str) -> None:
        """
        Fail if prepare tries to rebind a session with a live terminal.

        :param client: HTTP client created by the prepare helper.
        :param session_id: Existing session id.
        :param runner_id: Candidate runner id.
        :returns: None.
        """
        del client
        raise AssertionError(f"unexpected bind {session_id} to {runner_id}")

    async def fail_launch(
        client: object,
        session_id: str,
        claude_args: tuple[str, ...],
        *,
        command: str,
        bridge_dir: Path,
    ) -> str:
        """
        Fail if prepare tries to launch a duplicate terminal.

        :param client: HTTP client created by the prepare helper.
        :param session_id: Existing session id.
        :param claude_args: Claude CLI args.
        :param command: Claude executable.
        :param bridge_dir: Native Claude bridge directory.
        :returns: Never returns.
        """
        del client, claude_args, bridge_dir
        raise AssertionError(f"unexpected launch {session_id} using {command}")

    async def fake_fetch_labels(_client: object, _session_id: str) -> dict[str, str]:
        """
        Return the bridge label for the existing session.

        :param _client: HTTP client created by the prepare helper.
        :param _session_id: Existing session id.
        :returns: Labels containing the bridge id.
        """
        return {"omnigent.claude_native.bridge_id": "bridge_abc"}

    monkeypatch.setattr(claude_native, "_find_running_claude_terminal", fake_find)
    monkeypatch.setattr(claude_native, "_bind_session_runner", fail_bind)
    monkeypatch.setattr(claude_native, "_launch_claude_terminal", fail_launch)
    monkeypatch.setattr(claude_native, "_fetch_claude_session_labels", fake_fetch_labels)

    result = await claude_native._prepare_claude_terminal(
        base_url="https://example.com",
        headers={},
        session_id="conv_abc",
        runner_id="runner_new",
        session_bundle=None,
        claude_args=(),
        command="claude",
    )

    assert result == claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=claude_native.bridge_dir_for_bridge_id("bridge_abc"),
        reattached=True,
    )
    assert calls == ["find:conv_abc"]


@pytest.mark.asyncio
async def test_find_running_claude_terminal_reads_resource_endpoint() -> None:
    """
    Reattach lookup addresses the deterministic terminal resource id.

    The helper must use the session resource endpoint rather than
    issuing a create request, otherwise lookup itself could duplicate
    ``claude/main``.
    """
    requested_urls: list[str] = []
    terminal_id = claude_native.claude_terminal_resource_id()

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return one running Claude terminal resource.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": terminal_id,
                "type": "terminal",
                "metadata": {"running": True},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        found = await claude_native._find_running_claude_terminal(client, "conv with space")

    assert found == terminal_id
    assert requested_urls == [
        "https://example.com/v1/sessions/conv%20with%20space"
        "/resources/terminals/terminal_claude_main"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 409, 502, 503])
async def test_find_running_claude_terminal_miss_statuses_relaunch(
    status_code: int,
) -> None:
    """
    Missing or unavailable prior runners cause a deterministic relaunch.

    :param status_code: HTTP status returned by the Omnigent resource lookup.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a reattach miss response.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        del request
        return httpx.Response(status_code, json={"error": {"message": "not attachable"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        found = await claude_native._find_running_claude_terminal(client, "conv_abc")

    assert found is None


# ── same-machine tmux attach (Phase 4) ─────────────────────


def _make_fake_tmux(directory: Path) -> None:
    """
    Create an executable ``tmux`` stub in *directory*.

    Lets a test make ``shutil.which("tmux")`` resolve deterministically
    by putting *directory* on ``PATH`` — without depending on whether a
    real tmux is installed, and without clobbering the ``shutil``
    module singleton.

    :param directory: Directory to drop the stub into.
    """
    exe = directory / "tmux"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)


@pytest.mark.asyncio
async def test_read_claude_terminal_tmux_parses_metadata() -> None:
    """
    The tmux coordinates come straight from the terminal resource
    metadata.

    Proves the socket string is wrapped to a ``Path`` and the target is
    carried through. If the runner stopped advertising these (or the
    key names drift), the parse would yield ``None`` and the CLI would
    silently lose the direct-attach fast path — falling back to the
    WebSocket relay the feature is meant to avoid.
    """
    terminal_id = claude_native.claude_terminal_resource_id()

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one running Claude terminal with tmux coordinates."""
        del request
        return httpx.Response(
            200,
            json={
                "id": terminal_id,
                "type": "terminal",
                "metadata": {
                    "running": True,
                    "tmux_socket": "/tmp/omnigent-501/claude/tmux.sock",
                    "tmux_target": "main",
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        result = await claude_native._read_claude_terminal_tmux(client, "conv_abc")

    assert result.socket == Path("/tmp/omnigent-501/claude/tmux.sock")
    assert result.target == "main"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        # 404: terminal resource not found.
        httpx.Response(404, json={}),
        # 200 but no metadata block at all.
        httpx.Response(200, json={"id": "terminal_claude_main", "type": "terminal"}),
        # 200 with metadata but no tmux keys (e.g. a non-tmux backend).
        httpx.Response(
            200,
            json={"id": "terminal_claude_main", "type": "terminal", "metadata": {"running": True}},
        ),
    ],
    ids=["not-found", "no-metadata", "metadata-without-tmux"],
)
async def test_read_claude_terminal_tmux_unavailable(response: httpx.Response) -> None:
    """
    Any miss yields ``(None, None)`` so the caller takes the WS path.

    A non-``(None, None)`` result on these inputs would mean the helper
    fabricated coordinates and the CLI would try to attach to a tmux
    socket that doesn't exist.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the parametrized miss response."""
        del request
        return response

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        result = await claude_native._read_claude_terminal_tmux(client, "conv_abc")

    assert result.socket is None
    assert result.target is None


def test_can_attach_direct_tmux_true_when_socket_local_and_tmux_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Direct attach is chosen when the socket exists here and tmux is
    on PATH.

    This is the same-machine fast path: the runner shares this host (its
    tmux socket is on the local filesystem) so the local TTY can attach
    straight to the pane. A ``False`` here would send the user back to
    the WebSocket relay even on their own machine — the exact regression
    this feature fixes.
    """
    socket = tmp_path / "tmux.sock"
    socket.write_text("")
    _make_fake_tmux(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
        bridge_dir=tmp_path,
        reattached=False,
        tmux_socket=socket,
        tmux_target="main",
    )
    assert claude_native._can_attach_direct_tmux(prepared) is True


def test_can_attach_direct_tmux_false_when_socket_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A non-existent socket means the runner is remote → WebSocket path.

    The socket lives on the runner's filesystem; if it isn't present
    locally the runner is on another machine and a direct attach is
    impossible.
    """
    _make_fake_tmux(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
        bridge_dir=tmp_path,
        reattached=False,
        tmux_socket=tmp_path / "does-not-exist.sock",
        tmux_target="main",
    )
    assert claude_native._can_attach_direct_tmux(prepared) is False


def test_can_attach_direct_tmux_false_when_tmux_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without ``tmux`` on PATH the direct attach can't run → WebSocket path.

    PATH is pointed at an empty dir (no tmux stub), so
    ``shutil.which("tmux")`` returns ``None`` even though the socket
    exists.
    """
    socket = tmp_path / "tmux.sock"
    socket.write_text("")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
        bridge_dir=tmp_path,
        reattached=False,
        tmux_socket=socket,
        tmux_target="main",
    )
    assert claude_native._can_attach_direct_tmux(prepared) is False


def test_can_attach_direct_tmux_false_when_fields_none(tmp_path: Path) -> None:
    """
    A terminal that advertised no tmux coordinates → WebSocket path.

    The fresh-launch / reattach paths leave ``tmux_socket`` /
    ``tmux_target`` ``None`` when the runner exposed nothing; the guard
    must reject that rather than calling ``.exists()`` on ``None``.
    """
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
        bridge_dir=tmp_path,
        reattached=False,
    )
    assert claude_native._can_attach_direct_tmux(prepared) is False


@pytest.mark.asyncio
async def test_ensure_local_claude_resume_transcript_uses_workspace_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The resume transcript lands under the *workspace* project dir, not
    the process cwd.

    This is what lets a runner-side cold resume work: the runner passes
    its ``OMNIGENT_RUNNER_WORKSPACE`` (not the runner process's actual
    cwd), so the synthesized transcript sits where the ``claude``
    process — launched with that workspace as cwd — will look for it. If
    the helper ignored ``workspace`` and used ``Path.cwd()``, the file
    would land in the wrong project dir and ``--resume`` would find
    nothing.
    """
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    workspace = Path("/work/some-repo")

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a one-message (resumable) item-history page."""
        del request
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
                "has_more": False,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        written = await claude_native._ensure_local_claude_resume_transcript(
            client,
            session_id="conv_abc",
            external_session_id="sid123",
            workspace=workspace,
        )

    expected_dir = projects / claude_native._sanitize_claude_project_name(str(workspace))
    # Under the WORKSPACE-derived project dir (proves the param is
    # honoured, not Path.cwd()), and the file was actually created.
    assert written == expected_dir / "sid123.jsonl"
    assert written.is_file()
    # The synthesized transcript holds the converted message, not an empty
    # file (an empty file would make ``claude --resume`` exit on launch).
    assert written.read_text(encoding="utf-8").strip() != ""


@pytest.mark.asyncio
async def test_ensure_local_claude_resume_transcript_returns_none_when_no_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Empty Omnigent history → ``None`` and no transcript file written.

    ``claude --resume`` against a zero-record transcript exits with "No
    conversation found with session ID" instead of starting; for claude-
    native (terminal == agent) that tears the tmux session down and the
    web UI shows "Bridge closed: terminal resource not found". When the AP
    conversation yields no resumable records the helper must return
    ``None`` (so the caller launches fresh, no ``--resume``) and must NOT
    leave an empty ``<sid>.jsonl`` behind for a later launch to trip over.
    """
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    workspace = Path("/work/some-repo")

    def handler(request: httpx.Request) -> httpx.Response:
        """Return an empty (but valid) item-history page."""
        del request
        return httpx.Response(200, json={"data": [], "has_more": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        written = await claude_native._ensure_local_claude_resume_transcript(
            client,
            session_id="conv_abc",
            external_session_id="sid123",
            workspace=workspace,
        )

    assert written is None
    expected = (
        projects / claude_native._sanitize_claude_project_name(str(workspace)) / "sid123.jsonl"
    )
    assert not expected.exists()


def _resume_rebuild_handler(
    *,
    fail_file_fetch: bool = False,
    malformed_meta: bool = False,
) -> Any:
    """Mock server for resume-rebuild tests: history with a file_id image."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/resources/files/file_img/content"):
            if fail_file_fetch:
                return httpx.Response(404)
            return httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})
        if path.endswith("/resources/files/file_img"):
            if fail_file_fetch:
                return httpx.Response(404)
            if malformed_meta:
                # A proxy/gateway answering 200 with an HTML error page.
                return httpx.Response(
                    200,
                    content=b"<html>gateway error</html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                200,
                json={"id": "file_img", "filename": "photo.png", "content_type": "image/png"},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "file_id": "file_img",
                                "filename": "photo.png",
                            },
                            {"type": "input_text", "text": "look at this image"},
                        ],
                    }
                ],
                "has_more": False,
            },
        )

    return handler


@pytest.mark.asyncio
async def test_ensure_local_claude_resume_transcript_rematerializes_image_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A prior-turn image survives the resume-transcript rebuild.

    Items are persisted with unresolved ``file_id`` blocks and the old
    converter kept only ``input_text`` blocks, so every relaunch silently
    dropped the image from Claude's rebuilt transcript — the only
    survivor was a machine-local tmp path that is dead after a
    cross-machine resume or tmp cleanup. The rebuild must fetch the bytes
    back, re-materialize them under the session bridge dir, and reference
    the fresh file with a live ``[Attached: <path>]`` line.
    """
    from omnigent import claude_native_bridge

    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    bridge_dir = tmp_path / "bridge"
    monkeypatch.setattr(
        claude_native_bridge, "bridge_dir_for_conversation_id", lambda _conv: bridge_dir
    )
    workspace = Path("/work/some-repo")

    transport = httpx.MockTransport(_resume_rebuild_handler())
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        written = await claude_native._ensure_local_claude_resume_transcript(
            client,
            session_id="conv_abc",
            external_session_id="sid123",
            workspace=workspace,
        )

    assert written is not None
    records = [json.loads(line) for line in written.read_text(encoding="utf-8").splitlines()]
    user_content = records[0]["message"]["content"]
    # A lone surviving block collapses to a plain string.
    texts = (
        [user_content]
        if isinstance(user_content, str)
        else [block["text"] for block in user_content]
    )
    attached_lines = [t for t in texts if t.startswith("[Attached: ")]
    assert attached_lines, f"image block was silently dropped from the rebuild: {texts}"
    attached_path = Path(attached_lines[0].removeprefix("[Attached: ").removesuffix("]"))
    # The referenced file is live on THIS machine with the fetched bytes.
    assert attached_path.parent == bridge_dir / "uploads"
    assert attached_path.read_bytes() == b"png-bytes"
    assert "look at this image" in " ".join(texts)
    assert "file_id" not in written.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_ensure_local_claude_resume_transcript_marks_unresolvable_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A fetch-back failure leaves a visible marker, never a silent drop.

    When the file resource endpoints fail (auth/proxy/deleted file), the
    rebuilt record must carry the could-not-load placeholder so the model
    and the user see the attachment was lost instead of hallucinating.
    """
    from omnigent import claude_native_bridge

    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    bridge_dir = tmp_path / "bridge"
    monkeypatch.setattr(
        claude_native_bridge, "bridge_dir_for_conversation_id", lambda _conv: bridge_dir
    )
    workspace = Path("/work/some-repo")

    transport = httpx.MockTransport(_resume_rebuild_handler(fail_file_fetch=True))
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        written = await claude_native._ensure_local_claude_resume_transcript(
            client,
            session_id="conv_abc",
            external_session_id="sid123",
            workspace=workspace,
        )

    assert written is not None
    records = [json.loads(line) for line in written.read_text(encoding="utf-8").splitlines()]
    user_content = records[0]["message"]["content"]
    texts = (
        [user_content]
        if isinstance(user_content, str)
        else [block["text"] for block in user_content]
    )
    assert "[Attachment photo.png could not be loaded]" in texts
    assert "look at this image" in " ".join(texts)


@pytest.mark.asyncio
async def test_ensure_local_claude_resume_transcript_survives_malformed_file_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 200-but-unparseable metadata response must not abort the rebuild.

    Metadata only supplies the media-type hint; when its body is garbage
    (a proxy answering 200 with an HTML error page), the resolver falls
    back to the content response's ``Content-Type`` and the attachment
    still re-materializes — the whole transcript rebuild must not die on
    one bad metadata body.
    """
    from omnigent import claude_native_bridge

    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    bridge_dir = tmp_path / "bridge"
    monkeypatch.setattr(
        claude_native_bridge, "bridge_dir_for_conversation_id", lambda _conv: bridge_dir
    )
    workspace = Path("/work/some-repo")

    transport = httpx.MockTransport(_resume_rebuild_handler(malformed_meta=True))
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        written = await claude_native._ensure_local_claude_resume_transcript(
            client,
            session_id="conv_abc",
            external_session_id="sid123",
            workspace=workspace,
        )

    assert written is not None
    records = [json.loads(line) for line in written.read_text(encoding="utf-8").splitlines()]
    user_content = records[0]["message"]["content"]
    texts = (
        [user_content]
        if isinstance(user_content, str)
        else [block["text"] for block in user_content]
    )
    attached_lines = [t for t in texts if t.startswith("[Attached: ")]
    assert attached_lines, f"attachment was dropped on malformed metadata: {texts}"
    attached_path = Path(attached_lines[0].removeprefix("[Attached: ").removesuffix("]"))
    # Bytes came from the content response; the media type came from its
    # Content-Type header, not the unparseable metadata body.
    assert attached_path.read_bytes() == b"png-bytes"
    assert "look at this image" in " ".join(texts)


@pytest.mark.asyncio
async def test_create_claude_session_omits_title_for_generic_seed_path() -> None:
    """
    Session creation must not seed a title in create-time metadata.

    The previous placeholder-title carve-out has been removed: claude-
    native sessions now go through the same generic title-seed path as
    every other session — created with no title, then populated by
    ``_seed_missing_title_from_user_message`` on the first forwarded
    user message. The sidebar fills the create-to-first-message gap
    by rendering a default label off the
    ``omnigent.wrapper = claude-code-native-ui`` label
    (see ``web/src/shell/sidebarNav.ts::conversationDisplayLabel``).
    The labels must still reach the server unchanged because that
    sidebar fallback keys off the wrapper label.
    """
    captured_metadata: dict[str, object] = {}
    session_id_returned = "conv_0123456789abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Mock POST /v1/sessions (create). PATCH must not be issued.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        if request.method == "POST":
            body = request.content.decode("utf-8")
            marker = 'name="metadata"'
            idx = body.find(marker)
            assert idx != -1, f"metadata part not found in body: {body!r}"
            json_start = body.index("\r\n\r\n", idx) + 4
            json_end = body.index("\r\n", json_start)
            captured_metadata.update(json.loads(body[json_start:json_end]))
            return httpx.Response(200, json={"session_id": session_id_returned})
        raise AssertionError(
            f"unexpected request: {request.method} {request.url}; "
            "_create_claude_session must not PATCH the title — the server's "
            "seed helper now populates an empty title on the first user message."
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        session_id = await claude_native._create_claude_session(
            client,
            b"fake-bundle-bytes",
            bridge_id="bridge_abc",
        )

    assert session_id == session_id_returned
    # No title in metadata: any title here would defeat the generic seed
    # path and resurrect the claude-specific carve-out we just removed.
    assert "title" not in captured_metadata, (
        f"_create_claude_session must not include a title; the placeholder "
        f"behavior was removed in favor of the generic seed path plus a "
        f"label-driven sidebar default. Got title="
        f"{captured_metadata.get('title')!r}."
    )
    # Labels still reach the server unchanged: the sidebar's "Claude Code"
    # default label depends on the wrapper label being present.
    assert captured_metadata["labels"] == {
        **claude_native._SESSION_LABELS,
        claude_native.BRIDGE_ID_LABEL_KEY: "bridge_abc",
    }, (
        "_SESSION_LABELS must reach the server unchanged — the sidebar "
        "uses the wrapper label to render 'Claude Code' as the default "
        "display name until the seed helper populates an actual title."
    )


# ---------------------------------------------------------------------------
# Reconnect tests
#
# These tests cover the reconnect loop that lets ``omnigent claude``
# survive a remote-server bounce. The bug they guard against:
# previously, a single transient WebSocket close took down the entire
# TUI session — the user had to relaunch and lost their live Claude
# state. After the fix, the wrapper retries the WS attach with capped
# exponential backoff and invokes a recovery callback so the runner
# subprocess and session→runner binding are restored before each
# reconnect attempt.
# ---------------------------------------------------------------------------


@dataclass
class _AttachCallRecord:
    """
    One :func:`_attach_with_reconnect` attach attempt captured by a fake.

    :param attach_url: URL the helper passed in. Stable across attempts
        because the reconnect loop reuses the URL it was constructed with.
    :param headers: Auth headers the helper passed in. Same stability
        guarantee as ``attach_url`` — headers are not re-resolved per
        attempt by the reconnect loop itself (the optional ``recover``
        callback owns header refresh).
    """

    attach_url: str
    headers: dict[str, str]


@dataclass
class _ScriptedAttach:
    """
    Scripted attach callable for reconnect-loop tests.

    Each entry in *script* is either ``True`` (treat as user-requested
    exit), ``False`` (treat as server-initiated close), or a
    ``BaseException`` instance to raise.

    :param script: List of outcomes the fake will produce one per
        invocation, in order. Test must size the script so it lasts as
        many attempts as the loop will make — extra entries are ignored,
        a too-short script raises ``IndexError`` to fail loudly.
    :param calls: Captured record of each invocation, in order. Tests
        assert on this to verify the loop's retry shape.
    """

    script: list[bool | BaseException]
    calls: list[_AttachCallRecord] = field(default_factory=list)

    async def __call__(self, attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        Invoke the next scripted outcome.

        :param attach_url: URL passed by :func:`_attach_with_reconnect`.
        :param headers: Headers passed by :func:`_attach_with_reconnect`.
        :returns: ``True`` or ``False`` per the script; raises if the
            script entry is an exception.
        """
        self.calls.append(_AttachCallRecord(attach_url=attach_url, headers=dict(headers)))
        outcome = self.script[len(self.calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _make_connection_closed(code: int) -> ConnectionClosedError:
    """
    Build a :class:`ConnectionClosedError` whose ``rcvd`` reports *code*.

    :param code: The WebSocket close code the peer would have sent,
        e.g. ``WS_CLOSE_TERMINAL_NOT_FOUND`` (``4404``).
    :returns: A real ``ConnectionClosedError``. The reconnect helper
        introspects ``rcvd.code`` to decide whether to retry, so the
        ``rcvd`` close frame must be populated.
    """
    # ConnectionClosedError's signature: (rcvd, sent). We populate
    # `rcvd` so _is_terminal_not_found_close can read .rcvd.code.
    return ConnectionClosedError(Close(code, ""), None)


@pytest.mark.asyncio
async def test_attach_with_reconnect_exits_immediately_on_user_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A user-initiated exit (``attach`` returns ``True``) ends the loop
    on the first attempt.

    Without this behavior the wrapper would keep retrying the WebSocket
    handshake after the user pressed Ctrl+D, ignoring their request to
    leave the session.
    """
    # Patch sleep so the test never waits in the backoff branch (if the
    # loop were buggy, a wrong branch would hit asyncio.sleep with the
    # initial 0.5s delay — 100ms × n is fast but visible).
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    attach = _ScriptedAttach(script=[True])

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=lambda: _noop_async(),
    )

    # Exactly one attach call — no retries after a clean user exit.
    # If two calls land here, the loop is treating "user exit" as
    # "server bounce" and the user can't actually leave the session.
    assert len(attach.calls) == 1, (
        f"expected 1 attach call on user exit, got {len(attach.calls)}; "
        "the loop is retrying after a user-initiated EOF"
    )


@pytest.mark.asyncio
async def test_attach_with_reconnect_passes_terminal_gone_probe_to_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reconnect wiring enables the client-side terminal-gone watcher.

    The production ``omnigent claude`` path passes
    :func:`attach_local_terminal` through ``_attach_with_reconnect``.
    This test pins the handoff: when client-side close-on-gone is
    enabled, the attach callable receives a probe that checks the
    current terminal resource with the short watcher timeout.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    probe_calls: list[tuple[str, str, float]] = []
    attach_probe_seen = False

    async def fake_is_terminal_resource_gone(
        *,
        base_url: str,
        headers: dict[str, str],
        session_id: str,
        terminal_id: str,
        timeout_s: float = 10.0,
    ) -> bool:
        """
        Capture probe arguments and report the terminal gone.

        :param base_url: Omnigent base URL.
        :param headers: HTTP headers.
        :param session_id: Session id.
        :param terminal_id: Terminal resource id.
        :param timeout_s: Probe timeout in seconds.
        :returns: ``True``.
        """
        del base_url, headers
        probe_calls.append((session_id, terminal_id, timeout_s))
        return True

    async def fake_attach(
        attach_url: str,
        *,
        headers: dict[str, str],
        terminal_gone_probe: Any = None,
    ) -> bool:
        """
        Assert the terminal-gone probe is passed into attach.

        :param attach_url: Attach WebSocket URL.
        :param headers: WebSocket handshake headers.
        :param terminal_gone_probe: Probe passed by the reconnect loop.
        :returns: ``True`` to end the reconnect loop.
        """
        nonlocal attach_probe_seen
        assert attach_url.endswith(
            "/v1/sessions/conv_abc/resources/terminals/terminal_claude_main/attach"
        )
        assert headers == {"Authorization": "Bearer tok"}
        assert terminal_gone_probe is not None
        attach_probe_seen = await terminal_gone_probe()
        return True

    monkeypatch.setattr(
        claude_native,
        "_is_terminal_resource_gone",
        fake_is_terminal_resource_gone,
    )

    await claude_native._attach_with_reconnect(
        attach=fake_attach,
        attach_url="wss://example.com/original",
        headers={"Authorization": "Bearer tok"},
        recover=None,
        base_url="https://example.com",
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
        close_attach_on_terminal_gone=True,
    )

    assert attach_probe_seen is True
    assert probe_calls == [
        (
            "conv_abc",
            "terminal_claude_main",
            claude_native._CLAUDE_TERMINAL_GONE_WATCH_HTTP_TIMEOUT_S,
        )
    ]


@pytest.mark.asyncio
async def test_attach_with_reconnect_retries_after_websocket_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A non-terminal WebSocket exception triggers a backoff retry.

    Without retry, a server bounce that surfaces as an abnormal close
    would end the TUI session (the original reconnect bug).
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    # First two attempts raise a non-terminal abnormal-close error
    # (1011 is a server-side internal error — exactly the kind of
    # close a uvicorn restart can produce). Third attempt succeeds
    # with a user exit so the loop terminates.
    attach = _ScriptedAttach(
        script=[
            _make_connection_closed(1011),
            _make_connection_closed(1006),
            True,
        ],
    )

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=lambda: _noop_async(),
    )

    # Three attach calls = two failures + one success. Anything less
    # (e.g. 1) means the loop is propagating the exception instead of
    # retrying — i.e. the reconnect bug is back.
    assert len(attach.calls) == 3, (
        f"expected 3 attach calls (2 fail + 1 succeed), got {len(attach.calls)}; "
        "the reconnect loop is not retrying after a transient WS error"
    )


@pytest.mark.asyncio
async def test_attach_with_reconnect_exits_silently_on_terminal_not_found_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Close code 4404 (``WS_CLOSE_TERMINAL_NOT_FOUND``) means the terminal
    resource is gone — typically Claude exited and the tmux session
    died — so reconnecting is futile. The helper must return cleanly
    without raising and without further attach attempts.

    A regression here would either spin the loop forever against a
    terminal that no longer exists, or surface the ConnectionClosed up
    the stack as a crash on what is a normal end-of-session.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    attach = _ScriptedAttach(script=[_make_connection_closed(WS_CLOSE_TERMINAL_NOT_FOUND)])

    # No exception should escape — terminal-gone is a clean end state.
    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=lambda: _noop_async(),
    )

    # Single attach — the loop must not retry after 4404 because the
    # server has authoritatively said "no such resource". If 2 land
    # here, the loop is wasting backoff time on a doomed reconnect.
    assert len(attach.calls) == 1, (
        f"expected 1 attach call on terminal-not-found close, got {len(attach.calls)}; "
        "the loop is retrying after the terminal is permanently gone"
    )


@pytest.mark.asyncio
async def test_attach_with_reconnect_reports_detached_on_4405_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Close code 4405 (``WS_CLOSE_TERMINAL_DETACHED``) means the user
    detached from tmux — the session and Claude are still alive. The
    loop must end WITHOUT reconnecting and report ``DETACHED`` so the
    launcher keeps the runner serving the web UI.

    A regression here would either reconnect (snapping the user back
    into the session they tried to leave) or report ``EXITED`` (the old
    bug: the launcher then tears the runner down and the web UI flips
    to "Agent disconnected" even though Claude is still running).
    """

    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    attach = _ScriptedAttach(script=[_make_connection_closed(WS_CLOSE_TERMINAL_DETACHED)])

    outcome = await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=lambda: _noop_async(),
    )

    assert outcome is claude_native._AttachOutcome.DETACHED, (
        f"expected DETACHED outcome on 4405 close, got {outcome!r}; the "
        "launcher would otherwise tear down the still-live runner"
    )
    assert len(attach.calls) == 1, (
        f"expected 1 attach call on detach close, got {len(attach.calls)}; "
        "the loop is reconnecting and snapping the user back into the session"
    )


@pytest.mark.asyncio
async def test_attach_with_reconnect_reports_exited_on_terminal_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 4404 terminal-gone close reports ``EXITED`` (not ``DETACHED``).

    Pins the complement of the 4405 case: a genuinely-dead terminal
    must let the launcher tear the runner down, so the outcome must not
    be confused with a detach.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    attach = _ScriptedAttach(script=[_make_connection_closed(WS_CLOSE_TERMINAL_NOT_FOUND)])

    outcome = await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=lambda: _noop_async(),
    )

    assert outcome is claude_native._AttachOutcome.EXITED, (
        f"expected EXITED outcome on 4404 close, got {outcome!r}"
    )


@pytest.mark.asyncio
async def test_attach_with_reconnect_invokes_recover_between_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The recovery callback fires once between each pair of attach
    attempts but NOT before the first attempt. Otherwise a healthy
    session would needlessly bounce its runner on first connect.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    recover_calls: list[int] = []

    async def _recover() -> None:
        """
        Record this recovery firing. The body must remain a no-op so
        the test exercises only the loop's call shape.
        """
        recover_calls.append(len(recover_calls) + 1)

    attach = _ScriptedAttach(
        script=[
            _make_connection_closed(1011),  # attempt 1: fail
            _make_connection_closed(1011),  # attempt 2: fail
            True,  # attempt 3: user exit, loop terminates
        ],
    )

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=_recover,
    )

    # Three attaches, two recoveries. Recovery fires before attempts 2
    # and 3 only — never before attempt 1. If recover_calls is 3, the
    # loop is calling recover on first connect (wasteful, breaks the
    # local-server flow if it ever wires recover in). If 1, the loop
    # is only calling recover on the first failure, leaving subsequent
    # retries without runner / binding recovery.
    assert len(attach.calls) == 3
    assert len(recover_calls) == 2, (
        f"expected 2 recovery calls (between attempts 1→2 and 2→3), got {len(recover_calls)}"
    )


@pytest.mark.asyncio
async def test_attach_with_reconnect_recovery_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A raising recovery callback is logged and the loop still retries
    the attach. Otherwise a transient SDK / network hiccup in the
    recovery path (token refresh, runner subprocess startup race)
    would permanently kill the session even though the underlying
    server bounce had recovered.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    recover_calls = 0

    async def _flaky_recover() -> None:
        """
        Raise on the first call, succeed afterwards. Mirrors a
        token-refresh transient hiccup.
        """
        nonlocal recover_calls
        recover_calls += 1
        if recover_calls == 1:
            raise RuntimeError("transient: token refresh failed")

    attach = _ScriptedAttach(
        script=[
            _make_connection_closed(1011),  # attempt 1: fail
            _make_connection_closed(1011),  # attempt 2: fail (after flaky recover)
            True,  # attempt 3: user exit
        ],
    )

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=_flaky_recover,
    )

    # The loop must keep attempting attaches even after the first
    # recovery raised. If attach.calls is 1, the recovery's exception
    # propagated and killed the session — exactly the brittleness this
    # test guards against.
    assert len(attach.calls) == 3
    assert recover_calls == 2


@pytest.mark.asyncio
async def test_attach_with_reconnect_recover_none_does_not_retry_on_clean_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``recover=None`` (local-server flow), the loop is a single-shot:
    a clean server-side close ends the wrapper.

    Rationale: the local-server flow owns the server subprocess via
    ``_start_local_server``. If that server dies, there is nothing
    to reconnect to — retrying would just spin forever against a dead
    port. The remote-server flow is the one that wires a recover
    callback and gets reconnection.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    # First attach returns False (server-initiated clean close). With
    # recover=None the loop must NOT make a second attempt — even
    # though False would normally mean "server bounce, reconnect".
    attach = _ScriptedAttach(script=[False])

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=None,
    )

    assert len(attach.calls) == 1, (
        f"expected 1 attach call when recover=None, got {len(attach.calls)}; "
        "the loop is retrying without a recovery callback — the local-server "
        "flow would spin forever against a dead local server"
    )


@pytest.mark.asyncio
async def test_attach_with_reconnect_recover_none_propagates_websocket_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With ``recover=None`` a WebSocket error propagates so the
    local-server flow sees the failure and exits with a meaningful
    message instead of silently spinning. The 4404 terminal-gone
    sentinel is the one exception that still returns cleanly because
    it represents end-of-session, not a recoverable error.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    attach = _ScriptedAttach(script=[_make_connection_closed(1011)])

    # Without a recovery story, an abnormal close is an error the
    # caller must see. Suppressing it would mask the failure mode.
    with pytest.raises(ConnectionClosedError):
        await claude_native._attach_with_reconnect(
            attach=attach,
            attach_url="wss://example.com/attach",
            headers={"Authorization": "Bearer tok"},
            recover=None,
        )

    assert len(attach.calls) == 1


@pytest.mark.asyncio
async def test_attach_with_reconnect_recover_none_still_returns_on_terminal_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Even without a recovery callback, the 4404 close code must end the
    loop cleanly. That code means the terminal resource is gone — a
    normal end of session — so the wrapper exits without surfacing an
    error to the caller.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    attach = _ScriptedAttach(script=[_make_connection_closed(WS_CLOSE_TERMINAL_NOT_FOUND)])

    # Returns cleanly — no exception escapes.
    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=None,
    )

    assert len(attach.calls) == 1


@pytest.mark.asyncio
async def test_attach_with_reconnect_caps_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Exponential backoff is capped at ``_ATTACH_MAX_RECONNECT_DELAY_S``.
    A misconfigured cap (or an off-by-one in the doubling math) could
    grow the delay unboundedly, making the wrapper effectively dead
    after a few transient failures.
    """
    sleeps: list[float] = []

    async def _capture_sleep(delay: float) -> None:
        """
        Capture the delay each backoff step requests, without
        actually waiting.

        :param delay: Sleep argument passed by the reconnect loop.
        """
        sleeps.append(delay)

    monkeypatch.setattr(claude_native, "_sleep", _capture_sleep)
    # Long enough script to exercise the cap. Initial 0.5s doubles to
    # 1, 2, 4, then caps at 5 (the max). 10 failures is enough to see
    # the cap take effect multiple times.
    attach = _ScriptedAttach(script=[_make_connection_closed(1011)] * 10 + [True])

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=lambda: _noop_async(),
    )

    # The first sleep is the initial delay; each subsequent is at
    # most _ATTACH_MAX_RECONNECT_DELAY_S. The cap is what prevents
    # runaway exponential growth — if it's broken the last entry
    # would be 256s or more.
    assert sleeps[0] == claude_native._ATTACH_INITIAL_RECONNECT_DELAY_S
    assert all(s <= claude_native._ATTACH_MAX_RECONNECT_DELAY_S for s in sleeps), (
        f"backoff exceeded cap {claude_native._ATTACH_MAX_RECONNECT_DELAY_S}: {sleeps}"
    )
    # And the cap is actually reached — proves the doubling logic
    # ran far enough to hit the ceiling at least once.
    assert sleeps[-1] == claude_native._ATTACH_MAX_RECONNECT_DELAY_S


# ---------------------------------------------------------------------------
# Headers-mutated-in-place tests
#
# The recover callback was changed to mutate
# the shared headers dict in place (``clear() + update()``) rather
# than rebind the nonlocal name. The reconnect loop holds the dict
# reference, so an in-place mutation is the only way the next WS
# handshake sees the refreshed bearer. These tests guard that
# invariant — they would fail under the original "rebind" shape.
# ---------------------------------------------------------------------------


@dataclass
class _HeaderRecordingAttach:
    """
    Attach fake that snapshots the headers dict it received per call.

    :param outcomes: One outcome per call — ``True`` for user exit,
        ``False`` for server-initiated close, or an exception to raise.
    :param header_snapshots: One snapshot per call, captured by
        copy so later in-place mutations of the upstream dict don't
        retroactively change earlier records.
    """

    outcomes: list[bool | BaseException]
    header_snapshots: list[dict[str, str]] = field(default_factory=list)

    async def __call__(self, attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        :param attach_url: Ignored — captured by the integration test
            but irrelevant to this header-focused fake.
        :param headers: Headers dict the reconnect loop forwards.
            Snapshotted by ``dict(...)`` so a subsequent mutation
            doesn't poison this record.
        :returns: Next scripted outcome.
        """
        del attach_url
        self.header_snapshots.append(dict(headers))
        outcome = self.outcomes[len(self.header_snapshots) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_attach_with_reconnect_sees_in_place_header_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The reconnect loop's next attach call sees a header dict mutated
    in place by ``recover``.

    An earlier version had a real bug here: ``_recover`` rebinds the
    nonlocal ``headers`` variable in its closure scope, but the
    reconnect loop received the *original* dict reference at start
    time. A rotated Databricks bearer would not reach the new WS
    handshake — every reconnect would fail with 401 and the loop
    would spin forever printing "reconnecting...".

    The fix is to mutate the dict (``clear() + update()``). This
    test sets up an attach that fails once with a transient close,
    runs a recover that mutates the shared dict, and asserts that
    the *second* attach call observes the mutated dict.

    Failing this test means the bearer-rotation regression is back.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    headers = {"Authorization": "Bearer original"}
    attach = _HeaderRecordingAttach(
        outcomes=[_make_connection_closed(1011), True],
    )

    async def _recover_mutates() -> None:
        """
        Mutate the *headers* dict in place. The fix expects this
        pattern; a rebinding shape would not propagate.
        """
        headers.clear()
        headers["Authorization"] = "Bearer refreshed"

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers=headers,
        recover=_recover_mutates,
    )

    # First attach used the original bearer (call shape sanity).
    assert attach.header_snapshots[0] == {"Authorization": "Bearer original"}, (
        f"first attach should see the original bearer, got {attach.header_snapshots[0]}"
    )
    # Second attach must see the refreshed bearer. If this is still
    # "Bearer original", the recover callback's mutation did not
    # propagate to the loop's view of headers — exactly the bug
    # the PR review caught.
    assert attach.header_snapshots[1] == {"Authorization": "Bearer refreshed"}, (
        f"second attach should see the mutated bearer, got {attach.header_snapshots[1]}"
    )


# ---------------------------------------------------------------------------
# Terminal-gone probe tests
#
# A normal Claude exit (tmux ends
# cleanly with code 1000) was indistinguishable from a server bounce,
# the loop got a post-close probe that GETs the terminal resource
# and ends the loop when the resource reports gone/stopped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_with_reconnect_exits_when_probe_says_terminal_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A clean server-side close (no exception) plus a probe response of
    "terminal gone" ends the loop. Without the probe the loop would
    treat that close as a server bounce and reconnect forever — the
    exact failure mode the manual REPL verification hit when tmux
    exited because Claude quit.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    # Two clean closes. If the probe returns True after the first one,
    # the loop must exit and the second call must never happen.
    attach = _ScriptedAttach(script=[False, False])

    async def _gone_probe(**kwargs: Any) -> bool:
        """Pretend the Omnigent reports the terminal stopped."""
        del kwargs
        return True

    monkeypatch.setattr(claude_native, "_is_terminal_resource_gone", _gone_probe)

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=lambda: _noop_async(),
        base_url="https://example.com",
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
    )

    # Exactly one attach — the probe after that call said the
    # terminal is gone, so the loop returned without retrying.
    # A second attach would mean the probe is being ignored and
    # the wrapper is spinning against a dead terminal.
    assert len(attach.calls) == 1, (
        f"expected 1 attach call when probe reports terminal gone, "
        f"got {len(attach.calls)}; the terminal-gone probe is not "
        "ending the loop"
    )


@pytest.mark.asyncio
async def test_attach_with_reconnect_reconnects_when_probe_says_terminal_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A clean close plus a probe response showing the terminal is still
    running keeps the loop alive. The complementary path to the
    previous test — without it the loop would short-circuit even when
    the cause was a server bounce that the terminal will survive.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    # First attach: clean close → probe → still alive → retry.
    # Second attach: user exits, loop terminates.
    attach = _ScriptedAttach(script=[False, True])

    async def _alive_probe(**kwargs: Any) -> bool:
        """Pretend the Omnigent reports the terminal still running."""
        del kwargs
        return False

    monkeypatch.setattr(claude_native, "_is_terminal_resource_gone", _alive_probe)

    await claude_native._attach_with_reconnect(
        attach=attach,
        attach_url="wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
        recover=lambda: _noop_async(),
        base_url="https://example.com",
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
    )

    # Two attach calls — first was a clean close, probe said the
    # terminal is alive, so the loop reconnected. The second
    # (user exit) ended the loop. If only 1, the loop is treating
    # any clean close as terminal-gone, which would prematurely end
    # sessions that just hit a server bounce.
    assert len(attach.calls) == 2


@pytest.mark.asyncio
async def test_is_terminal_resource_gone_reports_404_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The probe treats HTTP 404 as definitive evidence the resource is
    gone. The wrapper uses this signal to end the loop instead of
    reconnecting against a session whose terminal was destroyed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Mock the terminal GET to return 404."""
        del request
        return httpx.Response(404, json={"detail": "not found"})

    real_client_cls = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def _mock_client_cls(**kwargs: Any) -> httpx.AsyncClient:
        """
        Return a real AsyncClient bound to the mock transport.

        Captures *real_client_cls* before the patch so this factory
        does not recurse into the patched name when constructing
        the underlying client.
        """
        return real_client_cls(transport=transport, **kwargs)

    monkeypatch.setattr(claude_native.httpx, "AsyncClient", _mock_client_cls)

    gone = await claude_native._is_terminal_resource_gone(
        base_url="https://example.com",
        headers={"Authorization": "Bearer tok"},
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
    )

    assert gone is True


@pytest.mark.asyncio
async def test_is_terminal_resource_gone_reports_running_false_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The probe treats ``metadata.running == False`` as definitive evidence
    the terminal is stopped. This is the runner-side signal for "tmux
    exited, the resource still exists but the process is dead" — the
    wrapper uses it to end cleanly after a normal Claude exit.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Mock the GET to return 200 with running=False."""
        del request
        return httpx.Response(
            200,
            json={
                "id": "terminal_claude_main",
                "type": "terminal",
                "metadata": {"running": False},
            },
        )

    real_client_cls = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def _mock_client_cls(**kwargs: Any) -> httpx.AsyncClient:
        """Real AsyncClient bound to the mock transport; see above."""
        return real_client_cls(transport=transport, **kwargs)

    monkeypatch.setattr(claude_native.httpx, "AsyncClient", _mock_client_cls)

    gone = await claude_native._is_terminal_resource_gone(
        base_url="https://example.com",
        headers={"Authorization": "Bearer tok"},
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
    )

    assert gone is True


@pytest.mark.asyncio
async def test_is_terminal_resource_gone_treats_transport_errors_as_not_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A bouncing server (HTTP unreachable) must not be misread as
    "terminal gone" — that would end the loop right when it should
    be retrying the WS attach against the new server.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate a server still down by raising ConnectError."""
        del request
        raise httpx.ConnectError("connection refused")

    real_client_cls = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def _mock_client_cls(**kwargs: Any) -> httpx.AsyncClient:
        """Real AsyncClient bound to the mock transport; see above."""
        return real_client_cls(transport=transport, **kwargs)

    monkeypatch.setattr(claude_native.httpx, "AsyncClient", _mock_client_cls)

    gone = await claude_native._is_terminal_resource_gone(
        base_url="https://example.com",
        headers={"Authorization": "Bearer tok"},
        session_id="conv_abc",
        terminal_id="terminal_claude_main",
    )

    # False means "keep retrying" — the loop tries the attach again,
    # and either the attach succeeds (server is back) or it fails
    # with 4404 (terminal really is gone) and exits authoritatively.
    assert gone is False


# ---------------------------------------------------------------------------
# Integration test: real WebSocket server + real attach_local_terminal
#
# Drives the actual ``attach_local_terminal`` function against a real
# ``websockets.serve`` server we control. The server "bounces" by
# closing the connection mid-session; the test verifies the reconnect
# loop reconnects to the second incarnation and that the recovery
# callback fires. This is the closest-to-real-world test that does
# not need tmux / a real Claude binary.
# ---------------------------------------------------------------------------


@dataclass
class _FakeTerminalServer:
    """
    Minimal echo WebSocket server stand-in for the Omnigent terminal-attach
    route. Tracks accept counts and supports a coordinated "bounce".

    :param accept_count: Number of WS connections accepted so far.
        Tests assert on this to prove the client reconnected.
    :param close_codes: List of close codes the server should send,
        one per connection, in order. ``None`` keeps the connection
        open until the test releases it.
    :param release_event: Event that, when set, releases all currently
        open server-side connections (so the test can wind down).
    :param port: Actual port the server bound to. Populated by
        :func:`_run_fake_ws_server` after the server starts so tests
        avoid the bind→use race of allocating a port separately and
        passing it to ``websockets.serve``.
    """

    accept_count: int = 0
    close_codes: list[int | None] = field(default_factory=list)
    release_event: asyncio.Event = field(default_factory=asyncio.Event)
    port: int = 0


async def _run_fake_ws_server(state: _FakeTerminalServer) -> Any:
    """
    Start a websockets server that follows *state*'s close-code script.

    Each accepted connection bumps ``state.accept_count``, reads its
    close code from ``state.close_codes`` (by accept ordering), and
    either closes immediately with that code or holds the socket until
    ``state.release_event`` is set. The server binds to port 0 so the
    OS picks a guaranteed-free port; the assigned port is written back
    to ``state.port``. Avoids the race where a separate ``socket.bind``
    + ``getsockname`` releases the port before ``websockets.serve``
    rebinds.

    :param state: Shared server state. The test inspects it after each
        client attempt to assert on reconnect counts.
    :returns: The websockets server object so the caller can close it.
    """

    async def handler(ws: Any) -> None:
        """
        Per-connection handler. Captured close-code script governs
        whether to bounce immediately or hold the socket.

        :param ws: Connected websockets server-side connection.
        """
        attempt = state.accept_count
        state.accept_count += 1
        # Drain the initial resize frame attach_local_terminal sends
        # right after handshake so the test can rely on a stable
        # connection state before exercising bounce/EOF behavior.
        # ConnectionClosed surfaces immediately if the peer is already
        # closing; TimeoutError fires when the peer holds the socket
        # without sending anything (legitimate when the test scripts
        # a 1.0s probe before driving more behavior).
        with contextlib.suppress(asyncio.TimeoutError, ConnectionClosedError):
            await asyncio.wait_for(ws.recv(), timeout=1.0)
        if attempt < len(state.close_codes):
            code = state.close_codes[attempt]
            if code is not None:
                await ws.close(code=code, reason="bounce")
                return
        # No scripted close → hold open until released.
        await state.release_event.wait()
        await ws.close(code=1000, reason="test done")

    server = await websockets.serve(handler, "127.0.0.1", 0)
    # ``server.sockets`` lists every bound listener; we asked for one
    # so index [0] is safe. Index [1] of the address tuple is the port.
    state.port = server.sockets[0].getsockname()[1]
    return server


@pytest.mark.asyncio
async def test_attach_reconnects_through_real_websocket_bounce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    End-to-end: a real ``attach_local_terminal`` reconnects after the
    server closes its WebSocket mid-session.

    This is the regression test for the reconnect loop. The bug:
    ``omnigent claude`` exited after the first WebSocket close, so
    a server redeploy ended the user's Claude session. The fix wraps
    the attach in a reconnect loop guarded by a recovery callback;
    this test drives that loop against a real websockets server that
    closes its first two connections with codes a Databricks Apps
    redeploy can produce (4500 internal-error, then 1011
    server-error). The third connection holds open until the test's
    stdin pipe sends EOF, simulating the user pressing Ctrl+D.

    What would fail without the fix:
    - ``attach_local_terminal`` returns after the first close
    - ``_attach_with_reconnect`` is absent or never re-enters attach
    - the test would see ``accept_count == 1`` and the recover
      callback never fired

    What the assertions prove:
    - ``accept_count == 3`` proves the client reconnected twice
    - ``recover_calls == 2`` proves the recovery callback fires
      between attempts (not before the first attempt)
    - the call returns cleanly with no exception, proving the
      stdin-EOF path still terminates the loop correctly
    """
    # Patch the production backoff helper to a no-op so CI is not
    # paying 0.5 + 1.0s of real wall time per reconnect. The real
    # ``websockets`` round-trip is what this test exercises; the
    # backoff is unit-tested separately.
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    state = _FakeTerminalServer(
        # Two bounces, then hold open until stdin EOF.
        # 4500 = WS_CLOSE_INTERNAL_ERROR (also a code the server route
        # uses when the runner tunnel factory fails on a stale
        # registration after restart). 1011 = stock server-error.
        close_codes=[4500, 1011, None],
    )
    server = await _run_fake_ws_server(state)

    # Pipe pair for stdin: we write EOF to the write end when we want
    # the attach loop to terminate cleanly. attach_local_terminal
    # treats EOF as user-initiated exit and returns True.
    stdin_r, stdin_w = os.pipe()
    # Stdout pipe: attach_local_terminal writes received WS frames to
    # this fd. We don't actually consume; the os.write just needs a
    # valid descriptor that won't block on its kernel buffer for the
    # small amount of data this test sends (none).
    stdout_r, stdout_w = os.pipe()

    recover_calls: list[int] = []

    async def _recover() -> None:
        """
        No-op recovery for the test — the fake server already accepts
        any reconnect attempt, so there is no runner subprocess to
        restart. We only count invocations to prove the recovery hook
        is wired into the loop.
        """
        recover_calls.append(1)

    async def _drive_eof_after_two_bounces() -> None:
        """
        Wait until the third connection has been accepted, then send
        EOF down stdin so the attach loop terminates cleanly.
        """
        for _ in range(50):
            if state.accept_count >= 3:
                # The third connection is the holding one. Closing
                # the write end of the stdin pipe sends EOF to the
                # reader inside attach_local_terminal.
                os.close(stdin_w)
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"server did not see 3 connections within 2.5s; accept_count={state.accept_count}"
        )

    eof_task = asyncio.create_task(_drive_eof_after_two_bounces())

    async def _attach_with_pipes(attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        Wrap :func:`attach_local_terminal` so the reconnect loop's
        attach signature matches while we still pass test pipes for
        stdin/stdout.

        :param attach_url: URL the reconnect loop hands in. Stable
            across attempts.
        :param headers: Headers the reconnect loop hands in.
        :returns: Forwarded value from
            :func:`attach_local_terminal`.
        """
        return await claude_native.attach_local_terminal(
            attach_url,
            headers=headers,
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
        )

    try:
        await claude_native._attach_with_reconnect(
            attach=_attach_with_pipes,
            attach_url=f"ws://127.0.0.1:{state.port}/attach",
            headers={},
            recover=_recover,
            # base_url/session_id/terminal_id deliberately omitted —
            # the fake server has no /v1/sessions/... HTTP endpoint
            # to probe, so the terminal-gone probe must be inactive
            # for this test. Stdin EOF still drives the clean exit.
        )
    finally:
        # Best-effort cleanup. ``stdin_w`` may already be closed —
        # the EOF driver closes it as part of the success path — so
        # suppress the resulting OSError on the duplicate close. The
        # other three descriptors are owned by this test and stay open
        # until here.
        with contextlib.suppress(OSError):
            os.close(stdin_w)
        os.close(stdin_r)
        os.close(stdout_w)
        os.close(stdout_r)
        state.release_event.set()
        await eof_task
        server.close()
        await server.wait_closed()

    # Three accepts = two failed/bounced + one successful that
    # received the EOF. The fix's whole point is that the wrapper
    # survives the first two bounces; if accept_count is 1, the
    # reconnect loop is missing and the user-reported bug is back.
    assert state.accept_count == 3, (
        f"expected 3 server-side accepts (2 bounces + 1 final), "
        f"got {state.accept_count}; the reconnect loop did not "
        "reconnect after the server-side close"
    )
    # Two recoveries — once before each reconnect attempt, never
    # before the first attempt. A count of 3 would mean the loop is
    # calling recover unnecessarily on first connect.
    assert len(recover_calls) == 2, (
        f"expected 2 recovery callbacks between attempts, got {len(recover_calls)}"
    )


@pytest.mark.asyncio
async def test_attach_exits_on_real_websocket_close_with_4404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    End-to-end: a real ``attach_local_terminal`` exits the reconnect
    loop on a 4404 server close instead of spinning.

    Regression for the user-reported endless ``Claude session
    connection closed by server; reconnecting...`` loop when claude
    exits inside tmux. The server bridge now closes with
    ``WS_CLOSE_TERMINAL_NOT_FOUND`` on PTY EOF; this test pins the
    client side. Before the fix, ``_websocket_to_stdout``'s
    ``async for message in ws`` swallowed the close, ``attach``
    returned normally, and the outer loop went down the "clean
    server close → retry" branch — forever.

    Asserts ``accept_count == 1`` and an empty ``recover_calls`` so a
    regression that retries even once surfaces here.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(claude_native, "_sleep", _noop_sleep)
    state = _FakeTerminalServer(close_codes=[WS_CLOSE_TERMINAL_NOT_FOUND])
    server = await _run_fake_ws_server(state)

    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()

    recover_calls: list[int] = []

    async def _recover() -> None:
        """Track invocations; should never fire on a 4404 close."""
        recover_calls.append(1)

    async def _attach_with_pipes(attach_url: str, *, headers: dict[str, str]) -> bool:
        """Adapter so ``attach_local_terminal`` runs against the test pipes."""
        return await claude_native.attach_local_terminal(
            attach_url,
            headers=headers,
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
        )

    try:
        # Wrap in a timeout so a regression that loops doesn't hang CI.
        await asyncio.wait_for(
            claude_native._attach_with_reconnect(
                attach=_attach_with_pipes,
                attach_url=f"ws://127.0.0.1:{state.port}/attach",
                headers={},
                recover=_recover,
            ),
            timeout=5.0,
        )
    finally:
        os.close(stdin_w)
        os.close(stdin_r)
        os.close(stdout_w)
        os.close(stdout_r)
        state.release_event.set()
        server.close()
        await server.wait_closed()

    assert state.accept_count == 1, (
        f"Expected exactly 1 connection (loop exited on 4404), got {state.accept_count}. "
        "If >1, the client is not recognizing the 4404 close code and is retrying — "
        "the endless 'Claude session connection closed by server; reconnecting...' bug."
    )
    assert recover_calls == [], (
        f"Recover must not fire on a 4404 close; got {len(recover_calls)} call(s)."
    )


@pytest.mark.asyncio
async def test_websocket_to_stdout_does_not_block_event_loop() -> None:
    """
    ``_websocket_to_stdout`` offloads the blocking ``os.write`` to a
    thread so the event loop stays responsive.

    Regression: the original implementation called ``os.write(stdout_fd,
    bytes(message))`` synchronously in an ``async for`` body. When the
    user's terminal couldn't render output fast enough, the write
    blocked the event loop and froze both input and output.

    This test verifies that a concurrent coroutine can progress while
    ``_websocket_to_stdout`` is writing.
    """
    stdout_r, stdout_w = os.pipe()
    progress_flag = asyncio.Event()

    class _FakeWS:
        """Minimal async-iterable that yields one binary frame."""

        def __init__(self) -> None:
            self._sent = False
            self.close_code: int | None = None

        def __aiter__(self) -> _FakeWS:
            return self

        async def __anext__(self) -> bytes:
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return b"ok"

    async def _set_flag() -> None:
        """Coroutine that proves the event loop is not blocked."""
        await asyncio.sleep(0)
        progress_flag.set()

    fake_ws = _FakeWS()
    try:
        flag_task = asyncio.create_task(_set_flag())
        await claude_native._websocket_to_stdout(fake_ws, stdout_w)
        await asyncio.wait_for(flag_task, timeout=2.0)
        assert progress_flag.is_set()

        os.close(stdout_w)
        stdout_w = -1
        received = os.read(stdout_r, 4096)
        assert received == b"ok"
    finally:
        if stdout_w != -1:
            os.close(stdout_w)
        os.close(stdout_r)


class _AttachWSStub:
    """
    Fake attach WebSocket for local terminal attach tests.

    :param output_frames: Binary terminal output frames to emit before
        blocking forever, e.g. ``[b"tail"]``.
    """

    def __init__(self, output_frames: list[bytes] | None = None) -> None:
        """
        Initialize captured frames and scripted output.

        :param output_frames: Binary terminal output frames to emit
            before blocking forever, e.g. ``[b"tail"]``.
        """
        self.close_calls: list[tuple[int, str]] = []
        self.sent: list[str | bytes] = []
        self._output_frames = list(output_frames or [])

    def __aiter__(self) -> _AttachWSStub:
        """
        Return the async iterator object.

        :returns: This fake WebSocket.
        """
        return self

    async def __anext__(self) -> bytes:
        """
        Emit scripted frames, then block until cancelled.

        :returns: Next scripted binary terminal output frame.
        """
        if self._output_frames:
            return self._output_frames.pop(0)
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def send(self, data: str | bytes) -> None:
        """
        Record client-to-server frames.

        :param data: Frame sent by the attach client.
        :returns: None.
        """
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """
        Record local close calls.

        :param code: WebSocket close code.
        :param reason: WebSocket close reason.
        :returns: None.
        """
        self.close_calls.append((code, reason))


class _AttachWSContext:
    """
    Async context manager yielding a fake attach WebSocket.

    :param ws: Fake WebSocket to yield.
    """

    def __init__(self, ws: _AttachWSStub) -> None:
        """
        Create the context manager.

        :param ws: Fake WebSocket to yield.
        """
        self._ws = ws

    async def __aenter__(self) -> _AttachWSStub:
        """
        Enter and yield the fake WebSocket.

        :returns: Fake WebSocket.
        """
        return self._ws

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """
        Exit the fake WebSocket context.

        :param exc_type: Exception type, if any.
        :param exc: Exception instance, if any.
        :param tb: Exception traceback, if any.
        :returns: None.
        """


@pytest.mark.asyncio
async def test_attach_local_terminal_closes_ws_when_terminal_probe_reports_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The client can exit without waiting for server WS close propagation.

    The terminal resource GET can report stopped before the attach
    WebSocket close frame reaches the CLI. When the optional probe
    reports gone, ``attach_local_terminal`` should close the local WS
    and return instead of waiting for ``_websocket_to_stdout`` to see a
    peer close.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    fake_ws = _AttachWSStub(output_frames=[b"tail"])
    monkeypatch.setattr(
        claude_native,
        "_websocket_connect",
        lambda attach_url, *, headers: _AttachWSContext(fake_ws),
    )

    probe_calls = 0

    async def terminal_gone_probe() -> bool:
        """
        Report the terminal gone on the first watcher probe.

        :returns: ``True``.
        """
        nonlocal probe_calls
        probe_calls += 1
        return True

    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()
    try:
        user_requested_exit = await asyncio.wait_for(
            claude_native.attach_local_terminal(
                "ws://example.test/attach",
                headers={},
                stdin_fd=stdin_r,
                stdout_fd=stdout_w,
                terminal_gone_probe=terminal_gone_probe,
                terminal_gone_watch_interval_s=0.01,
            ),
            timeout=1.0,
        )
        assert user_requested_exit is False
        assert probe_calls >= 1
        assert fake_ws.close_calls == [(1000, "terminal resource stopped")]

        os.close(stdout_w)
        stdout_w = -1
        stdout_output = os.read(stdout_r, 4096)
        assert stdout_output == b"tail"
    finally:
        os.close(stdin_w)
        os.close(stdin_r)
        if stdout_w != -1:
            os.close(stdout_w)
        os.close(stdout_r)


@pytest.mark.asyncio
async def test_attach_local_terminal_surfaces_terminal_probe_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Unexpected terminal-gone probe failures fail the attach call.

    The normal probe helper catches expected HTTP and transport
    failures itself. If another error escapes the probe, the attach
    path should not silently keep waiting on the old server-close path.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    fake_ws = _AttachWSStub()
    monkeypatch.setattr(
        claude_native,
        "_websocket_connect",
        lambda attach_url, *, headers: _AttachWSContext(fake_ws),
    )

    async def terminal_gone_probe() -> bool:
        """
        Raise an unexpected probe failure.

        :raises RuntimeError: Always, to verify fail-loud behavior.
        """
        raise RuntimeError("probe broke")

    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()
    try:
        with pytest.raises(RuntimeError, match="probe broke"):
            await asyncio.wait_for(
                claude_native.attach_local_terminal(
                    "ws://example.test/attach",
                    headers={},
                    stdin_fd=stdin_r,
                    stdout_fd=stdout_w,
                    terminal_gone_probe=terminal_gone_probe,
                    terminal_gone_watch_interval_s=0.01,
                ),
                timeout=1.0,
            )
        assert fake_ws.close_calls == []
    finally:
        os.close(stdin_w)
        os.close(stdin_r)
        os.close(stdout_w)
        os.close(stdout_r)


def test_websocket_connect_sets_short_close_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Native-Claude attach does not inherit websockets' long close timeout.

    The close handshake runs after the terminal resource has already
    gone away, so a long default timeout shows up to users as process
    termination lag. This test pins the explicit timeout passed to the
    WebSocket client factory.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_connect(url: str, **kwargs: Any) -> object:
        """
        Capture WebSocket connect kwargs.

        :param url: WebSocket URL passed by the wrapper.
        :param kwargs: Keyword args passed to ``websockets.connect``.
        :returns: Sentinel context-manager object.
        """
        captured["url"] = url
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(websockets, "connect", fake_connect)

    result = claude_native._websocket_connect(
        "wss://example.com/attach",
        headers={"Authorization": "Bearer tok"},
    )

    assert result is sentinel
    # A wss:// attach URL carries a verifying SSL context (asserted separately
    # since an SSLContext isn't equality-comparable to a literal).
    assert isinstance(captured.pop("ssl"), ssl.SSLContext)
    # The wrapper adds the first-party Origin sentinel alongside the
    # caller's auth header so the server's CSWSH origin guard admits this
    # non-browser attach client; the caller's bearer is preserved.
    assert captured == {
        "url": "wss://example.com/attach",
        "additional_headers": {
            "Authorization": "Bearer tok",
            "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
        },
        "close_timeout": claude_native._CLAUDE_ATTACH_WS_CLOSE_TIMEOUT_S,
    }


# ---------------------------------------------------------------------------
# Helpers for the reconnect tests above.
# ---------------------------------------------------------------------------


async def _noop_sleep(_delay: float) -> None:
    """
    Stand-in for :func:`asyncio.sleep` that returns immediately.

    The reconnect-loop tests must not pay the real backoff cost
    (0.5s → 5s); fast-forwarding the sleeps keeps the suite snappy
    while still exercising the loop's call shape.

    :param _delay: Ignored — kept for signature parity.
    """


async def _noop_async() -> None:
    """
    Awaitable no-op used as a default recover callback in tests that
    don't care about recovery semantics.
    """


# ── _strip_resume_from_claude_args ──────────────────────────


@pytest.mark.parametrize(
    "args,expected",
    [
        # Bare flag at end: strip just the flag.
        (("--foo", "--resume"), ("--foo",)),
        # Flag with value: strip both tokens, keep surrounding args.
        (("--foo", "--resume", "conv_x", "--bar"), ("--foo", "--bar")),
        # Short form.
        (("-r", "conv_x"), ()),
        # ``=``-form: strip the single token.
        (("--resume=conv_x", "tail"), ("tail",)),
        (("-r=conv_x",), ()),
        # Names containing "resume" but not the flag itself must be kept.
        (("--no-resume-here",), ("--no-resume-here",)),
        # Bare ``--resume`` followed by another flag: keep the next flag,
        # only the ``--resume`` token itself is stripped.
        (("--resume", "--print"), ("--print",)),
        (("-r", "--dangerously-skip-permissions"), ("--dangerously-skip-permissions",)),
        # Empty input is a no-op.
        ((), ()),
    ],
)
def test_strip_resume_from_claude_args_removes_recognized_forms(
    args: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    """
    Defense-in-depth strip removes every ``--resume`` / ``-r`` form
    a user could route past Click. Names that merely contain the
    word ``resume`` (e.g. ``--no-resume-here``) MUST survive so we
    don't break unrelated upstream Claude flags. If this parametrize
    case fails, upstream Claude will see the Omnigent conv id and
    open its own picker against its native session-id namespace
    (the misroute's root cause).
    """
    assert claude_native._strip_resume_from_claude_args(args) == expected


# ── _resolve_cold_resume_args ───────────────────────────────


def _conversation_response_body(
    *,
    labels: dict[str, str],
    external_session_id: str | None,
) -> dict[str, Any]:
    """
    Build a minimal Omnigent ``GET /v1/sessions/{id}`` response body.

    The route returns the full ``SessionResponse`` shape; the
    cold-resume helper only reads two fields — ``labels`` and
    ``external_session_id`` — so the fixture stays small.

    :param labels: ``labels`` field for the response payload, e.g.
        ``{"omnigent.wrapper": "claude-code-native-ui"}``.
    :param external_session_id: ``external_session_id`` field or
        ``None``.
    :returns: JSON-encodable response dict.
    """
    return {
        "id": "conv_abc",
        "agent_id": "ag_test",
        "status": "idle",
        "created_at": 1,
        "labels": labels,
        "external_session_id": external_session_id,
    }


def _items_response_body(
    items: list[dict[str, Any]],
    *,
    has_more: bool = False,
    last_id: str | None = None,
) -> dict[str, Any]:
    """
    Build a minimal Omnigent item-list response body.

    :param items: Session item dicts returned in ``data``.
    :param has_more: Whether a following page exists.
    :param last_id: Cursor for the final item in this page, e.g.
        ``"msg_abc123"``.
    :returns: JSON-encodable paginated list response.
    """
    return {
        "object": "list",
        "data": items,
        "first_id": items[0]["id"] if items else None,
        "last_id": last_id or (items[-1]["id"] if items else None),
        "has_more": has_more,
    }


async def _httpx_client_with_canned_response(
    body: dict[str, Any] | None,
    status_code: int,
    items: list[dict[str, Any]] | None = None,
) -> httpx.AsyncClient:
    """
    Build an ``httpx.AsyncClient`` that returns one canned response.

    Used to drive :func:`_resolve_cold_resume_args` without standing
    up a real server. The handler ignores the URL, so the same
    canned reply works regardless of the conv id under test.

    :param body: JSON body to return; ``None`` for an empty body.
    :param status_code: HTTP status to return.
    :param items: Session items to return from the ``/items`` history
        fetch. Defaults to a single user message so cold resume has at
        least one record to synthesize — an empty history now makes
        ``_resolve_cold_resume_args`` decline ``--resume`` (launch
        fresh), so tests that exercise the ``--resume`` path must supply
        real history.
    :returns: Connected :class:`httpx.AsyncClient`.
    """
    history = (
        items
        if items is not None
        else [
            {
                "id": "msg_1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            }
        ]
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return the canned response for any incoming request.

        :param request: Incoming HTTP request (ignored).
        :returns: The canned :class:`httpx.Response`.
        """
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=_items_response_body(history))
        if body is None:
            return httpx.Response(status_code)
        return httpx.Response(status_code, json=body)

    transport = httpx.MockTransport(_handler)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_resolve_cold_resume_args_injects_external_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Claude-native conv with external_session_id set yields
    ``("--resume", "<sid>")`` so the spawned terminal launches
    ``claude --resume <sid>`` and reattaches to the prior transcript.
    Without this, cold resume would launch fresh claude — the user
    would keep the Omnigent conv id but lose claude-side context.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used to isolate Claude
        project state from the developer's real ``~/.claude`` tree.
    """
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", tmp_path / "projects")
    client = await _httpx_client_with_canned_response(
        _conversation_response_body(
            labels={"omnigent.wrapper": "claude-code-native-ui"},
            external_session_id="claude-uuid-abc",
        ),
        200,
    )
    async with client:
        args = await claude_native._resolve_cold_resume_args(client, "conv_abc")
    assert args == ("--resume", "claude-uuid-abc")


@pytest.mark.asyncio
async def test_resolve_cold_resume_args_declines_resume_when_no_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Empty Omnigent history → ``()`` (launch fresh), not ``("--resume", sid)``.

    An ``external_session_id`` is set, but the conversation has no
    convertible items, so the synthesized transcript would be empty.
    ``claude --resume`` against an empty transcript exits with "No
    conversation found" instead of starting; on the interactive CLI path
    that leaves the user with a dead launch. The helper must decline
    ``--resume`` and return ``()`` so the spawn launches a fresh claude,
    mirroring the runner-side guard in
    :func:`_ensure_local_claude_resume_transcript`.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory isolating Claude project state.
    """
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", tmp_path / "projects")
    client = await _httpx_client_with_canned_response(
        _conversation_response_body(
            labels={"omnigent.wrapper": "claude-code-native-ui"},
            external_session_id="claude-uuid-abc",
        ),
        200,
        items=[],
    )
    async with client:
        args = await claude_native._resolve_cold_resume_args(client, "conv_abc")
    assert args == ()
    # And it must not leave an empty transcript behind for a later launch.
    assert list((tmp_path / "projects").rglob("claude-uuid-abc.jsonl")) == []


@pytest.mark.asyncio
async def test_resolve_cold_resume_args_bootstraps_missing_local_claude_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Cross-machine cold resume downloads Omnigent history into Claude JSONL.

    This is the regression case behind the feature: the server knows
    the Omnigent conversation and Claude external session id, but the local
    machine has no ``~/.claude/projects/<cwd>/<sid>.jsonl``. The
    helper must fetch committed Omnigent items and write a transcript before
    returning ``--resume <sid>``; otherwise Claude starts with no
    local context.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = tmp_path / "claude-projects"
    requested_paths: list[str] = []
    page_1_items = [
        {
            "id": "msg_user_1",
            "response_id": "resp_1",
            "type": "message",
            "status": "completed",
            "role": "user",
            "content": [{"type": "input_text", "text": "open TODO.md"}],
        },
        {
            "id": "fc_read_1",
            "response_id": "resp_1",
            "type": "function_call",
            "status": "completed",
            "model": "claude-native-ui",
            "name": "Read",
            "arguments": '{"file_path":"TODO.md"}',
            "call_id": "toolu_read_1",
        },
    ]
    page_2_items = [
        {
            "id": "fco_read_1",
            "response_id": "resp_1",
            "type": "function_call_output",
            "status": "completed",
            "call_id": "toolu_read_1",
            "output": "contents",
        },
        {
            "id": "msg_assistant_1",
            "response_id": "resp_1",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "model": "claude-native-ui",
            "content": [{"type": "output_text", "text": "TODO.md says contents"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve the session snapshot and two chronological item pages.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        requested_paths.append(str(request.url))
        if request.url.path == "/v1/sessions/conv_abc":
            return httpx.Response(
                200,
                json=_conversation_response_body(
                    labels={"omnigent.wrapper": "claude-code-native-ui"},
                    external_session_id="claude-uuid-abc",
                ),
            )
        if request.url.path == "/v1/sessions/conv_abc/items":
            after = request.url.params.get("after")
            if after is None:
                return httpx.Response(
                    200,
                    json=_items_response_body(page_1_items, has_more=True, last_id="fc_read_1"),
                )
            assert after == "fc_read_1"
            return httpx.Response(200, json=_items_response_body(page_2_items))
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    monkeypatch.chdir(workspace)
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        args = await claude_native._resolve_cold_resume_args(client, "conv_abc")

    assert args == ("--resume", "claude-uuid-abc")
    transcript_path = (
        projects
        / claude_native._sanitize_claude_project_name(str(workspace.resolve()))
        / "claude-uuid-abc.jsonl"
    )
    assert transcript_path.is_file(), (
        f"expected synthesized Claude transcript at {transcript_path}, but it was not written"
    )
    records = [
        json.loads(line)
        for line in transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["type"] for record in records] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert records[0]["message"] == {"role": "user", "content": "open TODO.md"}
    assert records[1]["message"]["content"] == [
        {
            "type": "tool_use",
            "id": "toolu_read_1",
            "name": "Read",
            "input": {"file_path": "TODO.md"},
        }
    ]
    assert records[2]["message"]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_read_1",
            "content": "contents",
        }
    ]
    assert records[2]["parentUuid"] == records[1]["uuid"]
    assert records[3]["message"]["content"] == [{"type": "text", "text": "TODO.md says contents"}]
    # An item's wire "model" is the Omnigent agent name, not a Claude model
    # id. Writing it through makes `--resume` reject it ("Session model
    # claude-native-ui could not be restored") and silently fall back to a
    # different model, so no record may carry one.
    assert all("model" not in record["message"] for record in records), (
        f"agent name leaked into Claude's model slot: {records!r}"
    )
    assert all(record["sessionId"] == "claude-uuid-abc" for record in records)
    assert all(record["cwd"] == str(workspace.resolve()) for record in records)
    assert any("after=fc_read_1" in path for path in requested_paths), (
        f"history pagination was not followed; requests were {requested_paths!r}"
    )


@pytest.mark.asyncio
async def test_resolve_cold_resume_args_replaces_existing_local_claude_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Cold resume treats Omnigent history as source of truth over local JSONL.

    Claude can leave a local ``~/.claude/projects/<cwd>/<sid>.jsonl``
    that diverges from the Omnigent transcript we have persisted. The resume
    path must still fetch Omnigent items and overwrite that stale file before
    returning ``--resume <sid>``. If the helper reintroduces an early
    return when the local target exists, this test keeps the stale line
    and fails.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = tmp_path / "claude-projects"
    transcript_path = (
        projects
        / claude_native._sanitize_claude_project_name(str(workspace.resolve()))
        / "claude-uuid-abc.jsonl"
    )
    transcript_path.parent.mkdir(mode=0o700, parents=True)
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "claude-uuid-abc",
                "message": {"role": "user", "content": "stale local text"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    item_from_ap = {
        "id": "msg_user_fresh",
        "response_id": "resp_1",
        "type": "message",
        "status": "completed",
        "role": "user",
        "content": [{"type": "input_text", "text": "fresh Omnigent text"}],
    }
    item_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve the session snapshot and AP-authoritative item page.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        nonlocal item_requests
        if request.url.path == "/v1/sessions/conv_abc":
            return httpx.Response(
                200,
                json=_conversation_response_body(
                    labels={"omnigent.wrapper": "claude-code-native-ui"},
                    external_session_id="claude-uuid-abc",
                ),
            )
        if request.url.path == "/v1/sessions/conv_abc/items":
            item_requests += 1
            return httpx.Response(200, json=_items_response_body([item_from_ap]))
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    monkeypatch.chdir(workspace)
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        args = await claude_native._resolve_cold_resume_args(client, "conv_abc")

    assert args == ("--resume", "claude-uuid-abc")
    assert item_requests == 1, "cold resume must fetch Omnigent items even when local JSONL exists"
    records = [
        json.loads(line)
        for line in transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["message"]["content"] for record in records] == ["fresh Omnigent text"]


@pytest.mark.asyncio
async def test_ensure_local_claude_resume_transcript_repairs_stale_duplicated_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An already-generated transcript with the old duplicate self-heals.

    Pre-fix rebuilds wrote an intact image's base64 twice — once in the
    rehydrated ``tool_result`` content block and again verbatim in
    ``toolUseResult``. The resume helper always rewrites the transcript
    from Omnigent items before launch (no cache, no migration), so a
    stale affected file is repaired on the next resume: after the
    rebuild the payload must appear exactly once.
    """
    # Padded so the fixture is already canonical standard base64.
    b64 = "iVBORw0KGgo" + "D" * 5000 + "="
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = tmp_path / "claude-projects"
    transcript_path = (
        projects
        / claude_native._sanitize_claude_project_name(str(workspace.resolve()))
        / "claude-uuid-img.jsonl"
    )
    transcript_path.parent.mkdir(mode=0o700, parents=True)
    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64},
    }
    stale_record = {
        "type": "user",
        "sessionId": "claude-uuid-img",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [image_block]}
            ],
        },
        "toolUseResult": json.dumps([image_block], separators=(",", ":")),
    }
    transcript_path.write_text(json.dumps(stale_record) + "\n", encoding="utf-8")
    assert transcript_path.read_text(encoding="utf-8").count(b64) == 2, "pre-fix wedged state"

    image_item = {
        "id": "fco_1",
        "response_id": "resp_1",
        "type": "function_call_output",
        "call_id": "toolu_1",
        "output": json.dumps([image_block], separators=(",", ":")),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve the AP-authoritative item page carrying the image output."""
        del request
        return httpx.Response(200, json=_items_response_body([image_item]))

    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        written = await claude_native._ensure_local_claude_resume_transcript(
            client,
            session_id="conv_abc",
            external_session_id="claude-uuid-img",
            workspace=workspace.resolve(),
        )

    assert written == transcript_path
    text = written.read_text(encoding="utf-8")
    assert text.count(b64) == 1, "rebuild must drop the duplicated toolUseResult base64"
    record = json.loads(text.splitlines()[0])
    content = record["message"]["content"][0]["content"]
    assert content[0]["source"]["data"] == b64, "the model-visible image must survive"
    assert b64 not in record["toolUseResult"]


@pytest.mark.asyncio
async def test_resolve_cold_resume_args_warns_when_external_session_id_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Claude-native conv with no captured external_session_id (crashed
    before first hook, etc.) returns ``()`` and prints a warning.
    The Omnigent conv id still survives — the new terminal binds
    to the same row — but Claude starts fresh. Critical: this
    branch MUST NOT raise so the user can recover the conv even
    when the prior claude side is unrecoverable.
    """
    client = await _httpx_client_with_canned_response(
        _conversation_response_body(
            labels={"omnigent.wrapper": "claude-code-native-ui"},
            external_session_id=None,
        ),
        200,
    )
    async with client:
        args = await claude_native._resolve_cold_resume_args(client, "conv_abc")
    assert args == ()
    captured = capsys.readouterr()
    # Warning lands on stderr (click.echo(..., err=True) in production).
    assert "claude session id was never captured" in captured.err


@pytest.mark.asyncio
async def test_resolve_cold_resume_args_rejects_non_claude_native_conv() -> None:
    """
    A conv whose wrapper label is NOT claude-native is an
    ``omnigent claude --resume <run-conv-id>`` programmer error.
    Fail loud with a redirect hint rather than silently launching
    claude over a chat session whose state the wrapper doesn't
    own.
    """
    client = await _httpx_client_with_canned_response(
        _conversation_response_body(
            # Wrapper label absent → treated as "not claude-native".
            labels={},
            external_session_id=None,
        ),
        200,
    )
    async with client:
        with pytest.raises(click.ClickException) as excinfo:
            await claude_native._resolve_cold_resume_args(client, "conv_abc")
    assert "not a claude-native session" in excinfo.value.message
    # Redirect hint includes the right command and conv id so the
    # user can copy-paste to recover. If this assertion fails, the
    # error becomes a dead-end.
    assert "omnigent run --resume conv_abc" in excinfo.value.message


@pytest.mark.asyncio
async def test_resolve_cold_resume_args_raises_on_missing_conversation() -> None:
    """
    404 from the server is an unambiguous "no such conv" — surface
    a clear error so the user doesn't wait for a session that won't
    materialize. The error must include the conv id so the user can
    spot a typo.
    """
    client = await _httpx_client_with_canned_response(None, 404)
    async with client:
        with pytest.raises(click.ClickException) as excinfo:
            await claude_native._resolve_cold_resume_args(client, "conv_missing")
    assert "conv_missing" in excinfo.value.message
    assert "not found" in excinfo.value.message


@pytest.mark.asyncio
async def test_resolve_cold_resume_args_warning_lands_in_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Missing ``external_session_id`` warns via ``_logger.warning``.

    The ``click.echo`` channel covers the foreground user; the
    logger channel covers post-hoc debug (log aggregation /
    Sentry). Without the logger call, a remote / cron run would
    silently lose Claude-side context with no breadcrumb. This
    test enforces both channels stay wired.

    Patches ``_logger.warning`` directly rather than relying on
    caplog — caplog is sensitive to logger propagation state set
    up by other tests in the same session, which made earlier
    versions of this assertion order-dependent.
    """
    captured_warnings: list[str] = []
    monkeypatch.setattr(
        claude_native._logger,
        "warning",
        lambda message, *args, **kwargs: captured_warnings.append(
            message % args if args else message,
        ),
    )

    client = await _httpx_client_with_canned_response(
        _conversation_response_body(
            labels={"omnigent.wrapper": "claude-code-native-ui"},
            external_session_id=None,
        ),
        200,
    )
    async with client:
        result = await claude_native._resolve_cold_resume_args(client, "conv_abc")
    assert result == ()
    # Warning was issued — if a regression replaced
    # ``_logger.warning(...)`` with ``pass`` or a print, this
    # assertion catches it.
    assert any("conv_abc" in m for m in captured_warnings), (
        f"Expected a WARNING mentioning 'conv_abc'; got {captured_warnings!r}"
    )


# ── _prepare_claude_terminal cold-resume integration ─────────


@pytest.mark.asyncio
async def test_prepare_claude_terminal_cold_resume_injects_external_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Cold-resume threads ``--resume <claude_sid>`` into the args
    passed to ``_launch_claude_terminal``.

    Load-bearing assertion: the conv id stays the SAME Omnigent
    id end-to-end (no new id minted), AND the spawned terminal
    receives Claude's prior session id as the first two args. A
    regression that dropped the cold-resume args at the launch
    seam would silently lose Claude-side context — the user keeps
    the Omnigent conv id but Claude starts fresh. Tests
    ``_resolve_cold_resume_args`` in isolation cannot catch this.
    """
    captured_terminal_args: dict[str, Any] = {}

    async def _fake_find_running(_client: object, _session_id: str) -> str | None:
        """
        No live terminal → force the cold-resume code path.

        :param _client: Unused HTTP client.
        :param _session_id: Unused conversation id.
        :returns: ``None`` to indicate "no terminal alive".
        """
        return None

    async def _fake_resolve_cold_resume_args(
        _client: object,
        _session_id: str,
    ) -> tuple[str, ...]:
        """
        Stand in for the real wrapper-label / external_session_id
        lookup. Returns the args that production injects.

        :param _client: Unused HTTP client.
        :param _session_id: Unused conversation id.
        :returns: Fixed ``("--resume", "claude-sid-abc")``.
        """
        return ("--resume", "claude-sid-abc")

    async def _fake_fetch_labels(
        _client: object,
        _session_id: str,
    ) -> dict[str, str]:
        """
        Return no bridge label for this legacy cold-resume fixture.

        :param _client: Unused HTTP client.
        :param _session_id: Existing conversation id.
        :returns: Empty labels so the bridge id falls back to the
            session id.
        """
        return {}

    async def _fake_bind_session_runner(*args: object, **kwargs: object) -> None:
        """No-op bind to keep the integration test focused on args."""
        del args, kwargs

    async def _fake_launch_claude_terminal(
        _client: object,
        session_id: str,
        claude_args: tuple[str, ...],
        *,
        command: str,
        bridge_dir: Path,
        claude_config: claude_native.ClaudeNativeUcodeConfig | None = None,
        append_system_prompt: str | None = None,
        allowed_tools: tuple[str, ...] = (),
    ) -> str:
        """
        Capture the launch args without invoking the real runner.

        :param _client: HTTP client (ignored).
        :param session_id: Omnigent conversation id — captured
            for the end-to-end assertion.
        :param claude_args: Args the launch will pass to claude —
            this is the load-bearing capture.
        :param command: Executable name (ignored).
        :param bridge_dir: Bridge directory (ignored).
        :param claude_config: Optional ucode config (ignored).
        :returns: A fixed terminal id.
        """
        captured_terminal_args["session_id"] = session_id
        captured_terminal_args["claude_args"] = claude_args
        captured_terminal_args["append_system_prompt"] = append_system_prompt
        captured_terminal_args["allowed_tools"] = allowed_tools
        del command, bridge_dir, claude_config
        return "terminal_claude_main"

    monkeypatch.setattr(
        claude_native,
        "_find_running_claude_terminal",
        _fake_find_running,
    )
    monkeypatch.setattr(
        claude_native,
        "_resolve_cold_resume_args",
        _fake_resolve_cold_resume_args,
    )
    monkeypatch.setattr(
        claude_native,
        "_fetch_claude_session_labels",
        _fake_fetch_labels,
    )
    monkeypatch.setattr(
        claude_native,
        "_bind_session_runner",
        _fake_bind_session_runner,
    )
    monkeypatch.setattr(
        claude_native,
        "_launch_claude_terminal",
        _fake_launch_claude_terminal,
    )
    monkeypatch.setattr(
        claude_native,
        "prepare_bridge_dir",
        lambda session_id, *, bridge_id=None, workspace, launch_model=None, launch_env=None: (
            tmp_path / (bridge_id or session_id)
        ),
    )
    monkeypatch.setattr(
        claude_native,
        "reset_transcript_forward_state",
        lambda bridge_dir: None,
    )

    async with httpx.AsyncClient(base_url="http://test") as http_client:
        prepared = await claude_native._prepare_claude_terminal(
            base_url="http://test",
            headers={},
            session_id="conv_abc",
            runner_id="runner_xyz",
            session_bundle=None,
            claude_args=("--print", "hello"),
            command="claude",
        )
        del http_client  # context-managed by the with block

    # Omnigent conv id survives end-to-end. If this assertion
    # fails, the wrapper minted a new session id on cold resume —
    # exactly what the user told us NOT to do.
    assert prepared.session_id == "conv_abc"
    assert captured_terminal_args["session_id"] == "conv_abc"
    # Cold-resume args land at the FRONT of claude's argv, with the
    # user's passthrough args after. A regression that appended
    # them OR dropped them would fail this assertion.
    assert captured_terminal_args["claude_args"] == (
        "--resume",
        "claude-sid-abc",
        "--print",
        "hello",
    )
    assert captured_terminal_args["append_system_prompt"] is None
    assert captured_terminal_args["allowed_tools"] == ()

    # Load-bearing for the duplicate-message bug: cold resume
    # MUST set ``cold_resumed=True`` so the transcript forwarder seeks
    # to the current transcript end on first read. Without this,
    # ``--resume <claude_sid>`` makes Claude reopen the prior JSONL
    # transcript, the forwarder reads it from offset 0, and every
    # prior turn is POSTed back to AP. There is no server-side dedup
    # to collapse those re-posts, so seeking to the end is the only
    # thing preventing duplicate turns. ``reattached`` stays ``False``
    # because we launched a new terminal — the wrapper still owns
    # teardown, unlike the hot-reattach case.
    assert prepared.cold_resumed is True, (
        "cold resume must set cold_resumed=True; without it the "
        "forwarder reads the prior transcript from offset 0 and "
        "re-broadcasts every prior turn on resume."
    )
    assert prepared.reattached is False, (
        "cold resume launches a new terminal; reattached must be False "
        "so the wrapper still closes the terminal on exit."
    )


@pytest.mark.asyncio
async def test_prepare_claude_terminal_fresh_session_is_not_cold_resumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Fresh sessions (no ``session_id``) must not be marked ``cold_resumed``.

    Counter-test for the cold-resume regression: a brand-new session
    has no prior transcript to skip, so the forwarder should read
    from offset 0 and surface every item as it arrives. Marking it
    ``cold_resumed=True`` would silently swallow the first turn’s
    transcript on a fresh launch — the user would type a prompt
    and see no assistant reply mirrored to the web UI.
    """

    async def _fake_create_session(
        _client: object,
        _bundle: bytes,
        *,
        bridge_id: str,
    ) -> str:
        """Return a freshly-minted conv id without HTTP."""
        del _client, _bundle, bridge_id
        return "conv_new"

    async def _fake_bind_session_runner(*args: object, **kwargs: object) -> None:
        """No-op bind to keep the test focused on the flag."""
        del args, kwargs

    async def _fake_launch_claude_terminal(
        _client: object,
        _session_id: str,
        _claude_args: tuple[str, ...],
        *,
        command: str,
        bridge_dir: Path,
        claude_config: claude_native.ClaudeNativeUcodeConfig | None = None,
        append_system_prompt: str | None = None,
        allowed_tools: tuple[str, ...] = (),
    ) -> str:
        """Return a fixed terminal id without spawning anything."""
        assert append_system_prompt is None
        assert allowed_tools == ()
        del _client, _session_id, _claude_args, command, bridge_dir, claude_config
        return "terminal_claude_main"

    monkeypatch.setattr(claude_native, "_create_claude_session", _fake_create_session)
    monkeypatch.setattr(claude_native, "_bind_session_runner", _fake_bind_session_runner)
    monkeypatch.setattr(claude_native, "_launch_claude_terminal", _fake_launch_claude_terminal)
    monkeypatch.setattr(
        claude_native,
        "prepare_bridge_dir",
        lambda session_id, *, bridge_id=None, workspace, launch_model=None, launch_env=None: (
            tmp_path / (bridge_id or session_id)
        ),
    )
    monkeypatch.setattr(
        claude_native,
        "reset_transcript_forward_state",
        lambda bridge_dir: None,
    )

    async with httpx.AsyncClient(base_url="http://test") as http_client:
        prepared = await claude_native._prepare_claude_terminal(
            base_url="http://test",
            headers={},
            session_id=None,
            runner_id="runner_xyz",
            session_bundle=b"fake-bundle",
            claude_args=(),
            command="claude",
        )
        del http_client

    # Both flags must be False on a fresh session. ``cold_resumed=True``
    # here would mean the forwarder skips to the end of the (empty)
    # transcript on first read — fine for empty files, but if the
    # check ever changes to ``cold_resumed or reattached`` AND the
    # transcript has any pre-existing content (e.g. a system header
    # claude writes on cold start), the first turn would be dropped.
    assert prepared.cold_resumed is False
    assert prepared.reattached is False


@pytest.mark.asyncio
async def test_attach_passes_start_at_end_true_on_cold_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``cold_resumed=True`` propagates to ``supervise_forwarder`` as
    ``start_at_end=True``.

    This is the load-bearing assertion that ties the
    ``PreparedClaudeTerminal.cold_resumed`` flag to the actual
    transcript-forwarder behavior. Without ``start_at_end=True``,
    the forwarder seeds ``byte_offset=0`` for a fresh cursor and
    re-reads every transcript record that ``claude --resume``
    reopened. If a future refactor changed ``_attach_with_transcript_forwarder``
    to pass ``prepared.reattached`` again (the original buggy
    behavior), this test would fail: cold resume's ``reattached``
    is ``False`` by construction, so the forwarder would still
    walk the prior transcript from offset 0 and Omnigent would broadcast
    every prior turn as new.
    """
    captured: dict[str, Any] = {}
    forwarder_started = asyncio.Event()

    async def fake_attach(attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        Wait for the forwarder to start, then exit.

        Without the wait, ``create_task(supervise_forwarder(...))``
        is scheduled but never gets a chance to run before the
        finally block cancels it, and ``captured`` stays empty.
        Awaiting the event yields to the loop so ``fake_forward``
        runs its first line.
        """
        del attach_url, headers
        await forwarder_started.wait()
        return True

    async def fake_forward(**kwargs: object) -> None:
        """Capture forwarder kwargs without doing any HTTP work."""
        captured.update(kwargs)
        forwarder_started.set()
        # Block so the create_task wrapper can cancel us in cleanup.
        await asyncio.sleep(3600)

    async def fake_close(**kwargs: object) -> None:
        """No-op terminal close — cold resume owns its terminal."""
        del kwargs

    monkeypatch.setattr(claude_native, "_close_claude_terminal", fake_close)
    monkeypatch.setattr(claude_native, "supervise_forwarder", fake_forward)

    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_abc",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
        cold_resumed=True,
    )
    await claude_native._attach_with_transcript_forwarder(
        base_url="https://example.com",
        headers={},
        prepared=prepared,
        agent_name=claude_native._AGENT_NAME,
        attach_url="wss://example.com/attach",
        attach=fake_attach,
    )

    # The supervisor MUST have been started with start_at_end=True.
    # A regression that wired ``start_at_end=prepared.reattached``
    # (the original buggy code) would land False here — cold resume
    # has reattached=False — and the forwarder would replay the
    # prior transcript on first poll. ``True`` confirms the OR-of-
    # flags fix in ``_attach_with_transcript_forwarder``.
    assert captured.get("start_at_end") is True, (
        f"cold_resumed=True must force start_at_end=True; got "
        f"start_at_end={captured.get('start_at_end')!r}. Without this, "
        f"every prior turn in the reopened claude transcript is "
        f"re-POSTed to Omnigent on resume and broadcast to live clients."
    )


@pytest.mark.asyncio
async def test_attach_passes_start_at_end_false_on_fresh_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Fresh launches (neither reattached nor cold-resumed) keep
    ``start_at_end=False``.

    Counter-test bracketing the OR fix: if the wrapper ever
    short-circuited to ``start_at_end=True`` unconditionally, the
    first turn of a brand-new session would be silently skipped —
    the forwarder would seek past whatever claude writes during
    its startup (system banner, ``SessionStart`` echo) and miss the
    user’s first message if it landed before the first poll.
    """
    captured: dict[str, Any] = {}
    forwarder_started = asyncio.Event()

    async def fake_attach(attach_url: str, *, headers: dict[str, str]) -> bool:
        """
        Wait for the forwarder to record kwargs, then exit.

        See the cold-resume counterpart for why we have to yield
        here — a synchronous return would cancel the forwarder
        task before its body runs.
        """
        del attach_url, headers
        await forwarder_started.wait()
        return True

    async def fake_forward(**kwargs: object) -> None:
        """Capture forwarder kwargs without doing any HTTP work."""
        captured.update(kwargs)
        forwarder_started.set()
        await asyncio.sleep(3600)

    async def fake_close(**kwargs: object) -> None:
        """No-op terminal close."""
        del kwargs

    monkeypatch.setattr(claude_native, "_close_claude_terminal", fake_close)
    monkeypatch.setattr(claude_native, "supervise_forwarder", fake_forward)

    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_new",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
        cold_resumed=False,
    )
    await claude_native._attach_with_transcript_forwarder(
        base_url="https://example.com",
        headers={},
        prepared=prepared,
        agent_name=claude_native._AGENT_NAME,
        attach_url="wss://example.com/attach",
        attach=fake_attach,
    )

    assert captured.get("start_at_end") is False, (
        f"fresh launch must keep start_at_end=False so the first "
        f"transcript items are forwarded; got "
        f"start_at_end={captured.get('start_at_end')!r}."
    )


# ── _is_claude_native_conversation (chat-side redirect helper) ─


def test_is_claude_native_conversation_returns_true_on_matching_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    200 + ``omnigent.wrapper=claude-code-native-ui`` → True.

    This is the load-bearing decision for the chat-redirect path
    (``_chat_with_server`` calls this to decide whether to redirect
    a resume into the claude wrapper). A False negative here is
    exactly the resume misroute.
    """
    from omnigent import chat

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """Canned 200 response with the claude-native wrapper label."""
        del url, headers, timeout
        return httpx.Response(
            200,
            json={
                "labels": {"omnigent.wrapper": "claude-code-native-ui"},
            },
        )

    monkeypatch.setattr(chat.httpx, "get", _fake_get)
    monkeypatch.setattr(chat, "_remote_headers", lambda server_url=None, **_kw: {})

    assert (
        chat._is_claude_native_conversation(
            base_url="http://test",
            conversation_id="conv_abc",
        )
        is True
    )


@pytest.mark.parametrize(
    "labels",
    [
        {},
        {"omnigent.wrapper": "some-other-wrapper"},
        {"unrelated": "x"},
    ],
)
def test_is_claude_native_conversation_returns_false_on_non_matching_label(
    monkeypatch: pytest.MonkeyPatch,
    labels: dict[str, str],
) -> None:
    """
    Non-claude wrapper / missing label / unrelated label → False.

    The chat REPL stays on its normal AP-REPL path for these
    conversations.
    """
    from omnigent import chat

    def _fake_get(_url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """Canned 200 response with the parametrized labels."""
        del headers, timeout
        return httpx.Response(200, json={"labels": labels})

    monkeypatch.setattr(chat.httpx, "get", _fake_get)
    monkeypatch.setattr(chat, "_remote_headers", lambda server_url=None, **_kw: {})

    assert (
        chat._is_claude_native_conversation(
            base_url="http://test",
            conversation_id="conv_abc",
        )
        is False
    )


@pytest.mark.parametrize("status_code", [401, 403, 404, 500, 502, 503])
def test_is_claude_native_conversation_logs_warning_on_non_200(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    """
    Non-200 returns False but ALSO logs a warning. Without the
    warning a misrouted resume (auth failure → silent Omnigent REPL on
    top of a tmux session) would have zero breadcrumbs in logs.

    Patches ``logger.warning`` directly (not caplog) to keep the
    assertion deterministic in the cross-file pytest sweep — caplog
    requires the right handler / propagation, which other tests'
    logging setup can disturb.
    """
    from omnigent import chat

    def _fake_get(_url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """Canned error response at the parametrized status code."""
        del headers, timeout
        return httpx.Response(status_code)

    captured_warnings: list[str] = []
    monkeypatch.setattr(chat.httpx, "get", _fake_get)
    monkeypatch.setattr(chat, "_remote_headers", lambda server_url=None, **_kw: {})
    monkeypatch.setattr(
        chat.logger,
        "warning",
        lambda message, *args, **kwargs: captured_warnings.append(
            message % args if args else message,
        ),
    )

    result = chat._is_claude_native_conversation(
        base_url="http://test",
        conversation_id="conv_abc",
    )
    assert result is False
    # Status code appears in the log so the operator can act on
    # 401/403 (auth) vs 5xx (server flake) without guessing.
    assert any(str(status_code) in m and "conv_abc" in m for m in captured_warnings), (
        f"Expected a WARNING mentioning status {status_code} and conv_abc; "
        f"got {captured_warnings!r}"
    )


def test_is_claude_native_conversation_returns_false_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Connection / DNS / TLS failure → False, with a warning logged.

    The caller falls back to the Omnigent REPL path, which surfaces its
    own connect-fail error; we just record what we saw so a flaky
    server doesn't cause a silent misroute.
    """
    from omnigent import chat

    def _raises(*_args: object, **_kwargs: object) -> httpx.Response:
        """Pretend the connect fails."""
        raise httpx.ConnectError("connection refused")

    captured_warnings: list[str] = []
    monkeypatch.setattr(chat.httpx, "get", _raises)
    monkeypatch.setattr(chat, "_remote_headers", lambda server_url=None, **_kw: {})
    monkeypatch.setattr(
        chat.logger,
        "warning",
        lambda message, *args, **kwargs: captured_warnings.append(
            message % args if args else message,
        ),
    )

    result = chat._is_claude_native_conversation(
        base_url="http://test",
        conversation_id="conv_abc",
    )
    assert result is False
    assert any("conv_abc" in m for m in captured_warnings), (
        f"Expected WARNING for transport error; got {captured_warnings!r}"
    )


# ── _align_working_directory_with_session (resume-time workspace prompt) ──
#
# These tests drive ``_align_working_directory_with_session``
# directly. The state module's roundtrip / persistence is covered
# in ``tests/test_claude_native_state.py``; here we exercise the
# decision-table behavior: matching cwd → no-op, mismatched +
# switch → chdir, mismatched + move → transcript move,
# mismatched + missing path → ClickException, legacy session →
# silent skip.
#
# Each test writes the launch state for a specific conv id via the
# real state module, then drives the helper. The autouse
# ``_isolate_claude_native_state`` fixture in ``tests/conftest.py``
# redirects the state root to a per-test tmp dir so writes never
# touch the developer's real ``~/.omnigent/``.


@dataclass(frozen=True)
class _WorkspaceActionTtyResult:
    """
    Result from a pseudo-terminal workspace action picker run.

    :param selected: Selected action, e.g. ``"move"``.
    :param rendered: Captured prompt-toolkit render output.
    """

    selected: str
    rendered: str


def _pick_workspace_action_with_tty_input(
    options: list[claude_native._ResumeWorkspaceActionOption],
    input_chunks: list[bytes],
    *,
    recorded_path: Path,
    current: Path,
) -> _WorkspaceActionTtyResult:
    """
    Run the workspace action picker against a real pseudo-terminal.

    :param options: Selectable action options.
    :param input_chunks: Raw keypress chunks, e.g. ``[b"\\x1b[B", b"\\r"]``.
    :param recorded_path: Recorded launch cwd rendered in the selector.
    :param current: Current cwd rendered in the selector.
    :returns: Selected action plus captured output.
    """
    import queue
    import threading

    result_queue: queue.Queue[str | BaseException] = queue.Queue()
    master_fd, slave_fd = os.openpty()
    slave_check_fd = os.dup(slave_fd)
    out = io.StringIO()

    def run_picker() -> None:
        """
        Run the prompt-toolkit picker on the slave TTY.

        :returns: None.
        """
        with os.fdopen(slave_fd, "r", encoding="utf-8", buffering=1) as slave:
            try:
                selected = claude_native._pick_resume_workspace_action_prompt_toolkit(
                    options,
                    recorded_path=recorded_path,
                    current=current,
                    out=out,
                    in_=slave,
                )
            except BaseException as exc:
                result_queue.put(exc)
            else:
                result_queue.put(selected)

    thread = threading.Thread(target=run_picker, daemon=True)
    thread.start()
    try:
        for index, chunk in enumerate(input_chunks):
            _wait_for_workspace_action_terminal_mode(slave_check_fd)
            render_count = out.getvalue().count("Keys:")
            os.write(master_fd, chunk)
            if index < len(input_chunks) - 1:
                _wait_for_workspace_action_redraw(out, render_count)
        thread.join(timeout=2)
        if thread.is_alive():
            pytest.fail(f"workspace action picker hung; output={out.getvalue()!r}")
        payload = result_queue.get_nowait()
        if isinstance(payload, BaseException):
            raise payload
        return _WorkspaceActionTtyResult(selected=payload, rendered=out.getvalue())
    finally:
        for fd in (master_fd, slave_check_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def _wait_for_workspace_action_terminal_mode(slave_check_fd: int) -> None:
    """
    Wait until prompt-toolkit has put the TTY in raw-ish mode.

    :param slave_check_fd: Duplicate slave pty file descriptor.
    :returns: None.
    """
    import termios
    import time

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        local_flags = termios.tcgetattr(slave_check_fd)[3]
        if not local_flags & termios.ICANON:
            return
        time.sleep(0.001)
    pytest.fail("workspace action picker did not enter terminal input mode")


def _wait_for_workspace_action_redraw(out: io.StringIO, previous_render_count: int) -> None:
    """
    Wait until the prompt-toolkit selector redraws after a keypress.

    :param out: Captured prompt-toolkit output stream.
    :param previous_render_count: Previous count of footer render markers.
    :returns: None.
    """
    import time

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if out.getvalue().count("Keys:") > previous_render_count:
            return
        time.sleep(0.005)


def test_align_working_directory_no_state_silent_skip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Legacy session (no recorded state) → silent no-op.

    Sessions created before this tracking landed, or sessions
    created on a different machine, have no client-side state for
    this conv id. The helper must not prompt (we have no recorded
    cwd to compare against) and must not raise (the user might
    still be in the right directory). The wrapper proceeds; Claude
    surfaces its own error if the cwd actually mismatched.
    """
    monkeypatch.chdir(tmp_path)
    starting_cwd = Path.cwd().resolve()

    def fake_prompt(*args: object, **kwargs: object) -> str:
        raise AssertionError(
            f"click.prompt must not fire for a session with no recorded "
            f"state; args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr(claude_native.click, "prompt", fake_prompt)

    # Must not raise even though the conv has no state on disk.
    claude_native._align_working_directory_with_session("conv_no_state")

    assert Path.cwd().resolve() == starting_cwd


def test_align_working_directory_matching_cwd_silent_skip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Recorded cwd matches current cwd → silent no-op.

    Verifies the path equality check uses ``Path.resolve()`` so a
    symlink-equivalent path matches the recorded canonical form.
    If this regresses to a string compare, every session opened
    via a symlinked directory (e.g. ``/repo`` vs
    ``/home/me/repo``) would prompt to chdir on every resume,
    which is noise the user has to dismiss every time.
    """
    from omnigent.claude_native_state import write_launch_state

    monkeypatch.chdir(tmp_path)
    starting_cwd = Path.cwd().resolve()
    write_launch_state("conv_match", str(starting_cwd))

    def fake_prompt(*args: object, **kwargs: object) -> str:
        raise AssertionError(
            f"click.prompt must NOT be called when cwd matches; args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr(claude_native.click, "prompt", fake_prompt)

    claude_native._align_working_directory_with_session("conv_match")

    assert Path.cwd().resolve() == starting_cwd, (
        f"cwd must not change when recorded path matches; "
        f"started at {starting_cwd}, now at {Path.cwd().resolve()}"
    )


def test_align_working_directory_switch_action_chdirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Mismatched cwd, recorded path exists, user chooses switch → chdir.

    This is the happy-path fix for the bug the user reported:
    ``omnigent claude --resume`` invoked from a different
    directory than the session was started in must offer to
    switch, and on switch must actually mutate the process cwd so
    subsequent ``Path.cwd()`` reads in the launch flow see the
    new value. If chdir is missing or points elsewhere, Claude
    will still exit on launch.
    """
    from omnigent.claude_native_state import write_launch_state

    recorded = tmp_path / "recorded-ws"
    recorded.mkdir()
    current = tmp_path / "current-ws"
    current.mkdir()
    monkeypatch.chdir(current)
    write_launch_state("conv_mismatch_switch", str(recorded.resolve()))

    prompt_calls: list[dict[str, object]] = []

    def fake_prompt(
        prompt: str,
        *,
        type: click.Choice,
        default: str,
        show_choices: bool,
        err: bool,
    ) -> str:
        """Capture the call and choose the switch action."""
        prompt_calls.append(
            {
                "prompt": prompt,
                "choices": tuple(type.choices),
                "default": default,
                "show_choices": show_choices,
                "err": err,
            }
        )
        return "switch"

    monkeypatch.setattr(claude_native.click, "prompt", fake_prompt)

    claude_native._align_working_directory_with_session("conv_mismatch_switch")

    # Side-effect: cwd actually moved. If chdir is missing the
    # picker UX is misleading — the user agrees to switch but
    # Claude still fails to resume because the wrapper never
    # acted on the answer.
    assert Path.cwd().resolve() == recorded.resolve(), (
        f"chdir on switch did not land; cwd is {Path.cwd().resolve()}, "
        f"expected {recorded.resolve()}."
    )
    # Action prompt fired exactly once. 0 = no prompt was shown
    # (silent chdir, bad UX); 2+ = duplicate prompt loop bug.
    assert len(prompt_calls) == 1, (
        f"expected exactly one prompt() call; got {len(prompt_calls)}: {prompt_calls!r}"
    )
    # ``switch`` stays the default when the original cwd exists:
    # pressing Enter should take the path Claude is known to accept.
    assert prompt_calls[0]["default"] == "switch", (
        f"workspace prompt default must be switch; got {prompt_calls[0]['default']!r}"
    )
    assert prompt_calls[0]["choices"] == ("switch", "leave"), (
        f"unexpected action choices without move; got {prompt_calls[0]['choices']!r}"
    )


def test_resume_workspace_action_tty_down_enter_selects_move(tmp_path: Path) -> None:
    """
    Down then Enter selects the highlighted ``move`` action.

    This exercises the real prompt-toolkit selector users see in a
    terminal. It fails if the workspace action prompt regresses to a
    typed-only Click prompt, if Down does not move the highlight, or
    if Enter does not select the highlighted action.
    """
    recorded = tmp_path / "old"
    recorded.mkdir()
    current = tmp_path / "new"
    current.mkdir()
    options = claude_native._resume_workspace_action_options(
        recorded_path=recorded.resolve(),
        current=current.resolve(),
        redirect_available=True,
    )

    result = _pick_workspace_action_with_tty_input(
        options,
        [b"\x1b[B", b"\r"],
        recorded_path=recorded.resolve(),
        current=current.resolve(),
    )

    assert result.selected == "move"
    action_lines = [
        line
        for line in result.rendered.splitlines()
        if "Switch working directory to" in line
        or "Move conversation to" in line
        or line.strip() == "Leave"
    ][-3:]
    assert len(action_lines) == 3
    assert not action_lines[0].lstrip().startswith(">")
    assert action_lines[1].lstrip().startswith(">"), (
        f"Down should highlight the move action. Output:\n{result.rendered!r}"
    )


def test_resume_workspace_action_tty_escape_leaves(tmp_path: Path) -> None:
    """
    Esc selects ``leave`` from the interactive workspace action prompt.

    The action prompt is a pre-launch safety gate, so Esc should not
    fall through into a known Claude crash. It maps to the explicit
    leave action that the caller turns into ``Resume cancelled``.
    """
    recorded = tmp_path / "old"
    recorded.mkdir()
    current = tmp_path / "new"
    current.mkdir()
    options = claude_native._resume_workspace_action_options(
        recorded_path=recorded.resolve(),
        current=current.resolve(),
        redirect_available=True,
    )

    result = _pick_workspace_action_with_tty_input(
        options,
        [b"\x1b"],
        recorded_path=recorded.resolve(),
        current=current.resolve(),
    )

    assert result.selected == "leave"


def test_resume_workspace_action_style_matches_shared_picker_theme() -> None:
    """
    Workspace action highlight colors match other terminal pickers.

    The resume conversation picker and this cwd-mismatch selector
    should feel like one UI surface. This catches regressions back to
    ad-hoc colors such as cyan for one selector and green for another.
    """
    style = claude_native._resume_workspace_action_style()

    selected = style.get_attrs_for_style_str("class:selected")
    accent = style.get_attrs_for_style_str(f"{PICKER_ACCENT} bold")
    muted = style.get_attrs_for_style_str(PICKER_MUTED)

    assert selected == accent
    assert style.get_attrs_for_style_str("class:accent-bold") == accent
    assert style.get_attrs_for_style_str("class:muted") == muted


def test_fetch_external_session_id_for_redirect_uses_session_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Redirect preflight fetches the session endpoint and returns the
    captured Claude session id.

    This catches regressions where the helper references the wrong
    URL-encoding function and silently falls back to no redirect,
    which removes the ``move`` action from the resume picker.
    """
    calls: list[dict[str, object]] = []

    class _FakeHttpxClient:
        """
        Minimal context-manager stand-in for :class:`httpx.Client`.

        :param base_url: Omnigent server base URL.
        :param headers: HTTP headers passed by the wrapper.
        :param timeout: Request timeout in seconds.
        :param trust_env: Whether env proxy settings are honored.
        """

        def __init__(
            self,
            *,
            base_url: str,
            headers: dict[str, str],
            timeout: float,
            trust_env: bool,
        ) -> None:
            """
            Capture construction arguments for later assertions.

            :param base_url: Omnigent server base URL.
            :param headers: HTTP headers passed by the wrapper.
            :param timeout: Request timeout in seconds.
            :param trust_env: Whether env proxy settings are honored.
            :returns: None.
            """
            calls.append(
                {
                    "base_url": base_url,
                    "headers": headers,
                    "timeout": timeout,
                    "trust_env": trust_env,
                }
            )

        def __enter__(self) -> _FakeHttpxClient:
            """
            Enter the fake client context.

            :returns: This fake client.
            """
            return self

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            """
            Exit the fake client context.

            :param exc_type: Exception type from the context, or
                ``None``.
            :param exc: Exception instance from the context, or
                ``None``.
            :param traceback: Traceback from the context, or
                ``None``.
            :returns: None.
            """

        def get(self, url: str) -> httpx.Response:
            """
            Return a session response for the requested URL.

            :param url: Relative Omnigent session path, e.g.
                ``"/v1/sessions/conv%20with%20space"``.
            :returns: HTTP response with ``external_session_id``.
            """
            calls.append({"url": url})
            return httpx.Response(
                200,
                json={"external_session_id": "claude-session-from-api"},
            )

    monkeypatch.setattr(claude_native.httpx, "Client", _FakeHttpxClient)

    result = claude_native._fetch_external_session_id_for_redirect(
        base_url="http://ap.example",
        headers={"Authorization": "Bearer token"},
        session_id="conv with space",
    )

    assert result == "claude-session-from-api"
    assert calls == [
        {
            "base_url": "http://ap.example",
            "headers": {"Authorization": "Bearer token"},
            "timeout": 10.0,
            # A remote host keeps the environment's proxy — only loopback
            # targets, which a proxy cannot reach, opt out.
            "trust_env": True,
        },
        {"url": "/v1/sessions/conv%20with%20space"},
    ]


def test_align_working_directory_leave_action_cancels_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Mismatched cwd, user chooses leave → no chdir, clear exception.

    Continuing would hand Claude a known-bad cwd and crash, so the
    third action exits before launch instead. The wrapper must not
    mutate cwd when the user chooses to leave.
    """
    from omnigent.claude_native_state import write_launch_state

    recorded = tmp_path / "recorded-leave"
    recorded.mkdir()
    current = tmp_path / "current-leave"
    current.mkdir()
    monkeypatch.chdir(current)
    starting_cwd = Path.cwd().resolve()
    write_launch_state("conv_mismatch_leave", str(recorded.resolve()))

    monkeypatch.setattr(
        claude_native.click,
        "prompt",
        lambda *args, **kwargs: "leave",
    )

    with pytest.raises(click.ClickException, match="Resume cancelled"):
        claude_native._align_working_directory_with_session("conv_mismatch_leave")

    assert Path.cwd().resolve() == starting_cwd, (
        f"cwd must remain unchanged when user leaves; "
        f"started at {starting_cwd}, now at {Path.cwd().resolve()}"
    )


def test_align_working_directory_move_without_external_id_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A ``move`` action without an external Claude id raises explicitly.

    The normal picker only offers ``move`` when redirect is available,
    but this runtime invariant must not rely on ``assert`` because
    Python strips asserts under ``-O``.
    """
    from omnigent.claude_native_state import write_launch_state

    recorded = tmp_path / "recorded-no-external"
    recorded.mkdir()
    current = tmp_path / "current-no-external"
    current.mkdir()
    monkeypatch.chdir(current)
    write_launch_state("conv_move_no_external", str(recorded.resolve()))
    monkeypatch.setattr(
        claude_native,
        "_fetch_external_session_id_for_redirect",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        claude_native,
        "_prompt_resume_workspace_action",
        lambda **kwargs: "move",
    )

    with pytest.raises(click.ClickException, match="no external session id"):
        claude_native._align_working_directory_with_session("conv_move_no_external")


def test_align_working_directory_raises_when_recorded_path_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Recorded path no longer exists → fail loud (no auto-create, no
    silent skip).

    Letting the launch proceed would only delay the failure:
    ``claude --resume`` cannot succeed without the original cwd,
    so Claude would exit immediately on launch with no
    explanation. Auto-creating the dir would surprise the user
    and mask the underlying "project moved / deleted" reality.
    The pre-flight error names the recorded path so the user
    can choose to recreate it, move the project back, or start a
    fresh session.
    """
    from omnigent.claude_native_state import write_launch_state

    monkeypatch.chdir(tmp_path)
    missing = "/this/path/should/not/exist/anywhere/nope-abcxyz"
    write_launch_state("conv_missing", missing)

    # ``prompt`` MUST NOT be reached when neither switch nor
    # move is viable.
    def fake_prompt(*args: object, **kwargs: object) -> str:
        raise AssertionError(
            f"prompt must not be called when recorded path is missing and "
            f"move is unavailable; "
            f"args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr(claude_native.click, "prompt", fake_prompt)

    with pytest.raises(click.ClickException) as excinfo:
        claude_native._align_working_directory_with_session("conv_missing")

    # The message MUST identify the missing path so the user knows
    # what to recreate. A generic error like "directory missing"
    # would leave them guessing which directory.
    assert missing in str(excinfo.value.message), (
        f"ClickException must include the missing path; got {excinfo.value.message!r}"
    )


def test_align_working_directory_redirect_moves_transcript_and_updates_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    User-approved redirect moves Claude's transcript into the current cwd.

    This exercises the intended "move Claude" path without touching
    real ``~/.claude`` state: find the old transcript by external
    session id, write it into the current cwd's Claude project dir,
    rewrite top-level ``cwd`` values, remove the original transcript,
    and update Omnigent launch state so future resumes treat the
    current cwd as the session home.
    """
    from omnigent.claude_native_state import read_launch_state, write_launch_state

    projects_dir = tmp_path / ".claude" / "projects"
    old_workspace = tmp_path / "old workspace"
    old_workspace.mkdir()
    current_workspace = tmp_path / "current workspace"
    current_workspace.mkdir()
    old_project_dir = projects_dir / claude_native._sanitize_claude_project_name(
        str(old_workspace.resolve())
    )
    old_project_dir.mkdir(parents=True)
    external_session_id = "claude-session-abc"
    source = old_project_dir / f"{external_session_id}.jsonl"
    original_lines = [
        json.dumps(
            {
                "type": "user",
                "cwd": str(old_workspace.resolve()),
                "sessionId": external_session_id,
            }
        ),
        json.dumps({"type": "assistant", "sessionId": external_session_id}),
        "",
    ]
    source.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(
        claude_native,
        "_fetch_external_session_id_for_redirect",
        lambda **kwargs: external_session_id,
    )
    monkeypatch.setattr(claude_native.click, "prompt", lambda *args, **kwargs: "move")
    monkeypatch.chdir(current_workspace)
    write_launch_state("conv_redirect", str(old_workspace.resolve()))

    claude_native._align_working_directory_with_session(
        "conv_redirect",
        base_url="http://ap.example",
        headers={"Authorization": "Bearer token"},
    )

    target = (
        projects_dir
        / claude_native._sanitize_claude_project_name(str(current_workspace.resolve()))
        / f"{external_session_id}.jsonl"
    )
    assert target.is_file(), f"redirect target {target} was not created"
    copied_payloads = [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert copied_payloads[0] == {
        "type": "user",
        "cwd": str(current_workspace.resolve()),
        "sessionId": external_session_id,
    }
    assert copied_payloads[1] == {
        "type": "assistant",
        "sessionId": external_session_id,
    }, "records without top-level cwd must be preserved aside from compact JSON formatting"
    assert not source.exists(), f"source transcript {source} should be removed after move"
    state = read_launch_state("conv_redirect")
    assert state is not None
    assert state.working_directory == str(current_workspace.resolve())


def test_align_working_directory_redirect_replaces_stale_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Redirect replaces a stale same-id target and removes the source.

    A prior copy-based redirect left the old project with a duplicate
    transcript. Moving the session back to that project should not
    fail on the stale target; it should make the current project the
    only owner of the Claude session id.
    """
    from omnigent.claude_native_state import read_launch_state, write_launch_state

    projects_dir = tmp_path / ".claude" / "projects"
    old_workspace = tmp_path / "old"
    old_workspace.mkdir()
    current_workspace = tmp_path / "current"
    current_workspace.mkdir()
    external_session_id = "claude-session-collision"
    old_project_dir = projects_dir / "old-project"
    old_project_dir.mkdir(parents=True)
    (old_project_dir / f"{external_session_id}.jsonl").write_text(
        json.dumps(
            {
                "cwd": str(old_workspace.resolve()),
                "sessionId": external_session_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    target_dir = projects_dir / claude_native._sanitize_claude_project_name(
        str(current_workspace.resolve())
    )
    target_dir.mkdir(parents=True)
    target = target_dir / f"{external_session_id}.jsonl"
    target.write_text('{"cwd":"do-not-overwrite"}\n', encoding="utf-8")
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(
        claude_native,
        "_fetch_external_session_id_for_redirect",
        lambda **kwargs: external_session_id,
    )
    monkeypatch.setattr(claude_native.click, "prompt", lambda *args, **kwargs: "move")
    monkeypatch.chdir(current_workspace)
    write_launch_state("conv_redirect_collision", str(old_workspace.resolve()))

    claude_native._align_working_directory_with_session(
        "conv_redirect_collision",
        base_url="http://ap.example",
        headers={},
    )

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "cwd": str(current_workspace.resolve()),
        "sessionId": external_session_id,
    }
    source = old_project_dir / f"{external_session_id}.jsonl"
    assert not source.exists(), f"source transcript {source} should be removed after move"
    state = read_launch_state("conv_redirect_collision")
    assert state is not None
    assert state.working_directory == str(current_workspace.resolve())


def test_align_working_directory_redirect_works_when_recorded_path_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Missing recorded cwd can still resume when redirect is available.

    This is the recovery path for a moved/deleted project: the
    original directory cannot be switched to, but the local Claude
    transcript still exists under ``~/.claude/projects``. The prompt
    should offer redirect as the default and the helper should move
    the transcript instead of failing early.
    """
    from omnigent.claude_native_state import read_launch_state, write_launch_state

    projects_dir = tmp_path / ".claude" / "projects"
    current_workspace = tmp_path / "current"
    current_workspace.mkdir()
    missing_workspace = tmp_path / "deleted"
    external_session_id = "claude-session-moved"
    old_project_dir = projects_dir / "deleted-project"
    old_project_dir.mkdir(parents=True)
    (old_project_dir / f"{external_session_id}.jsonl").write_text(
        json.dumps({"cwd": str(missing_workspace.resolve())}) + "\n",
        encoding="utf-8",
    )
    prompt_calls: list[dict[str, object]] = []

    def fake_prompt(
        prompt: str,
        *,
        type: click.Choice,
        default: str,
        show_choices: bool,
        err: bool,
    ) -> str:
        """Capture the redirect-only action set."""
        prompt_calls.append(
            {
                "prompt": prompt,
                "choices": tuple(type.choices),
                "default": default,
                "show_choices": show_choices,
                "err": err,
            }
        )
        return "move"

    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(
        claude_native,
        "_fetch_external_session_id_for_redirect",
        lambda **kwargs: external_session_id,
    )
    monkeypatch.setattr(claude_native.click, "prompt", fake_prompt)
    monkeypatch.chdir(current_workspace)
    write_launch_state("conv_missing_redirect", str(missing_workspace.resolve()))

    claude_native._align_working_directory_with_session(
        "conv_missing_redirect",
        base_url="http://ap.example",
        headers={},
    )

    assert prompt_calls == [
        {
            "prompt": "Resume action",
            "choices": ("move", "leave"),
            "default": "move",
            "show_choices": True,
            "err": True,
        }
    ]
    target = (
        projects_dir
        / claude_native._sanitize_claude_project_name(str(current_workspace.resolve()))
        / f"{external_session_id}.jsonl"
    )
    assert target.is_file(), f"redirect target {target} was not created"
    state = read_launch_state("conv_missing_redirect")
    assert state is not None
    assert state.working_directory == str(current_workspace.resolve())


# ── _clone_claude_transcript (forked clone) ─────────


def test_clone_claude_transcript_rewrites_session_and_cwd_into_clone_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Cloning a fork transcript lands it in the CLONE's project dir with a
    rewritten sessionId/cwd and a preserved uuid chain.

    This is the core of the clone-transcript approach: the clone resumes
    in a DIFFERENT directory than the source (the worktree case that
    ``--fork-session`` could not handle, because Claude's ``--resume`` is
    cwd-scoped). The
    copied transcript must (1) be written under the clone workspace's
    project dir — not the source's — so plain ``--resume <our_uuid>``
    finds it; (2) carry the clone's own ``sessionId`` on every record so
    the bridge tracks the clone, not the original; (3) carry the clone's
    ``cwd``; (4) preserve the ``uuid``/``parentUuid`` chain verbatim so
    Claude can rebuild history; (5) leave the source transcript intact
    (the original session is untouched by a fork).
    """
    projects_dir = tmp_path / ".claude" / "projects"
    source_workspace = tmp_path / "source repo"
    source_workspace.mkdir()
    clone_workspace = tmp_path / "clone worktree"
    clone_workspace.mkdir()
    source_uuid = "11111111-1111-1111-1111-111111111111"
    target_uuid = "22222222-2222-2222-2222-222222222222"
    source_project_dir = projects_dir / claude_native._sanitize_claude_project_name(
        str(source_workspace.resolve())
    )
    source_project_dir.mkdir(parents=True)
    source_path = source_project_dir / f"{source_uuid}.jsonl"
    source_lines = [
        # A meta record with neither cwd nor sessionId — must pass through
        # untouched (proves we only rewrite the two fields, nothing else).
        json.dumps({"type": "summary", "leafUuid": "abc"}),
        # A user turn carrying cwd + sessionId + the chain root.
        json.dumps(
            {
                "type": "user",
                "cwd": str(source_workspace.resolve()),
                "sessionId": source_uuid,
                "uuid": "u1",
                "parentUuid": None,
            }
        ),
        # An assistant turn continuing the chain.
        json.dumps(
            {
                "type": "assistant",
                "cwd": str(source_workspace.resolve()),
                "sessionId": source_uuid,
                "uuid": "u2",
                "parentUuid": "u1",
            }
        ),
        "",
    ]
    source_path.write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)

    result = claude_native._clone_claude_transcript(
        source_external_session_id=source_uuid,
        target_external_session_id=target_uuid,
        clone_workspace=clone_workspace.resolve(),
    )

    expected_target = (
        projects_dir
        / claude_native._sanitize_claude_project_name(str(clone_workspace.resolve()))
        / f"{target_uuid}.jsonl"
    )
    # The clone must land in the CLONE's project dir, not the source's —
    # this is the cross-dir fix that makes cwd-scoped --resume work in a
    # new worktree. A wrong path here means the worktree case still fails.
    assert result == expected_target
    assert expected_target.is_file(), f"clone transcript {expected_target} was not written"

    cloned_payloads = [
        json.loads(line)
        for line in expected_target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Meta record passes through verbatim (only cwd/sessionId are rewritten).
    assert cloned_payloads[0] == {"type": "summary", "leafUuid": "abc"}
    # sessionId rewritten to the clone's uuid on every record that had one
    # — if any still said source_uuid, the bridge would track the original.
    assert all(p.get("sessionId") == target_uuid for p in cloned_payloads[1:])
    # cwd rewritten to the clone workspace — if it still pointed at the
    # source dir, Claude would reject the resume as a cwd mismatch.
    assert all(p["cwd"] == str(clone_workspace.resolve()) for p in cloned_payloads[1:])
    # uuid/parentUuid chain preserved verbatim — Claude rebuilds history
    # from it; rewriting these would orphan the turns.
    assert [(p["uuid"], p["parentUuid"]) for p in cloned_payloads[1:]] == [
        ("u1", None),
        ("u2", "u1"),
    ]

    # Source transcript untouched — a fork must not mutate the original.
    source_after = source_path.read_text(encoding="utf-8")
    assert f'"sessionId":"{source_uuid}"' in source_after.replace(" ", "")
    assert source_path.is_file()


def test_clone_claude_transcript_returns_none_when_source_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    With no source transcript on this host, the clone helper returns
    ``None`` and writes nothing.

    The runner relies on this to fall back to a FRESH launch (rather than
    pointing ``--resume`` at a file that doesn't exist, which would leave
    ``external_session_id`` dangling). A non-None return — or a stray
    written file — would mean the fallback never triggers.
    """
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    clone_workspace = tmp_path / "clone"
    clone_workspace.mkdir()
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)

    result = claude_native._clone_claude_transcript(
        source_external_session_id="00000000-0000-0000-0000-000000000000",
        target_external_session_id="99999999-9999-9999-9999-999999999999",
        clone_workspace=clone_workspace.resolve(),
    )

    # No source → no clone; the runner launches fresh.
    assert result is None
    # Nothing should have been written to the clone's project dir.
    clone_project_dir = projects_dir / claude_native._sanitize_claude_project_name(
        str(clone_workspace.resolve())
    )
    assert not clone_project_dir.exists() or not any(clone_project_dir.iterdir())


def test_clone_claude_transcript_repairs_stale_image_duplication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A fork's FIRST launch must not replay a stale duplicated transcript.

    The clone path byte-copies the source's local JSONL, so a transcript
    synthesized before the ``toolUseResult`` redaction fix (image base64
    in both the structured content and the metadata) — or one holding an
    MCP screenshot as a raw string — would overflow the clone's first
    ``--resume`` before any later rebuild could heal it. The copy must
    repair image-bearing tool-result records on the way through: exactly
    one structured image copy per payload, redacted metadata, and no
    string-valued content carrying base64. Records without image
    duplication must pass through unchanged.
    """
    projects_dir = tmp_path / ".claude" / "projects"
    source_workspace = tmp_path / "source repo"
    source_workspace.mkdir()
    clone_workspace = tmp_path / "clone worktree"
    clone_workspace.mkdir()
    source_uuid = "11111111-1111-1111-1111-111111111111"
    target_uuid = "22222222-2222-2222-2222-222222222222"
    source_project_dir = projects_dir / claude_native._sanitize_claude_project_name(
        str(source_workspace.resolve())
    )
    source_project_dir.mkdir(parents=True)
    source_path = source_project_dir / f"{source_uuid}.jsonl"

    b64_structured = "iVBORw0KGgo" + "K" * 4000 + "="
    b64_mcp = _TINY_PNG_BASE64
    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64_structured},
    }
    mcp_object = {"type": "image", "data": b64_mcp, "mimeType": "image/png"}
    mixed_string = "screenshot taken\n" + json.dumps(mcp_object, separators=(",", ":"))
    stale_structured = {
        "type": "user",
        "cwd": str(source_workspace.resolve()),
        "sessionId": source_uuid,
        "uuid": "u1",
        "parentUuid": None,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [image_block]}
            ],
        },
        # Pre-fix metadata: the verbatim block array, base64 included.
        "toolUseResult": json.dumps([image_block], separators=(",", ":")),
    }
    stale_mixed = {
        "type": "user",
        "cwd": str(source_workspace.resolve()),
        "sessionId": source_uuid,
        "uuid": "u2",
        "parentUuid": "u1",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": mixed_string}
            ],
        },
        # Pre-fix metadata for the unparseable mixed string: a JSON
        # string literal that still embeds the full payload.
        "toolUseResult": json.dumps(mixed_string),
    }
    plain_result = {
        "type": "user",
        "cwd": str(source_workspace.resolve()),
        "sessionId": source_uuid,
        "uuid": "u3",
        "parentUuid": "u2",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_3", "content": "file written"}
            ],
        },
        "toolUseResult": json.dumps("file written"),
    }
    source_path.write_text(
        "".join(
            json.dumps(record) + "\n" for record in (stale_structured, stale_mixed, plain_result)
        ),
        encoding="utf-8",
    )
    source_text = source_path.read_text(encoding="utf-8")
    assert source_text.count(b64_structured) == 2, "pre-fix wedged state"
    assert source_text.count(b64_mcp) == 2, "pre-fix wedged state"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)

    result = claude_native._clone_claude_transcript(
        source_external_session_id=source_uuid,
        target_external_session_id=target_uuid,
        clone_workspace=clone_workspace.resolve(),
    )

    assert result is not None
    text = result.read_text(encoding="utf-8")
    assert text.count(b64_structured) == 1, "first-launch transcript must be repaired"
    assert text.count(b64_mcp) == 1, "first-launch transcript must be repaired"
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    # Structured duplicate: content keeps the one image copy, metadata redacted.
    content_one = records[0]["message"]["content"][0]["content"]
    assert content_one == [image_block]
    repaired_result = json.loads(records[0]["toolUseResult"])
    assert b64_structured not in json.dumps(repaired_result)
    assert repaired_result[0]["source"]["media_type"] == "image/png"
    # MCP mixed string: normalized to a text+image block list, metadata repaired.
    content_two = records[1]["message"]["content"][0]["content"]
    assert content_two == [
        {"type": "text", "text": "screenshot taken"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64_mcp},
        },
    ]
    assert b64_mcp not in records[1]["toolUseResult"]
    # No image duplication: the plain record is preserved untouched.
    assert records[2]["message"]["content"][0]["content"] == "file written"
    assert records[2]["toolUseResult"] == json.dumps("file written")
    # The fork must not mutate the source session's transcript.
    assert source_path.read_text(encoding="utf-8") == source_text


@pytest.mark.parametrize(
    "case",
    [
        "invalid-base64",
        "under-floor-jpeg",
        "non-image-bytes",
        "mime-mismatch",
        "riff-not-webp",
    ],
)
def test_clone_claude_transcript_leaves_invalid_image_shaped_text_unchanged(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The fork sanitizer must not "repair" text that only looks like an image.

    A cloned record whose tool_result content is image-shaped text with
    an invalid payload passes the same validation guard as the rebuild
    path: no block conversion, no metadata rewrite — the record arrives
    exactly as copied (modulo the usual cwd/sessionId rewrites).
    """
    projects_dir = tmp_path / ".claude" / "projects"
    source_workspace = tmp_path / "source repo"
    source_workspace.mkdir()
    clone_workspace = tmp_path / "clone worktree"
    clone_workspace.mkdir()
    source_uuid = "11111111-1111-1111-1111-111111111111"
    target_uuid = "22222222-2222-2222-2222-222222222222"
    source_project_dir = projects_dir / claude_native._sanitize_claude_project_name(
        str(source_workspace.resolve())
    )
    source_project_dir.mkdir(parents=True)
    source_path = source_project_dir / f"{source_uuid}.jsonl"
    if case == "invalid-base64":
        fake_data, fake_mime = "not!valid!base64", "image/png"
    elif case == "under-floor-jpeg":
        fake_data, fake_mime = _UNDER_FLOOR_JPEGS["soi-eoi"], "image/jpeg"
    elif case == "non-image-bytes":
        fake_data, fake_mime = base64.b64encode(b"plain text " * 8).decode(), "image/png"
    elif case == "mime-mismatch":
        fake_data, fake_mime = _TINY_PNG_BASE64, "image/jpeg"
    else:
        fake_data = base64.b64encode(b"RIFF" + b"\x00" * 4 + b"AVI " + b"\x00" * 40).decode()
        fake_mime = "image/webp"
    fake_text = json.dumps({"type": "image", "data": fake_data, "mimeType": fake_mime})
    record = {
        "type": "user",
        "cwd": str(source_workspace.resolve()),
        "sessionId": source_uuid,
        "uuid": "u1",
        "parentUuid": None,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": fake_text}],
        },
        "toolUseResult": json.dumps(fake_text),
    }
    source_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)

    result = claude_native._clone_claude_transcript(
        source_external_session_id=source_uuid,
        target_external_session_id=target_uuid,
        clone_workspace=clone_workspace.resolve(),
    )

    assert result is not None
    cloned = [json.loads(line) for line in result.read_text(encoding="utf-8").splitlines()]
    assert cloned[0]["message"]["content"][0]["content"] == fake_text
    assert cloned[0]["toolUseResult"] == json.dumps(fake_text)


def _stale_duplicated_jpeg_record(b64: str) -> dict[str, Any]:
    """A pre-fix synthesized record: image base64 in content AND metadata."""
    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
    }
    return {
        "type": "user",
        "sessionId": "sid",
        "uuid": "u1",
        "parentUuid": None,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [image_block]}
            ],
        },
        "toolUseResult": json.dumps([image_block], separators=(",", ":")),
    }


def _decoded_payload_copies(record: object, raw: bytes) -> int:
    """Count copies of *raw* by decoding, defeating wrapping and JSON escapes.

    A canonical-substring search cannot see a duplicate stored in the producer's
    wrapped spelling, which is exactly how one hid from an earlier fix.
    """
    run = re.compile(r"[A-Za-z0-9+/=\s]{64,}")

    def _strings(value: object) -> Iterator[str]:
        if isinstance(value, str):
            yield value
            try:
                nested = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return
            if isinstance(nested, (dict, list, str)):
                yield from _strings(nested)
        elif isinstance(value, dict):
            for item in value.values():
                yield from _strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from _strings(item)

    copies = 0
    for text in _strings(record):
        for candidate in run.findall(text):
            compact = "".join(candidate.split())
            padded = compact + "=" * (-len(compact) % 4)
            try:
                if raw in base64.b64decode(padded, validate=False):
                    copies += 1
            except (binascii.Error, ValueError):
                continue
    return copies


def _wrapped_image_spellings() -> tuple[bytes, dict[str, str]]:
    """A realistic PNG payload in every base64 spelling a producer may emit."""
    raw = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 28
    canonical = base64.b64encode(raw).decode()
    return raw, {
        "canonical": canonical,
        "mime-wrapped": "\n".join(canonical[i : i + 76] for i in range(0, len(canonical), 76)),
        "crlf-wrapped": "\r\n".join(canonical[i : i + 64] for i in range(0, len(canonical), 64)),
        "unpadded": canonical.rstrip("="),
        "space-separated": " ".join(canonical[i : i + 40] for i in range(0, len(canonical), 40)),
    }


def _assert_provider_ready_image(block: dict[str, Any], raw: bytes, canonical: str) -> None:
    """Assert a rebuilt block is exactly what the provider accepts.

    The provider validates ``source.data`` strictly and rejected a whole request
    on a wrapped payload (``invalid base64 image data: Invalid symbol 13, offset
    76``), so the emitted spelling — not just the bytes — is the contract.
    """
    data = block["source"]["data"]
    assert data == canonical, "structured payload must be canonical standard base64"
    assert not any(character.isspace() for character in data)
    assert "\\" not in data
    assert len(data) % 4 == 0
    # Provider compatibility: strict decoding must succeed on the emitted string.
    assert base64.b64decode(data, validate=True) == raw


@pytest.mark.parametrize(
    "spelling",
    ["canonical", "mime-wrapped", "crlf-wrapped", "unpadded", "space-separated"],
)
@pytest.mark.parametrize("stored", ["source-shaped", "mcp-shaped", "nested-escaped"])
def test_rebuilt_image_block_is_canonical_for_the_provider(spelling: str, stored: str) -> None:
    """Every valid payload reaches the model as canonical base64.

    A real hosted smoke failed 4/4 attempts at 0 tokens because the
    Anthropic-shaped passthrough kept the producer's CRLF wrapping — the shape
    the affected session actually stores. Bytes were intact throughout; only the
    spelling was fatal.
    """
    raw, spellings = _wrapped_image_spellings()
    canonical = base64.b64encode(raw).decode()
    payload = spellings[spelling]
    if stored == "mcp-shaped":
        output = json.dumps(
            {"type": "image", "data": payload, "mimeType": "image/png"}, separators=(",", ":")
        )
    else:
        blocks = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": payload},
            }
        ]
        output = json.dumps(blocks, separators=(",", ":"))
        if stored == "nested-escaped":
            # A JSON document nested inside a JSON string, as metadata stores it.
            output = json.loads(json.dumps(output))

    records = _image_output_records(output)
    record = records[0]
    content = record["message"]["content"][0]["content"]
    assert isinstance(content, list)
    _assert_provider_ready_image(content[-1], raw, canonical)
    assert _decoded_payload_copies(record, raw) == 1
    assert _decoded_payload_copies(record["toolUseResult"], raw) == 0


@pytest.mark.parametrize(
    "spelling",
    ["canonical", "mime-wrapped", "crlf-wrapped", "unpadded", "space-separated"],
)
@pytest.mark.parametrize("path", ["clone-string", "clone-list", "cwd-copy"])
def test_repair_paths_emit_canonical_source_data(spelling: str, path: str, tmp_path: Path) -> None:
    """Clone repair and the cwd copy canonicalize the same way."""
    raw, spellings = _wrapped_image_spellings()
    canonical = base64.b64encode(raw).decode()
    blocks = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": spellings[spelling]},
        }
    ]
    serialized = json.dumps(blocks, separators=(",", ":"))

    if path == "cwd-copy":
        source = tmp_path / "source.jsonl"
        target = tmp_path / "target.jsonl"
        source.write_text(
            json.dumps(
                {
                    "type": "user",
                    "cwd": "/old/workspace",
                    "sessionId": "11111111-1111-1111-1111-111111111111",
                    "uuid": "u1",
                    "parentUuid": None,
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "t", "content": serialized}
                        ],
                    },
                    "toolUseResult": json.dumps(serialized),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        claude_native._copy_transcript_with_cwd(source=source, target=target, current=tmp_path)
        record = next(json.loads(line) for line in target.read_text().splitlines() if line.strip())
    else:
        inner: Any = serialized if path == "clone-string" else blocks
        record = {
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t", "content": inner}],
            },
            "toolUseResult": json.dumps(inner if isinstance(inner, str) else json.dumps(inner)),
        }
        claude_native._sanitize_cloned_tool_result_record(record)

    content = record["message"]["content"][0]["content"]
    assert isinstance(content, list)
    _assert_provider_ready_image(content[-1], raw, canonical)
    assert _decoded_payload_copies(record, raw) == 1
    assert _decoded_payload_copies(record["toolUseResult"], raw) == 0


@pytest.mark.parametrize(
    "spelling",
    ["canonical", "mime-wrapped", "crlf-wrapped", "unpadded", "space-separated"],
)
@pytest.mark.parametrize("shape", ["string", "list"])
def test_clone_repair_leaves_one_payload_copy_in_any_spelling(spelling: str, shape: str) -> None:
    """Canonicalizing the content must not hide the metadata duplicate.

    The rebuilt block carries canonical base64 while metadata keeps the
    producer's original spelling, so an exact-substring guard skipped the record
    and left two bytes-equal copies. Counting decoded bytes across the whole
    record is what makes that visible.
    """
    raw, spellings = _wrapped_image_spellings()
    payload = spellings[spelling]
    if shape == "list":
        inner: Any = [{"type": "image", "data": payload, "mimeType": "image/png"}]
        metadata = json.dumps(json.dumps(inner))
    else:
        inner = json.dumps(
            {"type": "image", "data": payload, "mimeType": "image/png"}, separators=(",", ":")
        )
        metadata = json.dumps(inner)
    record: dict[str, Any] = {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": inner}],
        },
        "toolUseResult": metadata,
    }
    assert _decoded_payload_copies(record, raw) >= 2, "pre-repair wedged state"

    claude_native._sanitize_cloned_tool_result_record(record)

    assert _decoded_payload_copies(record, raw) == 1
    assert _decoded_payload_copies(record["toolUseResult"], raw) == 0


@pytest.mark.parametrize(
    "spelling",
    ["canonical", "mime-wrapped", "crlf-wrapped", "unpadded", "space-separated"],
)
def test_reconstruction_and_cwd_copy_keep_one_payload_copy(spelling: str, tmp_path: Path) -> None:
    """Cold-resume synthesis and the cwd copy hold the same invariant."""
    raw, spellings = _wrapped_image_spellings()
    payload = spellings[spelling]
    output = json.dumps(
        {"type": "image", "data": payload, "mimeType": "image/png"}, separators=(",", ":")
    )

    records = _image_output_records(output)
    assert _decoded_payload_copies(records[0], raw) == 1

    source = tmp_path / "source.jsonl"
    target = tmp_path / "target.jsonl"
    stale = {
        "type": "user",
        "cwd": "/old/workspace",
        "sessionId": "11111111-1111-1111-1111-111111111111",
        "uuid": "u1",
        "parentUuid": None,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": output}],
        },
        "toolUseResult": json.dumps(output),
    }
    source.write_text(json.dumps(stale) + "\n", encoding="utf-8")

    claude_native._copy_transcript_with_cwd(source=source, target=target, current=tmp_path)

    copied = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
    assert _decoded_payload_copies(copied, raw) == 1


def _oversized_invalid_image_object(marker: str) -> tuple[str, str]:
    """Return a valid-JSON MCP image object whose payload fails the gate.

    Large enough to cross the collapse threshold, so normalization drops it for
    a placeholder and reports ``dropped_oversized_image``.
    """
    payload = base64.b64encode(marker.encode() + b"not an image " * 4_000).decode()
    return payload, json.dumps(
        {"type": "image", "data": payload, "mimeType": "image/png"}, separators=(",", ":")
    )


@pytest.mark.parametrize("spelling", ["string", "errored-string", "list"])
def test_clone_repair_collapses_a_dropped_oversized_payload(spelling: str) -> None:
    """A payload normalization *dropped* must not survive clone repair.

    The sanitizer used to project straight to ``.blocks``; a placeholder carries
    no image payload, so the repair skipped the record and left the original
    base64 in both ``tool_result`` content and ``toolUseResult``.
    """
    payload, image_object = _oversized_invalid_image_object("clone")
    errored = spelling == "errored-string"
    content: Any = {
        "string": image_object,
        "errored-string": f"Error: {image_object}",
        "list": [{"type": "image", "data": payload, "mimeType": "image/png"}],
    }[spelling]
    record: dict[str, Any] = {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}],
        },
        "toolUseResult": json.dumps(content if isinstance(content, str) else json.dumps(content)),
    }
    assert json.dumps(record).count(payload) == 2, "pre-repair wedged state"

    claude_native._sanitize_cloned_tool_result_record(record)

    blob = json.dumps(record)
    assert blob.count(payload) == 0
    assert len(blob) < len(payload) // 10
    repaired = record["message"]["content"][0]["content"]
    assert isinstance(repaired, list)
    assert "omitted from history" in json.dumps(repaired)
    assert payload not in json.dumps(record["toolUseResult"])
    if errored:
        assert repaired[0] == {"type": "text", "text": "Error:"}


def test_clone_and_cwd_copy_paths_both_collapse_a_dropped_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both transcript-copy entry points repair a dropped-payload record."""
    projects_dir = tmp_path / ".claude" / "projects"
    source_workspace = tmp_path / "source repo"
    source_workspace.mkdir()
    clone_workspace = tmp_path / "clone worktree"
    clone_workspace.mkdir()
    source_uuid = "11111111-1111-1111-1111-111111111111"
    target_uuid = "22222222-2222-2222-2222-222222222222"
    source_project_dir = projects_dir / claude_native._sanitize_claude_project_name(
        str(source_workspace.resolve())
    )
    source_project_dir.mkdir(parents=True)
    source_path = source_project_dir / f"{source_uuid}.jsonl"
    payload, image_object = _oversized_invalid_image_object("paths")
    record = {
        "type": "user",
        "cwd": str(source_workspace.resolve()),
        "sessionId": source_uuid,
        "uuid": "u1",
        "parentUuid": None,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": image_object}
            ],
        },
        "toolUseResult": json.dumps(image_object),
    }
    source_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    source_text = source_path.read_text(encoding="utf-8")
    assert source_text.count(payload) == 2, "pre-repair wedged state"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)

    cloned = claude_native._clone_claude_transcript(
        source_external_session_id=source_uuid,
        target_external_session_id=target_uuid,
        clone_workspace=clone_workspace.resolve(),
    )
    assert cloned is not None
    assert cloned.read_text(encoding="utf-8").count(payload) == 0

    redirected = tmp_path / "redirected.jsonl"
    claude_native._copy_transcript_with_cwd(
        source=source_path, target=redirected, current=clone_workspace.resolve()
    )
    assert redirected.read_text(encoding="utf-8").count(payload) == 0
    # Neither copy path mutates the source transcript.
    assert source_path.read_text(encoding="utf-8") == source_text


def test_clone_claude_transcript_repairs_progressive_jpeg_duplication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A stale two-copy progressive JPEG repairs to one on fork clone.

    Before the multi-scan JPEG parser fix, a progressive JPEG stayed raw
    (validator rejected it), so the clone sanitizer could not recognize
    the record's image and left both payload copies in place. The clone
    must now repair the first-launch transcript to exactly one copy.
    """
    projects_dir = tmp_path / ".claude" / "projects"
    source_workspace = tmp_path / "source repo"
    source_workspace.mkdir()
    clone_workspace = tmp_path / "clone worktree"
    clone_workspace.mkdir()
    source_uuid = "11111111-1111-1111-1111-111111111111"
    target_uuid = "22222222-2222-2222-2222-222222222222"
    source_project_dir = projects_dir / claude_native._sanitize_claude_project_name(
        str(source_workspace.resolve())
    )
    source_project_dir.mkdir(parents=True)
    source_path = source_project_dir / f"{source_uuid}.jsonl"
    b64 = _TINY_PROGRESSIVE_JPEG_BASE64
    source_path.write_text(json.dumps(_stale_duplicated_jpeg_record(b64)) + "\n", encoding="utf-8")
    assert source_path.read_text(encoding="utf-8").count(b64) == 2, "pre-fix wedged state"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects_dir)

    result = claude_native._clone_claude_transcript(
        source_external_session_id=source_uuid,
        target_external_session_id=target_uuid,
        clone_workspace=clone_workspace.resolve(),
    )

    assert result is not None
    text = result.read_text(encoding="utf-8")
    assert text.count(b64) == 1
    record = json.loads(text.splitlines()[0])
    content = record["message"]["content"][0]["content"]
    assert content[0]["source"]["data"] == b64
    assert b64 not in json.dumps(json.loads(record["toolUseResult"]))


def test_copy_transcript_with_cwd_repairs_progressive_jpeg_duplication(
    tmp_path: Path,
) -> None:
    """
    The cwd-redirect copy path gets the same first-launch repair.

    ``_copy_transcript_with_cwd`` without ``new_session_id`` is the
    redirect/move form; a stale two-copy progressive JPEG record must be
    repaired to exactly one structured copy there too.
    """
    b64 = _TINY_PROGRESSIVE_JPEG_BASE64
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(_stale_duplicated_jpeg_record(b64)) + "\n", encoding="utf-8")
    target = tmp_path / "target.jsonl"

    claude_native._copy_transcript_with_cwd(source=source, target=target, current=tmp_path)

    text = target.read_text(encoding="utf-8")
    assert text.count(b64) == 1
    record = json.loads(text.splitlines()[0])
    content = record["message"]["content"][0]["content"]
    assert content[0]["source"]["data"] == b64
    assert b64 not in json.dumps(json.loads(record["toolUseResult"]))


# ── _record_launch_for_fresh_session ────────────────────────


def test_record_launch_for_fresh_session_writes_resolved_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The fresh-session write helper persists ``Path.cwd().resolve()``.

    Resolved (not raw) so a session created via a symlinked path
    records the canonical form. The resume-time check also
    resolves both sides before comparing, so a session created
    in ``/home/me/repo`` (a symlink) and resumed from
    ``/repo`` (the canonical) won't falsely flag as mismatched.
    """
    from omnigent.claude_native_state import read_launch_state

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    claude_native._record_launch_for_fresh_session("conv_fresh")

    state = read_launch_state("conv_fresh")
    assert state is not None, (
        "fresh-session record path did not persist state; the wrapper "
        "won't know the cwd at resume time."
    )
    assert state.working_directory == str(workspace.resolve()), (
        f"recorded cwd {state.working_directory!r} != resolved current "
        f"cwd {str(workspace.resolve())!r}; resume-time comparison will "
        f"falsely flag every resume."
    )


def test_record_launch_for_fresh_session_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A failed write logs and proceeds (no exception out).

    The launch state is a UX nicety. A single-shot OSError (disk
    full, read-only fs, permission denied) must not crash the
    wrapper between session creation and attach -- the user would
    be left with a session they can't cleanly terminate. Fall
    out is "no chdir prompt on resume", same as a legacy session.
    """
    monkeypatch.chdir(tmp_path)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated disk-full")

    monkeypatch.setattr(claude_native, "write_launch_state", boom)

    # Must not raise.
    claude_native._record_launch_for_fresh_session("conv_disk_full")


# ── Provider-aware native Claude launch config (configure harnesses) ──────────


def _seed_config(config_home: Path, providers: dict[str, object]) -> None:
    """Write a ``providers:`` block into an isolated config home."""
    (config_home / "config.yaml").write_text(yaml.safe_dump({"providers": providers}))


@pytest.fixture()
def _isolated_provider_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate config + ambient so provider resolution is deterministic."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in (
        "ANTHROPIC_API_KEY",
        "OMNIGENT_ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OMNIGENT_OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OMNIGENT_OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    return tmp_path


def _no_auth_claude_spec() -> Any:
    """A minimal claude-sdk spec with no executor.auth/profile."""
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    return AgentSpec(
        spec_version=1,
        name="t",
        instructions="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )


def test_provider_config_for_native_claude_key_injects_base_url_and_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``key`` provider becomes ANTHROPIC_BASE_URL + a printf apiKeyHelper.

    Mirrors what ucode injects, but from a configured OSS key — so a native
    Claude Code terminal routes through the provider. The static key must be
    delivered via the helper (the runner env strips ANTHROPIC_API_KEY), and
    the base_url + default model carried through. Failure means a native
    launch would ignore the configured provider. With no CLAUDE_CODE_USE_GATEWAY
    in the ambient env the gateway-safety beta-disable flag is set.
    """
    from omnigent.onboarding.provider_config import load_providers

    monkeypatch.delenv("CLAUDE_CODE_USE_GATEWAY", raising=False)

    entry = load_providers(
        {
            "providers": {
                "anthropic": {
                    "kind": "key",
                    "anthropic": {
                        "base_url": "https://api.anthropic.com",
                        "api_key": "sk-ant-test",
                        "models": {"default": "claude-sonnet-4-6"},
                    },
                }
            }
        }
    )["anthropic"]

    cfg = claude_native._provider_config_for_native_claude(entry)
    assert cfg is not None
    # ANTHROPIC_BASE_URL plus the gateway-safety beta-disable flag (gateways
    # 400 on beta flags they don't implement; see _provider_config_for_native_claude).
    assert cfg.env == {
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    # Static key delivered via the apiKeyHelper, never the env (allowlist).
    assert cfg.api_key_helper == "printf %s sk-ant-test"
    assert cfg.model == "claude-sonnet-4-6"


def test_provider_config_for_native_claude_uses_auth_command_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider ``auth_command`` is used as the apiKeyHelper verbatim."""
    from omnigent.onboarding.provider_config import load_providers

    monkeypatch.delenv("CLAUDE_CODE_USE_GATEWAY", raising=False)

    entry = load_providers(
        {
            "providers": {
                "gw": {
                    "kind": "gateway",
                    "anthropic": {
                        "base_url": "https://gw.example/v1",
                        "auth_command": "my-cli print-token",
                    },
                }
            }
        }
    )["gw"]

    cfg = claude_native._provider_config_for_native_claude(entry)
    assert cfg is not None
    assert cfg.api_key_helper == "my-cli print-token"
    assert cfg.env == {
        "ANTHROPIC_BASE_URL": "https://gw.example/v1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }


def test_provider_config_for_native_claude_keeps_betas_under_use_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With CLAUDE_CODE_USE_GATEWAY=1, the beta-disable flag is NOT set.

    Gateway-aware mode negotiates the anthropic-beta set with the gateway and
    keeps MCP tool search on (it rides on the ``advanced-tool-use`` beta), so
    disabling betas here would force every MCP tool schema to load eagerly.
    """
    from omnigent.onboarding.provider_config import load_providers

    monkeypatch.setenv("CLAUDE_CODE_USE_GATEWAY", "1")

    entry = load_providers(
        {
            "providers": {
                "gw": {
                    "kind": "gateway",
                    "anthropic": {
                        "base_url": "https://gw.example/v1",
                        "auth_command": "my-cli print-token",
                    },
                }
            }
        }
    )["gw"]

    cfg = claude_native._provider_config_for_native_claude(entry)
    assert cfg is not None
    assert cfg.env == {"ANTHROPIC_BASE_URL": "https://gw.example/v1"}
    assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in cfg.env


def test_bedrock_config_for_native_claude_static_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``bedrock`` provider sets the Bedrock env trio and no apiKeyHelper.

    Bedrock mode authenticates from ``AWS_BEARER_TOKEN_BEDROCK`` in the env and
    ignores ``apiKeyHelper``, so a static key must land in the env (never a
    helper) and the base_url maps to ``ANTHROPIC_BEDROCK_BASE_URL``. With no
    ``CLAUDE_CODE_USE_GATEWAY`` in the ambient env the beta-disable flag is set.
    """
    from omnigent.onboarding.provider_config import load_providers

    monkeypatch.delenv("CLAUDE_CODE_USE_GATEWAY", raising=False)

    entry = load_providers(
        {
            "providers": {
                "nexus": {
                    "kind": "bedrock",
                    "anthropic": {
                        "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
                        "api_key": "absk-test",
                        "models": {"default": "us.anthropic.claude-opus-4-5-20251101-v1:0"},
                    },
                }
            }
        }
    )["nexus"]

    cfg = claude_native._bedrock_config_for_native_claude(entry)
    assert cfg is not None
    assert cfg.env == {
        "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "AWS_BEARER_TOKEN_BEDROCK": "absk-test",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    # Bedrock ignores apiKeyHelper; the credential rides the env, not a helper.
    assert cfg.api_key_helper is None
    assert cfg.model == "us.anthropic.claude-opus-4-5-20251101-v1:0"


def test_bedrock_config_for_native_claude_resolves_auth_command() -> None:
    """A ``bedrock`` provider with only an ``auth_command`` mints the token.

    Regression: the credential gate previously read ``family.api_key`` alone, so
    an ``auth_command``-only config (a natural fit for rotating Bedrock bearer
    tokens) silently fell back to Claude's own login. The command's stdout must
    become ``AWS_BEARER_TOKEN_BEDROCK`` since Bedrock mode ignores apiKeyHelper.
    """
    from omnigent.onboarding.provider_config import load_providers

    entry = load_providers(
        {
            "providers": {
                "nexus": {
                    "kind": "bedrock",
                    "anthropic": {
                        "base_url": "https://gw.example/bedrock",
                        "auth_command": "printf minted-bedrock-token",
                        "models": {"default": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
                    },
                }
            }
        }
    )["nexus"]

    cfg = claude_native._bedrock_config_for_native_claude(entry)
    assert cfg is not None
    assert cfg.env["AWS_BEARER_TOKEN_BEDROCK"] == "minted-bedrock-token"
    assert cfg.api_key_helper is None


def test_bedrock_config_for_native_claude_keeps_betas_under_use_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bedrock-style gateway with CLAUDE_CODE_USE_GATEWAY=1 keeps betas on.

    Bedrock-compatible corporate gateways can run in gateway-aware mode; when
    CLAUDE_CODE_USE_GATEWAY=1 the beta-disable flag is skipped so MCP tool
    search stays enabled, matching the generic gateway provider path.
    """
    from omnigent.onboarding.provider_config import load_providers

    monkeypatch.setenv("CLAUDE_CODE_USE_GATEWAY", "1")

    entry = load_providers(
        {
            "providers": {
                "nexus": {
                    "kind": "bedrock",
                    "anthropic": {
                        "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
                        "api_key": "absk-test",
                        "models": {"default": "us.anthropic.claude-opus-4-5-20251101-v1:0"},
                    },
                }
            }
        }
    )["nexus"]

    cfg = claude_native._bedrock_config_for_native_claude(entry)
    assert cfg is not None
    assert cfg.env == {
        "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "AWS_BEARER_TOKEN_BEDROCK": "absk-test",
        "CLAUDE_CODE_USE_BEDROCK": "1",
    }
    assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in cfg.env


def test_bedrock_config_for_native_claude_non_anthropic_returns_none() -> None:
    """A ``bedrock`` provider not serving the anthropic surface → ``None``.

    The native Claude path only routes anthropic-surface providers; anything
    else falls back to Claude Code's own login.
    """
    from omnigent.onboarding.provider_config import load_providers

    entry = load_providers(
        {
            "providers": {
                "nexus": {
                    "kind": "bedrock",
                    "openai": {
                        "base_url": "https://gw.example/openai",
                        "api_key": "sk-o",
                    },
                }
            }
        }
    )["nexus"]

    assert claude_native._bedrock_config_for_native_claude(entry) is None


def test_resolve_native_claude_config_spec_provider_default(
    _isolated_provider_config: Path,
) -> None:
    """A spec with no auth + a configured anthropic key default → provider config.

    The whole point of the P0: a native-claude session honors `configure
    harness` exactly like the in-process claude-sdk harness. Failure means
    the native path ignored the configured provider.
    """
    _seed_config(
        _isolated_provider_config,
        {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-ant-default",
                    "models": {"default": "claude-sonnet-4-6"},
                },
            }
        },
    )

    cfg = claude_native.resolve_native_claude_config(spec=_no_auth_claude_spec())
    assert cfg is not None
    assert cfg.env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert cfg.api_key_helper == "printf %s sk-ant-default"


def test_resolve_native_claude_config_subscription_uses_cli_login(
    _isolated_provider_config: Path,
) -> None:
    """A claude subscription default → None (use the CLI's own enterprise login).

    A subscription means "use whatever ~/.claude is logged into" (e.g. a
    Claude Enterprise seat), NOT a gateway. The resolver must return None so
    the native launch leaves Claude's own login alone — and must NOT fall
    back to ucode.
    """
    _seed_config(
        _isolated_provider_config,
        {"claude-subscription": {"kind": "subscription", "cli": "claude", "default": True}},
    )

    cfg = claude_native.resolve_native_claude_config(spec=_no_auth_claude_spec())
    assert cfg is None


def test_resolve_native_claude_config_global_databricks_auth_uses_ucode(
    _isolated_provider_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec-less with a global ``auth: databricks`` block → ucode with its profile.

    Preserves the Databricks behavior after the ``--profile`` flag removal:
    a databricks user (no OSS provider configured) who set up a global
    ``auth:`` block via ``omnigent setup`` still routes a bare
    ``omnigent claude`` launch through ucode, keyed on the auth block's
    own profile. We assert the resolver delegates to
    `_ucode_config_for_profile` with that profile.
    """
    (_isolated_provider_config / "config.yaml").write_text(
        yaml.safe_dump({"auth": {"type": "databricks", "profile": "oss"}})
    )
    sentinel = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://db.example/gw"},
        api_key_helper="databricks auth token",
        model="databricks-claude",
    )
    seen: dict[str, str | bool | None] = {}

    def _fake_ucode(
        profile: str | None,
        *,
        refresh_models: bool = True,
    ) -> claude_native.ClaudeNativeUcodeConfig:
        seen["profile"] = profile
        seen["refresh_models"] = refresh_models
        return sentinel

    monkeypatch.setattr(claude_native, "_ucode_config_for_profile", _fake_ucode)

    cfg = claude_native.resolve_native_claude_config(spec=None)
    assert cfg is sentinel
    # The global auth block's profile was threaded to the ucode path.
    assert seen["profile"] == "oss"
    # Launch resolution keeps refreshing the model catalog by default.
    assert seen["refresh_models"] is True


def test_resolve_native_claude_config_databricks_provider_uses_ucode(
    _isolated_provider_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A databricks provider default delegates to ucode with its profile."""
    _seed_config(
        _isolated_provider_config,
        {"databricks": {"kind": "databricks", "default": True, "profile": "oss"}},
    )
    seen: dict[str, str | None] = {}
    monkeypatch.setattr(
        claude_native,
        "_ucode_config_for_profile",
        lambda profile, *, refresh_models=True: seen.setdefault("profile", profile),
    )

    claude_native.resolve_native_claude_config(spec=_no_auth_claude_spec())
    # The databricks provider's own profile drove the ucode lookup.
    assert seen["profile"] == "oss"


def test_resolve_native_claude_config_ambient_key(
    _isolated_provider_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec-less with only an ambient ANTHROPIC_API_KEY → provider config.

    First run without configure: a native `omnigent claude` launch still
    routes through the detected env key. Failure means a fresh machine's
    native Claude would ignore the ambient credential.
    """
    monkeypatch.delenv("CLAUDE_CODE_USE_GATEWAY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient")

    cfg = claude_native.resolve_native_claude_config(spec=None)
    assert cfg is not None
    assert cfg.env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    # Resolved from the env ref, delivered via the helper (no secret in env).
    assert cfg.api_key_helper == "printf %s sk-ant-ambient"


def test_resolve_native_claude_config_ambient_prefixed_key(
    _isolated_provider_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefixed Anthropic key routes native Claude without raw env exposure."""
    monkeypatch.delenv("CLAUDE_CODE_USE_GATEWAY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OMNIGENT_ANTHROPIC_API_KEY", "sk-ant-prefixed")

    cfg = claude_native.resolve_native_claude_config(spec=None)

    assert cfg is not None
    assert cfg.env == {
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }
    assert cfg.api_key_helper == "printf %s sk-ant-prefixed"


def test_bedrock_config_auth_command_failure_returns_none() -> None:
    """A failing bedrock auth_command falls back to Claude's own login (None)."""
    from omnigent.onboarding.provider_config import load_providers

    entry = load_providers(
        {
            "providers": {
                "b": {
                    "kind": "bedrock",
                    "anthropic": {
                        "base_url": "https://gw.example/bedrock",
                        "auth_command": "exit 7",
                        "models": {"default": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
                    },
                }
            }
        }
    )["b"]
    assert claude_native._bedrock_config_for_native_claude(entry) is None


def test_bedrock_config_no_model_default_leaves_model_none() -> None:
    """A bedrock provider without models.default builds with model=None (+warns).

    Claude Code then picks its own default model — usually not enabled on a
    Bedrock account — so the function warns; the config is still returned.
    """
    import logging

    from omnigent.onboarding.provider_config import load_providers

    entry = load_providers(
        {
            "providers": {
                "b": {"kind": "bedrock", "anthropic": {"base_url": "https://x", "api_key": "k"}}
            }
        }
    )["b"]
    logger = logging.getLogger(claude_native.__name__)
    with _capture_warnings(logger) as records:
        cfg = claude_native._bedrock_config_for_native_claude(entry)
    assert cfg is not None
    assert cfg.model is None
    assert any("models.default" in r.getMessage() for r in records)


class _capture_warnings:
    """Minimal context manager capturing WARNING records from *logger*."""

    def __init__(self, logger):
        self._logger = logger
        self._records = []
        self._handler = None

    def __enter__(self):
        import logging

        class _H(logging.Handler):
            def __init__(self, sink):
                super().__init__(level=logging.WARNING)
                self._sink = sink

            def emit(self, record):
                self._sink.append(record)

        self._handler = _H(self._records)
        self._logger.addHandler(self._handler)
        self._prev_level = self._logger.level
        self._logger.setLevel(logging.WARNING)
        return self._records

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)
        return False


def test_claude_transcript_records_handles_compaction_item() -> None:
    """Compaction items replace prior records with compacted_messages."""
    items: list[dict[str, Any]] = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
            "response_id": "resp_1",
        },
        {
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi there"}],
            "response_id": "resp_1",
        },
        {
            "id": "cmp_1",
            "type": "compaction",
            "summary": "compaction summary",
            "token_count": 4321,
            "compacted_messages": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "compacted reply"}],
                },
            ],
            "response_id": "compact_1",
        },
        {
            "id": "msg_3",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "after compaction"}],
            "response_id": "resp_2",
        },
    ]
    records = claude_native._claude_transcript_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
        cwd=Path("/tmp/test"),
        bridge_dir=Path("/tmp/test-bridge"),
    )
    types = [r.get("type") for r in records]
    # Should have compacted user + assistant + post-compaction user
    assert "user" in types
    assert "assistant" in types
    # Pre-compaction "hi there" should be gone, replaced by "compacted reply"
    all_text = " ".join(
        str(r.get("message", {}).get("content", ""))
        for r in records
        if r.get("type") == "assistant"
    )
    assert "compacted reply" in all_text
    assert "hi there" not in all_text
    # Post-compaction message should be present
    user_texts = [
        str(r.get("message", {}).get("content", "")) for r in records if r.get("type") == "user"
    ]
    assert any("after compaction" in t for t in user_texts)
    # The compact_boundary must carry a non-null compactMetadata: Claude
    # destructures it on every resume-time /compact and auto-compact, and a
    # missing object crashes compaction ("Cannot destructure property
    # 'cumulativeDroppedTokens' from null or undefined value").
    boundaries = [
        r for r in records if r.get("type") == "system" and r.get("subtype") == "compact_boundary"
    ]
    assert len(boundaries) == 1
    assert boundaries[0]["compactMetadata"]["postTokens"] == 4321


def test_websocket_connect_passes_ssl_context_for_wss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wss:// attach URL (remote https workspace) gets a verifying SSL context.

    Without it, claude-native attach fails with CERTIFICATE_VERIFY_FAILED on
    interpreters whose OpenSSL default trust store is empty (see issue #1730).
    """
    captured: dict[str, Any] = {}

    def _stub_connect(url: str, **kwargs: Any) -> str:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "cm"

    monkeypatch.setattr(websockets, "connect", _stub_connect)
    claude_native._websocket_connect("wss://example.databricksapps.com/attach", headers={})
    assert isinstance(captured["kwargs"]["ssl"], ssl.SSLContext)


def test_websocket_connect_no_ssl_context_for_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain ws:// attach URL (local runner) passes ssl=None — the library default."""
    captured: dict[str, Any] = {}

    def _stub_connect(url: str, **kwargs: Any) -> str:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "cm"

    monkeypatch.setattr(websockets, "connect", _stub_connect)
    claude_native._websocket_connect("ws://127.0.0.1:6767/attach", headers={})
    assert captured["kwargs"]["ssl"] is None


@pytest.mark.parametrize(
    ("output", "expected_parsed"),
    [
        # An angle-bracket display string (e.g. a TaskOutput result) is the
        # regression case: Claude's TaskOutput renderer JSON.parses
        # toolUseResult on resume and threw "Unrecognized token '<'" at boot.
        (
            "<retrieval_status>timeout</retrieval_status>",
            "<retrieval_status>timeout</retrieval_status>",
        ),
        # A <tool_use_error> blob is the same hazard from a different tool.
        (
            "<tool_use_error>No task found</tool_use_error>",
            "<tool_use_error>No task found</tool_use_error>",
        ),
        # Ordinary plain text must also round-trip to a string.
        ("plain text output", "plain text output"),
        # Already-JSON output passes through verbatim, not double-encoded.
        # (Image block arrays are JSON too; their redaction is covered below.)
        ('{"a":1}', {"a": 1}),
    ],
)
def test_claude_transcript_tool_use_result_is_json_parseable(
    output: str,
    expected_parsed: object,
) -> None:
    """
    Synthesized ``toolUseResult`` must survive Claude's resume-time parse.

    Claude Code ``JSON.parse``s ``toolUseResult`` for some built-in
    renderers (``TaskOutput``). A raw ``<...>`` display string crashed
    the TUI at boot before the input prompt rendered, so the resume
    failed. The synthesizer must always emit a JSON-parseable value,
    while leaving the ``tool_result`` content block as the verbatim
    string the model and web UI see.
    """
    items: list[dict[str, Any]] = [
        {
            "id": "fco_1",
            "response_id": "resp_1",
            "type": "function_call_output",
            "call_id": "toolu_1",
            "output": output,
        }
    ]
    records = claude_native._claude_transcript_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
        cwd=Path("/tmp/test"),
        bridge_dir=Path("/tmp/test-bridge"),
    )
    assert len(records) == 1
    record = records[0]
    # toolUseResult must parse without raising, and preserve the value.
    assert json.loads(record["toolUseResult"]) == expected_parsed
    # Plain-text results keep the raw string as the content block; a
    # content-block array (e.g. images) is rehydrated into real blocks so
    # ``claude --resume`` sends them as blocks, not text (see the dedicated
    # rehydration test below).
    content = record["message"]["content"][0]["content"]
    if isinstance(expected_parsed, list):
        assert content == expected_parsed
    else:
        assert content == output


def test_json_safe_tool_use_result_wraps_non_json() -> None:
    """Non-JSON strings become a JSON string literal; JSON passes through."""
    # A leading '<' is not valid JSON, so it is wrapped.
    wrapped = claude_native._json_safe_tool_use_result("<x>y</x>")
    assert json.loads(wrapped) == "<x>y</x>"
    # A bare number is valid JSON and must not be re-wrapped.
    assert claude_native._json_safe_tool_use_result("42") == "42"
    # A JSON object string passes through unchanged.
    assert claude_native._json_safe_tool_use_result('{"a":1}') == '{"a":1}'


# Header + zero-padding: signature-matching but structurally invalid per format.
# A structurally valid minimal SOF0 + SOS pair for building marker-only fakes.
_FAKE_SOF0 = b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
_FAKE_SOS = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"

# JPEG-signature strings too short to be any real image: below
# ``_MIN_IMAGE_BYTES``, so the payload gate still rejects them.
_UNDER_FLOOR_JPEGS: dict[str, str] = {
    name: base64.b64encode(payload).decode()
    for name, payload in {
        "soi-eoi": b"\xff\xd8\xff\xd9",
        "rst0-only": b"\xff\xd8\xff\xd0",
        "dht-only": b"\xff\xd8\xff\xc4\x00\x08\x01\x01\x01\x01\x01\x01\xff\xd9",
    }.items()
}

# JPEG-signature strings that clear the byte floor but carry no decodable
# frame or scan. The magic-byte gate accepts these by design; see
# ``test_signature_valid_but_undecodable_payloads_convert_by_design``.
_HEADER_VALID_CORRUPT_JPEGS: dict[str, str] = {
    name: base64.b64encode(payload).decode()
    for name, payload in {
        "app0-only": (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        ),
        "empty-sos": b"\xff\xd8" + _FAKE_SOF0 + _FAKE_SOS + b"\xff\xd9",
        "repeated-soi": b"\xff\xd8\xff\xd8" + _FAKE_SOF0 + _FAKE_SOS + b"\x01\x02\xff\xd9",
        "restart-only-entropy": (b"\xff\xd8" + _FAKE_SOF0 + _FAKE_SOS + b"\xff\xd0" + b"\xff\xd9"),
        "fill-only-entropy": b"\xff\xd8" + _FAKE_SOF0 + _FAKE_SOS + b"\xff\xff" + b"\xff\xd9",
    }.items()
}

_FAKE_IMAGE_PAYLOADS: dict[str, str] = {
    "image/png": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 512).decode(),
    "image/jpeg": base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 512).decode(),
    "image/gif": base64.b64encode(b"GIF89a" + b"\x00" * 512).decode(),
    "image/webp": base64.b64encode(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 512).decode(),
}


def _image_output_records(output: str) -> list[dict[str, Any]]:
    """Run one ``function_call_output`` item through the shared converter.

    Both fork carry-history rebuilds and cold resumes funnel through
    ``_claude_transcript_records_from_session_items``, so record-level
    coverage here protects both launch paths at once.
    """
    items: list[dict[str, Any]] = [
        {
            "id": "fco_1",
            "response_id": "resp_1",
            "type": "function_call_output",
            "call_id": "toolu_1",
            "output": output,
        }
    ]
    return claude_native._claude_transcript_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
        cwd=Path("/tmp/test"),
        bridge_dir=Path("/tmp/test-bridge"),
    )


def test_tool_use_result_redacts_inline_image_base64() -> None:
    """
    An intact replayed image exists exactly once in the rebuilt record.

    The structured ``tool_result`` content block keeps the real base64 —
    that is the image the model re-sees on ``--resume``. The
    ``toolUseResult`` metadata copy is replaced with a short marker, so a
    single screenshot no longer doubles its ~250K-token payload in the
    resumed transcript. Non-binary fields (media type, sibling text,
    renderer metadata) survive the redaction.
    """
    b64 = "iVBORw0KGgo" + "A" * 5000 + "="
    output = json.dumps(
        [
            {"type": "text", "text": "screenshot taken"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
                "width": 1280,
            },
        ],
        separators=(",", ":"),
    )
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    # The model-visible image survives intact in the content block.
    content = record["message"]["content"][0]["content"]
    assert content[0] == {"type": "text", "text": "screenshot taken"}
    assert content[1]["source"]["data"] == b64
    # The metadata copy is redacted but keeps its shape and non-binary fields.
    tool_use_result = json.loads(record["toolUseResult"])
    assert tool_use_result[0] == {"type": "text", "text": "screenshot taken"}
    redacted_source = tool_use_result[1]["source"]
    assert b64 not in redacted_source["data"]
    assert "image/png" in redacted_source["data"]
    assert redacted_source["media_type"] == "image/png"
    assert tool_use_result[1]["width"] == 1280
    # Whole-record invariant: the payload exists exactly once.
    assert json.dumps(record).count(b64) == 1


def test_tool_use_result_redaction_is_tool_name_independent() -> None:
    """
    Redaction keys on the payload shape, not the tool that produced it.

    Any tool or MCP server returning inline image data (e.g. a browser
    screenshot tool returning an object with a nested image block) gets
    the same treatment as a built-in image result: the payload leaves
    ``toolUseResult`` and is carried once by the ``tool_result`` content.
    Object-shaped output is not a text/image block array, so the content
    stays the raw string — the one surviving copy of the payload.
    """
    b64 = "U05BUFNIT1Q" + "B" * 4000
    output = json.dumps(
        {
            "tool": "mcp__browser__screenshot",
            "status": "ok",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                }
            ],
        },
        separators=(",", ":"),
    )
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    assert record["message"]["content"][0]["content"] == output
    tool_use_result = json.loads(record["toolUseResult"])
    assert tool_use_result["tool"] == "mcp__browser__screenshot"
    assert tool_use_result["status"] == "ok"
    redacted_block = tool_use_result["content"][0]
    assert b64 not in json.dumps(redacted_block)
    assert "image/jpeg" in redacted_block["source"]["data"]
    assert json.dumps(record).count(b64) == 1


def test_tool_use_result_redacts_inline_data_uris() -> None:
    """A ``data:`` URI is an inline base64 copy too; it is redacted as well."""
    b64 = "R0lGODdh" + "C" * 3000
    output = json.dumps(
        {"preview": f"data:image/gif;base64,{b64}", "ok": True},
        separators=(",", ":"),
    )
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    assert record["message"]["content"][0]["content"] == output
    tool_use_result = json.loads(record["toolUseResult"])
    assert b64 not in tool_use_result["preview"]
    assert "image/gif" in tool_use_result["preview"]
    assert tool_use_result["ok"] is True
    assert json.dumps(record).count(b64) == 1


@pytest.mark.parametrize("is_error", [False, True], ids=["ok", "error"])
def test_mcp_single_image_result_replays_as_one_structured_image(is_error: bool) -> None:
    """
    A lone MCP ``ImageContent`` replays as a real image block, once.

    Real MCP screenshot results persist as a JSON *object* string
    (``{"type":"image","data":...,"mimeType":...}``), which the old
    rehydrator could not recognize: the base64 stayed ~250K tokens of
    model-visible text AND sat in ``toolUseResult``. The rebuild must
    normalize it to one structured image block with redacted metadata.

    The failed spelling is the same payload behind an ``"Error: "``
    prefix, which is not valid JSON — so it regressed to the exact
    two-copy, model-visible-text shape after the object form was fixed.
    It must normalize identically, with the error preserved as a compact
    text block ahead of the image rather than silently dropped.
    """
    from mcp.types import ImageContent

    b64 = _TINY_PNG_BASE64
    output = _mcp_call_output(
        ImageContent(type="image", data=b64, mimeType="image/png"), is_error=is_error
    )
    assert output.startswith("Error: ") is is_error
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    content = record["message"]["content"][0]["content"]
    assert isinstance(content, list), "MCP image must not stay string-valued model content"
    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64},
    }
    if is_error:
        assert content == [{"type": "text", "text": "Error:"}, image_block]
    else:
        assert content == [image_block]
    tool_use_result = json.loads(record["toolUseResult"])
    assert b64 not in json.dumps(tool_use_result)
    assert tool_use_result[-1]["source"]["media_type"] == "image/png"
    assert json.dumps(record).count(b64) == 1


def test_mcp_errored_image_clone_record_is_repaired_like_the_ok_form() -> None:
    """
    A legacy cloned record holding the errored spelling is repaired too.

    A fork clone byte-copies the source transcript, so a record written
    before this fix carries the ``"Error: "``-prefixed string as
    model-visible content with the payload mirrored in metadata. The clone
    sanitizer runs through the same normalization seam, so it must
    recover the structured image and drop the duplicate — otherwise the
    stale record replays the overflow on the clone's first ``--resume``.
    """
    from mcp.types import ImageContent

    b64 = _TINY_PNG_BASE64
    output = _mcp_call_output(
        ImageContent(type="image", data=b64, mimeType="image/png"), is_error=True
    )
    record: dict[str, Any] = {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": output}],
        },
        "toolUseResult": json.dumps(output),
    }
    claude_native._sanitize_cloned_tool_result_record(record)
    content = record["message"]["content"][0]["content"]
    assert content == [
        {"type": "text", "text": "Error:"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        },
    ]
    assert b64 not in json.dumps(record["toolUseResult"])
    assert json.dumps(record).count(b64) == 1


def test_mcp_errored_non_image_result_representation_is_unchanged() -> None:
    """
    The prefix is only unwrapped when an image payload is at stake.

    Stripping it wherever it appears would silently restructure every
    errored tool result. With no payload to protect there is nothing to
    gain, so an errored text or non-image JSON result keeps replaying the
    raw string exactly as it did before.
    """
    from mcp.types import TextContent

    for output in (
        _mcp_call_output(TextContent(type="text", text="tool exploded"), is_error=True),
        _mcp_call_output(TextContent(type="text", text='{"foo":1}'), is_error=True),
    ):
        assert output.startswith("Error: ")
        rehydrated = trc.tool_result_content_blocks(output)
        assert rehydrated.blocks is None
        records = _image_output_records(output)
        assert records[0]["message"]["content"][0]["content"] == output


@pytest.mark.parametrize("is_error", [False, True], ids=["ok", "error"])
def test_mcp_mixed_text_and_image_result_replays_as_block_list(is_error: bool) -> None:
    """
    Text-plus-screenshot MCP output replays as a structured block list.

    ``_format_call_result`` newline-joins multi-block results, so the
    persisted string is NOT one JSON document — the worst pre-fix case:
    unparseable, so neither rehydration nor metadata redaction applied
    and the base64 survived in both places. The rebuild must recover the
    original block stream, keep the text verbatim, and hold the payload
    exactly once.

    The errored spelling only prefixes the first line, which is text
    either way, so this shape never regressed — pinned here so the lone
    image's prefix handling cannot change it.
    """
    from mcp.types import ImageContent, TextContent

    b64 = _TINY_PNG_BASE64
    output = _mcp_call_output(
        TextContent(type="text", text="took a screenshot"),
        ImageContent(type="image", data=b64, mimeType="image/png"),
        is_error=is_error,
    )
    # The persisted form is genuinely not one JSON document.
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)
    expected_text = "Error: took a screenshot" if is_error else "took a screenshot"
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    content = record["message"]["content"][0]["content"]
    assert content == [
        {"type": "text", "text": expected_text},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        },
    ]
    tool_use_result = json.loads(record["toolUseResult"])
    assert tool_use_result[0] == {"type": "text", "text": expected_text}
    assert b64 not in json.dumps(tool_use_result)
    assert json.dumps(record).count(b64) == 1


def test_mcp_image_replay_does_not_depend_on_tool_name() -> None:
    """
    An image from an arbitrarily named MCP tool gets the same replay.

    The converter only ever sees the output string — nothing keys on the
    producing tool — so this pins the invariant end-to-end with a
    realistic MCP-namespaced call preceding its result.
    """
    from mcp.types import ImageContent

    b64 = _TINY_JPEG_BASE64
    output = _mcp_call_output(ImageContent(type="image", data=b64, mimeType="image/jpeg"))
    items: list[dict[str, Any]] = [
        {
            "id": "fc_1",
            "response_id": "resp_1",
            "type": "function_call",
            "name": "mcp__playwright__browser_take_screenshot",
            "call_id": "toolu_1",
            "arguments": "{}",
        },
        {
            "id": "fco_1",
            "response_id": "resp_1",
            "type": "function_call_output",
            "call_id": "toolu_1",
            "output": output,
        },
    ]
    records = claude_native._claude_transcript_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
        cwd=Path("/tmp/test"),
        bridge_dir=Path("/tmp/test-bridge"),
    )
    assert len(records) == 2
    content = records[1]["message"]["content"][0]["content"]
    assert content[0]["source"]["data"] == b64
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert json.dumps(records[1]).count(b64) == 1


def test_mcp_multiple_images_replay_as_separate_blocks() -> None:
    """Two newline-joined MCP images become two blocks, each payload once."""
    from mcp.types import ImageContent

    b64_one = _TINY_PNG_BASE64
    b64_two = _TINY_GIF_BASE64
    output = _mcp_call_output(
        ImageContent(type="image", data=b64_one, mimeType="image/png"),
        ImageContent(type="image", data=b64_two, mimeType="image/gif"),
    )
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    content = record["message"]["content"][0]["content"]
    assert [block["source"]["data"] for block in content] == [b64_one, b64_two]
    blob = json.dumps(record)
    assert blob.count(b64_one) == 1
    assert blob.count(b64_two) == 1


def test_multiline_non_image_result_stays_raw_string() -> None:
    """
    Multi-line plain-text results keep their raw-string representation.

    Lines that parse as JSON but are not image blocks (and image-shaped
    lines without an ``image/*`` MIME type) must not be "recovered" into
    blocks — the newline-join normalization only fires on real images.
    """
    output = 'first line\n{"type": "image", "data": "QUJD", "mimeType": "text/plain"}\nlast line'
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    assert record["message"]["content"][0]["content"] == output
    # Image-free: the legacy metadata passthrough applies verbatim.
    assert record["toolUseResult"] == json.dumps(output)


def test_invalid_base64_image_shaped_text_stays_raw() -> None:
    """
    Image-shaped text with undecodable data is never converted.

    Documentation or logs can contain a line like
    ``{"type":"image","data":"not!valid!base64","mimeType":"image/png"}``.
    Converting it would emit an image block Claude rejects on every
    resume of the session — a persistent wedge rebuilt from the same
    stored item each launch. Both the lone-object and the newline-joined
    form must stay raw text.
    """
    fake = '{"type": "image", "data": "not!valid!base64", "mimeType": "image/png"}'
    lone_records = _image_output_records(fake)
    assert lone_records[0]["message"]["content"][0]["content"] == fake
    mixed_output = f"some log line\n{fake}"
    mixed_records = _image_output_records(mixed_output)
    assert mixed_records[0]["message"]["content"][0]["content"] == mixed_output


def test_valid_base64_of_non_image_bytes_stays_raw() -> None:
    """
    Decodable but non-image bytes must not become an image block.

    Uses the array-entry path: a base64 string that decodes cleanly but
    carries no image signature fails validation, so the whole output
    keeps its raw-string representation.
    """
    not_an_image = base64.b64encode(b"definitely just text bytes" * 100).decode()
    output = json.dumps(
        [{"type": "image", "data": not_an_image, "mimeType": "image/png"}],
        separators=(",", ":"),
    )
    records = _image_output_records(output)
    assert records[0]["message"]["content"][0]["content"] == output


def test_image_mime_signature_mismatch_stays_raw() -> None:
    """
    A payload whose signature disagrees with ``mimeType`` stays raw.

    Claude validates the bytes against the block's declared media type,
    so PNG-as-JPEG would fail the resume just like invalid data. An
    ``image/*`` type outside the supported set (SVG here) is rejected
    too — the prefix alone proves nothing.
    """
    mismatched = json.dumps(
        {"type": "image", "data": _TINY_PNG_BASE64, "mimeType": "image/jpeg"},
        separators=(",", ":"),
    )
    records = _image_output_records(mismatched)
    assert records[0]["message"]["content"][0]["content"] == mismatched

    svg = base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'/>").decode()
    unsupported = json.dumps(
        {"type": "image", "data": svg, "mimeType": "image/svg+xml"},
        separators=(",", ":"),
    )
    records = _image_output_records(unsupported)
    assert records[0]["message"]["content"][0]["content"] == unsupported


@pytest.mark.parametrize(
    ("mime_type", "payload"),
    [
        ("image/png", _TINY_PNG_BASE64),
        ("image/jpeg", _TINY_JPEG_BASE64),
        ("image/gif", _TINY_GIF_BASE64),
        ("image/webp", _TINY_WEBP_BASE64),
    ],
)
def test_mcp_image_result_replays_as_one_structured_image_all_formats(
    mime_type: str,
    payload: str,
) -> None:
    """
    Every supported format normalizes through the real MCP path.

    A genuine 1x1 image of each format Claude accepts is serialized by
    the real ``_format_call_result`` and must replay as exactly one
    structured image block with redacted metadata.
    """
    from mcp.types import ImageContent

    output = _mcp_call_output(ImageContent(type="image", data=payload, mimeType=mime_type))
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    content = record["message"]["content"][0]["content"]
    assert content == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": mime_type, "data": payload},
        }
    ]
    assert payload not in json.dumps(json.loads(record["toolUseResult"]))
    assert json.dumps(record).count(payload) == 1


@pytest.mark.parametrize(
    "payload",
    [
        _TINY_PROGRESSIVE_JPEG_BASE64,
        _TINY_PROGRESSIVE_GRAY_JPEG_BASE64,
        _TINY_CMYK_JPEG_BASE64,
    ],
    ids=["progressive-rgb", "progressive-grayscale", "cmyk"],
)
def test_mcp_progressive_jpeg_result_replays_as_one_structured_image(payload: str) -> None:
    """
    Progressive and CMYK JPEGs normalize through the real MCP path.

    Multi-scan (progressive) and CMYK JPEGs carry interleaved table
    segments and several SOS scans after the first; the structural
    validator must accept them (they are what real screenshot pipelines
    emit) so replay produces exactly one structured image block.
    """
    from mcp.types import ImageContent

    output = _mcp_call_output(ImageContent(type="image", data=payload, mimeType="image/jpeg"))
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    content = record["message"]["content"][0]["content"]
    assert content == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": payload},
        }
    ]
    assert payload not in json.dumps(json.loads(record["toolUseResult"]))
    assert json.dumps(record).count(payload) == 1


@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg", "image/gif", "image/webp"])
@pytest.mark.parametrize("form", ["lone", "mixed", "array"])
def test_signature_matching_padding_converts_in_every_form(mime_type: str, form: str) -> None:
    """
    Magic bytes plus padding convert in every persisted form, by design.

    The gate matches the declared type's signature and does not decode the
    container, so these convert rather than staying raw. What still holds in
    all three forms — lone object, newline-joined, array entry — is the
    invariant this workstream exists for: the payload lands in the structured
    block exactly once and never in the metadata.
    """
    payload = _FAKE_IMAGE_PAYLOADS[mime_type]
    image_object = json.dumps(
        {"type": "image", "data": payload, "mimeType": mime_type},
        separators=(",", ":"),
    )
    if form == "lone":
        output = image_object
    elif form == "mixed":
        output = f"log line\n{image_object}"
    else:
        output = json.dumps(
            [{"type": "image", "data": payload, "mimeType": mime_type}],
            separators=(",", ":"),
        )
    records = _image_output_records(output)
    assert len(records) == 1
    record = records[0]
    content = record["message"]["content"][0]["content"]
    assert isinstance(content, list)
    assert content[-1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": mime_type, "data": payload},
    }
    assert payload not in json.dumps(json.loads(record["toolUseResult"]))
    assert json.dumps(record).count(payload) == 1


@pytest.mark.parametrize("case", sorted(_UNDER_FLOOR_JPEGS))
@pytest.mark.parametrize("form", ["lone", "mixed", "array"])
def test_under_floor_image_payloads_stay_raw(case: str, form: str) -> None:
    """
    Payloads below the byte floor stay raw text in every persisted form.

    SOI+EOI, RST-only, and DHT-only are JPEG-signature strings far too short
    to be any real image, so the floor rejects them; converting them would
    emit image blocks Claude refuses on every resume. Each stays small, so it
    also stays raw rather than collapsing to the oversized placeholder.
    """
    payload = _UNDER_FLOOR_JPEGS[case]
    assert len(base64.b64decode(payload)) < trc._MIN_IMAGE_BYTES
    image_object = json.dumps(
        {"type": "image", "data": payload, "mimeType": "image/jpeg"},
        separators=(",", ":"),
    )
    if form == "lone":
        output = image_object
    elif form == "mixed":
        output = f"log line\n{image_object}"
    else:
        output = json.dumps(
            [{"type": "image", "data": payload, "mimeType": "image/jpeg"}],
            separators=(",", ":"),
        )
    records = _image_output_records(output)
    assert len(records) == 1
    assert records[0]["message"]["content"][0]["content"] == output


@pytest.mark.parametrize("case", sorted(_HEADER_VALID_CORRUPT_JPEGS))
def test_signature_valid_but_undecodable_payloads_convert_by_design(case: str) -> None:
    """
    A signature-valid but undecodable payload is converted, deliberately.

    The gate checks strict base64, a byte floor, and the declared type's magic
    bytes — it does not walk containers, so APP0-only, empty-SOS, repeated-SOI
    and restart/fill-only-entropy strings all become image blocks. Consequence
    if a producer ever emits one: Claude rejects that resume until the record
    ages out. Accepted because a store-truncated payload never parses as JSON
    and so never reaches here, and because source-shaped Claude image blocks
    already pass with no validation at all.
    """
    payload = _HEADER_VALID_CORRUPT_JPEGS[case]
    assert trc._is_supported_image_payload(payload, "image/jpeg")
    output = json.dumps(
        {"type": "image", "data": payload, "mimeType": "image/jpeg"},
        separators=(",", ":"),
    )
    records = _image_output_records(output)
    assert records[0]["message"]["content"][0]["content"] == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": payload},
        }
    ]
    # Still exactly one copy: the metadata never gets a second one.
    assert json.dumps(records[0]).count(payload) == 1


def test_oversized_invalid_image_collapses_instead_of_replaying_base64() -> None:
    """
    Rejecting a big invalid image must not cost more than accepting it.

    An image-shaped payload the gate rejects cannot become an image block, and
    the fallback keeps the raw string as ``tool_result`` content — which for a
    large payload replays the whole base64 as prompt text, the shape that
    overflowed the context window. Past ``_MAX_INVALID_IMAGE_REPLAY_CHARS`` it
    collapses to the omitted-image placeholder instead; small invalid snippets
    still replay verbatim so their text survives.
    """
    oversized = base64.b64encode(b"not an image payload " * 2_000).decode()
    assert len(oversized) > trc._MAX_INVALID_IMAGE_REPLAY_CHARS
    assert not trc._is_supported_image_payload(oversized, "image/jpeg")
    image_object = json.dumps(
        {"type": "image", "data": oversized, "mimeType": "image/jpeg"},
        separators=(",", ":"),
    )
    forms = {
        "lone": image_object,
        "mixed": f"screenshot follows\n{image_object}",
        "array": json.dumps(
            [{"type": "image", "data": oversized, "mimeType": "image/jpeg"}],
            separators=(",", ":"),
        ),
    }
    for form, output in forms.items():
        records = _image_output_records(output)
        assert len(records) == 1, form
        rendered = json.dumps(records[0])
        # The payload is gone from the whole record — content and metadata.
        assert oversized[:64] not in rendered, form
        assert "omitted from history" in rendered, form
        # And the record cannot recreate the overflow: it is a tiny
        # fraction of the payload it replaced.
        assert len(rendered) < len(oversized) // 10, form
    # The mixed form keeps its text alongside the placeholder.
    mixed_records = _image_output_records(forms["mixed"])
    content = mixed_records[0]["message"]["content"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "screenshot follows"}
    assert "omitted from history" in content[1]["text"]
    # Below the threshold nothing changes: the raw string still replays.
    small = base64.b64encode(b"not an image").decode()
    assert len(small) <= trc._MAX_INVALID_IMAGE_REPLAY_CHARS
    assert not trc._is_supported_image_payload(small, "image/jpeg")
    small_output = json.dumps(
        [{"type": "image", "data": small, "mimeType": "image/jpeg"}],
        separators=(",", ":"),
    )
    small_records = _image_output_records(small_output)
    assert small_records[0]["message"]["content"][0]["content"] == small_output


def test_large_text_block_array_keeps_byte_for_byte_tool_use_result() -> None:
    """
    An image-free result keeps its passthrough however large it is.

    The metadata fallback exists only to stop a *dropped* image payload
    from sneaking back in via the raw output, so it has to key on that
    explicit signal rather than on the passthrough's size. A big text
    block array drops nothing: it rehydrates into real blocks, carries no
    image payload, and must keep its documented byte-for-byte
    ``toolUseResult``.
    """
    long_text = "log line that goes on and on. " * 400
    output = json.dumps([{"type": "text", "text": long_text}], separators=(",", ":"))
    assert len(output) > trc._MAX_INVALID_IMAGE_REPLAY_CHARS
    rehydrated = trc.tool_result_content_blocks(output)
    assert rehydrated.blocks is not None
    assert rehydrated.dropped_oversized_image is False
    records = _image_output_records(output)
    assert len(records) == 1
    # Byte-for-byte passthrough, not the redacted block list.
    assert records[0]["toolUseResult"] == output
    assert records[0]["message"]["content"][0]["content"] == [{"type": "text", "text": long_text}]
    # And the dropped-payload case still swaps in the block list.
    oversized = base64.b64encode(b"not an image payload " * 2_000).decode()
    dropped = trc.tool_result_content_blocks(
        json.dumps({"type": "image", "data": oversized, "mimeType": "image/png"})
    )
    assert dropped.dropped_oversized_image is True


def test_claude_transcript_image_result_sent_as_blocks_not_text() -> None:
    """
    Image tool results resume as real content blocks, not base64 text.

    A screenshot tool result is persisted as a stringified content-block
    array. The old code dropped that string straight into the
    ``tool_result`` content, so ``claude --resume`` re-sent ~250K tokens of
    base64 as plain text and blew the context limit. The synthesizer must
    rehydrate it into image blocks so the API tokenizes it as an image.
    """
    # ~4K chars of base64 stands in for a real screenshot payload.
    big_b64 = "A" * 4096
    output = json.dumps(
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": big_b64},
            }
        ]
    )
    items: list[dict[str, Any]] = [
        {
            "id": "fco_1",
            "response_id": "resp_1",
            "type": "function_call_output",
            "call_id": "toolu_1",
            "output": output,
        }
    ]
    records = claude_native._claude_transcript_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
        cwd=Path("/tmp/test"),
        bridge_dir=Path("/tmp/test-bridge"),
    )
    assert len(records) == 1
    block = records[0]["message"]["content"][0]
    assert block["type"] == "tool_result"
    # content is a real content-block list — an image block, not a string.
    assert isinstance(block["content"], list)
    assert block["content"][0]["type"] == "image"
    assert block["content"][0]["source"]["data"] == big_b64


def test_claude_transcript_truncated_image_result_stripped_not_leaked() -> None:
    """
    A base64 image clipped at the store byte cap must not leak on resume.

    Real wedged sessions stored image tool results truncated at the
    conversation-store byte cap, leaving the base64 unterminated (invalid
    JSON). Rehydration fails on that, so the old path fell back to sending the
    raw ~250K-char base64 as ``tool_result`` text AND stashed it in
    ``toolUseResult`` — re-overflowing the resumed context. The synthesizer
    must collapse such a payload to a placeholder in both places.
    """
    big_b64 = "iVBORw0KGgo" + "A" * 100_000
    truncated = (
        '[{"type":"image","source":{"type":"base64","data":"'
        + big_b64
        + "…[truncated by conversation-store: item exceeded 245760B cap]"
    )
    items: list[dict[str, Any]] = [
        {
            "id": "fco_1",
            "response_id": "resp_1",
            "type": "function_call_output",
            "call_id": "toolu_1",
            "output": truncated,
        }
    ]
    records = claude_native._claude_transcript_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
        cwd=Path("/tmp/test"),
        bridge_dir=Path("/tmp/test-bridge"),
    )
    assert len(records) == 1
    # No base64 anywhere in the record — not in tool_result content, not in
    # toolUseResult metadata.
    blob = json.dumps(records[0])
    assert big_b64 not in blob, "truncated base64 must not survive into the transcript"
    assert "omitted from history" in blob


def _store_truncated(clipped_prefix: str) -> str:
    """Append the store's truncation marker, leaving unterminated JSON."""
    return clipped_prefix + "…[truncated by conversation-store: item exceeded 245760B cap]"


def _capped_mcp_image_output(
    *, is_error: bool = False, text: str | None = None
) -> tuple[str, str]:
    """Build a real MCP image result clipped by the real store cap.

    Goes through ``_format_call_result(ImageContent(...))`` and
    ``cap_tool_output`` so the fixture is the exact persisted shape, which
    carries no ``"base64"`` literal.
    """
    from mcp.types import CallToolResult, ImageContent, TextContent

    from omnigent.runtime.tool_output import cap_tool_output
    from omnigent.tools.mcp import _format_call_result

    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\xa5" * 900_000).decode()
    blocks: list[Any] = [ImageContent(type="image", data=payload, mimeType="image/png")]
    if text is not None:
        blocks.insert(0, TextContent(type="text", text=text))
    raw = _format_call_result(CallToolResult(content=blocks, isError=is_error))
    return payload, cap_tool_output(raw)


@pytest.mark.parametrize("spell", ["line-wrapped", "unpadded"])
def test_wrapped_base64_image_replays_as_one_structured_copy(spell: str) -> None:
    """A wrapped or unpadded payload replays as a real image, not a placeholder.

    Strict decoding used to reject both, so a valid screenshot was replaced with
    an omission placeholder and the image was lost for good.
    """
    canonical = base64.b64encode(base64.b64decode(_TINY_PNG_BASE64)).decode()
    payload = {
        "line-wrapped": "\n".join(canonical[i : i + 76] for i in range(0, len(canonical), 76)),
        "unpadded": canonical.rstrip("="),
    }[spell]
    output = json.dumps(
        {"type": "image", "data": payload, "mimeType": "image/png"}, separators=(",", ":")
    )

    rehydrated = trc.tool_result_content_blocks(output)
    assert rehydrated.dropped_oversized_image is False
    records = _image_output_records(output)
    record = records[0]
    content = record["message"]["content"][0]["content"]
    assert content == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": canonical},
        }
    ]
    blob = json.dumps(record)
    # Exactly one copy, and none of it in the metadata.
    assert blob.count(canonical) == 1
    assert canonical not in json.dumps(json.loads(record["toolUseResult"]))
    assert "omitted from history" not in blob


def test_clone_repair_keeps_a_wrapped_payload_as_one_image() -> None:
    """The clone path normalizes a wrapped payload instead of dropping it."""
    canonical = base64.b64encode(base64.b64decode(_TINY_PNG_BASE64)).decode()
    wrapped = "\n".join(canonical[i : i + 76] for i in range(0, len(canonical), 76))
    content = json.dumps(
        {"type": "image", "data": wrapped, "mimeType": "image/png"}, separators=(",", ":")
    )
    record: dict[str, Any] = {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}],
        },
        "toolUseResult": json.dumps(content),
    }

    claude_native._sanitize_cloned_tool_result_record(record)

    repaired = record["message"]["content"][0]["content"]
    assert repaired == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": canonical},
        }
    ]
    assert json.dumps(record).count(canonical) == 1
    assert canonical not in json.dumps(record["toolUseResult"])


@pytest.mark.parametrize("is_error", [False, True], ids=["ok", "error"])
@pytest.mark.parametrize("text", [None, "took a screenshot"], ids=["lone", "mixed"])
def test_store_capped_mcp_image_result_does_not_leak_base64(
    is_error: bool, text: str | None
) -> None:
    """A store-capped MCP ``ImageContent`` result collapses instead of replaying.

    The persisted MCP shape is ``{"type":"image","data":...,"mimeType":...}`` —
    no ``"base64"`` literal — so the old token-based guard never fired on it and
    the clipped payload replayed as ``tool_result`` text and again in metadata.
    """
    payload, capped = _capped_mcp_image_output(is_error=is_error, text=text)
    assert '"base64"' not in capped
    with pytest.raises(json.JSONDecodeError):
        json.loads(capped.removeprefix("Error: "))

    records = _image_output_records(capped)
    assert len(records) == 1
    blob = json.dumps(records[0])
    assert blob.count(payload[:64]) == 0, "clipped payload must not survive"
    # The record is bounded: a placeholder, not a copy of the capped output.
    assert len(blob) < len(capped) // 100
    content = records[0]["message"]["content"][0]["content"]
    assert isinstance(content, list)
    rendered = json.dumps(content)
    assert "omitted from history" in rendered
    if is_error:
        assert "Error:" in rendered
    if text is not None:
        assert text in rendered


def test_store_capped_multi_image_result_keeps_the_intact_image() -> None:
    """A clipped trailing image is collapsed without discarding earlier ones.

    The newline-joined form is several JSON documents, so a whole-body parse
    failure says nothing about the intact lines; only the clipped line is stood
    down to a placeholder.
    """
    from mcp.types import CallToolResult, ImageContent

    from omnigent.runtime.tool_output import cap_tool_output
    from omnigent.tools.mcp import _format_call_result

    clipped = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\xa5" * 900_000).decode()
    capped = cap_tool_output(
        _format_call_result(
            CallToolResult(
                content=[
                    ImageContent(type="image", data=_TINY_PNG_BASE64, mimeType="image/png"),
                    ImageContent(type="image", data=clipped, mimeType="image/png"),
                ],
                isError=False,
            )
        )
    )

    records = _image_output_records(capped)
    blob = json.dumps(records[0])
    assert blob.count(_TINY_PNG_BASE64) == 1, "the intact image must survive"
    assert blob.count(clipped[:64]) == 0, "the clipped payload must not"
    assert len(blob) < len(capped) // 100
    content = records[0]["message"]["content"][0]["content"]
    assert content[0]["source"]["data"] == _TINY_PNG_BASE64
    assert "omitted from history" in json.dumps(content[1:])


def test_errored_truncated_image_result_does_not_leak_base64() -> None:
    """
    An errored *and* truncated image payload leaks in neither place.

    Two independent guards each assumed the payload starts the string.
    ``_strip_unparseable_image_output`` checks for a leading ``[``/``{``,
    and rehydration needs parseable JSON — a failed MCP call puts
    ``"Error: "`` in front of the first, and store truncation breaks the
    second. Together they slipped past both, so the record fell back to
    the raw string and replayed the partial base64 twice: as
    model-visible ``tool_result`` text and again in ``toolUseResult``.
    The error must survive as compact text, the payload in neither place.
    """
    from mcp.types import ImageContent

    b64 = "iVBORw0KGgo" + "A" * 100_000
    # Real prefix from the real formatter, then the store's real clipping.
    errored = _mcp_call_output(
        ImageContent(type="image", data=b64, mimeType="image/png"), is_error=True
    )
    assert errored.startswith(trc._MCP_ERROR_PREFIX)
    truncated = _store_truncated(
        trc._MCP_ERROR_PREFIX + '[{"type":"image","source":{"type":"base64","data":"' + b64
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)
    records = _image_output_records(truncated)
    assert len(records) == 1
    record = records[0]
    blob = json.dumps(record)
    assert b64 not in blob, "truncated base64 must not survive, prefixed or not"
    # The error is preserved as structured compact text, not discarded.
    content = record["message"]["content"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Error:"}
    assert "omitted from history" in content[1]["text"]
    # Metadata carries the same collapsed form — no payload, still parseable.
    tool_use_result = json.loads(record["toolUseResult"])
    assert b64 not in json.dumps(tool_use_result)
    assert "omitted from history" in json.dumps(tool_use_result)


def test_errored_truncated_image_clone_record_is_collapsed_too() -> None:
    """
    A cloned record holding the errored+truncated shape is collapsed too.

    The clone sanitizer normally only touches records whose payload it can
    recover as an image block, and a truncated payload is exactly the one
    it cannot — so without a second route the byte-copied record replays
    the partial base64 on the clone's first ``--resume``.
    """
    b64 = "iVBORw0KGgo" + "A" * 100_000
    truncated = _store_truncated(
        trc._MCP_ERROR_PREFIX + '[{"type":"image","source":{"type":"base64","data":"' + b64
    )
    record: dict[str, Any] = {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": truncated}],
        },
        "toolUseResult": json.dumps(truncated),
    }
    claude_native._sanitize_cloned_tool_result_record(record)
    content = record["message"]["content"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Error:"}
    assert "omitted from history" in content[1]["text"]
    assert b64 not in json.dumps(record)


def test_tool_use_result_regression_old_flatten_would_crash_resume() -> None:
    """
    Pin the resume crash to the old ``toolUseResult = output`` flatten.

    Claude Code ``JSON.parse``s ``toolUseResult`` for its ``TaskOutput``
    renderer at resume time. A real ``isaac review`` result whose text
    starts with ``<retrieval_status>...`` is not valid JSON, so the old
    verbatim flatten crashed the TUI at boot with "Unrecognized token
    '<'" before the input prompt rendered — the failure the user hit.

    This models both sides of that parse: the pre-fix value would raise,
    the value the synthesizer emits today does not. It fails if anyone
    reverts to assigning ``output`` verbatim.
    """
    output = (
        "<retrieval_status>timeout</retrieval_status>\n\n"
        "<task_id>b51au379y</task_id>\n\n<status>running</status>"
    )

    # The synthesizer must emit a JSON-parseable toolUseResult. The old
    # verbatim flatten stored the raw "<...>" string, which threw
    # "Unrecognized token '<'" when Claude's TaskOutput renderer parsed it
    # at resume — this assertion fails if that flatten is restored.
    items: list[dict[str, Any]] = [
        {
            "id": "fco_1",
            "response_id": "resp_1",
            "type": "function_call_output",
            "call_id": "toolu_1",
            "output": output,
        }
    ]
    records = claude_native._claude_transcript_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
        cwd=Path("/tmp/test"),
        bridge_dir=Path("/tmp/test-bridge"),
    )
    assert len(records) == 1
    assert json.loads(records[0]["toolUseResult"]) == output


def test_routed_arms_repoint_the_family_aliases() -> None:
    """A routing-enabled launch spells the frozen arms, not just the newest models."""
    from omnigent.claude_model_vocabulary import claude_model_command_arg
    from omnigent.server.smart_routing import task_v1_claude_arms

    config = claude_native.ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "databricks-claude-haiku-4-5",
        },
        model="databricks-claude-opus-5",
        routable_models=(
            "databricks-claude-opus-5",
            "databricks-claude-opus-4-8",
            "databricks-claude-sonnet-5",
            "databricks-claude-haiku-4-5",
        ),
    )

    pinned = claude_native.claude_config_with_routed_arms_pinned(config, task_v1_claude_arms())

    assert pinned is not None
    assert pinned.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "databricks-claude-opus-4-8"
    assert pinned.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "databricks-claude-sonnet-5"
    # Untouched: haiku is not an arm, so it keeps the newest haiku.
    assert pinned.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "databricks-claude-haiku-4-5"
    assert config.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "databricks-claude-opus-5"
    assert claude_model_command_arg("databricks-claude-opus-4-8", pinned.env) == "opus"


def test_routed_arms_keep_the_existing_pin_when_no_spelling_is_servable() -> None:
    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5"},
        model="databricks-claude-opus-5",
        routable_models=("databricks-claude-opus-5",),
    )

    assert claude_native.claude_config_with_routed_arms_pinned(config, ("claude-opus-4-8",)) is (
        config
    )
    assert claude_native.claude_config_with_routed_arms_pinned(None, ("claude-opus-4-8",)) is None
    assert claude_native.claude_config_with_routed_arms_pinned(config, ()) is config
