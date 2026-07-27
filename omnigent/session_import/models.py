"""Models and provenance metadata shared by session import layers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from omnigent.entities import MessageData, NewConversationItem
from omnigent.entities.conversation import synthesize_conversation_title

ImportSource = Literal["claude", "codex", "kimi", "kiro", "opencode", "pi", "qwen"]

IMPORT_SOURCE_LABEL_KEY = "omnigent.import.source"
IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY = "omnigent.import.external_session_id"
IMPORT_PROVENANCE_LABEL_KEYS = frozenset(
    {
        IMPORT_SOURCE_LABEL_KEY,
        IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY,
    }
)


class SessionImportNotFoundError(FileNotFoundError):
    """Raised when a requested local harness session cannot be found."""


@dataclass(frozen=True)
class LocalSessionImport:
    """One local transcript normalized for the import API."""

    source: ImportSource
    external_session_id: str
    workspace: str | None
    items: tuple[NewConversationItem, ...]

    @property
    def title(self) -> str | None:
        """Return a sidebar title derived from the first user message."""
        return title_from_items(self.items)


def title_from_items(items: Sequence[NewConversationItem]) -> str | None:
    """Return a sidebar title derived from the first user message."""
    for item in items:
        if (
            isinstance(item.data, MessageData)
            and item.data.role == "user"
            and not item.data.is_meta
        ):
            return synthesize_conversation_title(item.data.content)
    return None
