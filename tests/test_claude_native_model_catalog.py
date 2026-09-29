"""Claude picker availability and discovery compatibility with older CLIs."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.harnesses.claude_native import main as claude_native


def _stub_picker(
    monkeypatch: pytest.MonkeyPatch,
    models: list[dict[str, Any]] | None,
    *,
    default: str = "claude-opus-5",
    control_failure: str | None = None,
    legacy_failure: bool = False,
    help_text: str = "Usage: /model <name>. Available: opus, fable, best, fable[1m], default, "
    "or a full model ID.",
) -> list[tuple[str, ...]]:
    launches: list[tuple[str, ...]] = []
    events = [
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "model-catalog",
                "response": {"models": models},
            },
        },
        {"type": "system", "subtype": "init", "model": default},
        {
            "type": "result",
            "result": f"Current model: `Opus 5`\n{help_text}",
        },
    ]

    class Process:
        returncode = 0

        def __init__(self, alias: str | None) -> None:
            self.alias = alias

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            if input is None:
                if self.alias is not None:
                    model = "claude-fable-5-1" if "fable" in self.alias else "claude-opus-5"
                    return json.dumps(
                        {"type": "system", "subtype": "init", "model": model}
                    ).encode(), b""
                if legacy_failure:
                    self.returncode = 1
                    return b"", b"probe failed"
                return "\n".join(json.dumps(event) for event in events[1:]).encode(), b""
            if control_failure == "timeout":
                self.returncode = None
                raise TimeoutError
            if control_failure == "cancelled":
                self.returncode = None
                raise asyncio.CancelledError
            if control_failure == "exit":
                self.returncode = 1
                return b"", b"unsupported control request"
            if control_failure == "unsupported":
                return json.dumps(
                    {
                        "type": "control_response",
                        "response": {"subtype": "error", "request_id": "model-catalog"},
                    }
                ).encode(), b""
            requests = [json.loads(line) for line in input.splitlines()]
            assert requests[0] == {
                "type": "control_request",
                "request_id": "model-catalog",
                "request": {"subtype": "initialize"},
            }
            assert requests[1]["message"] == {"role": "user", "content": "/model"}
            if control_failure == "after_picker":
                self.returncode = 1
            return "\n".join(json.dumps(event) for event in events).encode(), b""

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            assert self.returncode == -9
            return self.returncode

    async def spawn(command: str, *args: str, **kwargs: Any) -> Process:
        launches.append(args)
        alias = args[args.index("--model") + 1] if "--model" in args else None
        if "--input-format" in args:
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return Process(alias)

    monkeypatch.setattr(
        claude_native,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": spawn}),
    )
    return launches


@pytest.mark.parametrize("control_failure", [None, "after_picker"])
@pytest.mark.parametrize("disabled_row", [False, True], ids=["hidden", "disabled"])
async def test_catalog_excludes_unavailable_fable(
    monkeypatch: pytest.MonkeyPatch, disabled_row: bool, control_failure: str | None
) -> None:
    models: list[dict[str, Any]] = [
        {"value": "default", "resolvedModel": "claude-opus-5", "displayName": "Default"},
        {"value": "opus", "resolvedModel": "claude-opus-5", "displayName": "Opus 5"},
    ]
    if disabled_row:
        models.append(
            {
                "value": "fable",
                "resolvedModel": "claude-fable-5-1",
                "displayName": "Fable (disabled)",
                "disabled": True,
                "description": "Requires usage credits",
            }
        )
    launches = _stub_picker(monkeypatch, models, control_failure=control_failure)

    assert await claude_native.claude_model_catalog(None) == [
        {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5", "isDefault": True}
    ]
    assert len(launches) == 1


@pytest.mark.parametrize("default", ["fable", "claude-fable-5-1"])
async def test_catalog_does_not_restore_a_disabled_default(
    monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    _stub_picker(
        monkeypatch,
        [{"value": "fable", "resolvedModel": "claude-fable-5-1", "disabled": True}],
        default=default,
    )
    assert await claude_native.claude_model_catalog(None) == []


@pytest.mark.parametrize("configured", [False, True])
async def test_catalog_uses_cli_managed_picker_for_every_launch_config(
    monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    _stub_picker(
        monkeypatch,
        [{"value": "gateway-opus", "resolvedModel": "gateway-opus", "displayName": "Opus"}],
        default="gateway-opus",
    )
    # Managed-file rows alone cannot describe the CLI's availability decisions.
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_model_picker",
        lambda: (("fable", "Fable"), ("gateway-opus", "Opus")),
    )
    config = (
        claude_native.ClaudeNativeUcodeConfig(env={}, model="gateway-opus") if configured else None
    )
    assert await claude_native.claude_model_catalog(config) == [
        {"id": "gateway-opus", "model": "gateway-opus", "displayName": "Opus", "isDefault": True}
    ]


@pytest.mark.parametrize("failure", [None, "unsupported", "exit", "timeout"])
async def test_catalog_falls_back_for_older_claude(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    launches = _stub_picker(
        monkeypatch,
        None,
        control_failure=failure,
        help_text="Available: `opus`, default, or a full model ID.",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_model_picker",
        lambda: (("fable", "Fable"),),
    )
    assert await claude_native.claude_model_catalog(None) == [
        {"id": "opus", "model": "claude-opus-5", "displayName": "opus", "isDefault": True}
    ]
    assert len(launches) == 3
    assert "--input-format" in launches[0]
    assert launches[1][:2] == ("-p", "/model")
    assert "--input-format" not in launches[1]
    assert "--model" in launches[2]


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    "models",
    [[], [{"value": "fable", "resolvedModel": "claude-fable-5-1", "disabled": True}]],
    ids=["empty", "disabled-only"],
)
async def test_empty_structured_catalog_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, configured: bool, models: list[dict[str, Any]]
) -> None:
    launches = _stub_picker(monkeypatch, models)
    config = (
        claude_native.ClaudeNativeUcodeConfig(env={}, model="gateway-default")
        if configured
        else None
    )
    assert await claude_native.claude_model_catalog(config) == []
    assert len(launches) == 1


async def test_default_only_structured_picker_keeps_its_enabled_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(monkeypatch, [{"value": "default", "resolvedModel": "claude-opus-5"}])
    assert await claude_native.claude_model_catalog(None) == [
        {
            "id": "claude-opus-5",
            "model": "claude-opus-5",
            "displayName": "Opus 5",
            "isDefault": True,
        }
    ]


async def test_disabled_only_refresh_replaces_the_cached_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(claude_native, "claude_catalog_fingerprint", lambda _: "test-picker")
    model = {"value": "opus", "resolvedModel": "claude-opus-5", "displayName": "Opus 5"}
    _stub_picker(monkeypatch, [model])
    assert await claude_native.claude_launch_catalog(None)

    launches = _stub_picker(monkeypatch, [{**model, "disabled": True}])
    assert await claude_native.claude_reprobed_launch_catalog(None) == []
    assert await claude_native.claude_launch_catalog(None) == []
    assert len(launches) == 1


async def test_legacy_catalog_keeps_observed_default_without_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(monkeypatch, None, help_text="")
    assert await claude_native.claude_model_catalog(None) == [
        {
            "id": "claude-opus-5",
            "model": "claude-opus-5",
            "displayName": "Opus 5",
            "isDefault": True,
        }
    ]


async def test_legacy_catalog_failure_preserves_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(monkeypatch, None, legacy_failure=True)
    assert await claude_native.claude_model_catalog(None) is None


async def test_cancelled_catalog_probe_never_starts_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.harnesses.claude_native.main")
    launches = _stub_picker(monkeypatch, None, control_failure="cancelled")
    with pytest.raises(asyncio.CancelledError):
        await claude_native.claude_model_catalog(None)
    assert len(launches) == 1
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "claude_model_catalog_probe"
    ]
    assert records[-1].attributes["outcome"] == "cancelled"


async def test_catalog_keeps_enabled_fable_and_future_picker_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(
        monkeypatch,
        [
            {"value": "fable", "resolvedModel": "claude-fable-5-1", "displayName": "Fable 5.1"},
            {"value": "future", "resolvedModel": "vendor-future", "displayName": "Future model"},
        ],
        default="claude-fable-5-1",
    )
    assert await claude_native.claude_model_catalog(None) == [
        {
            "id": "fable",
            "model": "claude-fable-5-1",
            "displayName": "Fable 5.1",
            "isDefault": True,
        },
        {"id": "future", "model": "vendor-future", "displayName": "Future model"},
    ]


def test_default_catalog_bypass_requires_claude_owned_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_GATEWAY",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", ())
    monkeypatch.setattr("omnigent.onboarding.ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS", ())
    user_config_dir = tmp_path / "claude-config"
    user_config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user_config_dir))
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_gateway", lambda _paths=None: (None, False)
    )

    assert claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "claude_login"
    assert not claude_native.claude_default_catalog_bypass_is_safe(
        None, launch_config_resolution_failed=True
    )
    assert (
        claude_native.claude_launch_endpoint_marker(None, launch_config_resolution_failed=True)
        == "resolution_failed"
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(
        claude_native.ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
            model="gateway-default",
        )
    )

    monkeypatch.setenv("ANTHROPIC_MODEL", "synthetic-default")
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "ambient_models"
    monkeypatch.delenv("ANTHROPIC_MODEL")

    user_settings = user_config_dir / "settings.json"
    user_settings.write_text(json.dumps({"apiKeyHelper": "printf synthetic"}), encoding="utf-8")
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "user_settings"
    user_settings.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"}}),
        encoding="utf-8",
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "user_settings"
    user_settings.write_text(json.dumps({"model": "synthetic-default"}), encoding="utf-8")
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "user_settings"
    user_settings.write_text(json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
    assert claude_native.claude_default_catalog_bypass_is_safe(None)
    user_settings.unlink()

    workspace = tmp_path / "workspace"
    workspace_settings_dir = workspace / ".claude"
    workspace_settings_dir.mkdir(parents=True)
    workspace_settings = workspace_settings_dir / "settings.json"
    workspace_settings.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"}}),
        encoding="utf-8",
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(None, workspace=workspace)
    assert (
        claude_native.claude_launch_endpoint_marker(None, workspace=workspace)
        == "workspace_settings"
    )
    workspace_settings.write_text(json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
    assert claude_native.claude_default_catalog_bypass_is_safe(None, workspace=workspace)
    workspace_settings.unlink()
    workspace_local_settings = workspace_settings_dir / "settings.local.json"
    workspace_local_settings.write_text("{malformed", encoding="utf-8")
    assert not claude_native.claude_default_catalog_bypass_is_safe(None, workspace=workspace)
    assert (
        claude_native.claude_launch_endpoint_marker(None, workspace=workspace)
        == "workspace_settings"
    )
    workspace_local_settings.unlink()

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example/anthropic")
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "ambient_gateway"

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://[::1")
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "unknown"

    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_gateway",
        lambda _paths=None: ("https://gateway.example/anthropic", True),
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "managed_gateway"

    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_gateway",
        lambda _paths=None: ("https://gateway.example/anthropic", False),
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "managed_endpoint"

    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_gateway", lambda _paths=None: (None, False)
    )
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "synthetic-project")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-central1")
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "vertex"

    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX")
    managed_settings = tmp_path / "managed-settings.json"
    managed_settings.write_text("{malformed", encoding="utf-8")
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed_settings,))
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "managed_settings"

    managed_settings.write_text(
        json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}), encoding="utf-8"
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "bedrock"

    managed_settings.write_text(
        json.dumps({"env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "synthetic-sonnet"}}),
        encoding="utf-8",
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "managed_models"

    managed_settings.write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "configured-by-enterprise"}}),
        encoding="utf-8",
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "managed_settings"

    managed_settings.write_text(
        json.dumps(
            {
                "modelPicker": {
                    "replaceBuiltInOptions": True,
                    "options": [{"model": "synthetic-sonnet", "label": "Synthetic Sonnet"}],
                }
            }
        ),
        encoding="utf-8",
    )
    assert not claude_native.claude_default_catalog_bypass_is_safe(None)
    assert claude_native.claude_launch_endpoint_marker(None) == "managed_models"
