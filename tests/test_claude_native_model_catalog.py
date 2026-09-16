"""Claude picker availability and discovery compatibility with older CLIs."""

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


async def test_empty_structured_catalog_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    launches = _stub_picker(monkeypatch, [], default="")
    assert await claude_native.claude_model_catalog(None) == []
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
) -> None:
    launches = _stub_picker(monkeypatch, None, control_failure="cancelled")
    with pytest.raises(asyncio.CancelledError):
        await claude_native.claude_model_catalog(None)
    assert len(launches) == 1


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
