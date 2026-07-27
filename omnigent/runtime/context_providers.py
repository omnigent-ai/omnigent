"""Per-turn context providers.

A *context provider* is a dotted-path callable declared in an agent spec's
``context_providers:`` block. It is resolved and invoked on every turn; the text
it returns is **appended** to the system instructions (additive — it never
replaces the agent's prompt). Providers are read-only and best-effort: one that
raises, or returns nothing, is logged and skipped, and never blocks the turn.

Provider contract::

    async def provider(ctx: ContextProviderInput) -> str | None: ...

A synchronous callable returning ``str | None`` is also accepted. The ``path``
may name the provider directly, or a factory (when ``arguments`` are given in
the spec, it is called with those kwargs to produce the provider).
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any

from omnigent.policies.function import _resolve_dotted_path
from omnigent.spec.types import AgentSpec

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextProviderInput:
    """Read-only turn context handed to each provider.

    :param conversation_id: The owning session/conversation id, or ``None``.
    :param last_user_message: The latest user message text for this turn, or
        ``None`` when it could not be extracted.
    """

    conversation_id: str | None = None
    last_user_message: str | None = None


def _last_user_text(msg_body: dict[str, Any]) -> str | None:
    """Best-effort extraction of the latest user text from a turn message body.

    Handles the shapes the runner uses: a bare string, a list of content blocks
    (``{"type": "input_text", "text": ...}``), or message items
    (``{"type": "message", "role": "user", "content": [...]}``).
    """
    content = msg_body.get("content")
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, list):
            texts.extend(str(b["text"]) for b in inner if isinstance(b, dict) and b.get("text"))
        elif block.get("text"):
            texts.append(str(block["text"]))
    return "\n".join(texts) or None


def input_from_turn(conversation_id: str | None, msg_body: dict[str, Any]) -> ContextProviderInput:
    """Build a :class:`ContextProviderInput` from a runner turn message body."""
    return ContextProviderInput(
        conversation_id=conversation_id,
        last_user_message=_last_user_text(msg_body),
    )


async def run_context_providers(spec: AgentSpec, ctx: ContextProviderInput) -> str | None:
    """Invoke each declared context provider for this turn.

    :returns: The providers' outputs joined by blank lines, or ``None`` when the
        spec declares no providers or none produced text. Best-effort — a
        provider that errors is logged and skipped so memory never breaks a turn.
    """
    refs = getattr(spec, "context_providers", None)
    if not refs:
        return None
    parts: list[str] = []
    for ref in refs:
        try:
            target = _resolve_dotted_path(ref.path)
            provider = target(**ref.arguments) if ref.arguments is not None else target
            result = provider(ctx)
            if inspect.isawaitable(result):
                result = await result
            if result:
                parts.append(str(result).strip())
        except Exception:
            _logger.exception("context provider %r failed; skipping", ref.path)
    return "\n\n".join(p for p in parts if p) or None
