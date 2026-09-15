"""Claude's structured picker, rather than its help text, defines availability."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.harnesses.claude_native import main as claude_native


def _stub_picker(
    monkeypatch: pytest.MonkeyPatch,
    models: list[dict[str, Any]] | None,
    *,
    default: str = "claude-opus-5",
) -> None:
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
            "result": "Current model: `Opus 5`\n"
            "Usage: /model <name>. Available: opus, fable, best, fable[1m], default, "
            "or a full model ID.",
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
                return "\n".join(json.dumps(event) for event in events[1:]).encode(), b""
            requests = [json.loads(line) for line in input.splitlines()]
            assert requests[0] == {
                "type": "control_request",
                "request_id": "model-catalog",
                "request": {"subtype": "initialize"},
            }
            assert requests[1]["message"] == {"role": "user", "content": "/model"}
            return "\n".join(json.dumps(event) for event in events).encode(), b""

    async def spawn(command: str, *args: str, **kwargs: Any) -> Process:
        alias = args[args.index("--model") + 1] if "--model" in args else None
        if "--input-format" in args:
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return Process(alias)

    monkeypatch.setattr(
        claude_native,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": spawn}),
    )


@pytest.mark.parametrize("disabled_row", [False, True], ids=["hidden", "disabled"])
async def test_catalog_excludes_unavailable_fable(
    monkeypatch: pytest.MonkeyPatch, disabled_row: bool
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
    _stub_picker(monkeypatch, models)

    assert await claude_native.claude_model_catalog(None) == [
        {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5", "isDefault": True}
    ]


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


async def test_catalog_does_not_fall_back_to_help_or_managed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(monkeypatch, None)
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_model_picker",
        lambda: (("fable", "Fable"),),
    )
    assert await claude_native.claude_model_catalog(None) is None


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
