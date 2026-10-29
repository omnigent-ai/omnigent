"""admit the code_run item type in conversation_items

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-16 00:00:00.000000

Widens ``ck_conversation_items_type`` to accept ``12`` (``code_run``, a chat
code block run, see ``omnigent.db.enum_codecs.ITEM_TYPE``). Uses the batch
recipe from ``u1a2b3c4d5e6``: ``render_as_batch`` rebuilds the SQLite table so
the constraint swap lands; other dialects drop and re-add the constraint.
CockroachDB rejects re-adding a constraint name dropped in the same
transaction, so there the drop commits first, as in ``ar1b2c3d4e5f``, and the
swap checks the existing constraint so a retry after that commit resumes.

Deployment: additive; older application code never writes code 12. Roll back
by downgrading this revision, which deletes ``code_run`` rows first: older code
cannot decode them, and the narrower ``CHECK`` would refuse them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversation_items"
_CHECK = "ck_conversation_items_type"
# Pinned here rather than read from enum_codecs so the migration keeps the
# codes it was written against.
_CODE_RUN = 12
_PRIOR_CODES = tuple(range(1, _CODE_RUN))


# Casts the server may attach to literals, e.g. CockroachDB's ``1:::INT8``.
_LITERAL_CAST = re.compile(r"::+\w+")
_CHECK_KEYWORD = re.compile(r"CHECK\s*(?=\()", re.IGNORECASE)
_TYPE_IN = re.compile(r'(?:type|"type")\s+IN\s*\(\s*(\d+(?:\s*,\s*\d+)*)\s*\)', re.IGNORECASE)


def _strip_outer_parentheses(text: str) -> str:
    """Remove parentheses that wrap the whole expression, e.g. ``((a))`` to ``a``."""
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for index, char in enumerate(text):
            depth += {"(": 1, ")": -1}.get(char, 0)
            if depth == 0 and index < len(text) - 1:
                return text  # the first group closes early, as in ``(a) AND (b)``
        text = text[1:-1].strip()
    return text


def _parse_type_check_codes(sqltext: str) -> frozenset[int] | None:
    """Return the codes of an exact ``type IN (<integers>)`` CHECK, else ``None``."""
    text = _CHECK_KEYWORD.sub("", _LITERAL_CAST.sub("", sqltext).strip(), count=1)
    match = _TYPE_IN.fullmatch(_strip_outer_parentheses(text.strip()))
    if match is None:
        return None
    return frozenset(int(code) for code in match.group(1).split(","))


def _type_check_sqltext(bind: sa.Connection) -> str | None:
    """Return the item type ``CHECK`` expression, or ``None`` if it is absent."""
    for check in sa.inspect(bind).get_check_constraints(_TABLE):
        if check["name"] == _CHECK:
            return check["sqltext"]
    return None


def _set_type_check(codes: Sequence[int]) -> None:
    """Replace the item type ``CHECK`` with one admitting ``codes``."""
    bind = op.get_bind()
    condition = f"type IN ({', '.join(map(str, codes))})"
    if bind.dialect.name == "cockroachdb":
        # The checks let a retry resume after the committed drop below. Only a
        # recognized ``type IN`` with exactly these codes is left in place.
        current = _type_check_sqltext(bind)
        if current is not None and _parse_type_check_codes(current) == frozenset(codes):
            return
        if current is not None:
            with op.batch_alter_table(_TABLE) as batch_op:
                batch_op.drop_constraint(_CHECK, type_="check")
            # Publish the drop so the name can be reused in a new transaction.
            bind.commit()
            bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_check_constraint(_CHECK, condition)
        return
    recreate: Literal["always", "auto"] = "always" if bind.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(_TABLE, recreate=recreate) as batch_op:
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.create_check_constraint(_CHECK, condition)


def upgrade() -> None:
    _set_type_check((*_PRIOR_CODES, _CODE_RUN))


def downgrade() -> None:
    op.execute(f"DELETE FROM {_TABLE} WHERE type = {_CODE_RUN}")
    _set_type_check(_PRIOR_CODES)
