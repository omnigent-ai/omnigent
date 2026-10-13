"""Models and provenance metadata shared by session import layers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from omnigent.entities import MessageData, NewConversationItem
from omnigent.entities.conversation import synthesize_conversation_title

ImportSource = Literal["claude", "codex", "kimi", "kiro", "opencode", "pi", "qwen"]

# How messages name each harness ("Codex session '…' was not found").
IMPORT_SOURCE_LABELS: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "kimi": "Kimi",
    "kiro": "Kiro",
    "opencode": "OpenCode",
    "pi": "Pi",
    "qwen": "Qwen Code",
}

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


class SessionImportEmptyError(SessionImportNotFoundError):
    """Raised when a local session exists but holds no history to import.

    Typically a harness opened and closed without a prompt. Nothing is lost by
    not importing it, so callers report it as skipped rather than failed. It
    subclasses :class:`SessionImportNotFoundError` so callers that predate it
    still treat it as an unimportable session.
    """


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
    # Latest items the loader left out because the transcript was over the
    # import item cap (0 when nothing was trimmed or the count is unknown); see
    # ``omnigent.session_import.local.cap_import_items``.
    trimmed_item_count: int = 0
    # True when reading stopped at the item cap or the read byte budget, so an
    # uncounted amount of later history was left out; see
    # ``omnigent.session_import.local.IMPORT_READ_BUDGET_BYTES``.
    later_history_omitted: bool = False

    @property
    def title(self) -> str | None:
        """Prefer the harness's own title, else derive from the first user message."""
        return self.native_title or title_from_items(self.items)


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
