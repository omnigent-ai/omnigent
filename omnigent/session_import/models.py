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
    # The harness's own session title (Claude's custom/ai title, Codex's thread
    # title) when the transcript carried one; None to fall back to the first
    # user message.
    native_title: str | None = None

    @property
    def title(self) -> str | None:
        """Prefer the harness's own title, else derive from the first user message."""
        return self.native_title or title_from_items(self.items)


def local_session_identity_matches(source: str, requested_id: str, canonical_id: str) -> bool:
    """
    Return whether a harness's canonical session id identifies the session a
    caller requested by ``requested_id``.

    Every loader except Qwen echoes the requested id as its
    ``external_session_id``, so identity is literal equality. Qwen accepts a
    bare recording id but canonicalizes it to a project-qualified
    ``<project>:<id>`` locator, so a bare request legitimately resolves to a
    qualified id whose recording component is the requested id. A request that
    is already qualified must still match the canonical id exactly.
    """
    if canonical_id == requested_id:
        return True
    if source == "qwen" and ":" not in requested_id:
        return canonical_id.rsplit(":", 1)[-1] == requested_id
    return False


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
