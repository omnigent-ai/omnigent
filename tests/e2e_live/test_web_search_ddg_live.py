"""LIVE drift canary for the keyless DuckDuckGo web_search backend.

The backend parses an uncontrolled third-party HTML page, so its real risk is
silent markup drift (DDG has already dropped the ``/l/?uddg=`` redirect wrapper
once). The offline tests in ``tests/tools/test_web_search_duckduckgo.py`` pin
behavior against a *captured* page; this canary is the only thing that notices
when the *live* page changes shape.

It is deliberately fenced off from normal CI two ways, so it can never red the
PR/push gate:

1. It lives under ``tests/e2e_live/``, which the default ``addopts`` ignores
   (``--ignore=tests/e2e_live`` in pyproject.toml) — unit collection never sees
   it.
2. It is marked ``@pytest.mark.nightly`` — even when the dir is named
   explicitly, PR/push runs pass ``-m "not nightly"`` and skip it; only the
   scheduled / ``workflow_dispatch`` pass runs it.

Run it on demand::

    uv run pytest tests/e2e_live/ -m nightly

A FAIL here means DDG drifted: re-capture the golden fixture with
``tests/tools/fixtures/refresh_ddg_fixture.py`` and fix the parser in
``omnigent/tools/builtins/web_search_duckduckgo.py``. Transient throttling
(429 / 5xx / timeout) SKIPS — it is noise, not drift.
"""

from __future__ import annotations

import pytest

from omnigent.tools.builtins.web_search_duckduckgo import _search_duckduckgo


@pytest.mark.nightly
def test_ddg_live_canary_returns_results() -> None:
    """Hit real html.duckduckgo.com/html/ for an evergreen query and assert
    INVARIANTS only (never a specific result), so result churn can't flake it.

    Catches the whole silent-200 family in one check: an anti-bot block page,
    a ``result__a`` / ``result__snippet`` rename, or the snippet leaving its
    element all collapse into "evergreen query → 0 results / wrong shape"."""
    # "wikipedia" is evergreen — a non-zero result set is a true invariant.
    out = _search_duckduckgo("wikipedia", {})

    if out.startswith("DuckDuckGo search error"):
        pytest.skip(f"transient DDG error, not drift: {out}")  # 429/5xx/timeout

    assert out != "No results found.", (
        "DDG returned zero results for an evergreen query — blocked or the "
        "result__a / result__snippet selectors drifted. Refresh the golden "
        "fixture and check the parser."
    )
    assert out.startswith("1. "), "result formatting changed"
    assert "http" in out, "no URL decoded from any result"

    blocks = out.split("\n\n")
    with_snippet = sum(1 for b in blocks if len(b.splitlines()) >= 3 and b.splitlines()[2].strip())
    assert with_snippet >= len(blocks) // 2, (
        "snippets mostly empty — result__snippet likely moved or was renamed"
    )
