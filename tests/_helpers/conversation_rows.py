"""Read conversation rows straight from a database, bypassing the store's row lookups."""

from __future__ import annotations

from sqlalchemy import create_engine, select

from omnigent.db.db_models import SqlConversation, SqlConversationLabel


def conversation_rows(database_uri: str, conversation_id: str) -> dict[str, object]:
    """Return the conversation row count and label values stored for ``conversation_id``.

    A deleted conversation should have neither; labels written after the delete
    show up here even though ``get_conversation`` returns ``None``.

    :param database_uri: SQLAlchemy URL of the conversation database.
    :param conversation_id: Conversation identifier, e.g. ``"conv_abc123"``.
    :returns: ``{"conversation_rows": <int>, "labels": {key: value}}``.
    """
    engine = create_engine(database_uri)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                select(SqlConversation.id).where(SqlConversation.id == conversation_id)
            ).all()
            labels = connection.execute(
                select(SqlConversationLabel.key, SqlConversationLabel.value).where(
                    SqlConversationLabel.conversation_id == conversation_id
                )
            ).all()
    finally:
        engine.dispose()
    return {"conversation_rows": len(rows), "labels": dict(labels)}
