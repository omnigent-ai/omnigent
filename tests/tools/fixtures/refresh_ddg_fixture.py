"""Manual DuckDuckGo golden-fixture refresh — NOT a test.

Run this by hand to re-capture ``ddg_html_<YYYY-MM>.html`` when the live drift
canary (``tests/e2e_live/test_web_search_ddg_live.py``) goes red, i.e. DDG's
markup or endpoint changed::

    uv run python tests/tools/fixtures/refresh_ddg_fixture.py wikipedia

It reuses the *production* request path and parser so the captured body is
byte-identical to what the tool fetches at runtime, then prints the parsed
result count (0 ⇒ you were blocked or the markup drifted). Afterwards:

1. review the ``.html`` git diff,
2. rename ``ddg_html_latest.html`` → ``ddg_html_<YYYY-MM>.html`` and update the
   ``_DDG_GOLDEN`` filename + ``# captured`` date in
   ``tests/tools/test_web_search_duckduckgo.py``,
3. confirm the offline tests still pass.

This file lives under ``tests/`` and is never imported by the suite (no
``test_`` functions, no ``__init__`` in this dir), so it adds no runtime or CI
dependency on the network.
"""

from __future__ import annotations

import pathlib
import sys

import httpx

from omnigent.tools.builtins.web_search_duckduckgo import (
    _DDG_HTML_URL,
    _HEADERS,
    _parse_results,
)


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "wikipedia"
    resp = httpx.post(
        _DDG_HTML_URL,
        data={"q": query},
        headers=_HEADERS,
        timeout=15.0,
        follow_redirects=True,
    )
    resp.raise_for_status()

    out = pathlib.Path(__file__).parent / "ddg_html_latest.html"
    out.write_text(resp.text, encoding="utf-8")

    parsed = _parse_results(resp.text)
    print(f"wrote {out} ({len(resp.text)} bytes); parsed {len(parsed)} results")
    if not parsed:
        print("  ⚠️  0 results — you were blocked or the markup drifted.")
    for r in parsed[:3]:
        print(f"  - {r['title'][:60]} | {r['url']} | snippet={bool(r['snippet'])}")
    print("Rename to ddg_html_<YYYY-MM>.html, review the diff, update the test.")


if __name__ == "__main__":
    main()
