"""Claude picker availability and discovery compatibility with older CLIs."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native import main as claude_native

_AMBIENT_ANTHROPIC_ENV = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
)


@pytest.fixture(autouse=True)
def _isolate_ambient_anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer shell's own gateway env must not reach the no-config probes."""
    for name in _AMBIENT_ANTHROPIC_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", ())


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


_PASSTHROUGH_PICKER: list[dict[str, Any]] = [
    {"value": "default", "resolvedModel": "claude-opus-5-5[1m]", "displayName": "Default"},
    {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "displayName": "Opus"},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5", "displayName": "Sonnet"},
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001", "displayName": "Haiku"},
]
_PASSTHROUGH_ROWS = ["opus[1m]", "sonnet", "haiku"]
_LITELLM_URL = "http://litellm.litellm.svc.cluster.local"
_DATABRICKS_GATEWAY_URL = "https://wkspc.ai-gateway.cloud.databricks.com/anthropic"
_LITELLM_CREDENTIAL = {
    "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-subscription",
    "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-api-key: sk-litellm-virtual",
}
_BEARER_ONLY = {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-subscription"}


def _ambient_gateway(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    url: str = _LITELLM_URL,
    env: Mapping[str, str] | None = None,
) -> list[httpx.Request]:
    """No providers entry; the ambient env points Claude Code at a gateway.

    *respond* answers (or fails) the gateway's model listing. Returns the
    requests the gateway received.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
    for name, value in (_LITELLM_CREDENTIAL if env is None else env).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        claude_native,
        "_ambient_gateway_serves_canonical_ids",
        functools.partial(
            claude_native._ambient_gateway_serves_canonical_ids,
            transport=httpx.MockTransport(handler),
        ),
    )
    return requests


def _litellm_listing(request: httpx.Request) -> httpx.Response:
    """LiteLLM hides a ``claude-*`` passthrough route unless wildcard routes are asked for."""
    wildcard = request.url.params.get("return_wildcard_routes") == "true"
    data = [{"id": "claude-*", "object": "model"}] if wildcard else []
    return httpx.Response(200, json={"object": "list", "data": data})


def _lists(*ids: str) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": [{"id": mid} for mid in ids]})

    return respond


def _rejects(status: int) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "listing refused"}})

    return respond


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _answers(**response: Any) -> Callable[[httpx.Request], httpx.Response]:
    """A 200 with an arbitrary body, for listings that do not parse as a model list."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, **response)

    return respond


@pytest.mark.parametrize(
    ("respond", "url", "env", "kept", "asked"),
    [
        pytest.param(
            _litellm_listing, _LITELLM_URL, None, True, True, id="litellm-wildcard-route"
        ),
        pytest.param(
            _lists("claude-sonnet-5", "gpt-5"),
            _LITELLM_URL,
            None,
            True,
            True,
            id="concrete-claude-id",
        ),
        pytest.param(
            _lists("anthropic/claude-sonnet-5"),
            _LITELLM_URL,
            None,
            False,
            True,
            id="namespaced-only",
        ),
        pytest.param(_unreachable, _LITELLM_URL, None, True, True, id="unreachable"),
        pytest.param(_rejects(401), _LITELLM_URL, None, True, True, id="rejected-401"),
        pytest.param(_lists(), _LITELLM_URL, None, True, True, id="empty-listing"),
        pytest.param(
            _answers(content=b"not json"), _LITELLM_URL, None, True, True, id="invalid-json"
        ),
        pytest.param(
            _answers(json=["claude-*"]), _LITELLM_URL, None, True, True, id="non-object-json"
        ),
        pytest.param(
            _answers(json={"data": {"id": "claude-*"}}),
            _LITELLM_URL,
            None,
            True,
            True,
            id="data-not-a-list",
        ),
        pytest.param(_lists("claude-*"), _LITELLM_URL, {}, True, False, id="no-credential"),
        pytest.param(
            _lists("claude-*"), f"{_LITELLM_URL}:bad", None, True, False, id="malformed-base-url"
        ),
        pytest.param(
            _lists("claude-*"),
            _LITELLM_URL,
            {"ANTHROPIC_CUSTOM_HEADERS": "x-user: José"},
            True,
            False,
            id="non-ascii-custom-header",
        ),
        pytest.param(
            _lists("claude-*"),
            _LITELLM_URL,
            {"ANTHROPIC_AUTH_TOKEN": "sk-ant-ünïcode"},
            True,
            False,
            id="non-ascii-token",
        ),
        pytest.param(
            _unreachable,
            _DATABRICKS_GATEWAY_URL,
            _BEARER_ONLY,
            False,
            True,
            id="databricks-gateway-unreachable",
        ),
        pytest.param(
            _lists("claude-*"),
            _DATABRICKS_GATEWAY_URL,
            {},
            False,
            False,
            id="databricks-gateway-no-credential",
        ),
    ],
)
async def test_catalog_lets_the_ambient_gateway_decide_the_clis_bare_rows(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
    url: str,
    env: Mapping[str, str] | None,
    kept: bool,
    asked: bool,
) -> None:
    """No providers entry; ANTHROPIC_BASE_URL names a gateway.

    The CLI enumerates its picker with bare Anthropic ids either way; whether
    the catalog offers them is the gateway's own call. A bare id or wildcard in
    its listing keeps them, a namespaced-only listing drops them, and an
    unreadable listing keeps them (Claude Code's own picker beats an empty one)
    except on a Databricks Unity Gateway, which routes only its own ids.
    """
    requests = _ambient_gateway(monkeypatch, respond, url=url, env=env)
    _stub_picker(monkeypatch, _PASSTHROUGH_PICKER, default="claude-opus-5-5[1m]")

    rows = await claude_native.claude_model_catalog(None)

    assert rows is not None
    assert [row["id"] for row in rows] == (_PASSTHROUGH_ROWS if kept else [])
    assert [row["id"] for row in rows if row.get("isDefault") is True] == (
        ["opus[1m]"] if kept else []
    )
    assert bool(requests) is asked


async def test_catalog_asks_the_ambient_gateway_the_way_claude_code_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing request carries Claude Code's own credential and custom headers."""
    requests = _ambient_gateway(monkeypatch, _litellm_listing)
    _stub_picker(monkeypatch, _PASSTHROUGH_PICKER, default="claude-opus-5-5[1m]")

    await claude_native.claude_model_catalog(None)

    (request,) = requests
    assert request.url.host == "litellm.litellm.svc.cluster.local"
    assert request.url.path == "/v1/models"
    assert request.url.params["return_wildcard_routes"] == "true"
    assert request.headers["authorization"] == "Bearer sk-ant-oat01-subscription"
    assert request.headers["x-litellm-api-key"] == "sk-litellm-virtual"
    assert request.headers["anthropic-version"]


async def test_catalog_lets_custom_headers_override_the_derived_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit custom header wins over the env-derived one, whatever its case."""
    requests = _ambient_gateway(
        monkeypatch,
        _litellm_listing,
        env={
            "ANTHROPIC_AUTH_TOKEN": "sk-ant-env",
            "ANTHROPIC_CUSTOM_HEADERS": (
                "authorization: Bearer custom\nANTHROPIC-VERSION: 2024-01-01"
            ),
        },
    )
    _stub_picker(monkeypatch, _PASSTHROUGH_PICKER, default="claude-opus-5-5[1m]")

    await claude_native.claude_model_catalog(None)

    (request,) = requests
    assert request.headers.get_list("authorization") == ["Bearer custom"]
    assert request.headers.get_list("anthropic-version") == ["2024-01-01"]


async def test_launch_catalog_reprobes_when_the_ambient_credential_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A different key for the same gateway URL must not reuse a persisted empty answer."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    _stub_picker(monkeypatch, _PASSTHROUGH_PICKER, default="claude-opus-5-5[1m]")
    _ambient_gateway(
        monkeypatch,
        _lists("anthropic/claude-sonnet-5"),
        env={"ANTHROPIC_AUTH_TOKEN": "sk-key-scoped-to-namespaced-routes"},
    )
    assert await claude_native.claude_launch_catalog(None) == []

    _ambient_gateway(
        monkeypatch, _litellm_listing, env={"ANTHROPIC_AUTH_TOKEN": "sk-key-scoped-to-claude"}
    )
    rows = await claude_native.claude_launch_catalog(None)

    assert rows is not None
    assert [row["id"] for row in rows] == _PASSTHROUGH_ROWS


async def test_catalog_asks_the_managed_settings_gateway_over_the_shell_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Managed settings win at Claude Code's launch: their gateway is asked, with their key."""
    managed = tmp_path / "managed-settings.json"
    managed.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://managed.gateway.example",
                    "ANTHROPIC_AUTH_TOKEN": "sk-managed",
                }
            }
        )
    )
    requests = _ambient_gateway(
        monkeypatch,
        _lists("anthropic/claude-sonnet-5"),
        url="http://shell.gateway.example",
        env={"ANTHROPIC_AUTH_TOKEN": "sk-shell"},
    )
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed,))
    _stub_picker(monkeypatch, _PASSTHROUGH_PICKER, default="claude-opus-5-5[1m]")

    assert await claude_native.claude_model_catalog(None) == []
    (request,) = requests
    assert request.url.host == "managed.gateway.example"
    assert request.headers["authorization"] == "Bearer sk-managed"


async def test_catalog_sends_an_api_key_only_credential_as_x_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``ANTHROPIC_API_KEY`` alone authenticates the listing the way Claude Code does."""
    requests = _ambient_gateway(monkeypatch, _litellm_listing, env={"ANTHROPIC_API_KEY": "sk-api"})
    _stub_picker(monkeypatch, _PASSTHROUGH_PICKER, default="claude-opus-5-5[1m]")

    rows = await claude_native.claude_model_catalog(None)

    assert rows is not None
    assert [row["id"] for row in rows] == _PASSTHROUGH_ROWS
    (request,) = requests
    assert request.headers["x-api-key"] == "sk-api"
    assert "authorization" not in request.headers
