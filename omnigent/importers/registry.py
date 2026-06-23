"""Adapter registry and the core import functions.

The registry maps a harness name to its :class:`TranscriptAdapter`. Adding a
new harness (e.g. ``pi``) is one new adapter module plus one entry in
:data:`_ADAPTERS` — the import functions and CLI are harness-agnostic.

:func:`import_transcript` persists a single parsed transcript into a
:class:`~omnigent.stores.conversation_store.ConversationStore`; it mirrors how
``fork_conversation`` pre-populates a conversation: create the row, append the
items, then stamp identity (external session id) and the ``imported_from``
label. :func:`import_all` runs that over every transcript an adapter discovers.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.importers.base import IMPORTED_FROM_LABEL_KEY, ParsedTranscript, TranscriptAdapter
from omnigent.importers.claude_code import ClaudeCodeAdapter
from omnigent.importers.codex import CodexAdapter
from omnigent.stores.conversation_store import ConversationStore

# Harness name -> adapter. The single place to register a new harness.
_ADAPTERS: dict[str, TranscriptAdapter] = {
    ClaudeCodeAdapter.harness_name: ClaudeCodeAdapter(),
    CodexAdapter.harness_name: CodexAdapter(),
}


def available_harnesses() -> list[str]:
    """Return the registered harness names, sorted.

    :returns: e.g. ``["claude_code", "codex"]``.
    """
    return sorted(_ADAPTERS)


def get_adapter(harness: str) -> TranscriptAdapter:
    """Return the adapter for *harness*.

    :param harness: A registered harness name, e.g. ``"claude_code"``.
    :returns: The matching :class:`TranscriptAdapter`.
    :raises KeyError: If *harness* is not registered.
    """
    try:
        return _ADAPTERS[harness]
    except KeyError:
        raise KeyError(
            f"unknown harness {harness!r}; available: {', '.join(available_harnesses())}"
        ) from None


def persist_transcript(
    store: ConversationStore,
    harness_name: str,
    parsed: ParsedTranscript,
) -> str:
    """Persist an already-parsed transcript into *store*.

    Creates an agentless conversation (``agent_id`` stays NULL — an imported
    chat is read-only history, not a runnable session), appends its items,
    records the harness-native session id, and stamps the
    :data:`IMPORTED_FROM_LABEL_KEY` label.

    :param store: Destination conversation store.
    :param harness_name: Source harness, used as the ``imported_from`` value.
    :param parsed: The parsed transcript to persist.
    :returns: The new conversation id.
    """
    conversation = store.create_conversation(
        kind="default",
        title=parsed.title,
        workspace=parsed.cwd,
        git_branch=parsed.git_branch,
    )
    if parsed.items:
        store.append(conversation.id, parsed.items)
    if parsed.external_session_id:
        store.set_external_session_id(conversation.id, parsed.external_session_id)
    store.set_labels(conversation.id, {IMPORTED_FROM_LABEL_KEY: harness_name})
    return conversation.id


def import_transcript(
    store: ConversationStore,
    adapter: TranscriptAdapter,
    path: Path,
) -> str | None:
    """Parse one transcript file and persist it as a conversation.

    A transcript that parses to zero items (empty or unrecognized file) is
    skipped rather than persisted as an empty conversation.

    :param store: Destination conversation store.
    :param adapter: The harness adapter to parse with.
    :param path: Path to a single transcript file.
    :returns: The new conversation id, or ``None`` when the transcript held no
        importable items.
    """
    parsed = adapter.parse(path)
    if not parsed.items:
        return None
    return persist_transcript(store, adapter.harness_name, parsed)


def import_all(
    store: ConversationStore,
    adapter: TranscriptAdapter,
    root: Path,
) -> list[str]:
    """Discover and import every top-level transcript under *root*.

    Transcripts that parse to zero items are skipped (see
    :func:`import_transcript`), so the returned list may be shorter than the
    number of discovered files.

    :param store: Destination conversation store.
    :param adapter: The harness adapter to discover + parse with.
    :param root: Directory to scan.
    :returns: The new conversation ids, in discovery order.
    """
    ids = [import_transcript(store, adapter, ref.path) for ref in adapter.discover(root)]
    return [conversation_id for conversation_id in ids if conversation_id is not None]
