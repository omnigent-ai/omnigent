"""merge main's scheduled_tasks smart_routing migration

Revision ID: 10181aec0f14
Revises: 5ab01aaca219, 750fa7b2cd48
Create Date: 2026-11-20 13:20:13.040752
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '10181aec0f14'
down_revision: Union[str, None] = ('5ab01aaca219', '750fa7b2cd48')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
