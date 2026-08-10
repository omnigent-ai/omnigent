"""Tests for the unified ``web_scrape`` built-in tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins.web_scrape import WebScrapeTool

# ── Registry ─────────────────────────────────────────


def test_get_builtin_tool_returns_web_scrape() -> None:
    """``get_builtin_tool("web_scrape")`` returns a WebScrapeTool."""
    tool = get_builtin_tool("web_scrape")
    assert isinstance(tool, WebScrapeTool), f"Expected WebScrapeTool, got {type(tool).__name__}."


def test_get_builtin_tool_unknown_returns_none() -> None:
    """``get_builtin_tool`` returns ``None`` for unregistered names."""
    assert get_builtin_tool("nonexistent") is None


def test_tool_name_is_web_scrape() -> None:
    """Tool name is 'web_scrape'."""
    assert WebScrapeTool.name() == "web_scrape"


# ── Schema ───────────────────────────────────────────


def test_schema_is_function_with_required_url() -> None:
    """Schema is a standard function schema requiring a ``url`` param."""
    tool = WebScrapeTool()
    schema = tool.get_schema()
    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "web_scrape"
    assert "url" in func["parameters"]["required"]
    assert "url" in func["parameters"]["properties"]


def test_is_sync() -> None:
    """``web_scrape`` always runs synchronously in the parent tool loop."""
    assert WebScrapeTool(config={"scrape_provider": "jina"}).is_async() is False


# ── Argument validation ──────────────────────────────


def test_missing_url_returns_error(tool_ctx: ToolContext) -> None:
    """Tool returns error when the url param is missing."""
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    result = tool.invoke(json.dumps({}), tool_ctx)
    assert "Error" in result and "url" in result.lower()


def test_invalid_arguments_return_error(tool_ctx: ToolContext) -> None:
    """Malformed and non-object JSON return tool errors instead of raising."""
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    malformed = tool.invoke("{", tool_ctx)
    non_object = tool.invoke("[]", tool_ctx)
    assert "Error" in malformed and "malformed JSON" in malformed
    assert "Error" in non_object and "JSON object" in non_object


@pytest.mark.parametrize("url", [123, True, "  ", ""])
def test_invalid_url_returns_error(tool_ctx: ToolContext, url: object) -> None:
    """Empty / non-string urls are rejected before backend selection."""
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    result = tool.invoke(json.dumps({"url": url}), tool_ctx)
    assert "Error" in result and "url" in result.lower()


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "example.com"])
def test_non_http_url_rejected(tool_ctx: ToolContext, url: str) -> None:
    """Only http:// and https:// URLs are accepted."""
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    result = tool.invoke(json.dumps({"url": url}), tool_ctx)
    assert "Error" in result and "http" in result.lower()


# ── No scrape_provider set ───────────────────────────


def test_no_scrape_provider_fails_loudly(tool_ctx: ToolContext) -> None:
    """
    Without ``scrape_provider``, web_scrape returns a loud, helpful error
    naming the available engines rather than silently picking one — so it is
    always explicit which engine ran.
    """
    tool = WebScrapeTool()
    result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert result.startswith("web_scrape error: no scrape_provider")
    # The error names every available engine so the choice is explicit.
    assert "jina" in result.lower()
    assert "nimble" in result.lower()
    assert "firecrawl" in result.lower()


def test_unknown_scrape_provider_fails_loudly(tool_ctx: ToolContext) -> None:
    """An unrecognized ``scrape_provider`` names the valid engines."""
    tool = WebScrapeTool(config={"scrape_provider": "bogus"})
    result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert result.startswith("web_scrape error: unknown scrape_provider 'bogus'")
    assert "jina" in result.lower()


# ── scrape_provider: jina (keyless) ──────────────────


def test_jina_backend_keyless(tool_ctx: ToolContext) -> None:
    """
    With scrape_provider=jina and no api_key, the tool hits Jina Reader
    keyless and returns its markdown; it must NOT error on a missing key.
    """
    fake_response = MagicMock()
    fake_response.text = "# Example\n\nHello world."

    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    assert "Hello world." in result
    assert "api_key" not in result  # no "missing api_key" error


def test_jina_url_is_percent_encoded(tool_ctx: ToolContext) -> None:
    """
    The target URL is percent-encoded so its own query string is not parsed
    as r.jina.ai's — a URL with ``?q=x`` must survive intact.
    """
    fake_response = MagicMock()
    fake_response.text = "content"

    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        tool.invoke(json.dumps({"url": "https://example.com/p?q=x&n=2"}), tool_ctx)

    url = mock_get.call_args.args[0]
    headers = mock_get.call_args.kwargs["headers"]
    # The whole target URL is encoded into a single path segment (no bare '?').
    assert url.endswith("/https%3A%2F%2Fexample.com%2Fp%3Fq%3Dx%26n%3D2")
    assert headers["Accept"] == "text/markdown"
    assert "Authorization" not in headers  # keyless


def test_jina_api_key_sets_bearer(tool_ctx: ToolContext) -> None:
    """When an api_key is present, Jina gets a Bearer header to lift the rate limit."""
    fake_response = MagicMock()
    fake_response.text = "content"

    tool = WebScrapeTool(config={"scrape_provider": "jina", "api_key": "jina-key"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer jina-key"


def test_jina_rate_limit_hint(tool_ctx: ToolContext) -> None:
    """A 429 from keyless Jina suggests setting an api_key."""
    fake_response = MagicMock()
    fake_response.status_code = 429
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.side_effect = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=fake_response
        )
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "429" in result
    assert "api_key" in result


def test_jina_empty_content_message(tool_ctx: ToolContext) -> None:
    """Empty Jina output yields the no-content message, not a blank string."""
    fake_response = MagicMock()
    fake_response.text = "   "
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "no content extracted" in result


# ── scrape_provider: nimble ──────────────────────────


def test_nimble_backend_via_spec_config(tool_ctx: ToolContext) -> None:
    """With scrape_provider=nimble and api_key, the tool returns extracted content."""
    fake_response = MagicMock()
    # Nimble nests the body under ``data`` — markdown for the markdown format.
    fake_response.json.return_value = {"data": {"markdown": "The page body."}}

    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "nimble-key"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    assert "The page body." in result


def test_nimble_reads_parsing_content(tool_ctx: ToolContext) -> None:
    """A parsed extract under ``data.parsing.content`` is read too."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"parsing": {"content": "Parsed body."}}}

    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    assert "Parsed body." in result


def test_nimble_requests_formats_array_and_main_content(tool_ctx: ToolContext) -> None:
    """
    The request body uses Nimble's ``formats`` array and asks for the
    Readability ``main_content`` markdown backend (not a scalar output_format).
    """
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"markdown": "body"}}

    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    body = mock_post.call_args.kwargs["json"]
    assert body["formats"] == ["markdown"]
    assert body["markdown_backend"] == "main_content"
    assert "output_format" not in body


def test_nimble_missing_key_returns_error(tool_ctx: ToolContext) -> None:
    """With scrape_provider=nimble but no api_key, returns error."""
    tool = WebScrapeTool(config={"scrape_provider": "nimble"})
    result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "api_key" in result


def test_nimble_spec_config_used_in_http_call(tool_ctx: ToolContext) -> None:
    """api_key is a Bearer header; the body carries url / driver / render."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"markdown": "body"}}

    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "spec-nimble"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer spec-nimble"
    body = mock_post.call_args.kwargs["json"]
    assert body["url"] == "https://example.com"
    # Default driver vx8 renders + adds anti-bot.
    assert body["driver"] == "vx8"
    assert body["render"] is True


def test_nimble_sends_x_client_source_header(tool_ctx: ToolContext) -> None:
    """Every request carries the ``X-Client-Source`` header identifying Omnigent."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"markdown": "body"}}

    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "spec-nimble"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["X-Client-Source"] == "omnigent"


def test_nimble_rejects_unsupported_driver(tool_ctx: ToolContext) -> None:
    """An unsupported ``driver`` is rejected with a clear error, no HTTP call."""
    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "k", "driver": "vx99"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "driver" in result
    assert mock_post.call_count == 0, "Must not call the API for an invalid driver."


def test_nimble_vx6_disables_render(tool_ctx: ToolContext) -> None:
    """The plain-HTTP driver vx6 does not turn on the browser render."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"content": "body"}

    fake_response.json.return_value = {"data": {"markdown": "body"}}
    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "k", "driver": "vx6"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    body = mock_post.call_args.kwargs["json"]
    assert body["driver"] == "vx6"
    assert body["render"] is False
    # vx6 is plain HTTP — no markdown_backend needed, but still markdown format.
    assert body["formats"] == ["markdown"]


def test_nimble_http_error_returns_error_string(tool_ctx: ToolContext) -> None:
    """An HTTP error (e.g. 401) is returned as a string, never raised."""
    fake_response = MagicMock()
    fake_response.status_code = 401
    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=fake_response
        )
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "Nimble scrape error" in result
    assert "401" in result


def test_nimble_detects_block_page(tool_ctx: ToolContext) -> None:
    """A short body that is only a captcha/denied marker is reported as blocked."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"markdown": "Access denied. Are you a robot?"}}

    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    assert "bot-protected" in result


def test_nimble_block_detection_boundary_at_500(tool_ctx: ToolContext) -> None:
    """A body >= 500 chars is NOT block-flagged even if it mentions a marker."""
    # A legitimate article that merely discusses captchas, padded past 500 chars.
    body = "This article explains how captcha systems work. " + ("detail " * 80)
    assert len(body) >= 500
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"markdown": body}}

    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    assert "bot-protected" not in result
    assert "captcha systems work" in result


def test_nimble_empty_content_suggests_stronger_driver(tool_ctx: ToolContext) -> None:
    """Empty extracted content suggests trying a stronger driver."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"markdown": ""}}

    tool = WebScrapeTool(config={"scrape_provider": "nimble", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_nimble.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    assert "no content extracted" in result
    assert "vx10" in result


# ── scrape_provider: firecrawl ───────────────────────


def test_firecrawl_backend_via_spec_config(tool_ctx: ToolContext) -> None:
    """With scrape_provider=firecrawl and api_key, the tool returns the markdown."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"success": True, "data": {"markdown": "# Page\n\nBody."}}

    tool = WebScrapeTool(config={"scrape_provider": "firecrawl", "api_key": "fc-key"})
    with patch("omnigent.tools.builtins.web_scrape_firecrawl.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    assert "Body." in result


def test_firecrawl_missing_key_returns_error(tool_ctx: ToolContext) -> None:
    """With scrape_provider=firecrawl but no api_key, returns error."""
    tool = WebScrapeTool(config={"scrape_provider": "firecrawl"})
    result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "api_key" in result


def test_firecrawl_spec_config_used_in_http_call(tool_ctx: ToolContext) -> None:
    """api_key is a Bearer header; the body carries url / formats / proxy."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"success": True, "data": {"markdown": "body"}}

    tool = WebScrapeTool(config={"scrape_provider": "firecrawl", "api_key": "spec-fc"})
    with patch("omnigent.tools.builtins.web_scrape_firecrawl.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer spec-fc"
    body = mock_post.call_args.kwargs["json"]
    assert body["url"] == "https://example.com"
    assert body["formats"] == ["markdown"]
    # Default proxy escalates on block.
    assert body["proxy"] == "auto"


def test_firecrawl_rejects_unsupported_proxy(tool_ctx: ToolContext) -> None:
    """An unsupported ``proxy`` is rejected with a clear error, no HTTP call."""
    tool = WebScrapeTool(config={"scrape_provider": "firecrawl", "api_key": "k", "proxy": "turbo"})
    with patch("omnigent.tools.builtins.web_scrape_firecrawl.httpx.post") as mock_post:
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "proxy" in result
    assert mock_post.call_count == 0, "Must not call the API for an invalid proxy."


def test_firecrawl_accepts_enhanced_proxy(tool_ctx: ToolContext) -> None:
    """``enhanced`` is a valid proxy tier (the residential/harder-target one)."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"success": True, "data": {"markdown": "body"}}
    tool = WebScrapeTool(
        config={"scrape_provider": "firecrawl", "api_key": "k", "proxy": "enhanced"}
    )
    with patch("omnigent.tools.builtins.web_scrape_firecrawl.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert mock_post.call_args.kwargs["json"]["proxy"] == "enhanced"


def test_firecrawl_missing_success_treated_as_failure(tool_ctx: ToolContext) -> None:
    """A response with no ``success`` field is treated as a failure, not content."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"markdown": "leaked"}}  # no "success"
    tool = WebScrapeTool(config={"scrape_provider": "firecrawl", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_firecrawl.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "was not scraped successfully" in result


def test_firecrawl_http_error_returns_error_string(tool_ctx: ToolContext) -> None:
    """An HTTP error (e.g. 402 quota) is returned as a string, never raised."""
    fake_response = MagicMock()
    fake_response.status_code = 402
    tool = WebScrapeTool(config={"scrape_provider": "firecrawl", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_firecrawl.httpx.post") as mock_post:
        mock_post.side_effect = httpx.HTTPStatusError(
            "402", request=MagicMock(), response=fake_response
        )
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "Firecrawl scrape error" in result
    assert "402" in result


def test_firecrawl_empty_content_message(tool_ctx: ToolContext) -> None:
    """Empty Firecrawl markdown yields the no-content message."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"success": True, "data": {"markdown": ""}}
    tool = WebScrapeTool(config={"scrape_provider": "firecrawl", "api_key": "k"})
    with patch("omnigent.tools.builtins.web_scrape_firecrawl.httpx.post") as mock_post:
        mock_post.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "no content extracted" in result


# ── Finalize: source header + central truncation ─────


def test_success_prepends_source_header(tool_ctx: ToolContext) -> None:
    """A successful scrape is prefixed with a ``Source: <url>`` line for grounding."""
    fake_response = MagicMock()
    fake_response.text = "The article body."
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com/a"}), tool_ctx)
    assert result.startswith("Source: https://example.com/a\n\n")
    assert result.endswith("The article body.")


def test_error_returns_have_no_source_header(tool_ctx: ToolContext) -> None:
    """Error/notice returns are passed through verbatim (no Source header)."""
    fake_response = MagicMock()
    fake_response.text = "   "  # empty → no-content notice
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert not result.startswith("Source:")
    assert result.startswith("web_scrape:")


def test_content_truncated_centrally(tool_ctx: ToolContext) -> None:
    """Content beyond the cap is truncated once, centrally, with a marker."""
    huge = "x" * 60_000
    fake_response = MagicMock()
    fake_response.text = huge
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert result.endswith("[content truncated]")
    assert "x" * 50_000 in result
    assert len(result) < 60_000  # actually shortened


def test_unicode_content_preserved(tool_ctx: ToolContext) -> None:
    """Multibyte unicode (CJK, emoji, RTL) survives intact."""
    body = "# 你好\n\nHello 🌍 مرحبا"
    fake_response = MagicMock()
    fake_response.text = body
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.return_value = fake_response
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert "你好" in result and "🌍" in result and "مرحبا" in result


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.TooManyRedirects("loop"),
        httpx.RemoteProtocolError("bad frame"),
    ],
)
def test_jina_request_errors_never_raise(tool_ctx: ToolContext, exc: Exception) -> None:
    """Every httpx.RequestError subclass is caught and returned as a string."""
    tool = WebScrapeTool(config={"scrape_provider": "jina"})
    with patch("omnigent.tools.builtins.web_scrape_jina.httpx.get") as mock_get:
        mock_get.side_effect = exc
        result = tool.invoke(json.dumps({"url": "https://example.com"}), tool_ctx)
    assert result.startswith("Jina scrape error:")
