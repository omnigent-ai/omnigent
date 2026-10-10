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
from omnigent.models import model_catalog
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, GATEWAY_KIND


@pytest.fixture(autouse=True)
def _isolate_ambient_anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the shell's Anthropic endpoint env from reaching a real gateway listing."""
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


async def test_catalog_keeps_canonical_rows_for_bare_id_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured gateway whose /v1/models lists bare claude-* ids keeps canonical rows.

    When a gateway provider (e.g. a LiteLLM passthrough) lists bare claude-*
    ids in /v1/models, the catalog keeps the probe's canonical rows instead
    of filtering them as non-canonical. Concrete listed ids are surfaced.
    """

    def _gateway_models_handler(request: httpx.Request) -> httpx.Response:
        """Serve a mock /v1/models listing bare claude-* ids."""
        if request.url.path == "/v1/models":
            # Verify the return_wildcard_routes parameter is sent.
            assert request.url.query == b"return_wildcard_routes=true"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "claude-opus-4", "object": "model"},
                        {"id": "claude-sonnet-4", "object": "model"},
                    ]
                },
            )
        return httpx.Response(404)

    # Mock the probe to return canonical rows.
    async def _fake_probe(config: object) -> claude_native.ClaudeModelProbe:
        return claude_native.ClaudeModelProbe(
            alias_rows=[
                {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5"},
                {"id": "sonnet", "model": "claude-sonnet-4", "displayName": "Sonnet 4"},
            ],
            default_model="claude-opus-5",
            empty_picker=False,
            disabled_models=set(),
        )

    monkeypatch.setattr(claude_native, "probe_claude_model_options", _fake_probe)

    # Create a gateway provider config with a mock transport.
    config = claude_native.ClaudeNativeUcodeConfig(
        env={},
        model=None,
        listing_provider=model_catalog.ResolvedModelProvider(
            kind=GATEWAY_KIND,
            family=ANTHROPIC_FAMILY,
            base_url="https://gateway.example.com",
            api_key="test-key",
            detail="test gateway",
        ),
    )

    # Mock the listing fetch with the gateway handler.
    original_fetch = model_catalog._fetch_openai_compatible_listing

    def _mocked_fetch(
        provider: model_catalog.ResolvedModelProvider,
        *,
        transport: object = None,
        params: object = None,
        headers: object = None,
    ) -> model_catalog.ModelListing:
        return original_fetch(
            provider,
            transport=httpx.MockTransport(_gateway_models_handler),
            params=params,
            headers=headers,
        )

    monkeypatch.setattr(model_catalog, "_fetch_openai_compatible_listing", _mocked_fetch)

    catalog = await claude_native.claude_model_catalog(config)
    assert catalog is not None
    # Verify canonical rows from the probe are kept.
    ids = [row.get("id") for row in catalog]
    models = [row.get("model") for row in catalog]
    assert "opus" in ids
    assert "sonnet" in ids
    assert "claude-opus-4" in models  # Concrete id from gateway listing
    assert "claude-sonnet-4" in models  # Concrete id from gateway listing


async def test_catalog_drops_bare_rows_when_configured_databricks_listing_is_undetermined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured Databricks AI Gateway whose /v1/models fails keeps no bare claude-* rows.

    The gateway routes only its own namespaced ids, so an undetermined listing
    fails closed there instead of offering aliases that fail at the first turn.
    """

    async def _fake_probe(config: object) -> claude_native.ClaudeModelProbe:
        return claude_native.ClaudeModelProbe(
            alias_rows=[
                {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5"},
                {"id": "sonnet", "model": "claude-sonnet-4", "displayName": "Sonnet 4"},
            ],
            default_model="claude-opus-5",
            empty_picker=False,
            disabled_models=set(),
        )

    monkeypatch.setattr(claude_native, "probe_claude_model_options", _fake_probe)
    original_fetch = model_catalog._fetch_openai_compatible_listing

    def _not_found_fetch(
        provider: model_catalog.ResolvedModelProvider, **kwargs: Any
    ) -> model_catalog.ModelListing:
        kwargs["transport"] = httpx.MockTransport(lambda _request: httpx.Response(404))
        return original_fetch(provider, **kwargs)

    monkeypatch.setattr(model_catalog, "_fetch_openai_compatible_listing", _not_found_fetch)
    config = claude_native.ClaudeNativeUcodeConfig(
        env={},
        model=None,
        listing_provider=model_catalog.ResolvedModelProvider(
            kind=GATEWAY_KIND,
            family=ANTHROPIC_FAMILY,
            base_url="https://example.cloud.databricks.com/ai-gateway/anthropic",
            api_key="test-key",
            detail="databricks gateway",
        ),
    )

    catalog = await claude_native.claude_model_catalog(config)

    assert [row for row in catalog or [] if str(row.get("model", "")).startswith("claude-")] == []
