"""Convert enum-like varchar columns to SMALLINT int codes.

Revision ID: u1a2b3c4d5e6
Revises: t1a2b3c4d5e6
Create Date: 2026-07-07

Several low-cardinality closed-set columns were stored as ``VARCHAR``
guarded by string ``CHECK`` constraints. This migration converts them to
compact ``SMALLINT`` integer codes (client-side name↔int conversion lives
in ``omnigent.db.enum_codecs``), matching the existing int-coded
``session_permissions.level``. The string names remain the contract above
the store layer, so only the stored representation changes.

Columns converted (name → code):

- ``conversations.kind``          default=1, sub_agent=2
- ``conversation_items.type``     message=1 … terminal_command=11
- ``conversation_items.status``   completed=1 (in_progress=2, incomplete=3,
                                   failed=4 reserved)
- ``comments.status``             draft=1, addressed=2
- ``account_tokens.kind``         invite=1, magic=2
- ``policies.type``               python=1, url=2
- ``hosts.status``                online=1, offline=2

Each column is converted with the add-int-column → backfill-with-``CASE`` →
drop-old-column → rename pattern (portable across SQLite and PostgreSQL),
swapping the string ``CHECK`` for an integer one. ``render_as_batch`` (see
migrations/env.py) rebuilds the SQLite table so the constraint swap lands.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "u1a2b3c4d5e6"
down_revision: str | None = "t1a2b3c4d5e6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


# Name → int code, mirroring omnigent.db.enum_codecs. Duplicated here on
# purpose: a migration must be pinned to the codes as they were when it was
# written, independent of later edits to the codec module.
_CONVERSATION_KIND = {"default": 1, "sub_agent": 2}
_ITEM_TYPE = {
    "message": 1,
    "function_call": 2,
    "function_call_output": 3,
    "reasoning": 4,
    "error": 5,
    "compaction": 6,
    "native_tool": 7,
    "resource_event": 8,
    "routing_decision": 9,
    "slash_command": 10,
    "terminal_command": 11,
}
_ITEM_STATUS = {"completed": 1, "in_progress": 2, "incomplete": 3, "failed": 4}
_COMMENT_STATUS = {"draft": 1, "addressed": 2}
_ACCOUNT_TOKEN_KIND = {"invite": 1, "magic": 2}
_POLICY_TYPE = {"python": 1, "url": 2}
_POLICY_SCOPE = {"default": 1, "session": 2}
_HOST_STATUS = {"online": 1, "offline": 2}
_AGENT_KIND = {"template": 1, "session": 2}


def _case_sql(column: str, mapping: dict[str, int]) -> str:
    """Build a ``CASE`` expression mapping string names to int codes."""
    whens = " ".join(f"WHEN '{name}' THEN {code}" for name, code in mapping.items())
    return f"CASE {column} {whens} END"


def _case_sql_reverse(column: str, mapping: dict[str, int]) -> str:
    """Build a ``CASE`` expression mapping int codes back to string names."""
    whens = " ".join(f"WHEN {code} THEN '{name}'" for name, code in mapping.items())
    return f"CASE {column} {whens} END"


def _int_check(mapping: dict[str, int]) -> str:
    """Build an ``IN (...)`` predicate over the mapping's int codes."""
    codes = ", ".join(str(c) for c in sorted(mapping.values()))
    return f"{{col}} IN ({codes})"


def _string_check(mapping: dict[str, int]) -> str:
    """Build an ``IN (...)`` predicate over the mapping's string names."""
    names = ", ".join(f"'{n}'" for n in mapping)
    return f"{{col}} IN ({names})"


def _swap_to_int(
    table: str,
    column: str,
    mapping: dict[str, int],
    *,
    check_name: str | None,
    nullable: bool,
) -> None:
    """
    Replace a string enum *column* with an int-coded ``SmallInteger``.

    Adds ``<column>_int``, backfills it from the string values via ``CASE``,
    then drops the old column, renames the new one into place, and (re)creates
    the integer ``CHECK``. ``check_name`` drops a pre-existing string ``CHECK``
    of that name inside the batch rebuild; pass ``None`` when the column has no
    ``CHECK`` today.
    """
    tmp = f"{column}_int"
    op.add_column(table, sa.Column(tmp, sa.SmallInteger(), nullable=True))
    op.execute(f"UPDATE {table} SET {tmp} = {_case_sql(column, mapping)}")
    recreate = "always" if _is_sqlite() else "auto"
    with op.batch_alter_table(table, recreate=recreate) as batch_op:
        if check_name is not None:
            batch_op.drop_constraint(check_name, type_="check")
        batch_op.drop_column(column)
        # existing_type required by MySQL for CHANGE/MODIFY COLUMN.
        batch_op.alter_column(
            tmp, new_column_name=column, nullable=nullable, existing_type=sa.SmallInteger()
        )
        batch_op.create_check_constraint(
            check_name or f"ck_{table}_{column}",
            _int_check(mapping).format(col=column),
        )


def _swap_to_string(
    table: str,
    column: str,
    mapping: dict[str, int],
    *,
    check_name: str | None,
    nullable: bool,
    length: int,
) -> None:
    """Inverse of :func:`_swap_to_int` — restore the string enum column."""
    tmp = f"{column}_str"
    op.add_column(table, sa.Column(tmp, sa.String(length=length), nullable=True))
    op.execute(f"UPDATE {table} SET {tmp} = {_case_sql_reverse(column, mapping)}")
    recreate = "always" if _is_sqlite() else "auto"
    with op.batch_alter_table(table, recreate=recreate) as batch_op:
        batch_op.drop_constraint(check_name or f"ck_{table}_{column}", type_="check")
        batch_op.drop_column(column)
        # existing_type required by MySQL for CHANGE/MODIFY COLUMN.
        batch_op.alter_column(
            tmp,
            new_column_name=column,
            nullable=nullable,
            existing_type=sa.String(length=length),
        )
        if check_name is not None:
            batch_op.create_check_constraint(check_name, _string_check(mapping).format(col=column))


def _recreate_conversations_indexes(*, kind_is_int: bool) -> None:
    """
    Recreate the ``conversations`` indexes dropped for the ``kind`` swap.

    The two partial indexes and the plain ``kind`` index are dropped before
    the batch rebuild (SQLite batch mode can't copy a partial-index predicate
    across a column swap) and recreated here. ``kind_is_int`` selects the
    predicate literal for ``idx_conversations_parent`` — ``kind = 2`` after the
    upgrade, ``kind = 'sub_agent'`` after a downgrade.
    """
    op.create_index("ix_conversations_kind", "conversations", ["kind"])
    op.create_index(
        "ix_conversations_parent_title_unique",
        "conversations",
        ["parent_conversation_id", "title"],
        unique=True,
        sqlite_where=sa.text("parent_conversation_id IS NOT NULL"),
        postgresql_where=sa.text("parent_conversation_id IS NOT NULL"),
        mysql_length={"title": 512},
    )
    sub_agent = "2" if kind_is_int else "'sub_agent'"
    op.create_index(
        "idx_conversations_parent",
        "conversations",
        ["parent_conversation_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
        sqlite_where=sa.text(f"kind = {sub_agent}"),
        postgresql_where=sa.text(f"kind = {sub_agent}"),
    )


def _index_exists(table: str, index_name: str) -> bool:
    """Return True if *index_name* exists on *table* in the current schema."""
    return any(idx["name"] == index_name for idx in sa.inspect(op.get_bind()).get_indexes(table))


def _drop_conversations_kind_indexes() -> None:
    """Drop the ``conversations`` indexes that block the ``kind`` batch swap."""
    op.drop_index("idx_conversations_parent", table_name="conversations")
    # Guard against the case where a later migration (w1a2b3c4d5e6) already
    # dropped this index during its own downgrade.
    if _index_exists("conversations", "ix_conversations_parent_title_unique"):
        op.drop_index("ix_conversations_parent_title_unique", table_name="conversations")
    op.drop_index("ix_conversations_kind", table_name="conversations")


def _drop_agents_kind_index() -> None:
    """Drop the partial index whose predicate references ``agents.kind``."""
    op.drop_index("ix_agents_template_name", table_name="agents")


def _recreate_agents_kind_index(*, kind_is_int: bool) -> None:
    """Recreate ``ix_agents_template_name`` (partial on the template kind)."""
    template = "1" if kind_is_int else "'template'"
    op.create_index(
        "ix_agents_template_name",
        "agents",
        ["name"],
        unique=True,
        sqlite_where=sa.text(f"kind = {template}"),
        postgresql_where=sa.text(f"kind = {template}"),
    )


def _drop_policies_scope_index() -> None:
    """Drop the partial index whose predicate references ``policies.scope``."""
    op.drop_index("ix_policies_default_name", table_name="policies")


def _recreate_policies_scope_index(*, scope_is_int: bool) -> None:
    """Recreate ``ix_policies_default_name`` (partial on the default scope)."""
    default = "1" if scope_is_int else "'default'"
    op.create_index(
        "ix_policies_default_name",
        "policies",
        ["name"],
        unique=True,
        sqlite_where=sa.text(f"scope = {default}"),
        postgresql_where=sa.text(f"scope = {default}"),
    )


def upgrade() -> None:
    """Convert every enum-like varchar column to a SMALLINT int code."""
    sqlite = _is_sqlite()
    # SQLite runs migrations with foreign_keys ON; a batch table-rebuild then
    # cascade-deletes child rows through the ON DELETE CASCADE FKs that point at
    # the rebuilt table. Disable enforcement for the rebuilds (SQLite-only), and
    # restore it after. Matches the p1/o1 migrations' guard.
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # conversations has two partial indexes and a plain index on kind; SQLite
    # batch mode can't copy a partial-index predicate across a column swap, so
    # drop all three, swap the column, then recreate them (idx_conversations_
    # parent's predicate now compares the int code).
    _drop_conversations_kind_indexes()
    _swap_to_int(
        "conversations",
        "kind",
        _CONVERSATION_KIND,
        check_name="ck_conversations_kind",
        nullable=False,
    )
    _recreate_conversations_indexes(kind_is_int=True)

    _swap_to_int(
        "conversation_items",
        "type",
        _ITEM_TYPE,
        check_name=None,
        nullable=False,
    )
    _swap_to_int(
        "conversation_items",
        "status",
        _ITEM_STATUS,
        check_name=None,
        nullable=False,
    )
    _swap_to_int(
        "comments",
        "status",
        _COMMENT_STATUS,
        check_name=None,
        nullable=False,
    )
    _swap_to_int(
        "account_tokens",
        "kind",
        _ACCOUNT_TOKEN_KIND,
        check_name="ck_account_tokens_kind",
        nullable=False,
    )
    # policies has a partial index (ix_policies_default_name) whose predicate
    # references scope; drop it around both policy-column swaps so the batch
    # rebuild doesn't copy a stale predicate, then recreate against the code.
    _drop_policies_scope_index()
    _swap_to_int(
        "policies",
        "type",
        _POLICY_TYPE,
        check_name=None,
        nullable=False,
    )
    _swap_to_int(
        "policies",
        "scope",
        _POLICY_SCOPE,
        check_name=None,
        nullable=False,
    )
    _recreate_policies_scope_index(scope_is_int=True)
    _swap_to_int(
        "hosts",
        "status",
        _HOST_STATUS,
        check_name="ck_hosts_status",
        nullable=False,
    )
    # agents has a partial index (ix_agents_template_name) whose predicate
    # references kind; drop it around the swap and recreate against the code.
    _drop_agents_kind_index()
    _swap_to_int(
        "agents",
        "kind",
        _AGENT_KIND,
        check_name=None,
        nullable=False,
    )
    _recreate_agents_kind_index(kind_is_int=True)

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """Restore the original string enum columns and their CHECKs."""
    sqlite = _is_sqlite()
    # Same FK guard as upgrade(): the batch rebuilds below would otherwise
    # cascade-delete child rows through ON DELETE CASCADE FKs on SQLite.
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    _drop_conversations_kind_indexes()
    _swap_to_string(
        "conversations",
        "kind",
        _CONVERSATION_KIND,
        check_name="ck_conversations_kind",
        nullable=False,
        length=32,
    )
    _recreate_conversations_indexes(kind_is_int=False)

    _swap_to_string(
        "conversation_items", "type", _ITEM_TYPE, check_name=None, nullable=False, length=32
    )
    _swap_to_string(
        "conversation_items",
        "status",
        _ITEM_STATUS,
        check_name=None,
        nullable=False,
        length=32,
    )
    _swap_to_string(
        "comments", "status", _COMMENT_STATUS, check_name=None, nullable=False, length=32
    )
    _swap_to_string(
        "account_tokens",
        "kind",
        _ACCOUNT_TOKEN_KIND,
        check_name="ck_account_tokens_kind",
        nullable=False,
        length=16,
    )
    _drop_policies_scope_index()
    _swap_to_string("policies", "type", _POLICY_TYPE, check_name=None, nullable=False, length=16)
    _swap_to_string("policies", "scope", _POLICY_SCOPE, check_name=None, nullable=False, length=16)
    _recreate_policies_scope_index(scope_is_int=False)
    _swap_to_string(
        "hosts", "status", _HOST_STATUS, check_name="ck_hosts_status", nullable=False, length=16
    )
    _drop_agents_kind_index()
    _swap_to_string("agents", "kind", _AGENT_KIND, check_name=None, nullable=False, length=16)
    _recreate_agents_kind_index(kind_is_int=False)

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
