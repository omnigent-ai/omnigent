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


# Preview text is one line in a picker row; longer strings only push the
# useful part of the message off screen.
_PREVIEW_TEXT_LIMIT = 240


@dataclass(frozen=True)
class LocalSessionPreviewMessage:
    """One visible message shown when a picker row is expanded.

    :param role: ``"user"`` or ``"assistant"``.
    :param text: Collapsed message text, truncated to 240 characters.
    """

    role: str
    text: str


@dataclass(frozen=True)
class LocalSessionSummary:
    """One local transcript described well enough to pick from a list.

    :param source: Harness that owns the session, e.g. ``"claude"``.
    :param external_session_id: Harness-native session id.
    :param workspace: Absolute working directory recorded in the
        transcript, or ``None`` when the harness records none.
    :param title: Title synthesized by :func:`title_from_items`, or
        ``None`` when the transcript has no usable user text.
    :param item_count: Number of Omnigent items the import would create.
    :param preview: Leading visible messages, for the expandable row.
    """

    source: ImportSource
    external_session_id: str
    workspace: str | None
    title: str | None
    item_count: int
    preview: tuple[LocalSessionPreviewMessage, ...]


def _preview_text(content: list[dict[str, object]]) -> str | None:
    """Collapse message content blocks into one truncated preview line."""
    parts = [
        text.strip()
        for block in content
        if isinstance(block, dict) and isinstance(text := block.get("text"), str)
    ]
    collapsed = " ".join(" ".join(parts).split())
    if not collapsed:
        return None
    if len(collapsed) > _PREVIEW_TEXT_LIMIT:
        return collapsed[: _PREVIEW_TEXT_LIMIT - 1] + "…"
    return collapsed


def summarize_local_session(
    imported: LocalSessionImport,
    *,
    preview_limit: int = 6,
) -> LocalSessionSummary:
    """Describe one loaded transcript for the import picker.

    The title comes from :func:`title_from_items` so a browsed row and
    the session it creates always read the same.

    :param imported: The loaded transcript.
    :param preview_limit: Maximum preview messages to keep.
    :returns: The picker-facing summary.
    """
    preview: list[LocalSessionPreviewMessage] = []
    for item in imported.items:
        if len(preview) >= preview_limit:
            break
        data = item.data
        if not isinstance(data, MessageData) or data.is_meta:
            continue
        if data.role not in ("user", "assistant"):
            continue
        text = _preview_text(data.content)
        if text is not None:
            preview.append(LocalSessionPreviewMessage(role=data.role, text=text))
    return LocalSessionSummary(
        source=imported.source,
        external_session_id=imported.external_session_id,
        workspace=imported.workspace,
        title=imported.title,
        item_count=len(imported.items),
        preview=tuple(preview),
    )


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
