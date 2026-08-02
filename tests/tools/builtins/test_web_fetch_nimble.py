"""Tests for the Nimble Extract-backed ``web_fetch`` backend."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from omnigent.tools.builtins.web_fetch_nimble import (
    _fetch_nimble,
    _format_extract,
    _resolve_render,
)


def test_missing_api_key_returns_error() -> None:
    """Without api_key the backend returns a clear error, never raises."""
    result = _fetch_nimble("https://example.com", {})
    assert "api_key" in result


def test_extract_sends_bearer_client_source_and_body() -> None:
    """api_key → Bearer header, X-Client-Source is set, body carries url/render/formats."""
    fake = MagicMock()
    fake.json.return_value = {
        "data": {"markdown": "# Doc"},
        "url": "https://example.com",
        "status": "success",
    }
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake
        _fetch_nimble("https://example.com", {"api_key": "spec-key"})

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer spec-key", (
        f"Expected spec config api_key in header, got {headers['Authorization']!r}"
    )
    assert headers["X-Client-Source"] == "omnigent", (
        f"Expected X-Client-Source 'omnigent', got {headers.get('X-Client-Source')!r}"
    )
    body = mock_post.call_args.kwargs["json"]
    assert body["url"] == "https://example.com"
    assert body["render"] is True
    assert body["formats"] == ["markdown"]


def test_markdown_preferred_and_source_prefixed() -> None:
    """Markdown is preferred over HTML and the final URL is prefixed as a source."""
    fake = MagicMock()
    fake.json.return_value = {
        "data": {"markdown": "# Title\ntext", "html": "<h1>Title</h1>"},
        "url": "https://example.com",
    }
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = _fetch_nimble("https://example.com", {"api_key": "k"})

    assert "# Title" in result
    assert "<h1>" not in result, "Markdown must be preferred over HTML."
    assert "https://example.com" in result


def test_html_fallback_when_no_markdown() -> None:
    """When markdown is absent, HTML is used as the fallback body."""
    fake = MagicMock()
    fake.json.return_value = {
        "data": {"markdown": None, "html": "<p>Body</p>"},
        "url": "https://example.com",
    }
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = _fetch_nimble("https://example.com", {"api_key": "k"})

    assert "<p>Body</p>" in result


def test_no_content_returns_status_message() -> None:
    """An empty extraction returns a short status message (with the status)."""
    fake = MagicMock()
    fake.json.return_value = {
        "data": {"markdown": None, "html": None},
        "url": "https://example.com",
        "status": "blocked",
    }
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = _fetch_nimble("https://example.com", {"api_key": "k"})

    assert "No content" in result
    assert "blocked" in result


def test_http_error_returns_string_not_raised() -> None:
    """An HTTP error (e.g. 403) is returned as a string, never raised."""
    fake = MagicMock()
    fake.status_code = 403
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.side_effect = httpx.HTTPStatusError("403", request=MagicMock(), response=fake)
        result = _fetch_nimble("https://example.com", {"api_key": "k"})

    assert "Nimble extract error" in result
    assert "403" in result


def test_connect_error_returns_string() -> None:
    """A transport error is returned as a string, never raised."""
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.side_effect = httpx.ConnectError("boom")
        result = _fetch_nimble("https://example.com", {"api_key": "k"})

    assert "Nimble extract error" in result


def test_country_added_when_present() -> None:
    """A ``country`` config value is forwarded to the request body for geo-targeting."""
    fake = MagicMock()
    fake.json.return_value = {"data": {"markdown": "x"}, "url": "https://example.com"}
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake
        _fetch_nimble("https://example.com", {"api_key": "k", "country": "US"})

    body = mock_post.call_args.kwargs["json"]
    assert body["country"] == "US"


def test_country_omitted_by_default() -> None:
    """No ``country`` → no geo field in the body (non-enterprise-safe default)."""
    fake = MagicMock()
    fake.json.return_value = {"data": {"markdown": "x"}, "url": "https://example.com"}
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake
        _fetch_nimble("https://example.com", {"api_key": "k"})

    assert "country" not in mock_post.call_args.kwargs["json"]


def test_render_config_parsing() -> None:
    """``render`` config (a str from the spec parser) is coerced; default is True."""
    assert _resolve_render({}) is True
    assert _resolve_render({"render": "true"}) is True
    assert _resolve_render({"render": "false"}) is False
    assert _resolve_render({"render": "0"}) is False
    assert _resolve_render({"render": "no"}) is False
    assert _resolve_render({"render": "off"}) is False


def test_render_false_sent_in_body() -> None:
    """``render: false`` reaches the request body as a bool False."""
    fake = MagicMock()
    fake.json.return_value = {"data": {"markdown": "x"}, "url": "https://example.com"}
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake
        _fetch_nimble("https://example.com", {"api_key": "k", "render": "false"})

    assert mock_post.call_args.kwargs["json"]["render"] is False


def test_truncation_caps_content() -> None:
    """A very large page is truncated so it cannot blow the model context."""
    fake = MagicMock()
    fake.json.return_value = {"data": {"markdown": "x" * 60_000}, "url": "https://example.com"}
    with patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = _fetch_nimble("https://example.com", {"api_key": "k"})

    assert "[content truncated]" in result
    assert len(result) < 55_000


def test_format_extract_direct() -> None:
    """``_format_extract`` prefers markdown and prefixes the final URL."""
    out = _format_extract(
        {"data": {"markdown": "hello"}, "url": "https://final.example"}, "https://req.example"
    )
    assert out == "Source: https://final.example\n\nhello"
