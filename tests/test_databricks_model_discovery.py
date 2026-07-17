"""Tests for live Databricks Claude model discovery."""

from __future__ import annotations

import httpx

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
                    {"id": "databricks-claude-sonnet-4-6"},
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
        "sonnet": "databricks-claude-sonnet-4-6",
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
