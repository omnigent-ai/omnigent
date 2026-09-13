"""E2E: the session-list page read must not slow down with total session count.

Reproduces the sidebar session-list slowdown: a 20-row ``GET /v1/sessions``
page — the read behind the web sidebar — takes time
proportional to the user's TOTAL session count, not the page size. Measured on
loopback SQLite: ~4 ms on an empty DB, ~40 ms at 5k sessions, ~240 ms at 20k,
~1 s at 100k. The store's ``list_conversations`` prefetches every
permission-qualifying conversation id for the user and binds each one into an
``IN (...)`` filter, so every page pays an O(total-sessions) fetch + bind.

The test boots two real ``omni server`` subprocesses (no runner, no LLM) via
the benchmark harness, one against a small seeded corpus and one against a
16x larger one, and times the same 20-row page read against each. A paged,
index-backed read costs roughly the same at both sizes (ratio ~1x); the O(N)
prefetch makes the large corpus several times slower (~10x observed). The
ratio assertion is machine-speed independent, so it fails specifically on the
scaling defect and passes once the ACL filter is pushed down into the query.

Run it directly (no LLM key needed)::

    uv run --no-sync pytest tests/e2e/test_sessions_page_latency_scaling.py -v
"""

from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from dev.benchmarks.omnigent.environment import BenchEnvironment
from dev.benchmarks.omnigent.seed import seed

# The sidebar's page read: first 20 sessions, newest first (the route's
# defaults for order/sort/kind match what the SPA requests).
_PAGE_LIMIT = 20

# Corpus sizes. 16x growth keeps the seed + measurement fast while leaving a
# wide gap between healthy paging (~1x latency ratio) and the O(N) prefetch
# (~10x observed at these sizes on loopback SQLite).
_SMALL_SESSIONS = 1_000
_LARGE_SESSIONS = 16_000

# Median-of-samples keeps a single scheduler hiccup from deciding the verdict.
_WARMUP_REQUESTS = 3
_SAMPLES = 15

# A 16x corpus may cost a LITTLE more (bigger table, colder caches) but must
# not cost 16x. Healthy paging measures ~1x; the current prefetch ~10x.
_MAX_LATENCY_RATIO = 3.0


def _bypass_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exempt loopback from any ambient HTTP(S) proxy.

    CI sandboxes route egress through a proxy via ``HTTP_PROXY`` /
    ``HTTPS_PROXY``; without a loopback exemption the harness's own health
    probe and the timed requests would be proxied (or blackholed) instead of
    hitting the local server.
    """
    for var in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(var, "")
        hosts = "127.0.0.1,localhost"
        monkeypatch.setenv(var, f"{existing},{hosts}" if existing else hosts)


def _spread_created_at(db_uri: str) -> None:
    """Give the seeded sessions distinct, insertion-ordered ``created_at``s.

    The fast seeder stamps every row with the same wall-clock second, which no
    real corpus has — a heavy user's sessions accumulate over months. A fully
    tied ``created_at`` makes the ORDER BY tiebreak itself degenerate (one
    giant tie group), which measures a different pathology than the reported
    one. One second apart per row keeps the corpus realistic while preserving
    insertion order.
    """
    engine = sa.create_engine(db_uri)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE conversations SET created_at = created_at + rowid,"
            " updated_at = updated_at + rowid"
        )
    engine.dispose()


async def _median_page_latency_ms(db_uri: str) -> float:
    """Boot a real server against *db_uri* and time the sidebar page read.

    :param db_uri: Pre-seeded SQLAlchemy URI the server boots against.
    :returns: Median wall-clock latency, in ms, of
        ``GET /v1/sessions?limit=20`` over :data:`_SAMPLES` sequential
        requests (after :data:`_WARMUP_REQUESTS` discarded warmups).
    """
    async with BenchEnvironment(database_uri=db_uri) as env:
        assert env.client is not None
        for _ in range(_WARMUP_REQUESTS):
            resp = await env.client.get("/v1/sessions", params={"limit": _PAGE_LIMIT})
            resp.raise_for_status()
        samples: list[float] = []
        for _ in range(_SAMPLES):
            start = time.perf_counter()
            resp = await env.client.get("/v1/sessions", params={"limit": _PAGE_LIMIT})
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            resp.raise_for_status()
            # Both corpora exceed a page, so a full page proves the journey
            # actually listed sessions (guards against an "optimization" that
            # gets fast by returning nothing).
            assert len(resp.json()["data"]) == _PAGE_LIMIT
            samples.append(elapsed_ms)
        return statistics.median(samples)


# Two server boots + seeding 17k sessions comfortably exceed the strict
# per-test caps some e2e workflows pass on the CLI.
@pytest.mark.timeout(600)
async def test_list_sessions_page_latency_does_not_scale_with_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 20-row session-list page must cost O(page), not O(total sessions)."""
    _bypass_proxy_for_loopback(monkeypatch)

    small_uri = f"sqlite:///{tmp_path / 'small.db'}"
    large_uri = f"sqlite:///{tmp_path / 'large.db'}"
    # Deterministic corpora owned by the loopback user "local" (the identity
    # every request resolves to on an auth-less server), so list_sessions'
    # permission scope matches all seeded rows — exactly a heavy user's DB.
    seed(small_uri, sessions=_SMALL_SESSIONS, items_per_session=1, projects=0, filed_fraction=0.0)
    seed(large_uri, sessions=_LARGE_SESSIONS, items_per_session=1, projects=0, filed_fraction=0.0)
    _spread_created_at(small_uri)
    _spread_created_at(large_uri)

    small_ms = await _median_page_latency_ms(small_uri)
    large_ms = await _median_page_latency_ms(large_uri)

    ratio = large_ms / small_ms
    assert ratio <= _MAX_LATENCY_RATIO, (
        f"GET /v1/sessions?limit={_PAGE_LIMIT} median latency scales with the "
        f"total session count: {small_ms:.1f} ms at {_SMALL_SESSIONS} sessions "
        f"vs {large_ms:.1f} ms at {_LARGE_SESSIONS} ({ratio:.1f}x for a 16x "
        f"corpus; expected <= {_MAX_LATENCY_RATIO}x). The sidebar's page read "
        "is paying an O(total-sessions) cost per request."
    )
