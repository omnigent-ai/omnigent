"""Tests for the keyless DuckDuckGo ``web_search`` backend.

Covers the parser (title/url/snippet extraction, redirect decoding, ad
skipping, whitespace, the result cap), the HTTP path (success + error
handling, mocked via ``respx``), and the selector wiring (no
``search_provider`` defaults to DuckDuckGo).
"""

from __future__ import annotations

import pathlib

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

# A real captured html.duckduckgo.com/html/ body (query "wikipedia",
# captured 2026-06). Re-capture with tests/tools/fixtures/refresh_ddg_fixture.py
# when the live drift canary (tests/e2e_live/) goes red.
_DDG_GOLDEN = (pathlib.Path(__file__).parent / "fixtures" / "ddg_html_2026-06.html").read_text(
    encoding="utf-8"
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


def test_web_search_no_provider_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``search_provider``, the selector returns a loud, helpful error
    naming the engines — it does NOT silently pick one (per maintainer review),
    so it's always explicit which engine ran. The DDG backend is not invoked."""
    import omnigent.tools.builtins.web_search_duckduckgo as ddg
    from omnigent.tools.builtins.web_search import _search

    monkeypatch.setattr(
        ddg, "_search_duckduckgo", lambda q, c: pytest.fail("must not auto-run DDG")
    )
    out = _search("hello world", {})
    assert out.startswith("web_search error: no search_provider")
    assert "duckduckgo" in out


def test_web_search_explicit_duckduckgo_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``search_provider: duckduckgo`` selects the DDG backend explicitly."""
    import omnigent.tools.builtins.web_search_duckduckgo as ddg
    from omnigent.tools.builtins.web_search import _search

    monkeypatch.setattr(ddg, "_search_duckduckgo", lambda q, c: "DDG-OK")
    assert _search("hi", {"search_provider": "duckduckgo"}) == "DDG-OK"


# ── robustness: HTML scraping is best-effort, so it must degrade, never crash ──


@respx.mock
@pytest.mark.parametrize(
    "exc",
    [
        httpx.RemoteProtocolError("peer closed connection"),
        httpx.ReadError("connection reset"),
        httpx.DecodingError("bad gzip"),
    ],
)
def test_search_duckduckgo_transport_errors_no_raise(exc: httpx.HTTPError) -> None:
    """A flaky/rate-limiting DDG endpoint raises RemoteProtocol/Read/Decoding
    errors — none of which are ``TransportError`` — and they must surface as a
    readable string, not crash the tool. (Regression: the old narrow catch let
    these escape.)"""
    respx.post(_DDG_HTML_URL).mock(side_effect=exc)
    assert _search_duckduckgo("q", {}).startswith("DuckDuckGo search error")


@respx.mock
def test_search_duckduckgo_connect_error() -> None:
    """A connect failure still surfaces as a readable error (broadened catch)."""
    respx.post(_DDG_HTML_URL).mock(side_effect=httpx.ConnectError("no route"))
    assert _search_duckduckgo("q", {}).startswith("DuckDuckGo search error")


@respx.mock
def test_search_duckduckgo_rate_limit_429() -> None:
    """The 429 throttle still surfaces its status number (HTTPStatusError stays
    distinct from the broadened transport catch)."""
    respx.post(_DDG_HTML_URL).mock(return_value=httpx.Response(429))
    assert _search_duckduckgo("q", {}) == "DuckDuckGo search error: HTTP 429"


@respx.mock
def test_search_duckduckgo_blocked_200_is_empty() -> None:
    """A 200 block/anomaly page (no ``result__a``) yields "No results found." —
    we intentionally do NOT phrase-match block pages, so it's indistinguishable
    from a genuine zero-result query (best-effort contract)."""
    blocked = (
        "<html><body><div class='no-results'>"
        "If this persists, please let us know. Performed automatically."
        "</div></body></html>"
    )
    respx.post(_DDG_HTML_URL).mock(return_value=httpx.Response(200, text=blocked))
    assert _search_duckduckgo("q", {}) == "No results found."


@respx.mock
def test_search_duckduckgo_empty_body() -> None:
    """An empty 200 body degrades to "No results found." over the full path."""
    respx.post(_DDG_HTML_URL).mock(return_value=httpx.Response(200, text=""))
    assert _search_duckduckgo("q", {}) == "No results found."


@respx.mock
def test_search_duckduckgo_garbage_body() -> None:
    """Garbage HTML degrades gracefully (never raises) over the full path."""
    respx.post(_DDG_HTML_URL).mock(
        return_value=httpx.Response(200, text="<<garbage && >> not html")
    )
    assert _search_duckduckgo("q", {}) == "No results found."


@respx.mock
def test_search_duckduckgo_caps_rendered_output_at_max_results() -> None:
    """More than _MAX_RESULTS results render only the first _MAX_RESULTS — the
    cap is tied to the rendered string, not just the parsed list."""
    block = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fe.com%2F{n}">'
        "Title {n}</a>"
        '<a class="result__snippet" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fe.com%2F{n}">snip {n}</a>'
    )
    html = "".join(block.format(n=i) for i in range(_MAX_RESULTS + 5))
    respx.post(_DDG_HTML_URL).mock(return_value=httpx.Response(200, text=html))
    out = _search_duckduckgo("q", {})
    assert f"{_MAX_RESULTS}. " in out
    assert f"{_MAX_RESULTS + 1}. " not in out


@respx.mock
def test_search_duckduckgo_sends_browser_user_agent() -> None:
    """The request carries a browser-like UA — the endpoint blocks/empties
    requests without one, so this is a load-bearing contract."""
    route = respx.post(_DDG_HTML_URL).mock(return_value=httpx.Response(200, text=_FIXTURE_HTML))
    _search_duckduckgo("q", {})
    assert route.calls.last.request.headers["user-agent"].startswith("Mozilla/5.0")


def test_parse_results_empty_and_garbage() -> None:
    """The parser returns [] (never raises) for empty or non-HTML input."""
    assert _parse_results("") == []
    assert _parse_results("<<< not really html & broken >>>") == []


def test_parse_results_truncated_html_no_raise() -> None:
    """Truncated markup (stream cut mid-result) degrades gracefully — no raise,
    at most a partial result; it never crashes search."""
    html = (
        '<div class="result"><a class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com">Titl'
    )
    results = _parse_results(html)  # must not raise
    assert len(results) <= 1


def test_decode_result_href_preserves_encoded_ampersand_in_target() -> None:
    """A redirect whose target has an encoded ``&`` (``%26``) plus a literal
    ``&amp;`` separator decodes to the correct URL — the query inside the
    target survives."""
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fx%26y%3D1&amp;rut=z"
    assert _decode_result_href(href) == "https://a.com/x&y=1"


# Verbatim slice of a LIVE html.duckduckgo.com/html/ response (captured
# 2026-06). Note the current markup drift the parser must tolerate:
#   * href is now a DIRECT url, NOT the //duckduckgo.com/l/?uddg= redirect
#   * result__a carries rel="nofollow"
#   * snippets embed <b>...</b> highlight tags
_LIVE_DIRECT_HTML = (
    '<div class="result results_links results_links_deep web-result">'
    '<a rel="nofollow" class="result__a" href="https://claude.ai/">'
    "Sign in - Claude</a>"
    '<a class="result__snippet" href="https://claude.ai/">'
    "<b>Claude</b> is an AI assistant by <b>Anthropic</b>, "
    "designed to assist with creative tasks.</a>"
    "</div>"
)


def test_parse_results_live_direct_url_markup() -> None:
    """Parse the shape DDG actually serves today (verified live): a direct
    ``href`` (no ``/l/?uddg=`` redirect), ``rel="nofollow"`` on ``result__a``,
    and ``<b>`` highlight tags inside the snippet. The parser passes the direct
    URL through, ignores ``rel``, and strips the bold tags to plain text.

    This is the regression guard for the exact markup drift the reviewer flagged
    as fragile — it already happened, and the backend handles it."""
    results = _parse_results(_LIVE_DIRECT_HTML)
    assert len(results) == 1
    assert results[0]["url"] == "https://claude.ai/"
    assert results[0]["title"] == "Sign in - Claude"
    assert "<b>" not in results[0]["snippet"]
    assert results[0]["snippet"] == (
        "Claude is an AI assistant by Anthropic, designed to assist with creative tasks."
    )


def test_parse_results_returns_partial_when_parser_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the underlying HTMLParser ever raises mid-stream, ``_parse_results``
    must not propagate it — it returns whatever was parsed before the fault.
    (``html.parser`` is tolerant enough that no real input triggers this, so we
    inject the failure to prove the H2 guard.)"""
    import omnigent.tools.builtins.web_search_duckduckgo as ddg

    def _boom(self: ddg._ResultParser, data: str) -> None:
        self.results.append({"title": "Partial", "url": "https://a.com", "snippet": "s"})
        raise RuntimeError("parser blew up mid-stream")

    monkeypatch.setattr(ddg._ResultParser, "feed", _boom)
    out = ddg._parse_results("<anything>")  # must not raise
    assert out == [{"title": "Partial", "url": "https://a.com", "snippet": "s"}]


# ── recorded real-response fixture: parse a genuine captured DDG page offline ──


def test_parse_real_captured_ddg_page_structure() -> None:
    """Parse a REAL captured html.duckduckgo.com/html/ body (captured 2026-06).
    Asserts structure, not upstream content, so it can't flake on result churn
    — but it WILL catch a markup change that the inline fixtures don't model."""
    results = _parse_results(_DDG_GOLDEN)
    assert len(results) >= 5, f"expected several organic results, got {len(results)}"
    for r in results:
        assert r["title"], r  # non-empty title
        assert r["url"].startswith(("http://", "https://")), r["url"]
        assert "duckduckgo.com/l/" not in r["url"], r["url"]  # redirect fully decoded
        assert "/y.js" not in r["url"], r["url"]  # no ad / JS link leaked
        assert "<b>" not in r["snippet"], r["snippet"]  # highlight tags stripped


@respx.mock
def test_search_duckduckgo_over_real_captured_page() -> None:
    """The full HTTP path renders the real captured body to numbered blocks."""
    respx.post(_DDG_HTML_URL).mock(return_value=httpx.Response(200, text=_DDG_GOLDEN))
    out = _search_duckduckgo("wikipedia", {})
    assert out.startswith("1. ")
    assert out != "No results found."


def test_parse_results_snippet_in_non_anchor_tag() -> None:
    """Drift guard (parser hardening): the snippet is captured by CSS class even
    when DDG moves it out of an ``<a>`` into a ``<div>`` (with nested children),
    and capture ends only when that ``<div>`` closes — not an inner one."""
    html = (
        '<a class="result__a" href="https://example.com/p">Title</a>'
        '<div class="result__snippet">A snippet '
        "<div>with a nested wrapper</div> and a tail.</div>"
    )
    results = _parse_results(html)
    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/p"
    assert results[0]["title"] == "Title"
    assert results[0]["snippet"] == "A snippet with a nested wrapper and a tail."
