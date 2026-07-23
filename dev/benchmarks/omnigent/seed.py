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

Also seeds first-class projects (``projects`` rows owned by ``"local"``) and
files a fraction of sessions into them, so the project read journeys
(``list_projects`` / ``list_project_sessions``) measure a realistic sidebar —
a folder count and per-folder depth — instead of an empty project set.

Run standalone::

    uv run --no-sync dev/benchmarks/omnigent/seed.py \
        --database-uri sqlite:///tmp/bench.db --sessions 5000 --items-per-session 50 \
        --projects 20 --filed-fraction 0.5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

# Allow ``uv run <path>`` (no package context) to import omnigent + siblings.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine.url import make_url

from omnigent.db.db_models import (
    LABEL_VALUE_MAX_LEN,
    SqlAgent,
    SqlConversation,
    SqlConversationItem,
    SqlConversationLabel,
    SqlConversationMetadata,
    SqlProject,
    SqlSessionPermission,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.enum_codecs import (
    encode_agent_kind,
    encode_conversation_kind,
    encode_item_status,
    encode_item_type,
)
from omnigent.db.utils import (
    _FTS_TABLE,
    _get_head_db_revision,
    generate_agent_id,
    generate_conversation_id,
    generate_item_id,
    get_or_create_engine,
    now_epoch,
    strip_nul_bytes,
)
from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

# Label key stamped on the first seeded session recording the corpus config, so
# a later run can detect an existing (and matching) seed and skip re-seeding.
_SEED_META_LABEL = "omni_bench_seed"

# Fixed identifiers so the corpus is byte-stable across runs at a given config.
_AGENT_NAME = "bench-agent"
_AGENT_BUNDLE = "bench/seed"  # never validated on the read path
_DEFAULT_SESSIONS = 5000
_DEFAULT_ITEMS = 50
_DEFAULT_RNG_SEED = 1234

# First-class projects seeded into the corpus, and the fraction of sessions
# filed into one (round-robin). Without these the project read journeys
# (list_projects / list_project_sessions) would measure an empty project set —
# a 0-folder union and a 0-member folder — which tests nothing. The defaults
# give a realistic sidebar: 20 folders, each holding ~1/20th of the filed half
# of the corpus (e.g. 5000 sessions × 0.5 / 20 = 125 sessions/project).
_DEFAULT_PROJECTS = 20
_DEFAULT_FILED_FRACTION = 0.5

# Owner of every seeded project. The loopback bench server is single-user, so
# it resolves each request to the reserved ``"local"`` user; the project list
# and the ``?project=`` filter are owner-scoped, so a project owned by anyone
# else would be invisible to the benchmark's reads.
_PROJECT_OWNER = RESERVED_USER_LOCAL

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

# FTS5 mirror row written per item on SQLite (must match omnigent.db.utils
# ``insert_fts`` / ``_FTS_TABLE``). Bound by name in :data:`_FTS_INSERT_SQL`.
_FTS_INSERT_SQL = text(
    f"INSERT INTO {_FTS_TABLE} (item_id, conversation_id, search_text) "
    "VALUES (:item_id, :cid, :st)"
)

# Rows buffered per Core ``executemany`` flush. Only the item/FTS buffers
# (1:1 with items) approach this; the per-session tables are held in full
# (a few thousand rows) and inserted in one shot each. 100k keeps a 5000×200
# corpus to ~10 flushes per table and bounds peak memory to a few tens of MB.
_CORE_ITEM_CHUNK = 100_000


def _meta_value(
    sessions: int,
    items_per_session: int,
    rng_seed: int,
    projects: int,
    filed_fraction: float,
    head: str,
) -> str:
    """Serialize the corpus config into the seed-marker label value.

    Includes the Alembic *head* read at seed time, so a corpus seeded under an
    older schema auto-mismatches the current head and is reseeded — no
    hand-maintained revision constant. The project knobs are part of the key so
    a pre-existing corpus seeded without projects is reseeded once they're added.
    """
    return (
        f"sessions={sessions};items={items_per_session};rng={rng_seed};"
        f"projects={projects};filed={filed_fraction:g};rev={head}"
    )


def _project_id(index: int) -> str:
    """Deterministic 32-char-hex project id for the ``index``-th seeded project.

    Derived from the index (not random) so both write paths produce identical
    project rows and a re-seed at the same config is byte-stable.
    """
    return hashlib.sha256(f"bench-project-{index}".encode()).hexdigest()[:32]


def _project_plan(
    sessions: int,
    projects: int,
    filed_fraction: float,
) -> tuple[list[tuple[str, str]], dict[int, str]]:
    """Plan the corpus's first-class projects and their session membership.

    :returns: ``(project_specs, project_of_session)`` where ``project_specs`` is
        ``[(id, name), …]`` for the ``projects`` rows to create, and
        ``project_of_session`` maps a session index to the project id it is
        filed under. The first ``round(sessions × filed_fraction)`` sessions are
        filed, assigned round-robin across the projects so each folder holds a
        realistic, even share; the rest stay unfiled.
    """
    if projects <= 0 or filed_fraction <= 0 or sessions <= 0:
        return [], {}
    specs = [(_project_id(p), f"bench project {p}") for p in range(projects)]
    filed_count = min(sessions, round(sessions * filed_fraction))
    project_of_session = {s: specs[s % projects][0] for s in range(filed_count)}
    return specs, project_of_session


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
        text_str = f"{rng.choice(_FRAGMENTS)} (item {i})"
        items.append(
            NewConversationItem(
                type="message",
                response_id=f"resp_seed_{i}",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text_str}]),
            )
        )
    return items


def _progress(s: int, sessions: int) -> None:
    """Print a coarse progress line every 10% (only for sizeable corpora)."""
    if sessions >= 100 and s % (sessions // 10) == 0 and s:
        print(f"seed: {s}/{sessions} sessions")


def seed(
    db_uri: str,
    *,
    sessions: int = _DEFAULT_SESSIONS,
    items_per_session: int = _DEFAULT_ITEMS,
    rng_seed: int = _DEFAULT_RNG_SEED,
    projects: int = _DEFAULT_PROJECTS,
    filed_fraction: float = _DEFAULT_FILED_FRACTION,
    reseed: bool = False,
    _fast: bool | None = None,
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
    :param projects: Number of first-class ``projects`` rows to create (owned by
        ``"local"``). ``0`` seeds no projects.
    :param filed_fraction: Fraction of sessions filed into a project (round-robin
        across the ``projects`` folders); the rest stay unfiled. Ignored when
        ``projects`` is 0.
    :param reseed: Seed even when a matching corpus is already present.
    :param _fast: Override the write strategy. ``None`` (default) uses the
        bulk-insert Core fast path for SQLite and the store-API loop for every
        other dialect; ``True`` forces the fast path (falls back to the loop on
        non-SQLite); ``False`` forces the store-API loop (used by the
        byte-stability test to compare both paths on SQLite).
    :returns: The number of sessions created (0 when a matching seed is reused).
    """
    conv = SqlAlchemyConversationStore(db_uri)

    dialect = make_url(db_uri).get_backend_name()
    use_fast = (dialect == "sqlite") if _fast is None else bool(_fast)
    if use_fast and dialect != "sqlite":
        # The fast path is SQLite-only (FTS5 + single-transaction bulk insert);
        # a forced fast request on another dialect degrades to the store loop.
        use_fast = False

    # Read the current schema head at runtime (no DB contacted) and fold it into
    # the reuse marker, so a corpus from an older schema is auto-reseeded.
    head = _get_head_db_revision("sqlite:///:memory:")
    want = _meta_value(sessions, items_per_session, rng_seed, projects, filed_fraction, head)
    if not reseed:
        existing = _existing_seed_meta(conv)
        if existing == want:
            print(f"seed: matching corpus already present ({want}); skipping")
            return 0
        if existing is not None:
            print(f"seed: existing corpus differs ({existing!r} != {want!r}); pass --reseed")
            return 0

    project_specs, project_of_session = _project_plan(sessions, projects, filed_fraction)

    if use_fast:
        n = _seed_via_core(
            db_uri,
            sessions=sessions,
            items_per_session=items_per_session,
            rng_seed=rng_seed,
            want=want,
            project_specs=project_specs,
            project_of_session=project_of_session,
        )
    else:
        perms = SqlAlchemyPermissionStore(db_uri)
        n = _seed_via_store(
            conv,
            perms,
            sessions=sessions,
            items_per_session=items_per_session,
            rng_seed=rng_seed,
            want=want,
            project_specs=project_specs,
            project_of_session=project_of_session,
        )

    print(
        f"seed: created {n} sessions × {items_per_session} items, "
        f"{len(project_specs)} projects, {len(project_of_session)} filed ({want})"
    )
    return n


def _seed_via_store(
    conv: SqlAlchemyConversationStore,
    perms: SqlAlchemyPermissionStore,
    *,
    sessions: int,
    items_per_session: int,
    rng_seed: int,
    want: str,
    project_specs: list[tuple[str, str]],
    project_of_session: dict[int, str],
) -> int:
    """Seed through the production store ORM API (one row/commit at a time).

    This is the original path and the only one used on non-SQLite dialects
    (e.g. the nightly Postgres benchmark). It is kept verbatim so behavior
    there stays identical, save for the added first-class projects.
    """
    perms.ensure_user(RESERVED_USER_LOCAL)
    rng = random.Random(rng_seed)

    # First-class projects, owned by "local" so the owner-scoped project reads
    # see them. Created before the sessions so membership can be set inline.
    projects_store = SqlAlchemyProjectStore(conv.storage_location)
    for project_id, name in project_specs:
        projects_store.create(project_id, name, owner_user_id=_PROJECT_OWNER)

    last_sid = ""
    for s in range(sessions):
        created = conv.create_session_with_agent(
            agent_id=generate_agent_id(),
            agent_name=_AGENT_NAME,
            agent_bundle_location=_AGENT_BUNDLE,
            agent_description=None,
            title=f"bench session {s}: {rng.choice(_FRAGMENTS)}",
        )
        sid = created.conversation.id
        last_sid = sid
        perms.grant(RESERVED_USER_LOCAL, sid, LEVEL_OWNER)
        if items_per_session:
            conv.append(sid, _make_items(rng, items_per_session))
        project_id = project_of_session.get(s)
        if project_id is not None:
            conv.set_conversation_project(sid, project_id)
        _progress(s, sessions)

    # Stamp the corpus config on the LAST (newest) session — that's the one
    # ``_existing_seed_meta``'s default desc listing returns, so the reuse
    # check finds it regardless of corpus size.
    if last_sid:
        conv.set_labels(last_sid, {_SEED_META_LABEL: want})

    return sessions


def _seed_via_core(
    db_uri: str,
    *,
    sessions: int,
    items_per_session: int,
    rng_seed: int,
    want: str,
    project_specs: list[tuple[str, str]],
    project_of_session: dict[int, str],
) -> int:
    """Seed the entire corpus in one transaction via SQLAlchemy Core.

    Writes the same DB rows the store-API loop would, but batches them into a
    handful of ``executemany`` flushes under a single ``BEGIN``/``COMMIT`` —
    ~10 batched INSERTs and 1 commit instead of ~2M single-row INSERTs and
    ~20k commits. The schema at head carries no FK constraints (migration
    ``p1a2b3c4d5e6`` dropped them all), so insert order is free and the
    engine's ``PRAGMA foreign_keys=ON`` enforces nothing.

    The RNG draw order and the per-row serialization are kept identical to the
    store path: per session, the title fragment is drawn first, then the
    ``items_per_session`` item fragments. Item ``data`` is
    ``strip_nul_bytes(json.dumps(...))`` (default separators) of a plain dict
    that mirrors ``MessageData.model_dump(exclude_none=True)``, and
    ``search_text`` mirrors ``extract_search_text``'s message branch — both
    built directly (no pydantic) so the 1M-item Python build stays cheap. So a
    corpus seeded here is the same shape (same ids-space, same text, same
    positions, same labels) as one seeded through the store — only the write
    strategy differs. ``tests/benchmarks/test_seed_fast_path.py`` pins the two
    paths to identical corpora.
    """
    engine = get_or_create_engine(db_uri)
    ws = current_workspace_id()
    rng = random.Random(rng_seed)

    # Per-session scalar rows (conversations/agents/metadata/permissions) are
    # small (a few thousand); hold them in full and insert each in one shot.
    conv_rows: list[dict] = []
    agent_rows: list[dict] = []
    meta_rows: list[dict] = []
    perm_rows: list[dict] = []
    # Items + FTS mirror are 1:1 with items and dominate the volume (1M+ for a
    # full seed); stream them in chunks to bound memory while staying in the
    # single transaction.
    item_buf: list[dict] = []
    fts_buf: list[dict] = []
    last_sid = ""

    with engine.begin() as conn:
        # ensure_user("local") — ON CONFLICT DO NOTHING, mirroring the store.
        conn.execute(
            sqlite_insert(SqlUser)
            .values(workspace_id=ws, id=RESERVED_USER_LOCAL, is_admin=False)
            .on_conflict_do_nothing(index_elements=["workspace_id", "id"])
        )

        for s in range(sessions):
            now = now_epoch()
            agent_id = generate_agent_id()
            sid = generate_conversation_id()
            # RNG draw order matches the store path: title first, then items.
            title = f"bench session {s}: {rng.choice(_FRAGMENTS)}"
            last_sid = sid

            conv_rows.append(
                {
                    "workspace_id": ws,
                    "id": sid,
                    "created_at": now,
                    "updated_at": now,
                    "title": title,
                    "title_hash": hashlib.sha256(title.encode("utf-8")).digest()[:16],
                    "parent_conversation_id": None,
                    "root_conversation_id": sid,
                    "next_position": items_per_session,
                    "agent_id": agent_id,
                    "session_overrides": None,
                    "archived": False,
                }
            )
            agent_rows.append(
                {
                    "workspace_id": ws,
                    "id": agent_id,
                    "created_at": now,
                    "name": _AGENT_NAME,
                    "bundle_location": _AGENT_BUNDLE,
                    "version": 1,
                    "kind": encode_agent_kind("session"),
                    "description": None,
                    "updated_at": None,
                }
            )
            meta_rows.append(
                {
                    "workspace_id": ws,
                    "id": sid,
                    "kind": encode_conversation_kind("default"),
                    "runner_id": None,
                    "host_id": None,
                    "sub_agent_name": None,
                    "external_session_id": None,
                    "session_state": None,
                    "session_usage": None,
                    "terminal_launch_args": None,
                    "workspace": None,
                    "git_branch": None,
                    "runner_last_seen": None,
                    "live_status": None,
                    "pending_elicitation_count": None,
                    "project_id": project_of_session.get(s),
                }
            )
            perm_rows.append(
                {
                    "workspace_id": ws,
                    "user_id": RESERVED_USER_LOCAL,
                    "conversation_id": sid,
                    "level": LEVEL_OWNER,
                }
            )

            if items_per_session:
                # Build item payloads straight to dicts (no pydantic) so the
                # 1M-item Python build stays cheap. The output is byte-identical
                # to the store path: the text format mirrors ``_make_items``,
                # the ``data`` dict mirrors ``MessageData.model_dump(exclude_none=
                # True)``, and ``search`` mirrors ``extract_search_text``'s
                # message branch. The byte-stability test
                # (tests/benchmarks/test_seed_fast_path.py) pins this to the
                # store path's rows. ``_make_items`` is still used by the slow
                # path above, so the text format stays single-sourced there.
                for i in range(items_per_session):
                    text_str = f"{rng.choice(_FRAGMENTS)} (item {i})"
                    data_dict = {
                        "role": "user",
                        "content": [{"type": "input_text", "text": text_str}],
                    }
                    data = strip_nul_bytes(json.dumps(data_dict))
                    search = strip_nul_bytes(
                        " ".join(
                            block["text"]
                            for block in data_dict["content"]
                            if isinstance(block, dict) and block.get("text")
                        )
                    )
                    item_id = generate_item_id("message")
                    item_buf.append(
                        {
                            "workspace_id": ws,
                            "conversation_id": sid,
                            "id": item_id,
                            "response_id": f"resp_seed_{i}",
                            "created_at": now,
                            "status": encode_item_status("completed"),
                            "position": i,
                            "type": encode_item_type("message"),
                            "data": data,
                            "search_text": search,
                            "created_by": None,
                        }
                    )
                    fts_buf.append({"item_id": item_id, "cid": sid, "st": search})

            if len(item_buf) >= _CORE_ITEM_CHUNK:
                conn.execute(SqlConversationItem.__table__.insert(), item_buf)
                conn.execute(_FTS_INSERT_SQL, fts_buf)
                item_buf.clear()
                fts_buf.clear()

            _progress(s, sessions)

        if item_buf:
            conn.execute(SqlConversationItem.__table__.insert(), item_buf)
            conn.execute(_FTS_INSERT_SQL, fts_buf)
            item_buf.clear()
            fts_buf.clear()

        # No FKs at head → order is free; insert the per-session scalar tables
        # now (after the streamed items) in one shot each.
        conn.execute(SqlConversation.__table__.insert(), conv_rows)
        conn.execute(SqlAgent.__table__.insert(), agent_rows)
        conn.execute(SqlConversationMetadata.__table__.insert(), meta_rows)
        conn.execute(SqlSessionPermission.__table__.insert(), perm_rows)

        # First-class projects, owned by "local" (the reserved single-user id
        # the owner-scoped project reads resolve to). Membership already lives
        # on each metadata row's ``project_id`` above. ``updated_at`` is NULL,
        # matching a freshly created project (the store sets it only on rename).
        if project_specs:
            project_now = now_epoch()
            conn.execute(
                SqlProject.__table__.insert(),
                [
                    {
                        "workspace_id": ws,
                        "id": project_id,
                        "name": name,
                        "owner_user_id": _PROJECT_OWNER,
                        "created_at": project_now,
                        "updated_at": None,
                    }
                    for project_id, name in project_specs
                ],
            )

        # Stamp the corpus config on the LAST (newest) session, matching the
        # store path's ``set_labels`` upsert (clamped to LABEL_VALUE_MAX_LEN).
        if last_sid:
            label_now = now_epoch()
            label_value = want[:LABEL_VALUE_MAX_LEN]
            conn.execute(
                sqlite_insert(SqlConversationLabel)
                .values(
                    workspace_id=ws,
                    conversation_id=last_sid,
                    key=_SEED_META_LABEL,
                    value=label_value,
                    updated_at=label_now,
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id", "conversation_id", "key"],
                    set_={"value": label_value, "updated_at": label_now},
                )
            )

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
        "--projects",
        type=int,
        default=_DEFAULT_PROJECTS,
        metavar="N",
        help="First-class projects to seed (0 = none). Filed sessions are "
        "spread round-robin across them.",
    )
    parser.add_argument(
        "--filed-fraction",
        type=float,
        default=_DEFAULT_FILED_FRACTION,
        metavar="F",
        help="Fraction of sessions filed into a project (0..1); the rest stay "
        "unfiled. Ignored when --projects is 0.",
    )
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
        projects=args.projects,
        filed_fraction=args.filed_fraction,
        reseed=args.reseed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
