"""Tests for live Databricks Claude model discovery."""

from __future__ import annotations

import httpx
import pytest

from omnigent.databricks_model_discovery import discover_databricks_claude_models


def test_model_services_are_paginated_filtered_and_version_sorted() -> None:
    """The UC listing keeps system Claude services and chooses newest versions."""
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer token"
        page_token = request.url.params.get("page_token")
        if page_token is None:
            return httpx.Response(
                200,
                json={
                    "model_services": [
                        {"name": "model-services/system.ai.claude-opus-4-9"},
                        {"name": "model-services/main.ai.claude-opus-99"},
                        {"name": "model-services/system.ai.gpt-5-5"},
                    ],
                    "next_page_token": "next",
                },
            )
        assert page_token == "next"
        return httpx.Response(
            200,
            json={
                "model_services": [
                    {"name": "model-services/system.ai.claude-opus-4-10"},
                    {"name": "model-services/system.ai.claude-sonnet-4-6"},
                    {"name": "model-services/system.ai.claude-sonnet-5"},
                    {"name": "system.ai.claude-haiku-4-5"},
                ]
            },
        )

    models = discover_databricks_claude_models(
        "https://workspace.example.com/",
        "token",
        transport=httpx.MockTransport(_handler),
    )

    assert models == {
        "opus": "system.ai.claude-opus-4-10",
        "sonnet": "system.ai.claude-sonnet-5",
        "haiku": "system.ai.claude-haiku-4-5",
    }
    assert len(requests) == 2
    assert requests[0].url.params["page_size"] == "100"


def test_anthropic_gateway_is_the_legacy_fallback() -> None:
    """A workspace without UC Claude services falls back to ``/v1/models``."""
    paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/model-services"):
            return httpx.Response(
                200,
                json={"model_services": [{"name": "model-services/system.ai.gpt-5-5"}]},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "databricks-claude-opus-4-8"},
                    {"id": "databricks-claude-3-7-sonnet"},
                    {"id": "databricks-claude-3-5-haiku"},
                    {"id": "databricks-claude-sonnet-4-6-anthropic"},
                ]
            },
        )

    models = discover_databricks_claude_models(
        "https://workspace.example.com",
        "token",
        transport=httpx.MockTransport(_handler),
    )

    assert models == {
        "opus": "databricks-claude-opus-4-8",
        "sonnet": "databricks-claude-3-7-sonnet",
        "haiku": "databricks-claude-3-5-haiku",
    }
    assert paths == [
        "/api/2.1/unity-catalog/model-services",
        "/ai-gateway/anthropic/v1/models",
    ]


def test_successful_empty_discovery_is_authoritative() -> None:
    """Two successful empty listings return empty instead of inventing models."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model-services"):
            return httpx.Response(200, json={"model_services": []})
        return httpx.Response(200, json={"data": []})

    assert (
        discover_databricks_claude_models(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        )
        == {}
    )


def test_primary_failure_can_still_use_gateway_fallback() -> None:
    """A transient model-services failure does not hide the legacy catalog."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model-services"):
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"id": "databricks-claude-haiku-4-5"}]})

    assert discover_databricks_claude_models(
        "https://workspace.example.com",
        "token",
        transport=httpx.MockTransport(_handler),
    ) == {"haiku": "databricks-claude-haiku-4-5"}


def test_primary_failure_with_empty_gateway_raises_instead_of_empty() -> None:
    """A transient UC outage plus a Claude-less gateway is NOT authoritative.

    Returning ``{}`` here would make callers treat the workspace as having no
    Claude models and hard-fail the launch; the primary failure must surface
    so they fall back to cached models instead.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model-services"):
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"data": []})

    with pytest.raises(httpx.HTTPStatusError):
        discover_databricks_claude_models(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        )


def test_primary_failure_with_non_claude_gateway_raises_instead_of_empty() -> None:
    """Same contract when the gateway answers with only non-Claude routes."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model-services"):
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"data": [{"id": "databricks-gpt-5-5"}]})

    with pytest.raises(httpx.HTTPStatusError):
        discover_databricks_claude_models(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        )


def test_truncated_pagination_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Exhausting the page budget with more pages pending logs a warning."""

    def _handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("page_token", "0")
        return httpx.Response(
            200,
            json={
                "model_services": [{"name": f"model-services/system.ai.claude-opus-{token}"}],
                "next_page_token": str(int(token) + 1),
            },
        )

    with caplog.at_level("WARNING", logger="omnigent.databricks_model_discovery"):
        models = discover_databricks_claude_models(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        )

    assert models == {"opus": "system.ai.claude-opus-99"}
    assert any("truncated" in record.message for record in caplog.records)


def test_successful_primary_empty_is_authoritative_when_legacy_is_unavailable() -> None:
    """Removed UC services do not revive stale models when legacy returns 404."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model-services"):
            return httpx.Response(200, json={"model_services": []})
        return httpx.Response(404)

    assert (
        discover_databricks_claude_models(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        )
        == {}
    )


def test_both_discovery_endpoints_failing_raises() -> None:
    """A total discovery outage is distinct from an authoritative empty list."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    with pytest.raises(httpx.HTTPStatusError):
        discover_databricks_claude_models(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        )


def test_both_discovery_endpoints_malformed_raises() -> None:
    """Malformed success payloads cannot be mistaken for removed models."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"], request=request)

    with pytest.raises(ValueError, match="must be an object"):
        discover_databricks_claude_models(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        )
