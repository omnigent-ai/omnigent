"""merge migration heads

Revision ID: bd7b23a1b633
Revises: e5d9bc8ac650, zb1b2c3d4e5f
Create Date: 2026-08-24 16:32:34.133922
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bd7b23a1b633'
down_revision: Union[str, None] = ('e5d9bc8ac650', 'zb1b2c3d4e5f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
