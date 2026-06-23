"""Shared types and helpers for importing external-harness chat transcripts.

An *adapter* knows how to (1) discover transcript files on disk for one
harness and (2) parse a single transcript file into a
:class:`ParsedTranscript` of Omnigent conversation items. The registry
(:mod:`omnigent.importers.registry`) then persists a parsed transcript into a
:class:`~omnigent.stores.conversation_store.ConversationStore`.

Adding a new harness (e.g. ``pi``) is a single new adapter module plus one
registry entry — nothing in the store, registry, or CLI layers needs to
change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from omnigent.db.utils import generate_task_id
from omnigent.entities import MessageData, NewConversationItem, synthesize_conversation_title

# Conversation label key stamped on every imported conversation; the value is
# the source harness name (e.g. ``"claude_code"``). Lets the server / UI tell
# imported chats apart from natively created ones, and gives a future importer
# a cheap way to find what it already brought in.
IMPORTED_FROM_LABEL_KEY = "imported_from"


@dataclass(frozen=True)
class TranscriptRef:
    """A transcript file discovered on disk.

    :param path: Absolute path to the JSONL transcript file.
    :param session_id: Harness-native session id hint derived at discovery
        time (Claude Code: the filename stem; Codex: the uuid embedded in the
        rollout filename). Used to satisfy ``--session ID`` lookups without
        parsing every file.
    """

    path: Path
    session_id: str


@dataclass
class ParsedTranscript:
    """The harness-agnostic result of parsing one transcript file.

    :param items: Ordered conversation items ready to ``append`` to a freshly
        created conversation. Empty when the file held no importable turns.
    :param external_session_id: Harness-native session id, persisted on the
        conversation so a future resume could locate the source. ``None`` when
        the transcript carried no id.
    :param title: Conversation title, or ``None`` when none could be derived.
    :param model: Resolved model/agent string used for every assistant-side
        item, or ``None`` when the transcript never named a model.
    :param cwd: Working directory recorded by the harness, stored as the
        conversation workspace. ``None`` when absent.
    :param git_branch: Git branch recorded by the harness, or ``None``.
    :param created_at: Earliest transcript timestamp as Unix epoch seconds, or
        ``None``. Informational only — the store stamps the conversation row
        with import time; the CLI surfaces this so the operator can see how old
        the imported chat is.
    """

    items: list[NewConversationItem] = field(default_factory=list)
    external_session_id: str | None = None
    title: str | None = None
    model: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    created_at: int | None = None


class TranscriptAdapter(ABC):
    """Per-harness importer: discovers transcripts and parses one file.

    Subclasses set :attr:`harness_name` and implement :meth:`default_root`,
    :meth:`discover`, and :meth:`parse`. All harness-specific knowledge —
    on-disk layout, envelope shape, field names — lives in the subclass; the
    registry and CLI treat every adapter the same way.
    """

    #: Stable harness identifier, e.g. ``"claude_code"``. Used as the registry
    #: key and the :data:`IMPORTED_FROM_LABEL_KEY` label value.
    harness_name: str

    @abstractmethod
    def default_root(self) -> Path:
        """Return the default on-disk directory holding this harness's
        transcripts, e.g. ``~/.claude/projects``.

        :returns: The default discovery root (``~`` already expanded).
        """
        ...

    @abstractmethod
    def discover(self, root: Path) -> list[TranscriptRef]:
        """Return every importable top-level transcript under *root*.

        Implementations skip sub-agent / sidechain files so v1 imports only
        top-level sessions.

        :param root: Directory to scan, e.g. ``~/.claude/projects``.
        :returns: Discovered transcript references; empty when *root* is
            missing or holds no transcripts.
        """
        ...

    @abstractmethod
    def parse(self, path: Path) -> ParsedTranscript:
        """Parse one transcript file into Omnigent conversation items.

        :param path: Path to a single JSONL transcript file.
        :returns: The parsed transcript (metadata + ordered items).
        """
        ...


class ResponseGrouper:
    """Assigns Omnigent ``response_id``s mirroring the live native-bridge
    turn grouping (see ``omnigent.claude_native_bridge``).

    The rules, matching how natively created conversations are grouped:

    - A user text message starts a fresh response and ends the active
      assistant turn (the next assistant-side item opens a new turn).
    - Assistant-side items (assistant message, reasoning, function/tool call)
      share the active turn's id.
    - A tool output (``function_call_output``) joins the active assistant turn
      — it answers the call that turn made — and does *not* end it.

    Ids are minted with :func:`~omnigent.db.utils.generate_task_id` (the same
    ``resp_`` helper the runtime uses), so imported turns are indistinguishable
    in shape from natively recorded ones.
    """

    def __init__(self) -> None:
        self._active: str | None = None

    def for_user_message(self) -> str:
        """Allocate a fresh response id for a user prompt and end the active
        assistant turn.

        :returns: A new ``resp_`` id.
        """
        response_id = generate_task_id()
        self._active = None
        return response_id

    def for_assistant_turn(self) -> str:
        """Return the active assistant turn's response id, opening one if no
        turn is active.

        :returns: The active (or freshly minted) ``resp_`` id.
        """
        if self._active is None:
            self._active = generate_task_id()
        return self._active

    def for_tool_output(self) -> str:
        """Return the response id a tool output belongs to: the active
        assistant turn, or a fresh one if the output is orphaned.

        :returns: The active (or freshly minted) ``resp_`` id.
        """
        if self._active is None:
            self._active = generate_task_id()
        return self._active


def title_from_first_user_message(items: list[NewConversationItem]) -> str | None:
    """Derive a fallback title from the first user message in *items*.

    Used when a transcript carries no explicit title. Delegates to
    :func:`~omnigent.entities.synthesize_conversation_title`, which truncates
    and strips attachment markers.

    :param items: Parsed conversation items in order.
    :returns: A one-line title, or ``None`` when no user text is present.
    """
    for item in items:
        if (
            item.type == "message"
            and isinstance(item.data, MessageData)
            and item.data.role == "user"
        ):
            title = synthesize_conversation_title(item.data.content)
            if title:
                return title
    return None


def earliest_timestamp(records: list[dict[str, object]]) -> int | None:
    """Return the earliest top-level ``timestamp`` across *records* as Unix
    epoch seconds.

    Both harnesses put the canonical per-record timestamp on the top-level
    envelope (Claude Code's transcript records and Codex's ``{timestamp, type,
    payload}`` envelopes), so a single scan of the decoded records yields the
    earliest transcript time regardless of harness.

    :param records: Decoded transcript records, each potentially carrying a
        top-level ``"timestamp"`` ISO-8601 string.
    :returns: The minimum parseable timestamp, or ``None`` when none parse.
    """
    epochs = [
        epoch
        for record in records
        if (epoch := epoch_from_iso8601(record.get("timestamp"))) is not None
    ]
    return min(epochs) if epochs else None


def epoch_from_iso8601(value: object) -> int | None:
    """Convert an ISO-8601 timestamp string to Unix epoch seconds.

    Tolerates the trailing ``Z`` (UTC) form both Claude Code and Codex write,
    which :func:`datetime.datetime.fromisoformat` accepts from Python 3.11.

    :param value: A timestamp string, e.g. ``"2026-05-29T17:55:29.106Z"``.
        Non-string or unparseable input yields ``None``.
    :returns: Unix epoch seconds, or ``None`` when *value* can't be parsed.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return int(parsed.timestamp())
