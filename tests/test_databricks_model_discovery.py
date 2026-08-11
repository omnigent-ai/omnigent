"""Tests for live Databricks Claude model discovery."""

from __future__ import annotations

import httpx
import pytest

from omnigent.databricks_model_discovery import discover_databricks_claude_catalog

# One-shot stubs for the two discovery endpoints: ``(status, json_payload)``,
# with a ``None`` payload meaning "no body" (a bare error response).
_Stub = tuple[int, object | None]

_UC_EMPTY: _Stub = (200, {"model_services": []})
_UC_DOWN: _Stub = (503, None)
_GATEWAY_EMPTY: _Stub = (200, {"data": []})
_GATEWAY_GONE: _Stub = (404, None)


def _discover(uc: _Stub, gateway: _Stub) -> object:
    """Run discovery against canned UC / legacy-gateway responses."""

    def _handler(request: httpx.Request) -> httpx.Response:
        status, payload = uc if request.url.path.endswith("/model-services") else gateway
        if payload is None:
            return httpx.Response(status, request=request)
        return httpx.Response(status, json=payload, request=request)

    return discover_databricks_claude_catalog(
        "https://workspace.example.com",
        "token",
        transport=httpx.MockTransport(_handler),
    )


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

    families = discover_databricks_claude_catalog(
        "https://workspace.example.com/",
        "token",
        transport=httpx.MockTransport(_handler),
    ).families

    assert families == {
        "opus": "system.ai.claude-opus-4-10",
        "sonnet": "system.ai.claude-sonnet-5",
        "haiku": "system.ai.claude-haiku-4-5",
    }
    # Both UC pages, then the legacy gateway (always consulted so the spelling
    # preference is deterministic).
    assert len(requests) == 3
    assert requests[0].url.params["max_results"] == "1000"
    assert requests[0].url.params["parent"] == "schemas/system.ai"


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

    families = discover_databricks_claude_catalog(
        "https://workspace.example.com",
        "token",
        transport=httpx.MockTransport(_handler),
    ).families

    assert families == {
        "opus": "databricks-claude-opus-4-8",
        "sonnet": "databricks-claude-3-7-sonnet",
        "haiku": "databricks-claude-3-5-haiku",
    }
    assert paths == [
        "/api/2.1/unity-catalog/model-services",
        "/ai-gateway/anthropic/v1/models",
    ]


@pytest.mark.parametrize(
    ("uc", "gateway", "expected_families", "expected_model_ids"),
    [
        # Two successful empty listings return empty instead of inventing models.
        (_UC_EMPTY, _GATEWAY_EMPTY, {}, ()),
        # A transient model-services failure does not hide the legacy catalog.
        (
            _UC_DOWN,
            (200, {"data": [{"id": "databricks-claude-haiku-4-5"}]}),
            {"haiku": "databricks-claude-haiku-4-5"},
            ("databricks-claude-haiku-4-5",),
        ),
        # Removed UC services do not revive stale models when legacy 404s.
        (_UC_EMPTY, _GATEWAY_GONE, {}, ()),
        # The family picks drop older generations; the catalog keeps them.
        (
            (
                200,
                {
                    "model_services": [
                        {"name": "model-services/system.ai.claude-opus-5"},
                        {"name": "model-services/system.ai.claude-opus-4-8"},
                        {"name": "model-services/system.ai.claude-sonnet-5"},
                        {"name": "model-services/system.ai.gpt-5-5"},
                    ]
                },
            ),
            _GATEWAY_EMPTY,
            {"opus": "system.ai.claude-opus-5", "sonnet": "system.ai.claude-sonnet-5"},
            (
                "system.ai.claude-sonnet-5",
                "system.ai.claude-opus-5",
                "system.ai.claude-opus-4-8",
            ),
        ),
        # The gateway fallback reports its whole Claude list too.
        (
            _UC_EMPTY,
            (
                200,
                {
                    "data": [
                        {"id": "databricks-claude-opus-5"},
                        {"id": "databricks-claude-opus-4-8"},
                        {"id": "databricks-gpt-5-5"},
                    ]
                },
            ),
            {"opus": "databricks-claude-opus-5"},
            ("databricks-claude-opus-5", "databricks-claude-opus-4-8"),
        ),
        # An authoritative listing with no Claude at all yields an empty
        # catalog, not an error.
        (_UC_EMPTY, (200, {"data": [{"id": "databricks-gpt-5-5"}]}), {}, ()),
    ],
)
def test_discovered_catalog_from_the_two_endpoints(
    uc: _Stub,
    gateway: _Stub,
    expected_families: dict[str, str],
    expected_model_ids: tuple[str, ...],
) -> None:
    catalog = _discover(uc, gateway)
    assert catalog.families == expected_families  # type: ignore[attr-defined]
    assert catalog.model_ids == expected_model_ids  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("uc", "gateway", "exc", "match"),
    [
        # A transient UC outage plus a Claude-less gateway is NOT authoritative.
        # Returning ``{}`` here would make callers treat the workspace as having
        # no Claude models and hard-fail the launch; the primary failure must
        # surface so they fall back to cached models instead.
        (_UC_DOWN, _GATEWAY_EMPTY, httpx.HTTPStatusError, None),
        # Same contract when the gateway answers with only non-Claude routes.
        (_UC_DOWN, (200, {"data": [{"id": "databricks-gpt-5-5"}]}), httpx.HTTPStatusError, None),
        # A total discovery outage is distinct from an authoritative empty list.
        (_UC_DOWN, (503, None), httpx.HTTPStatusError, None),
        # Malformed success payloads cannot be mistaken for removed models.
        (
            (200, ["not", "an", "object"]),
            (200, ["not", "an", "object"]),
            ValueError,
            "must be an object",
        ),
    ],
)
def test_discovery_failures_raise_instead_of_reporting_empty(
    uc: _Stub,
    gateway: _Stub,
    exc: type[Exception],
    match: str | None,
) -> None:
    with pytest.raises(exc, match=match):
        _discover(uc, gateway)


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
        families = discover_databricks_claude_catalog(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        ).families

    assert families == {"opus": "system.ai.claude-opus-99"}
    assert any("truncated" in record.message for record in caplog.records)


def test_duplicate_spellings_collapse_onto_the_databricks_id() -> None:
    """A workspace serving both spellings names each model one way, every time."""
    catalog = _discover(
        (
            200,
            {
                "model_services": [
                    {"name": "model-services/system.ai.claude-opus-5"},
                    {"name": "model-services/system.ai.claude-opus-4-8"},
                    {"name": "model-services/system.ai.claude-sonnet-5"},
                ]
            },
        ),
        (
            200,
            {
                "data": [
                    {"id": "databricks-claude-opus-5"},
                    {"id": "databricks-claude-opus-4-8"},
                    {"id": "databricks-claude-sonnet-5"},
                ]
            },
        ),
    )
    assert catalog.families == {  # type: ignore[attr-defined]
        "opus": "databricks-claude-opus-5",
        "sonnet": "databricks-claude-sonnet-5",
    }
    assert catalog.model_ids == (  # type: ignore[attr-defined]
        "databricks-claude-sonnet-5",
        "databricks-claude-opus-5",
        "databricks-claude-opus-4-8",
    )


def test_a_model_only_unity_catalog_serves_keeps_its_own_spelling() -> None:
    catalog = _discover(
        (200, {"model_services": [{"name": "model-services/system.ai.claude-haiku-4-5"}]}),
        (200, {"data": [{"id": "databricks-claude-opus-5"}]}),
    )
    assert catalog.families == {  # type: ignore[attr-defined]
        "opus": "databricks-claude-opus-5",
        "haiku": "system.ai.claude-haiku-4-5",
    }


def test_family_shim_warns_and_delegates_to_the_catalog() -> None:
    from omnigent.databricks_model_discovery import discover_databricks_claude_models

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = (
            {"model_services": [{"name": "model-services/system.ai.claude-opus-5"}]}
            if request.url.path.endswith("/model-services")
            else {"data": []}
        )
        return httpx.Response(200, json=payload, request=request)

    with pytest.warns(DeprecationWarning, match="v0.10.0"):
        families = discover_databricks_claude_models(
            "https://workspace.example.com",
            "token",
            transport=httpx.MockTransport(_handler),
        )
    assert families == {"opus": "system.ai.claude-opus-5"}
