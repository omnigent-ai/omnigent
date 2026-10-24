"""Add nullable metadata stamps for disconnect reconciliation.

Deploy before the application starts using either column. Older binaries
ignore them; existing rows retain NULL until an upgraded writer observes
status or liveness. Roll back the application before removing the columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "ng12ab34cd56"
down_revision = "c8e4f2a6b901"
branch_labels = None
depends_on = None

_TABLE = "omnigent_conversation_metadata"
_COLUMNS = ("live_status_at", "runner_last_connected")


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    for name in _COLUMNS:
        if name not in existing:
            kind = sa.BigInteger() if name == "live_status_at" else sa.Integer()
            op.add_column(_TABLE, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    for name in reversed(_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
