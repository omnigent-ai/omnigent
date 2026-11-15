"""Estimate what fills a session's context window, for the web breakdown view.

Every figure here is derived from content size (see
:data:`~omnigent.llms.errors.APPROX_BYTES_PER_TOKEN`), not from a tokenizer:
the provider-reported total on the session snapshot stays the measured value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from omnigent.entities import ConversationItem
from omnigent.entities.conversation import (
    CompactionData,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NativeToolData,
    external_item_id,
    is_omitted_binary_payload,
)
from omnigent.entities.file import StoredFile
from omnigent.errors import OmnigentError
from omnigent.harness_aliases import is_native_harness
from omnigent.inner.native_attachments import uses_filesystem_delivery
from omnigent.llms.adapters._content import redact_binary_payloads
from omnigent.llms.errors import APPROX_BYTES_PER_TOKEN
from omnigent.mcp_selection import selected_mcp_spec
from omnigent.runtime.content_resolver import is_text_like_attachment
from omnigent.runtime.prompt import (
    build_instructions_nullable,
    build_native_startup_instructions,
    dedupe_tool_output_images,
)
from omnigent.server.schemas import (
    ContextBreakdownAttachment,
    ContextBreakdownHistory,
    ContextBreakdownTools,
    SessionContextBreakdown,
)
from omnigent.spec.types import AgentSpec

# Roughly one full-resolution image on current vision models; its token cost
# does not follow its encoded size.
IMAGE_TOKEN_ESTIMATE = 1_600

# Most recent items read per breakdown, so a very long session stays cheap.
MAX_SCANNED_ITEMS = 2_000

_TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text", "text", "refusal"})
_ATTACHMENT_BLOCK_TYPES = frozenset({"input_image", "input_file"})
# Provider-shaped binary blocks, e.g. in a native harness's checkpoint messages.
_PROVIDER_BINARY_BLOCK_TYPES = frozenset({"image", "document"})
_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
# Bookkeeping keys on a retained checkpoint entry that are never sent to the model.
_SNAPSHOT_ENVELOPE_KEYS = frozenset({"id", "forwarded_source_ids", "message_id", "status"})


def estimate_text_tokens(text: str) -> int:
    """
    Estimate prompt tokens for *text* from its UTF-8 size.

    :param text: Any prompt text, e.g. ``"Hello"``.
    :returns: Estimated tokens, rounded up, e.g. ``2``.
    """
    return _bytes_to_tokens(len(text.encode("utf-8")))


def _bytes_to_tokens(size: int) -> int:
    """Estimated tokens for *size* bytes of text, rounded up."""
    return -(-size // APPROX_BYTES_PER_TOKEN)


def _estimate_without_binary(value: object) -> int:
    """Estimate structured content, counting each embedded image as one image."""
    images = 0

    def drop_payload(media_type: str, _length: int) -> str:
        nonlocal images
        if media_type.startswith("image/"):
            images += 1
        return ""

    stripped = redact_binary_payloads(value, drop_payload)
    text = stripped if isinstance(stripped, str) else json.dumps(stripped, ensure_ascii=False)
    return estimate_text_tokens(text) + images * IMAGE_TOKEN_ESTIMATE


def _estimate_tool_output_tokens(output: str) -> int:
    """Estimate a tool result as it is replayed, with stored images reduced to placeholders."""
    return _estimate_without_binary(dedupe_tool_output_images(output))


def estimate_system_prompt_tokens(spec: AgentSpec, harness: str | None) -> int:
    """
    Estimate the instructions Omnigent composes for *spec*.

    :param spec: The session's resolved agent spec.
    :param harness: The session's canonical harness, e.g. ``"claude-native"``.
        Native harnesses receive only the startup instructions.
    :returns: Estimated tokens; ``0`` when nothing is composed.
    """
    if is_native_harness(harness):
        text: str | None = build_native_startup_instructions(spec)
    else:
        text = build_instructions_nullable(spec, None, [])
    return estimate_text_tokens(text) if text else 0


def estimate_skill_listing_tokens(spec: AgentSpec) -> int:
    """
    Estimate the skill name/description listing a harness shows the model.

    :param spec: The session's resolved agent spec.
    :returns: Estimated tokens; ``0`` without bundled skills.
    """
    listing = "\n".join(f"- {skill.name}: {skill.description}" for skill in spec.skills)
    return estimate_text_tokens(listing) if listing else 0


def summarize_tools(spec: AgentSpec, labels: dict[str, str]) -> ContextBreakdownTools:
    """
    List the tool sources the session's agent declares.

    :param spec: The session's resolved agent spec.
    :param labels: Session labels carrying per-session MCP selections.
    :returns: Enabled MCP servers and tool counts.
    """
    try:
        selected = selected_mcp_spec(spec, labels)
    except OmnigentError:
        # A malformed selection label fails the turn elsewhere; list the declared servers.
        selected = spec
    return ContextBreakdownTools(
        mcp_servers=[server.name for server in selected.mcp_servers],
        local_tool_count=len(spec.local_tools),
        builtin_tool_count=len(spec.tools.builtins),
    )


@dataclass
class _AttachmentRef:
    """An attachment block found in the live history, before its file lookup."""

    file_id: str | None
    filename: str | None
    is_image: bool
    inline_bytes: int | None
    has_data_uri: bool = False
    # A compaction snapshot replaced the payload with a marker.
    omitted: bool = False
    # A provider-shaped block a checkpoint replays to the model as-is.
    provider_block: bool = False


@dataclass
class _LiveEntry:
    """One live item's estimate, kept until overlap with a checkpoint is known."""

    item_id: str
    tokens: int
    attachments: list[_AttachmentRef]


@dataclass
class _Checkpoint:
    """What the model receives in place of the compacted history."""

    tokens: int
    attachments: list[_AttachmentRef]
    # Items the checkpoint's retained messages already carry.
    covered_item_ids: set[str]


def _block_payload(block: dict[str, Any]) -> object:
    """The data-URI field a native harness decodes (``image_url`` first)."""
    return block.get("image_url") or block.get("file_data")


def _inline_payload_bytes(block: dict[str, Any]) -> int | None:
    """Decoded size of a block's inline base64 payload, if it carries one."""
    for key in ("file_data", "image_url"):
        payload = block.get(key)
        if not isinstance(payload, str) or not payload or is_omitted_binary_payload(payload):
            continue
        if payload.startswith("data:"):
            payload = payload.split(",", 1)[-1]
        elif "://" in payload:
            # A remote URL carries no payload of its own.
            return None
        return len(payload.rstrip("=")) * 3 // 4
    return None


def _attachment_ref(block: dict[str, Any]) -> _AttachmentRef:
    """Describe an ``input_image`` / ``input_file`` block."""
    file_id = block.get("file_id")
    filename = block.get("filename")
    payload = _block_payload(block)
    return _AttachmentRef(
        file_id=file_id if isinstance(file_id, str) else None,
        filename=filename if isinstance(filename, str) else None,
        is_image=block.get("type") == "input_image",
        inline_bytes=_inline_payload_bytes(block),
        has_data_uri=isinstance(payload, str) and payload.startswith("data:"),
        omitted=is_omitted_binary_payload(payload),
    )


def _provider_block_ref(block: dict[str, Any]) -> _AttachmentRef:
    """Describe a provider-shaped ``image`` / ``document`` block from a checkpoint."""
    source = block.get("source")
    data = source.get("data") if isinstance(source, dict) else None
    return _AttachmentRef(
        file_id=None,
        filename=None,
        is_image=block.get("type") == "image",
        inline_bytes=None,
        omitted=is_omitted_binary_payload(data),
        provider_block=True,
    )


def _snapshot_block_tokens(block: dict[str, Any]) -> tuple[int, _AttachmentRef | None]:
    """Estimate a checkpoint block outside the plain text and attachment types."""
    block_type = block.get("type")
    if block_type in _THINKING_BLOCK_TYPES:
        # Prior-turn thinking does not count toward the input context.
        return 0, None
    if block_type in _PROVIDER_BINARY_BLOCK_TYPES:
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "text":
            data = source.get("data")
            return (estimate_text_tokens(data) if isinstance(data, str) else 0), None
        return 0, _provider_block_ref(block)
    # Tool calls and results inside a transcript message.
    return _estimate_without_binary(block), None


def _content_tokens(
    content: object, *, snapshot: bool = False
) -> tuple[int, list[_AttachmentRef]]:
    """
    Estimate message content and collect its attachments, in block order.

    Live messages count text and attachment blocks; a checkpoint's retained
    transcript messages also carry tool and provider blocks that it replays.
    """
    if isinstance(content, str):
        return estimate_text_tokens(content), []
    tokens = 0
    attachments: list[_AttachmentRef] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in _TEXT_BLOCK_TYPES:
            text = block.get("text")
            if isinstance(text, str):
                tokens += estimate_text_tokens(text)
        elif block_type in _ATTACHMENT_BLOCK_TYPES:
            attachments.append(_attachment_ref(block))
        elif snapshot:
            block_tokens, ref = _snapshot_block_tokens(block)
            tokens += block_tokens
            if ref is not None:
                attachments.append(ref)
    return tokens, attachments


def _snapshot_entry_tokens(entry: dict[str, Any]) -> tuple[int, list[_AttachmentRef]]:
    """Estimate one retained checkpoint entry: a message, or a tool call/output record."""
    if entry.get("type", "message") == "message" and "content" in entry:
        return _content_tokens(entry["content"], snapshot=True)
    payload = {key: value for key, value in entry.items() if key not in _SNAPSHOT_ENVELOPE_KEYS}
    return _estimate_without_binary(payload), []


def _item_tokens(item: ConversationItem) -> tuple[int, list[_AttachmentRef]] | None:
    """
    Estimate one live item, or ``None`` when it is not model context.

    Reasoning is skipped: providers drop or encrypt prior reasoning.
    """
    data = item.data
    if isinstance(data, MessageData):
        return _content_tokens(data.content)
    if isinstance(data, FunctionCallData):
        return estimate_text_tokens(data.name + data.arguments), []
    if isinstance(data, FunctionCallOutputData):
        return _estimate_tool_output_tokens(data.output), []
    if isinstance(data, NativeToolData):
        native_item = data.item
        if native_item.get("type") == "image_generation_call" and "result" in native_item:
            # The generated image is stored as bare base64 under ``result``.
            rest = {key: value for key, value in native_item.items() if key != "result"}
            return _estimate_without_binary(rest) + IMAGE_TOKEN_ESTIMATE, []
        return _estimate_without_binary(native_item), []
    return None


def _checkpoint(
    data: CompactionData, *, session_id: str | None, native: bool
) -> _Checkpoint | None:
    """
    Size what a compaction replays, or ``None`` when replay ignores it.

    Retained ``compacted_messages`` replace the summary when present. The
    runtime rejects an empty summary or synthetic anchor; a native harness
    replays only checkpoints with retained messages and skips live items whose
    ``forwarded_source_ids`` they already carry.
    """
    if native:
        if not data.compacted_messages:
            return None
    else:
        anchor = data.last_item_id
        if not data.summary or not anchor or anchor.startswith("synthetic_"):
            return None
    if not data.compacted_messages:
        # Native forwarders record 0; fall back to the summary's own size.
        tokens = data.token_count if data.token_count > 0 else estimate_text_tokens(data.summary)
        return _Checkpoint(tokens=tokens, attachments=[], covered_item_ids=set())
    tokens = 0
    attachments: list[_AttachmentRef] = []
    covered: set[str] = set()
    for entry in data.compacted_messages:
        if not isinstance(entry, dict):
            continue
        entry_tokens, entry_attachments = _snapshot_entry_tokens(entry)
        tokens += entry_tokens
        attachments.extend(entry_attachments)
        source_ids = entry.get("forwarded_source_ids")
        if native and session_id is not None and isinstance(source_ids, list):
            covered.update(
                external_item_id(session_id, source_id)
                for source_id in source_ids
                if isinstance(source_id, str)
            )
    return _Checkpoint(tokens=tokens, attachments=attachments, covered_item_ids=covered)


def _is_history_content(item: ConversationItem) -> bool:
    """Whether an item is a message or tool call/result a summary can replace."""
    return isinstance(
        item.data, MessageData | FunctionCallData | FunctionCallOutputData | NativeToolData
    )


def scan_history(
    items_newest_first: Iterable[ConversationItem],
    *,
    session_id: str | None = None,
    native: bool = False,
    max_items: int = MAX_SCANNED_ITEMS,
) -> tuple[ContextBreakdownHistory, list[_AttachmentRef]]:
    """
    Estimate the live history from items read newest first.

    The runtime replays the latest compaction (unless it rejects it) in place
    of the items up to its ``last_item_id``. A native harness replays the latest
    compaction with retained messages in place of everything before it, and
    skips later items those messages already carry.

    :param items_newest_first: The session's items in descending position.
    :param session_id: The session, used to match a checkpoint's
        ``forwarded_source_ids`` to item ids; ``None`` skips that matching.
    :param native: The session runs a native harness.
    :param max_items: Items to read before marking the result partial.
    :returns: The history estimate and live attachments, oldest first.
    """
    history = ContextBreakdownHistory()
    live: list[_LiveEntry] = []
    checkpoint: _Checkpoint | None = None
    compaction_seen = False
    boundary_id: str | None = None
    summarized = False
    for scanned, item in enumerate(items_newest_first):
        if scanned >= max_items:
            if summarized:
                history.summarized_partial = True
            else:
                history.partial = True
            break
        if summarized or (boundary_id is not None and item.id == boundary_id):
            summarized = True
            if _is_history_content(item):
                history.summarized_item_count += 1
            continue
        if isinstance(item.data, CompactionData):
            if native:
                if checkpoint is None:
                    checkpoint = _checkpoint(item.data, session_id=session_id, native=True)
                    # Native replay drops every record before its checkpoint.
                    summarized = checkpoint is not None
            elif not compaction_seen:
                compaction_seen = True
                checkpoint = _checkpoint(item.data, session_id=session_id, native=False)
                if checkpoint is not None:
                    boundary_id = item.data.last_item_id
            continue
        estimate = _item_tokens(item)
        if estimate is not None:
            live.append(_LiveEntry(item.id, *estimate))
    covered = checkpoint.covered_item_ids if checkpoint is not None else set()
    attachments: list[_AttachmentRef] = []
    if checkpoint is not None:
        history.summary_tokens = checkpoint.tokens
        history.estimated_tokens += checkpoint.tokens
        attachments.extend(checkpoint.attachments)
    for entry in reversed(live):
        if entry.item_id in covered:
            continue
        history.item_count += 1
        history.estimated_tokens += entry.tokens
        attachments.extend(entry.attachments)
    return history, attachments


def resolve_attachment(
    ref: _AttachmentRef,
    lookup_file: Callable[[str], StoredFile | None],
    *,
    native: bool = False,
) -> ContextBreakdownAttachment:
    """
    Resolve an attachment's size, delivery and token estimate.

    :param ref: The attachment block found in the history.
    :param lookup_file: Returns the session's stored file for an id, or ``None``.
    :param native: The session runs a native harness, which hands each
        attachment it can write to disk to its CLI as a file path.
    :returns: The attachment row for the breakdown.
    """
    if ref.file_id is None:
        # Bytes carried inline in the message itself.
        size = ref.inline_bytes
        if ref.omitted:
            # A compaction snapshot dropped the payload, so only a marker remains.
            return ContextBreakdownAttachment(filename=ref.filename, delivery="missing")
        if ref.provider_block:
            # Replayed to the model as-is from the checkpoint.
            return ContextBreakdownAttachment(
                delivery="inline",
                estimated_tokens=IMAGE_TOKEN_ESTIMATE if ref.is_image else None,
            )
        if native:
            # Only a data URI materializes; a remote URL or bare base64 reaches
            # the CLI as a "could not be loaded" marker.
            return ContextBreakdownAttachment(
                filename=ref.filename,
                bytes=size,
                delivery="workspace" if ref.has_data_uri else "missing",
            )
        tokens: int | None = None
        if ref.is_image:
            tokens = IMAGE_TOKEN_ESTIMATE
        elif size is not None and is_text_like_attachment("", ref.filename):
            tokens = _bytes_to_tokens(size)
        return ContextBreakdownAttachment(
            filename=ref.filename, bytes=size, delivery="inline", estimated_tokens=tokens
        )
    stored = lookup_file(ref.file_id)
    if stored is None:
        return ContextBreakdownAttachment(
            file_id=ref.file_id, filename=ref.filename, delivery="missing"
        )
    filename = stored.filename or ref.filename
    content_type = stored.content_type
    if native or uses_filesystem_delivery(filename, stored.source_metadata):
        return ContextBreakdownAttachment(
            file_id=ref.file_id,
            filename=filename,
            content_type=content_type,
            bytes=stored.bytes,
            delivery="workspace",
        )
    estimated: int | None = None
    if ref.is_image or (content_type or "").startswith("image/"):
        estimated = IMAGE_TOKEN_ESTIMATE
    elif is_text_like_attachment(content_type or "", filename):
        estimated = _bytes_to_tokens(stored.bytes)
    return ContextBreakdownAttachment(
        file_id=ref.file_id,
        filename=filename,
        content_type=content_type,
        bytes=stored.bytes,
        delivery="inline",
        estimated_tokens=estimated,
    )


def build_context_breakdown(
    session_id: str,
    *,
    spec: AgentSpec | None,
    harness: str | None,
    labels: dict[str, str],
    items_newest_first: Iterable[ConversationItem],
    lookup_file: Callable[[str], StoredFile | None],
    max_items: int = MAX_SCANNED_ITEMS,
) -> SessionContextBreakdown:
    """
    Estimate what fills a session's context window.

    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param spec: The session's resolved agent spec, or ``None`` when it could
        not load (the prompt, skill and tool rows are then left empty).
    :param harness: The session's canonical harness, e.g. ``"claude-sdk"``.
    :param labels: Session labels carrying per-session MCP selections.
    :param items_newest_first: The session's items in descending position.
    :param lookup_file: Returns the session's stored file for an id, or ``None``.
    :param max_items: Items to read before marking the history partial.
    :returns: The estimated breakdown.
    """
    native = is_native_harness(harness)
    history, refs = scan_history(
        items_newest_first, session_id=session_id, native=native, max_items=max_items
    )
    breakdown = SessionContextBreakdown(
        session_id=session_id,
        history=history,
        attachments=[resolve_attachment(ref, lookup_file, native=native) for ref in refs],
        bytes_per_token=APPROX_BYTES_PER_TOKEN,
    )
    if spec is not None:
        breakdown.system_prompt_tokens = estimate_system_prompt_tokens(spec, harness)
        breakdown.skill_count = len(spec.skills)
        breakdown.skill_listing_tokens = estimate_skill_listing_tokens(spec)
        breakdown.tools = summarize_tools(spec, labels)
    return breakdown
