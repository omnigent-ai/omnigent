"""Tests for the Keenable web-search backend."""

from __future__ import annotations

import httpx
import pytest
import respx

from omnigent.tools.builtins.web_search_keenable import (
    _format_results,
    _search_keenable,
)


def test_format_results_uses_snippet_when_description_is_empty() -> None:
    """Keenable's page text lives in ``snippet``; empty descriptions must not
    erase the useful text returned by the API.

    Regression test for issue #5088.
    """
    data = {
        "results": [
            {
                "title": "Example",
                "url": "https://example.com",
                "description": "",
                "snippet": "Useful page text from the search result.",
            }
        ]
    }

    assert _format_results(data, 5) == (
        "1. Example\n"
        "   https://example.com\n"
        "   Useful page text from the search result."
    )


def test_format_results_prefers_description_when_present() -> None:
    """Keep compatibility with responses that provide a useful description."""
    data = {
        "results": [
            {
                "title": "Example",
                "url": "https://example.com",
                "description": "Description text.",
                "snippet": "Longer snippet text.",
            }
        ]
    }

    assert _format_results(data, 5).endswith("Description text.")


@respx.mock
def test_search_keenable_formats_snippet_from_public_api() -> None:
    """The full public-search path preserves snippet text in its output."""
    route = respx.post("https://api.keenable.ai/v1/search/public").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Pydantic",
                        "url": "https://docs.pydantic.dev/",
                        "description": "",
                        "snippet": "Data validation using Python type hints.",
                    }
                ]
            },
        )
    )

    output = _search_keenable("pydantic", {})

    assert route.called
    assert "Pydantic" in output
    assert "https://docs.pydantic.dev/" in output
    assert "Data validation using Python type hints." in output


@respx.mock
def test_search_keenable_http_error() -> None:
    """HTTP failures remain readable tool errors."""
    respx.post("https://api.keenable.ai/v1/search/public").mock(
        return_value=httpx.Response(503)
    )

    assert _search_keenable("q", {}) == "Keenable search error: HTTP 503"


@pytest.mark.parametrize(
    "payload",
    [
        {"results": []},
        {"results": [{"title": "", "url": "", "description": "", "snippet": ""}]},
    ],
)
def test_format_results_empty_content_is_stable(payload: dict[str, object]) -> None:
    """Empty result payloads retain the existing no-results contract."""
    output = _format_results(payload, 5)
    assert output == "No results found." or output.startswith("1. ")
