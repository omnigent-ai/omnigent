"""Claude picker availability and discovery compatibility with older CLIs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native import main as claude_native


@pytest.fixture(autouse=True)
def _no_ambient_anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog reads the ambient Anthropic env; start every test without one."""
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)


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


# The CLI's picker through an Anthropic passthrough gateway: every alias resolves
# to a bare canonical id, exactly as it does against api.anthropic.com.
_PASSTHROUGH_PICKER: list[dict[str, Any]] = [
    {"value": "default", "resolvedModel": "claude-opus-5[1m]", "displayName": "Default"},
    {
        "value": "opus[1m]",
        "resolvedModel": "claude-opus-5[1m]",
        "displayName": "Opus (1M context)",
    },
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5", "displayName": "Sonnet"},
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5", "displayName": "Haiku"},
]
_PASSTHROUGH_ROWS: list[dict[str, object]] = [
    {
        "id": "opus[1m]",
        "model": "claude-opus-5[1m]",
        "displayName": "Opus (1M context)",
        "isDefault": True,
    },
    {"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet"},
    {"id": "haiku", "model": "claude-haiku-4-5", "displayName": "Haiku"},
]


def _stub_ambient_gateway(
    monkeypatch: pytest.MonkeyPatch, base_url: str, serves: bool | None
) -> None:
    """Point the ambient env at *base_url* whose ``/v1/models`` answer is *serves*."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
    monkeypatch.setattr(
        claude_native, "_ambient_gateway_serves_canonical_ids", lambda: serves, raising=False
    )


@pytest.mark.parametrize("serves", [True, None], ids=["lists-claude-ids", "listing-undetermined"])
async def test_ambient_gateway_catalog_keeps_the_clis_canonical_rows(
    monkeypatch: pytest.MonkeyPatch, serves: bool | None
) -> None:
    """The CLI's rows stay whether the listing confirms ``claude-*`` or cannot be read."""
    _stub_picker(monkeypatch, _PASSTHROUGH_PICKER, default="claude-opus-5[1m]")
    _stub_ambient_gateway(monkeypatch, "http://litellm.local", serves)
    assert await claude_native.claude_model_catalog(None) == _PASSTHROUGH_ROWS


async def test_ambient_gateway_catalog_drops_canonical_rows_when_the_listing_is_namespaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway that lists only namespaced ids cannot route the bare ones."""
    gateway_row = {
        "value": "gateway-opus",
        "resolvedModel": "anthropic/claude-opus-5",
        "displayName": "Opus (gateway)",
    }
    _stub_picker(monkeypatch, [*_PASSTHROUGH_PICKER, gateway_row], default="claude-opus-5[1m]")
    _stub_ambient_gateway(monkeypatch, "http://openrouter.local/api/v1", False)
    assert await claude_native.claude_model_catalog(None) == [
        {"id": "gateway-opus", "model": "anthropic/claude-opus-5", "displayName": "Opus (gateway)"}
    ]


async def test_ambient_databricks_gateway_catalog_drops_canonical_rows_without_a_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Databricks AI Gateway routes only its own ids even when it lists nothing."""
    databricks_row = {
        "value": "opus",
        "resolvedModel": "databricks-claude-opus-5",
        "displayName": "Opus",
    }
    _stub_picker(
        monkeypatch, [*_PASSTHROUGH_PICKER, databricks_row], default="databricks-claude-opus-5"
    )
    _stub_ambient_gateway(
        monkeypatch, "https://example.cloud.databricks.com/ai-gateway/anthropic", None
    )
    assert await claude_native.claude_model_catalog(None) == [
        {
            "id": "opus",
            "model": "databricks-claude-opus-5",
            "displayName": "Opus",
            "isDefault": True,
        }
    ]


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"object": "list", "data": [{"id": "claude-*", "object": "model"}]}, True),
        (200, {"data": [{"id": "gpt-5"}, {"id": "claude-sonnet-5"}]}, True),
        (200, {"data": [{"id": "anthropic/claude-sonnet-5"}, {"id": "gpt-5"}]}, False),
        (200, {"data": []}, None),
        (200, {"models": ["claude-sonnet-5"]}, None),
        (401, {"error": {"message": "invalid key"}}, None),
    ],
    ids=["wildcard", "concrete", "namespaced", "empty", "unparseable", "unauthorized"],
)
def test_ambient_gateway_listing_classifies_what_the_gateway_serves(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: dict[str, Any],
    expected: bool | None,
) -> None:
    """``/v1/models`` is asked for wildcard routes with the credential Claude Code sends."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.local/v1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-bearer")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS", "x-litellm-api-key: virtual-key\nX-Team: platform"
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=payload)

    result = claude_native._ambient_gateway_serves_canonical_ids(
        transport=httpx.MockTransport(handler)
    )

    assert result is expected
    (request,) = requests
    assert str(request.url) == "http://litellm.local/v1/models?return_wildcard_routes=true"
    assert request.headers["authorization"] == "Bearer gateway-bearer"
    assert request.headers["x-api-key"] == "gateway-bearer"
    assert request.headers["x-litellm-api-key"] == "virtual-key"
    assert request.headers["x-team"] == "platform"


def test_ambient_gateway_listing_is_undetermined_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.local")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    assert claude_native._ambient_gateway_serves_canonical_ids(transport=transport) is None


def test_ambient_gateway_listing_is_skipped_without_an_ambient_gateway() -> None:
    assert claude_native._ambient_gateway_serves_canonical_ids() is None
