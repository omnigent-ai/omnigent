"""Provider diagnostics retain typed facts without copying private payloads."""

import json
from pathlib import Path

import pytest

from omnigent.harnesses.opencode_native.bridge import xdg_config_home_for_bridge_dir
from omnigent.harnesses.opencode_native.failure_telemetry import opencode_failure_context
from omnigent.native.failure_telemetry import failure_log_attributes


def _write_config(bridge_dir: Path, config: object) -> Path:
    path = xdg_config_home_for_bridge_dir(bridge_dir) / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("url", "origin", "family"),
    [
        ("https://gateway.example.test/ai-gateway/mlflow/v1", "gateway.example.test", "mlflow"),
        (
            "https://user:password@gateway.example.test:8443/ai-gateway/anthropic/"
            "?token=secret#private",
            "gateway.example.test:8443",
            "anthropic",
        ),
        (
            "https://gateway.example.test/serving-endpoints/",
            "gateway.example.test",
            "serving-endpoints",
        ),
        ("http://[::1]:8000/private-prompt?api_key=secret", "[::1]:8000", "custom"),
    ],
)
def test_configured_route_retains_only_origin_and_fixed_family(
    tmp_path: Path, url: str, origin: str, family: str
) -> None:
    config = {
        "providers": {
            "gateway": {"settings": {"baseURL": url, "apiKey": "secret"}},
            "other": {"settings": {"baseURL": "https://wrong.example.test/serving-endpoints"}},
        },
        "instructions": ["private-prompt"],
    }
    _write_config(tmp_path, config)
    context = opencode_failure_context(error={}, model="gateway/model-a", bridge_dir=tmp_path)
    assert context["configured_provider_origin"] == origin
    assert context["configured_provider_route_family"] == family
    assert "secret" not in json.dumps(context)
    assert "password" not in json.dumps(context)
    assert "private-prompt" not in json.dumps(context)
    assert "https://" not in json.dumps(context)
    assert failure_log_attributes(context)["configured_provider_origin"] == origin


@pytest.mark.parametrize(
    "config",
    [
        None,
        [],
        {},
        {"providers": []},
        {"providers": {"gateway": None}},
        {"providers": {"gateway": {"settings": []}}},
        {"providers": {"gateway": {"settings": {"baseURL": 12}}}},
        {"providers": {"other": {"settings": {"baseURL": "https://other.example.test"}}}},
    ],
)
def test_missing_provider_config_does_not_guess_route(tmp_path: Path, config: object) -> None:
    _write_config(tmp_path, config)
    context = opencode_failure_context(error={}, model="gateway/model-a", bridge_dir=tmp_path)
    assert "configured_provider_origin" not in context
    assert "configured_provider_route_family" not in context
    assert context["native_model"] == "gateway/model-a"


@pytest.mark.parametrize("override", ["https://model.example.test/ai-gateway/anthropic", None])
def test_model_route_override_does_not_report_provider_default(
    tmp_path: Path, override: object
) -> None:
    _write_config(
        tmp_path,
        {
            "providers": {
                "gateway": {
                    "settings": {"baseURL": "https://default.example.test/serving-endpoints"},
                    "models": {"model-a": {"settings": {"baseURL": override}}},
                }
            }
        },
    )
    context = opencode_failure_context(error={}, model="gateway/model-a", bridge_dir=tmp_path)
    if override is None:
        assert "configured_provider_origin" not in context
    else:
        assert context["configured_provider_origin"] == "model.example.test"
        assert context["configured_provider_route_family"] == "anthropic"


def test_absent_config_is_optional(tmp_path: Path) -> None:
    context = opencode_failure_context(
        error={"status": 404}, model="gateway/a", bridge_dir=tmp_path
    )
    assert context["http_status"] == 404
    assert "configured_provider_origin" not in context


@pytest.mark.parametrize(
    "raw",
    [b"{bad", b"\xff", b" " * (1024 * 1024 + 1)],
    ids=["malformed-json", "invalid-unicode", "oversized"],
)
def test_invalid_or_oversized_config_cannot_replace_failure(tmp_path: Path, raw: bytes) -> None:
    path = _write_config(tmp_path, {})
    path.write_bytes(raw)
    context = opencode_failure_context(
        error={"status": 404}, model="gateway/a", bridge_dir=tmp_path
    )
    assert context["http_status"] == 404
    assert "configured_provider_origin" not in context


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "file:///private",
        "https://host:bad/path",
        "https://[bad",
        "https://host/\nprompt",
    ],
)
def test_malformed_url_adds_no_route_fields(tmp_path: Path, url: str) -> None:
    _write_config(tmp_path, {"providers": {"gateway": {"settings": {"baseURL": url}}}})
    context = opencode_failure_context(error={}, model="gateway/a", bridge_dir=tmp_path)
    assert "configured_provider_origin" not in context


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        "unqualified",
        "/a",
        "gateway/",
        "gateway/private prompt",
        "gateway/https://secret",
    ],
)
def test_unobserved_or_malformed_model_is_not_inferred(tmp_path: Path, model: str | None) -> None:
    _write_config(tmp_path, {"model": "gateway/configured-but-not-observed"})
    context = opencode_failure_context(error={}, model=model, bridge_dir=tmp_path)
    assert "native_model" not in context
    assert "native_provider" not in context


@pytest.mark.parametrize(
    "error", [None, [], "failed", {"status": True}, {"status": "404"}, {"status": 999}]
)
def test_missing_or_malformed_error_fields_are_omitted(error: object) -> None:
    context = opencode_failure_context(error=error, model=None, bridge_dir=None)
    assert context == {
        "native_harness": "opencode-native",
        "failure_source": "opencode_execution_failed",
    }


def test_typed_error_fields_exclude_credentials_prompt_and_ambiguous_request_id() -> None:
    context = opencode_failure_context(
        error={
            "type": "provider.invalid-request",
            "status": 404,
            "provider_request_id": "req_upstream_1",
            "request_id": "intake-not-proven-upstream",
            "message": "private prompt",
            "headers": {"Authorization": "Bearer secret"},
            "body": {"messages": ["private prompt"]},
        },
        model="gateway/system.ai.model-a",
        bridge_dir=None,
    )
    assert context["provider_request_id"] == "req_upstream_1"
    assert context["http_status"] == 404
    assert context["native_provider"] == "gateway"
    encoded = json.dumps(context)
    for private in ("private prompt", "secret", "intake-not-proven-upstream", "headers", "body"):
        assert private not in encoded


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        "Bearer secret",
        "https://host/path?token=secret",
        'password="secret"',
        "private prompt",
        "a" * 257,
    ],
)
def test_malformed_or_sensitive_identifiers_are_not_retained(value: object) -> None:
    context = opencode_failure_context(
        error={"type": value, "provider_request_id": value}, model=None, bridge_dir=None
    )
    assert "provider_request_id" not in context
    assert "provider_error_type" not in context
