"""Tests for the keyless DuckDuckGo ``web_search`` backend.

Covers the parser (title/url/snippet extraction, redirect decoding, ad
skipping, whitespace, the result cap), the HTTP path (success + error
handling, mocked via ``respx``), and the selector wiring (no
``search_provider`` defaults to DuckDuckGo).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from omnigent.tools.builtins.web_search_duckduckgo import (
    _DDG_HTML_URL,
    _MAX_RESULTS,
    _decode_result_href,
    _format_results,
    _parse_results,
    _search_duckduckgo,
)

# A realistic slice of html.duckduckgo.com/html/: two organic results
# (redirect-wrapped hrefs, with ``&amp;`` entities) plus one ad whose ``y.js``
# href has no ``uddg`` target and must be skipped.
_FIXTURE_HTML = """
<div class="result result--ad">
  <a class="result__a" href="//duckduckgo.com/y.js?ad_provider=foo&amp;u3=bar">Sponsored thing</a>
  <a class="result__snippet" href="//duckduckgo.com/y.js?ad=1">An ad snippet</a>
</div>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example
       Title</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">A
     useful   snippet about example.</a>
</div>
<div class="result results_links results_links_deep web-result">
  <a class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fother.org%2Fd&amp;rut=x">Other Docs</a>
  <a class="result__snippet"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fother.org%2Fd">Docs snippet.</a>
</div>
"""


def test_decode_uddg_redirect() -> None:
    """A scheme-relative DDG redirect resolves to its decoded ``uddg`` target."""
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa+b&rut=x"
    assert _decode_result_href(href) == "https://example.com/a b"


def test_decode_direct_http_href_passthrough() -> None:
    """A direct ``http(s)`` href is returned unchanged."""
    assert _decode_result_href("https://direct.example/x") == "https://direct.example/x"


def test_decode_ad_link_returns_none() -> None:
    """An ad / JS ``y.js`` link (no ``uddg`` target) is skipped."""
    assert _decode_result_href("//duckduckgo.com/y.js?ad_provider=foo") is None


def test_parse_results_extracts_and_skips_ads() -> None:
    """Parsing yields organic results (title/url/snippet), skipping the ad,
    and normalizes whitespace in titles and snippets."""
    results = _parse_results(_FIXTURE_HTML)
    assert len(results) == 2, results  # the ad result is skipped

    first = results[0]
    assert first["title"] == "Example Title"  # newline/indentation collapsed
    assert first["url"] == "https://example.com/page"
    assert first["snippet"] == "A useful snippet about example."

    second = results[1]
    assert second["title"] == "Other Docs"
    assert second["url"] == "https://other.org/d"


def test_parse_results_caps_at_max_results() -> None:
    """No more than ``_MAX_RESULTS`` results are returned."""
    block = (
        '<a class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fe.com%2F{n}">Title {n}</a>'
        '<a class="result__snippet" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fe.com%2F{n}">snip {n}</a>'
    )
    html = "".join(block.format(n=i) for i in range(_MAX_RESULTS + 5))
    assert len(_parse_results(html)) == _MAX_RESULTS


def test_format_results_numbered_blocks() -> None:
    """Results format as numbered ``title / url / snippet`` blocks."""
    out = _format_results([{"title": "T", "url": "https://x.example", "snippet": "S"}])
    assert out == "1. T\n   https://x.example\n   S"


def test_format_results_empty() -> None:
    """No results → a clear message, not an empty string."""
    assert _format_results([]) == "No results found."


@respx.mock
def test_search_duckduckgo_success() -> None:
    """A 200 from the HTML endpoint is parsed and formatted."""
    route = respx.post(_DDG_HTML_URL).mock(return_value=httpx.Response(200, text=_FIXTURE_HTML))
    out = _search_duckduckgo("example query", {})
    assert route.called
    # Sent as a form POST with the query.
    assert b"q=example" in route.calls.last.request.content
    assert out.startswith("1. Example Title")
    assert "https://example.com/page" in out


@respx.mock
def test_search_duckduckgo_http_error() -> None:
    """A non-2xx response surfaces as a readable error, not an exception."""
    respx.post(_DDG_HTML_URL).mock(return_value=httpx.Response(503))
    assert _search_duckduckgo("q", {}) == "DuckDuckGo search error: HTTP 503"


@respx.mock
def test_search_duckduckgo_timeout() -> None:
    """A network timeout surfaces as a readable error."""
    respx.post(_DDG_HTML_URL).mock(side_effect=httpx.TimeoutException("slow"))
    out = _search_duckduckgo("q", {})
    assert out.startswith("DuckDuckGo search error")


def test_web_search_defaults_to_duckduckgo_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``search_provider``, the selector routes to the keyless DDG
    backend — so ``web_search`` works out of the box. This is the core fix:
    previously it returned a configuration error and the agent had no search."""
    import omnigent.tools.builtins.web_search_duckduckgo as ddg
    from omnigent.tools.builtins.web_search import _search

    monkeypatch.setattr(ddg, "_search_duckduckgo", lambda q, c: f"DDG:{q}")
    assert _search("hello world", {}) == "DDG:hello world"


def test_web_search_explicit_duckduckgo_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``search_provider: duckduckgo`` selects the DDG backend explicitly."""
    import omnigent.tools.builtins.web_search_duckduckgo as ddg
    from omnigent.tools.builtins.web_search import _search

    monkeypatch.setattr(ddg, "_search_duckduckgo", lambda q, c: "DDG-OK")
    assert _search("hi", {"search_provider": "duckduckgo"}) == "DDG-OK"
