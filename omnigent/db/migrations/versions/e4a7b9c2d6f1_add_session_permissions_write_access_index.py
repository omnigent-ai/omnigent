"""Add a covering index for a session's write grants.

Revision ID: e4a7b9c2d6f1
Revises: d0a1f879a01d

Equality on workspace/session followed by a level range supports bounded
lookups ordered by level, user_id. Keep the conversation index: it supports
user_id ordering independently of level.

Deploy this schema before callers use or force the new index. Rolling back
those callers to this prerequisite build can leave the index in place; older
builds that do not know this revision reject it at startup. Native index
creation may block writes during the build, so plan the schema deployment
window for the table's size. Downgrade only after dependent callers are removed.
"""

from alembic import op

revision = "e4a7b9c2d6f1"
down_revision = "d0a1f879a01d"
branch_labels = None
depends_on = None

_INDEX = "ix_session_permissions_write_access"
_TABLE = "session_permissions"


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, ["workspace_id", "conversation_id", "level", "user_id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
