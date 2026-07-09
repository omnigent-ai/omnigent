"""Deterministic corpus seeder for the performance benchmark.

The v1 harness booted an empty DB, so the read journeys measured a best-case
near-empty table. This seeds a sizeable, realistic corpus directly through the
store API (no HTTP, no runner) so ``list_sessions`` / ``get_session`` /
``load_conversation_history`` read a production-shaped volume.

Writes to the same DB URI the server later boots against; startup migrations
are an idempotent no-op on an at-head DB. The seed is deterministic (fixed RNG,
fixed counts) so the same config always yields the same corpus — which is what
makes "seed once, reuse" sound. The reuse marker records the Alembic head read
at seed time, so a corpus from an older schema is auto-reseeded (no manual
revision bookkeeping).

Listable-corpus recipe, per session (the permission grant is the gotcha — the
loopback server resolves every request to user ``"local"`` and
``list_sessions`` filters by it):

1. ``create_session_with_agent`` — conversation + session-scoped agent row.
2. ``permission_store.grant("local", sid, LEVEL_OWNER)`` — makes it listable.
3. one batched ``append(sid, items)`` — user-role message items.

Run standalone::

    uv run --no-sync dev/benchmarks/omnigent/seed.py \
        --database-uri sqlite:///tmp/bench.db --sessions 5000 --items-per-session 50
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

# Allow ``uv run <path>`` (no package context) to import omnigent + siblings.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from omnigent.db.utils import _get_head_db_revision, generate_agent_id
from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

# Label key stamped on the first seeded session recording the corpus config, so
# a later run can detect an existing (and matching) seed and skip re-seeding.
_SEED_META_LABEL = "omni_bench_seed"

# Fixed identifiers so the corpus is byte-stable across runs at a given config.
_AGENT_NAME = "bench-agent"
_DEFAULT_SESSIONS = 5000
_DEFAULT_ITEMS = 50
_DEFAULT_RNG_SEED = 1234

# A pool of realistic-ish message fragments; the RNG assembles item text from
# these so search_text has lexical variety without external data.
_FRAGMENTS = (
    "investigate the failing migration",
    "the runner keeps disconnecting under load",
    "add pagination to the sessions endpoint",
    "why does the policy classifier time out",
    "refactor the conversation store append path",
    "benchmark the list endpoints against postgres",
    "the web UI drops the last streamed token",
    "trace the tunnel handshake for this runner id",
    "summarize the changes in this pull request",
    "reproduce the elicitation race on reconnect",
)


def _meta_value(sessions: int, items_per_session: int, rng_seed: int, head: str) -> str:
    """Serialize the corpus config into the seed-marker label value.

    Includes the Alembic *head* read at seed time, so a corpus seeded under an
    older schema auto-mismatches the current head and is reseeded — no
    hand-maintained revision constant.
    """
    return f"sessions={sessions};items={items_per_session};rng={rng_seed};rev={head}"


def _existing_seed_meta(conv: SqlAlchemyConversationStore) -> str | None:
    """Return the seed-marker label value if a bench corpus already exists.

    Looks up the most recent ``bench-agent`` session and reads its
    ``omni_bench_seed`` label. ``None`` means no (recognizable) seed present.
    """
    listing = conv.list_conversations(limit=1, agent_name=_AGENT_NAME)
    if not listing.data:
        return None
    marked = conv.get_conversation(listing.data[0].id)
    return marked.labels.get(_SEED_META_LABEL) if marked is not None else None


def _make_items(rng: random.Random, count: int) -> list[NewConversationItem]:
    """Build *count* deterministic user-role message items.

    User-role only: assistant messages require an ``agent`` field the store
    only assigns after a real turn, and the seeded read path is role-agnostic.
    """
    items: list[NewConversationItem] = []
    for i in range(count):
        text = f"{rng.choice(_FRAGMENTS)} (item {i})"
        items.append(
            NewConversationItem(
                type="message",
                response_id=f"resp_seed_{i}",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
        )
    return items


def seed(
    db_uri: str,
    *,
    sessions: int = _DEFAULT_SESSIONS,
    items_per_session: int = _DEFAULT_ITEMS,
    rng_seed: int = _DEFAULT_RNG_SEED,
    reseed: bool = False,
) -> int:
    """Seed *sessions* sessions × *items_per_session* items into *db_uri*.

    Idempotent: if a matching seed already exists (same config + schema
    revision) it is left untouched unless *reseed* is set. Constructing the
    store runs migrations to head on first init, so *db_uri* need not
    pre-exist.

    :param db_uri: SQLAlchemy URI the server will also boot against, e.g.
        ``"sqlite:///abs/bench.db"`` or ``"postgresql+psycopg://…"``.
    :param sessions: Number of listable sessions to create.
    :param items_per_session: Conversation items appended to each session.
    :param rng_seed: Seed for the deterministic text RNG.
    :param reseed: Seed even when a matching corpus is already present.
    :returns: The number of sessions created (0 when a matching seed is reused).
    """
    conv = SqlAlchemyConversationStore(db_uri)
    perms = SqlAlchemyPermissionStore(db_uri)

    # Read the current schema head at runtime (no DB contacted) and fold it into
    # the reuse marker, so a corpus from an older schema is auto-reseeded.
    head = _get_head_db_revision("sqlite:///:memory:")
    want = _meta_value(sessions, items_per_session, rng_seed, head)
    if not reseed:
        existing = _existing_seed_meta(conv)
        if existing == want:
            print(f"seed: matching corpus already present ({want}); skipping")
            return 0
        if existing is not None:
            print(f"seed: existing corpus differs ({existing!r} != {want!r}); pass --reseed")
            return 0

    perms.ensure_user(RESERVED_USER_LOCAL)
    rng = random.Random(rng_seed)

    last_sid = ""
    for s in range(sessions):
        created = conv.create_session_with_agent(
            agent_id=generate_agent_id(),
            agent_name=_AGENT_NAME,
            agent_bundle_location="bench/seed",  # never validated on the read path
            agent_description=None,
            title=f"bench session {s}: {rng.choice(_FRAGMENTS)}",
        )
        sid = created.conversation.id
        last_sid = sid
        perms.grant(RESERVED_USER_LOCAL, sid, LEVEL_OWNER)
        if items_per_session:
            conv.append(sid, _make_items(rng, items_per_session))
        if sessions >= 100 and s % (sessions // 10) == 0 and s:
            print(f"seed: {s}/{sessions} sessions")

    # Stamp the corpus config on the LAST (newest) session — that's the one
    # ``_existing_seed_meta``'s default desc listing returns, so the reuse
    # check finds it regardless of corpus size.
    if last_sid:
        conv.set_labels(last_sid, {_SEED_META_LABEL: want})

    print(f"seed: created {sessions} sessions × {items_per_session} items ({want})")
    return sessions


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="omnigent-benchmark-seed",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-uri",
        metavar="URI",
        help="DB to seed. Required unless --print-head.",
    )
    parser.add_argument("--sessions", type=int, default=_DEFAULT_SESSIONS, metavar="N")
    parser.add_argument("--items-per-session", type=int, default=_DEFAULT_ITEMS, metavar="N")
    parser.add_argument("--rng-seed", type=int, default=_DEFAULT_RNG_SEED, metavar="N")
    parser.add_argument(
        "--reseed",
        action="store_true",
        help="Seed even if a matching corpus is already present.",
    )
    parser.add_argument(
        "--print-head",
        action="store_true",
        help="Print the repo's Alembic head revision and exit (drift-check helper).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.print_head:
        print(_get_head_db_revision("sqlite:///:memory:"))
        return 0
    if not args.database_uri:
        print("seed: --database-uri is required (unless --print-head)", file=sys.stderr)
        return 2
    seed(
        args.database_uri,
        sessions=args.sessions,
        items_per_session=args.items_per_session,
        rng_seed=args.rng_seed,
        reseed=args.reseed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
