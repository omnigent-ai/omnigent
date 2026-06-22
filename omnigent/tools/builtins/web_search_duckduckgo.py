"""Built-in tool backend: zero-key DuckDuckGo web search.

Searches DuckDuckGo's HTML endpoint (``html.duckduckgo.com/html/``) over
plain HTTP — **no API key required**. This is the keyless DEFAULT backend so
``web_search`` works out of the box (e.g. for research); agents that want a
sturdier, higher-rate backend can still set ``search_provider`` to
``google`` / ``perplexity`` / ``nimble`` with credentials.

Why DuckDuckGo specifically: of the major engines, its HTML endpoint is the
one that tolerates a plain request and returns parseable results without a
key or a JS/anti-bot wall (at modest volume). Google/Bing block scraping and
require a paid API. The trade-off: this endpoint is best-effort — it can
rate-limit or change its markup — so it is the zero-setup fallback, not a
guaranteed-robust backend. Without this, an agent with no search credentials
has no way to search and may escalate to a heavyweight browser tool.

Parsing uses the stdlib :mod:`html.parser` — no ``lxml`` / ``bs4`` dependency.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

_logger = logging.getLogger(__name__)

_DDG_HTML_URL = "https://html.duckduckgo.com/html/"

# Cap on results returned, matching the other backends.
_MAX_RESULTS: int = 10

# The HTML endpoint returns an empty body / blocks requests that lack a
# browser-like User-Agent, so send a realistic one.
_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _decode_result_href(href: str) -> str | None:
    """Resolve a DuckDuckGo result ``href`` to the real target URL.

    DDG wraps result links as redirects of the form
    ``//duckduckgo.com/l/?uddg=<url-encoded-target>&rut=...``; the real URL is
    the ``uddg`` query parameter. A direct ``http(s)`` href is returned as-is.
    Anything else (ad / JS ``y.js`` links, fragments) yields ``None`` so the
    caller skips it.

    :param href: The raw ``href`` from a ``result__a`` anchor.
    :returns: An ``http(s)`` URL, or ``None`` to skip this link.
    """
    if href.startswith(("http://", "https://")):
        return href
    # Scheme-relative DDG redirect (``//duckduckgo.com/l/?uddg=...``).
    normalized = "https:" + href if href.startswith("//") else href
    uddg = parse_qs(urlsplit(normalized).query).get("uddg")
    if uddg:
        target = unquote(uddg[0])
        if target.startswith(("http://", "https://")):
            return target
    return None


class _ResultParser(HTMLParser):
    """Extract ``(title, url, snippet)`` triples from DDG HTML results.

    DDG renders each result as an ``<a class="result__a" href=redirect>title``
    anchor followed by an ``<a class="result__snippet">snippet`` anchor. We
    capture anchor text by CSS class and attach the snippet to the most recent
    result. Ad / JS links (no decodable target) are skipped.
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        # Which anchor we are currently inside: "title" | "snippet" | None.
        self._mode: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Begin capturing when entering a result title/snippet anchor."""
        if tag != "a":
            return
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if "result__a" in classes:
            url = _decode_result_href(attr.get("href") or "")
            if url is not None:
                self.results.append({"title": "", "url": url, "snippet": ""})
                self._mode = "title"
            else:
                self._mode = None
        elif "result__snippet" in classes and self.results:
            self._mode = "snippet"

    def handle_data(self, data: str) -> None:
        """Accumulate anchor text into the current result's title/snippet."""
        if self._mode == "title" and self.results:
            self.results[-1]["title"] += data
        elif self._mode == "snippet" and self.results:
            self.results[-1]["snippet"] += data

    def handle_endtag(self, tag: str) -> None:
        """Stop capturing at the end of an anchor."""
        if tag == "a":
            self._mode = None


def _parse_results(html: str) -> list[dict[str, str]]:
    """Parse DDG HTML into normalized result dicts.

    :param html: Raw HTML from the DDG HTML endpoint.
    :returns: Up to :data:`_MAX_RESULTS` results, each a dict with
        whitespace-normalized ``title``, ``url``, and ``snippet``.
    """
    parser = _ResultParser()
    try:
        parser.feed(html)
    except Exception:  # malformed markup must never crash search — degrade instead
        _logger.warning(
            "DuckDuckGo HTML parse failed; returning any partial results",
            exc_info=True,
        )
    out: list[dict[str, str]] = []
    for r in parser.results:
        title = " ".join(r["title"].split())
        snippet = " ".join(r["snippet"].split())
        if title and r["url"]:
            out.append({"title": title, "url": r["url"], "snippet": snippet})
        if len(out) >= _MAX_RESULTS:
            break
    return out


def _format_results(results: list[dict[str, str]]) -> str:
    """Format results as numbered ``title / url / snippet`` blocks.

    Matches the output shape of the other ``web_search`` backends.

    :param results: Parsed results from :func:`_parse_results`.
    :returns: Numbered blocks, or ``"No results found."`` when empty.
    """
    if not results:
        return "No results found."
    blocks = [
        f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results)
    ]
    return "\n\n".join(blocks)


def _search_duckduckgo(query: str, config: dict[str, str]) -> str:
    """Run a keyless DuckDuckGo HTML search and format the results.

    :param query: The search query string.
    :param config: Spec-level config; unused (DDG needs no credentials).
        Accepted for a uniform backend signature.
    :returns: Formatted results, an empty-results message, or an error string.
    """
    del config  # DuckDuckGo HTML needs no credentials.
    try:
        resp = httpx.post(
            _DDG_HTML_URL,
            data={"q": query},
            headers=_HEADERS,
            timeout=15.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"DuckDuckGo search error: HTTP {exc.response.status_code}"
    except httpx.HTTPError as exc:
        # Broad on purpose: a flaky / rate-limiting DDG endpoint raises
        # RemoteProtocolError / ReadError / DecodingError (peer reset, dropped
        # body, bad gzip or charset) — all ``httpx.HTTPError`` but NOT
        # ``TransportError`` — and these must surface as a readable string,
        # never crash the tool. The HTTPStatusError branch above stays first
        # so a status code (e.g. the 429 throttle signal) still shows its number.
        return f"DuckDuckGo search error: {exc}"
    return _format_results(_parse_results(resp.text))
