#!/usr/bin/env python3
"""Back-date the fixture sessions omnidev just imported and tag a few with a repo.

`omnigent session import` stamps every seeded session idle / now / no-labels, so
the sidebar's Updated grouping and Repo toggle have nothing to show. This dev-only
step rewrites the pod DB directly (the server stamps `updated_at` to now on every
mutation, so it can't be back-dated through the API). It touches only the session
ids it's given — the ones the supervisor imported in this run — and never
replaces a label a session already has.

Usage: seed_sidebar_metadata.py <chat.db path> <session id>...
"""

import itertools
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, time, timedelta

# (days ago, repo URL or None), assigned to the given sessions in order and
# repeated past the end. Two per Updated group (Today / Yesterday / Previous 7
# days / Previous 30 days / Older), and three carry a sandbox repo for Show -> Repo.
LAYOUT: list[tuple[int, str | None]] = [
    (0, "https://github.com/omnigent-ai/omnigent#main"),
    (0, None),
    (1, "https://github.com/omnigent-ai/web-ui#fix/sidebar"),
    (1, None),
    (3, None),
    (6, "https://github.com/omnigent-ai/omnigent-docs#main"),
    (12, None),
    (25, None),
    (45, None),
    (120, None),
]
REPO_LABEL = "omnigent.sandbox.repo.0"


def updated_at(days_ago: int, now: datetime) -> int:
    """Unix seconds for noon `days_ago` calendar days before `now`, in its zone; today is `now`."""
    if days_ago == 0:
        return int(now.timestamp())
    day = now.date() - timedelta(days=days_ago)
    return int(datetime.combine(day, time(12), tzinfo=now.tzinfo).timestamp())


def seed(db_path: str, session_ids: list[str], now: datetime) -> int:
    """Apply LAYOUT to `session_ids` in order; returns how many sessions were found."""
    conn = sqlite3.connect(db_path)
    seeded = 0
    try:
        for session_id, (days_ago, repo) in zip(session_ids, itertools.cycle(LAYOUT)):
            # API ids are bare 32-char hex (a legacy prefix is tolerated); the
            # column stores the 16 raw bytes.
            cid = uuid.UUID(session_id.rsplit("_", 1)[-1]).bytes
            row = conn.execute(
                "SELECT workspace_id FROM conversations WHERE id = ?", (cid,)
            ).fetchone()
            if row is None:
                continue
            ts = updated_at(days_ago, now)
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, cid))
            if repo is not None:
                conn.execute(
                    "INSERT INTO conversation_labels "
                    "(workspace_id, conversation_id, key, value, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(workspace_id, conversation_id, key) DO NOTHING",
                    (row[0], cid, REPO_LABEL, repo, ts),
                )
            seeded += 1
        conn.commit()
    finally:
        conn.close()
    return seeded


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: seed_sidebar_metadata.py <chat.db> <session id>...", file=sys.stderr)
        sys.exit(2)
    # Local time: the sidebar buckets sessions by the viewer's calendar day.
    count = seed(sys.argv[1], sys.argv[2:], datetime.now(UTC).astimezone())
    print(f"seeded sidebar metadata on {count} session(s)")
