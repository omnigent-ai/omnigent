#!/usr/bin/env python3
"""Back-date imported fixture sessions and tag a few with a sandbox repo.

`omnigent session import` stamps every seeded session idle / now / no-labels, so
the sidebar's Updated grouping and Repo toggle have nothing to show. This dev-only
step rewrites the pod DB directly (the only way to backdate `updated_at` — the
server always stamps it to now on any mutation) so omnidev's conversation list
spans several day-buckets and some rows carry a repo name.

Usage: seed_sidebar_metadata.py <chat.db path>. Idempotent: re-running just
re-applies the same fixed layout to the newest sessions.
"""

import sqlite3
import sys
import time

# (days-ago, repo URL or None) for the newest sessions, newest first. Spreads
# them across Today / Yesterday / Previous 7 days / Previous 30 days / Older and
# gives three a sandbox repo so Show -> Repo renders a name.
LAYOUT: list[tuple[int, str | None]] = [
    (0, "https://github.com/omnigent-ai/omnigent#main"),
    (0, None),
    (1, "https://github.com/omnigent-ai/web-ui#fix/sidebar"),
    (3, None),
    (5, "https://github.com/omnigent-ai/omnigent-docs#main"),
    (12, None),
    (40, None),
]
REPO_LABEL = "omnigent.sandbox.repo.0"


def main(db_path: str) -> int:
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    try:
        # Top-level sessions only (sub-agent conversations have a parent), newest
        # first — on a fresh pod these are exactly the just-imported fixtures.
        rows = conn.execute(
            "SELECT workspace_id, id FROM conversations "
            "WHERE agent_id IS NOT NULL AND parent_conversation_id IS NULL "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (len(LAYOUT),),
        ).fetchall()
        # Re-running picks the same rows (created_at is import-stable, unlike the
        # updated_at we rewrite) and clears their old repo labels first, so the
        # layout converges instead of accumulating.
        conn.execute(
            f"DELETE FROM conversation_labels WHERE key = '{REPO_LABEL}'",
        )
        # Fewer rows than LAYOUT on a small DB is fine — zip stops at the shorter.
        for (workspace_id, cid), (days_ago, repo) in zip(rows, LAYOUT, strict=False):
            # 12:00 local, days_ago back — lands mid-day so it can't drift buckets.
            ts = now - days_ago * 86400 - 3600
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE workspace_id = ? AND id = ?",
                (ts, workspace_id, cid),
            )
            if repo is not None:
                conn.execute(
                    "INSERT INTO conversation_labels "
                    "(workspace_id, conversation_id, key, value, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(workspace_id, conversation_id, key) "
                    "DO UPDATE SET value = excluded.value",
                    (workspace_id, cid, REPO_LABEL, repo, ts),
                )
        conn.commit()
        print(f"seeded sidebar metadata on {len(rows)} session(s)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: seed_sidebar_metadata.py <chat.db>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
