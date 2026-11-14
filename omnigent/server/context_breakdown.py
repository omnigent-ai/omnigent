"""Estimate what fills a session's context window, for the web breakdown view.

Every figure here is derived from content size (see
:data:`~omnigent.llms.errors.APPROX_BYTES_PER_TOKEN`), not from a tokenizer:
the provider-reported total on the session snapshot stays the measured value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from omnigent.entities import ConversationItem
from omnigent.entities.conversation import (
    CompactionData,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NativeToolData,
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


@dataclass
class _HistoryScan:
    """Accumulated history estimate while reading items newest first."""

    history: ContextBreakdownHistory = field(default_factory=ContextBreakdownHistory)
    # Each message's attachments in block order, messages newest first.
    attachment_groups: list[list[_AttachmentRef]] = field(default_factory=list)
    compaction_seen: bool = False
    summary_boundary_id: str | None = None
    summarized: bool = False


def _inline_payload_bytes(block: dict[str, object]) -> int | None:
    """Decoded size of a block's inline base64 payload, if it carries one."""
    for key in ("file_data", "image_url"):
        payload = block.get(key)
        if not isinstance(payload, str) or not payload:
            continue
        if payload.startswith("data:"):
            payload = payload.split(",", 1)[-1]
        elif "://" in payload:
            # A remote URL carries no payload of its own.
            return None
        return len(payload.rstrip("=")) * 3 // 4
    return None


def _message_tokens(data: MessageData, scan: _HistoryScan) -> int:
    """Estimate a message's text and record its attachments."""
    tokens = 0
    attachments: list[_AttachmentRef] = []
    for block in data.content:
        block_type = block.get("type")
        if block_type in _TEXT_BLOCK_TYPES:
            text = block.get("text")
            if isinstance(text, str):
                tokens += estimate_text_tokens(text)
        elif block_type in _ATTACHMENT_BLOCK_TYPES:
            file_id = block.get("file_id")
            filename = block.get("filename")
            attachments.append(
                _AttachmentRef(
                    file_id=file_id if isinstance(file_id, str) else None,
                    filename=filename if isinstance(filename, str) else None,
                    is_image=block_type == "input_image",
                    inline_bytes=_inline_payload_bytes(block),
                )
            )
    if attachments:
        scan.attachment_groups.append(attachments)
    return tokens


def _item_tokens(item: ConversationItem, scan: _HistoryScan) -> int | None:
    """
    Estimate one live item, or ``None`` when it is not model context.

    Reasoning is skipped: providers drop or encrypt prior reasoning.
    """
    data = item.data
    if isinstance(data, MessageData):
        return _message_tokens(data, scan)
    if isinstance(data, FunctionCallData):
        return estimate_text_tokens(data.name + data.arguments)
    if isinstance(data, FunctionCallOutputData):
        return _estimate_tool_output_tokens(data.output)
    if isinstance(data, NativeToolData):
        native_item = data.item
        if native_item.get("type") == "image_generation_call" and "result" in native_item:
            # The generated image is stored as bare base64 under ``result``.
            rest = {key: value for key, value in native_item.items() if key != "result"}
            return _estimate_without_binary(rest) + IMAGE_TOKEN_ESTIMATE
        return _estimate_without_binary(native_item)
    return None


def _apply_compaction(data: CompactionData, scan: _HistoryScan) -> None:
    """Start the summarized range at a valid compaction and count its summary."""
    anchor = data.last_item_id
    if not data.summary or not anchor or anchor.startswith("synthetic_"):
        return
    scan.summary_boundary_id = anchor
    # Native forwarders record 0; fall back to the summary's own size.
    tokens = data.token_count if data.token_count > 0 else estimate_text_tokens(data.summary)
    scan.history.summary_tokens = tokens
    scan.history.estimated_tokens += tokens


def _is_history_content(item: ConversationItem) -> bool:
    """Whether an item is a message or tool call/result a summary can replace."""
    return isinstance(
        item.data, MessageData | FunctionCallData | FunctionCallOutputData | NativeToolData
    )


def scan_history(
    items_newest_first: Iterable[ConversationItem],
    *,
    max_items: int = MAX_SCANNED_ITEMS,
) -> tuple[ContextBreakdownHistory, list[_AttachmentRef]]:
    """
    Estimate the live history from items read newest first.

    Only the latest compaction counts, as in the runtime's history load: items
    up to its ``last_item_id`` are replaced by its summary, and items after it
    are still sent in full. A compaction the runtime rejects (empty summary or
    synthetic anchor) leaves the full history live.

    :param items_newest_first: The session's items in descending position.
    :param max_items: Items to read before marking the result partial.
    :returns: The history estimate and live attachments, oldest first.
    """
    scan = _HistoryScan()
    history = scan.history
    for scanned, item in enumerate(items_newest_first):
        if scanned >= max_items:
            if scan.summarized:
                history.summarized_partial = True
            else:
                history.partial = True
            break
        if scan.summarized:
            if _is_history_content(item):
                history.summarized_item_count += 1
            continue
        if scan.summary_boundary_id is not None and item.id == scan.summary_boundary_id:
            scan.summarized = True
            if _is_history_content(item):
                history.summarized_item_count += 1
            continue
        if isinstance(item.data, CompactionData):
            if not scan.compaction_seen:
                scan.compaction_seen = True
                _apply_compaction(item.data, scan)
            continue
        tokens = _item_tokens(item, scan)
        if tokens is None:
            continue
        history.item_count += 1
        history.estimated_tokens += tokens
    attachments = [ref for group in reversed(scan.attachment_groups) for ref in group]
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
    :param native: The session runs a native harness, which hands every
        attachment to its CLI as a file path.
    :returns: The attachment row for the breakdown.
    """
    if ref.file_id is None:
        # Bytes carried inline in the message itself.
        size = ref.inline_bytes
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
    history, refs = scan_history(items_newest_first, max_items=max_items)
    native = is_native_harness(harness)
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
