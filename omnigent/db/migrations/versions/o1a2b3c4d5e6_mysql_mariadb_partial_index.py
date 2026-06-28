"""mysql/mariadb: fix agents.name unique index

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-06-25 00:00:00.000000

On SQLite and PostgreSQL the ``ix_agents_template_name`` index is created
as a *partial* unique index (``WHERE session_id IS NULL``), meaning only
template agents (session_id IS NULL) must have unique names.  Session-scoped
agents share names across sessions freely (e.g. every session loads a
"claude-native" agent).

MySQL and MariaDB silently ignore the ``postgresql_where`` / ``sqlite_where``
kwargs on ``Index()`` and create a *full* unique index on ``name`` instead.
That would block multiple sessions from loading the same built-in agent name
(e.g. "claude-native"), crashing session startup from the second session on.

Neither MySQL nor MariaDB support partial/filtered indexes (WHERE clause on
CREATE INDEX).  This migration therefore drops the over-restrictive full
unique index on MySQL/MariaDB.  Template-agent name uniqueness is enforced
at the application level (the agent store checks for name conflicts before
inserting a template agent).

SQLite and PostgreSQL already have the correct partial index from the
original migration (``d7a6b3c91f48``); this migration is a no-op for them.
"""

from alembic import op

revision = "o1a2b3c4d5e6"
down_revision = "n1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in ("mysql", "mariadb"):
        # SQLite and PostgreSQL already have the correct partial index.
        return

    # MySQL/MariaDB created a full unique index on agents.name (ignoring the
    # WHERE clause).  Drop it — partial indexes are unsupported on these
    # dialects.  Template-agent name uniqueness is enforced by the application
    # layer instead.
    op.drop_index("ix_agents_template_name", table_name="agents", if_exists=True)


def downgrade() -> None:
    # The original migration (d7a6b3c91f48) owns the canonical definition;
    # rolling back this migration is a no-op.
    pass
