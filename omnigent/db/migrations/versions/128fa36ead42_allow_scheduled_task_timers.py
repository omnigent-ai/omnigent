"""Allow timer scheduled tasks.

Revision ID: 128fa36ead42
Revises: f7c2d8a4b901

``kind`` 3 marks a timer an agent set with ``sys_timer_set``, which the server
keeps so it fires into the agent's session after the runner stops.
``ix_scheduled_tasks_kind_session`` is ``(kind, state, workspace_id, session_id,
id)``: it bounds counting one session's active timers, and the downgrade's
lookups, which find timers across workspaces. It is non-unique, like the table's
other indexes, and holds the whole primary key.

Deployment: apply this migration before any server that keeps timers. Servers
that migrate at startup do so on their own. Building the index reads the table;
on a large table, apply it outside peak hours. Only a server with the
``durable_timers`` release feature on writes ``kind`` 3. Processes running an
older build keep working on the migrated schema, but an older build does not
start on it: startup refuses a schema revision the build does not know.

Rollback: turn ``durable_timers`` off and delete the remaining active or paused
timers, which ``GET /v1/scheduled-tasks?kind=all`` lists. The application can
then roll back to the build that added this revision, which reads timer rows but
keeps no timers. To remove the schema as well, stop the servers, downgrade with
that build (``OMNIGENT_DB_URL=<uri> alembic -c omnigent/db/alembic.ini downgrade
f7c2d8a4b901``), and start the older build. The downgrade refuses while an active
or paused timer remains. It turns finished and deleted timers into deleted
automations, which cleanup purges, in batches that each commit, so a downgrade
interrupted during that conversion can be run again.
"""

from __future__ import annotations

from contextlib import nullcontext

import sqlalchemy as sa
from alembic import context, op

from omnigent.db.db_models import Uuid16

revision: str = "128fa36ead42"
down_revision: str | None = "f7c2d8a4b901"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "scheduled_tasks"
_INDEX = "ix_scheduled_tasks_kind_session"
_AUTOMATION = 1
_TIMER = 3
_ACTIVE = 1
_PAUSED = 2
_DELETED = 3
_COMPLETED = 4
_CLEANUP_BATCH = 200


def _commit_cockroach_batch(connection: sa.Connection) -> None:
    # The CRDB migration caller owns the transaction, so Alembic cannot autocommit it.
    if connection.dialect.name == "cockroachdb":
        connection.commit()
        connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def upgrade() -> None:
    """Accept timers in the kind check and index them by session."""
    # CockroachDB can't drop a check added earlier in the same transaction, as one
    # upgrade through 27d16059fab9 would, so publish the earlier revisions first.
    _commit_cockroach_batch(op.get_bind())
    with op.batch_alter_table(_TABLE) as batch:
        # A distinct name lets CockroachDB replace the check in one transaction.
        batch.drop_constraint("ck_scheduled_tasks_kind", type_="check")
        batch.create_check_constraint("ck_scheduled_tasks_task_kind", "kind IN (1, 2, 3)")
        batch.create_index(_INDEX, ["kind", "state", "workspace_id", "session_id", "id"])


def downgrade() -> None:
    """Restore the previous kind check once no live timer remains."""
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires a live connection to check task kinds")
    tasks = sa.table(
        _TABLE,
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("session_id", Uuid16()),
        sa.column("kind", sa.SmallInteger()),
        sa.column("state", sa.SmallInteger()),
    )
    connection = op.get_bind()
    # Preserve preceding revisions whose DDL already committed if the check fails.
    _commit_cockroach_batch(connection)
    live = sa.select(tasks.c.id).where(
        tasks.c.kind == _TIMER, tasks.c.state.in_((_ACTIVE, _PAUSED))
    )
    if connection.execute(live.limit(1)).first():
        raise RuntimeError("Delete active or paused timer scheduled tasks before downgrading")
    # Converted rows leave the index range, so each page starts where the last one ended.
    finished = (
        sa.select(tasks.c.workspace_id, tasks.c.id)
        .where(tasks.c.kind == _TIMER, tasks.c.state.in_((_DELETED, _COMPLETED)))
        .order_by(
            tasks.c.kind, tasks.c.state, tasks.c.workspace_id, tasks.c.session_id, tasks.c.id
        )
        .limit(_CLEANUP_BATCH)
    )
    page = connection.execute(finished).all()
    cockroach = connection.dialect.name == "cockroachdb"
    if page:
        # Each batch commits on its own; the revision moves only after the DDL below.
        with nullcontext() if cockroach else op.get_context().autocommit_block():
            while page:
                # A full primary key per row keeps each update a key lookup.
                for row in page:
                    connection.execute(
                        tasks.update()
                        .where(tasks.c.workspace_id == row.workspace_id, tasks.c.id == row.id)
                        .values(kind=_AUTOMATION, state=_DELETED)
                    )
                _commit_cockroach_batch(connection)
                page = connection.execute(finished).all()
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_index(_INDEX)
        batch.drop_constraint("ck_scheduled_tasks_task_kind", type_="check")
        batch.create_check_constraint("ck_scheduled_tasks_kind", "kind IN (1, 2)")
    _commit_cockroach_batch(connection)
