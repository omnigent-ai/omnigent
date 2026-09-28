"""Session-search benchmark on a ~1M-item PostgreSQL corpus.

Times, per search term, the two earliest-match snippet queries on one search
page — the portable ``MIN(position)`` aggregate ("before") and the PostgreSQL
ordered LATERAL probe ("after") — and the whole ``list_conversations`` search
call with each. The "before" whole call swaps the probe for the aggregate, so
both sides run the same store code otherwise. Both forms must return identical
snippets and pages, or the run exits 1.

Not run in CI. Point it at a disposable local PostgreSQL (never production)::

    OMNIGENT_BENCH_DATABASE_URI=postgresql+psycopg://postgres:pw@127.0.0.1:5432/bench \\
        uv run --no-sync dev/benchmarks/omnigent/session_search.py

The corpus comes from ``seed.py`` (8000 sessions x 128 items by default, about
1M items), seeded only into an empty database and reused as-is after. Its
~50-byte item texts are then prefixed with ``--pad-bytes`` of lowercase-hex
filler so each ILIKE scans a realistic body; pick terms that are not pure hex.
A whole search call that hits the store's statement timeout is recorded as such.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

# Allow ``uv run <path>`` (no package context) to import omnigent + siblings.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from dev.benchmarks.omnigent.seed import seed
from omnigent.stores.conversation_store import sqlalchemy_store as store_mod

_URI_ENV = "OMNIGENT_BENCH_DATABASE_URI"

# Sidebar search page size (web appConfig ``sessionPageSize``).
_PAGE_SIZE = 30

# Over the seed.py corpus: a fragment present early and often in every session,
# a term only the last item of each session carries, and no match at all.
_DEFAULT_TERMS = ("runner", "item 127)", "no-such-term")


# Prefixes each seeded item text with md5 hex (poorly compressible, so TOAST
# can't shrink it away). Items already at the target size are left alone.
_PAD_SQL = text(
    "UPDATE conversation_items SET search_text = ("
    " SELECT string_agg(md5(k::text || encode(id, 'hex')), '')"
    " FROM generate_series(1, :chunks) AS k"
    ") || ' ' || search_text"
    " WHERE octet_length(search_text) < :pad_bytes"
)


def _timed_ms(fn: Callable[[], object], runs: int, warmup: int) -> dict[str, float]:
    """Return median and p95 wall time (ms) of ``fn`` over *runs* calls."""
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(runs):
        start = time.monotonic()
        fn()
        samples.append((time.monotonic() - start) * 1000)
    samples.sort()
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 3),
    }


@contextmanager
def _before_fix() -> Iterator[None]:
    """Route the store's snippet lookup through the ``MIN(position)`` aggregate."""
    probe = store_mod._earliest_match_by_probe
    store_mod._earliest_match_by_probe = store_mod._earliest_match_by_min
    try:
        yield
    finally:
        store_mod._earliest_match_by_probe = probe


def _search(
    store: store_mod.SqlAlchemyConversationStore, term: str
) -> list[tuple[str, str | None]]:
    """Run the sidebar's search call and return its ``(id, snippet)`` page."""
    page = store.list_conversations(
        search_query=term, limit=_PAGE_SIZE, sort_by="updated_at", order="desc"
    )
    return [(c.id, c.search_snippet) for c in page.data]


def _is_statement_timeout(exc: OperationalError) -> bool:
    """Whether *exc* is the store's ``SET LOCAL statement_timeout`` firing."""
    return getattr(exc.orig, "sqlstate", None) == "57014"  # query_canceled


def _search_within_deadline(
    store: store_mod.SqlAlchemyConversationStore, term: str
) -> list[tuple[str, str | None]] | None:
    """Like :func:`_search`, but ``None`` when the statement timeout fires."""
    try:
        return _search(store, term)
    except OperationalError as exc:
        if not _is_statement_timeout(exc):
            raise
        return None


def _call_ms(
    store: store_mod.SqlAlchemyConversationStore, term: str, runs: int
) -> dict[str, float] | str:
    """Time the whole search call, or ``"statement_timeout"`` if any run hits it."""
    try:
        return _timed_ms(lambda: _search(store, term), runs, warmup=1)
    except OperationalError as exc:
        if not _is_statement_timeout(exc):
            raise
        return "statement_timeout"


def _bench_term(
    store: store_mod.SqlAlchemyConversationStore,
    term: str,
    runs: int,
    warmup: int,
    call_runs: int,
) -> dict[str, object]:
    """Measure one term; raise ``AssertionError`` if the two forms disagree."""
    after_page = _search_within_deadline(store, term)
    ids = [conv_id for conv_id, _ in after_page or []]
    if not ids:
        # No hits (or timed out): probe the most recent page instead.
        recent = store.list_conversations(limit=_PAGE_SIZE, sort_by="updated_at", order="desc")
        ids = [c.id for c in recent.data]
    pattern = f"%{term.lower()}%"

    result: dict[str, object] = {
        "term": term,
        "page_hits": None if after_page is None else len(after_page),
    }
    with store._conv_session("bench_session_search") as session:
        for label, build in (
            ("before", store_mod._earliest_match_by_min),
            ("after", store_mod._earliest_match_by_probe),
        ):
            stmt = build(ids, pattern)
            result[f"snippet_sql_{label}"] = _timed_ms(
                lambda stmt=stmt: session.execute(stmt).all(), runs, warmup
            )
        after_snippets = store_mod._fetch_search_snippets(session, ids, term)
        with _before_fix():
            before_snippets = store_mod._fetch_search_snippets(session, ids, term)
    assert before_snippets == after_snippets, f"snippet mismatch for {term!r}"
    result["snippets"] = len(after_snippets)

    result["list_conversations_after"] = _call_ms(store, term, call_runs)
    with _before_fix():
        before_page = _search_within_deadline(store, term)
        result["list_conversations_before"] = _call_ms(store, term, call_runs)
    if after_page is not None and before_page is not None:
        assert before_page == after_page, f"page mismatch for {term!r}"
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="omnigent-benchmark-session-search",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-uri",
        metavar="URI",
        default=os.environ.get(_URI_ENV),
        help=f"PostgreSQL URI to seed and search (default: ${_URI_ENV}).",
    )
    parser.add_argument("--sessions", type=int, default=8000, metavar="N")
    parser.add_argument("--items-per-session", type=int, default=128, metavar="N")
    parser.add_argument(
        "--pad-bytes",
        type=int,
        default=4096,
        metavar="N",
        help="Filler prepended to each item's search text (0 = none).",
    )
    parser.add_argument("--runs", type=int, default=20, metavar="N")
    parser.add_argument("--warmup", type=int, default=3, metavar="N")
    parser.add_argument(
        "--call-runs",
        type=int,
        default=5,
        metavar="N",
        help="Timed runs of the whole list_conversations call (after one warmup).",
    )
    parser.add_argument("--term", action="append", dest="terms", metavar="TEXT")
    parser.add_argument("--output", type=Path, metavar="FILE", help="Also write JSON here.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if not args.database_uri:
        print(f"session_search: pass --database-uri or set ${_URI_ENV}", file=sys.stderr)
        return 2
    store = store_mod.SqlAlchemyConversationStore(args.database_uri)
    if store._conv_engine.dialect.name != "postgresql":
        print("session_search: the benchmark targets PostgreSQL only", file=sys.stderr)
        return 2
    with store._conv_engine.connect() as conn:
        empty = conn.exec_driver_sql("SELECT NOT EXISTS (SELECT 1 FROM conversations)").scalar()
    if empty:
        seed(
            args.database_uri,
            sessions=args.sessions,
            items_per_session=args.items_per_session,
            projects=0,
        )
    if args.pad_bytes > 0:
        with store._conv_engine.begin() as conn:
            padded = conn.execute(
                _PAD_SQL, {"chunks": args.pad_bytes // 32, "pad_bytes": args.pad_bytes}
            ).rowcount
        print(f"session_search: padded {padded} items to ~{args.pad_bytes} bytes")
    with store._conv_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("VACUUM ANALYZE conversation_items")
        conn.exec_driver_sql("ANALYZE conversations")
        items = conn.exec_driver_sql("SELECT count(*) FROM conversation_items").scalar_one()
        server = conn.exec_driver_sql("SHOW server_version").scalar_one()

    try:
        results = [
            _bench_term(store, term, args.runs, args.warmup, args.call_runs)
            for term in (args.terms or _DEFAULT_TERMS)
        ]
    except AssertionError as exc:
        print(f"session_search: {exc}", file=sys.stderr)
        return 1
    report = {
        "postgres": server,
        "items": items,
        "pad_bytes": args.pad_bytes,
        "page_size": _PAGE_SIZE,
        "results": results,
    }
    print(json.dumps(report, indent=2))
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
