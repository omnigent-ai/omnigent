"""Typed dataclasses for API response objects."""

from __future__ import annotations

from dataclasses import dataclass, field


def _json_int(value: object) -> int:
    """Parse an integer-compatible JSON scalar."""
    if isinstance(value, (bool, int, float, str)):
        return int(value)
    raise TypeError(f"expected a JSON number or numeric string, got {type(value).__name__}")


@dataclass
class ConversationRef:
    """Reference to a conversation, as returned on response objects."""

    id: str


@dataclass
class Usage:
    """
    Token usage statistics for a completed response.

    :param input_tokens: Number of input (prompt) tokens consumed
        (sum across all LLM sub-calls for multi-call turns).
    :param output_tokens: Number of output (completion) tokens
        generated (sum across all LLM sub-calls).
    :param total_tokens: Billing total — ``input_tokens + output_tokens``
        summed across all sub-calls.
    :param context_tokens: Context-fill estimate for the next turn.
        Present only for executors that make multiple LLM sub-calls per
        turn (e.g. ``openai-agents``); ``None`` for single-call
        executors where ``total_tokens`` serves the same purpose.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    context_tokens: int | None = None


@dataclass
class ErrorInfo:
    """Structured error information from the server."""

    code: str
    message: str


@dataclass
class IncompleteDetails:
    """Details about why a response stopped early."""

    reason: str  # "max_iterations", "execution_timeout", "context_overflow", etc.


@dataclass
class Response:
    """A response object from the server.

    Mirrors the JSON shape from ``POST /v1/responses`` and
    ``GET /v1/responses/{id}``.
    """

    id: str
    status: str  # "queued", "in_progress", "completed", "failed", "incomplete", "cancelled"
    model: str
    output: list[dict[str, object]] = field(default_factory=list)
    created_at: int = 0
    completed_at: int | None = None
    previous_response_id: str | None = None
    conversation: ConversationRef | None = None
    usage: Usage | None = None
    error: ErrorInfo | None = None
    incomplete_details: IncompleteDetails | None = None
    background: bool = False
    instructions: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Response:
        """Parse a response object from a JSON dict."""
        conv_raw = data.get("conversation")
        conversation = (
            ConversationRef(id=str(conv_raw["id"])) if isinstance(conv_raw, dict) else None
        )
        usage_raw = data.get("usage")
        usage = (
            Usage(
                input_tokens=_json_int(usage_raw.get("input_tokens", 0)),
                output_tokens=_json_int(usage_raw.get("output_tokens", 0)),
                total_tokens=_json_int(usage_raw.get("total_tokens", 0)),
                context_tokens=(
                    _json_int(usage_raw["context_tokens"])
                    if usage_raw.get("context_tokens") is not None
                    else None
                ),
            )
            if isinstance(usage_raw, dict)
            else None
        )
        error_raw = data.get("error")
        error = (
            ErrorInfo(
                code=str(error_raw.get("code", "")),
                message=str(error_raw.get("message", "")),
            )
            if isinstance(error_raw, dict)
            else None
        )
        inc_raw = data.get("incomplete_details")
        incomplete = (
            IncompleteDetails(reason=str(inc_raw.get("reason", "")))
            if isinstance(inc_raw, dict)
            else None
        )
        output_raw = data.get("output", [])
        return cls(
            id=str(data.get("id", "")),
            status=str(data.get("status", "")),
            model=str(data.get("model", "")),
            output=output_raw if isinstance(output_raw, list) else [],
            created_at=_json_int(data.get("created_at", 0)),
            completed_at=_json_int(data["completed_at"])
            if data.get("completed_at") is not None
            else None,
            previous_response_id=(
                str(data["previous_response_id"])
                if data.get("previous_response_id") is not None
                else None
            ),
            conversation=conversation,
            usage=usage,
            error=error,
            incomplete_details=incomplete,
            background=bool(data.get("background", False)),
            instructions=(
                str(data["instructions"]) if data.get("instructions") is not None else None
            ),
        )


@dataclass
class Agent:
    """An agent registered on the server."""

    id: str
    name: str
    description: str | None
    created_at: int

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Agent:
        """Parse an agent from a JSON dict."""
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data["description"]) if data.get("description") is not None else None,
            created_at=_json_int(data.get("created_at", 0)),
        )


@dataclass
class File:
    """A file uploaded to the server."""

    id: str
    filename: str
    bytes: int
    created_at: int

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> File:
        """Parse a file from a JSON dict."""
        return cls(
            id=str(data.get("id", "")),
            filename=str(data.get("filename", "")),
            bytes=_json_int(data.get("bytes", 0)),
            created_at=_json_int(data.get("created_at", 0)),
        )


@dataclass
class Conversation:
    """
    A conversation on the server.

    :param id: Server-assigned conversation id, e.g. ``"conv_abc123"``.
    :param title: Optional user-assigned title.
    :param created_at: Unix epoch timestamp of creation.
    :param labels: Guardrails labels on this conversation (keys are
        label names, values are their current string values). Empty
        dict when the server's PolicyEngine hasn't written any
        labels yet. Surfaced so callers like the REPL's Ctrl+O debug
        overlay can render them at parity with the legacy Ctrl+G
        overview. Older servers that don't include ``labels`` in
        their ``ConversationObject`` response parse to ``{}``.
    """

    id: str
    title: str | None
    created_at: int
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Conversation:
        """Parse a conversation from a JSON dict."""
        raw_labels = data.get("labels")
        labels: dict[str, str] = {}
        if isinstance(raw_labels, dict):
            # Narrow to ``dict[str, str]`` — the server's schema declares
            # that shape but the JSON boundary only gives us
            # ``dict[str, object]``. Drop non-string entries rather than
            # crash the overlay render on a malformed response.
            labels = {str(k): str(v) for k, v in raw_labels.items() if isinstance(v, str)}
        return cls(
            id=str(data.get("id", "")),
            title=str(data["title"]) if data.get("title") is not None else None,
            created_at=_json_int(data.get("created_at", 0)),
            labels=labels,
        )


@dataclass
class PaginatedList:
    """A paginated list response from the server."""

    data: list[dict[str, object]]
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PaginatedList:
        """Parse a paginated list from a JSON dict."""
        items_raw = data.get("data", [])
        return cls(
            data=items_raw if isinstance(items_raw, list) else [],
            first_id=str(data["first_id"]) if data.get("first_id") is not None else None,
            last_id=str(data["last_id"]) if data.get("last_id") is not None else None,
            has_more=bool(data.get("has_more", False)),
        )
