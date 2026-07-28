"""Per-turn context providers.

A *context provider* is a dotted-path callable declared in an agent spec's
``context_providers:`` block. It is resolved and invoked on every turn; the text
it returns is **appended** to the system instructions (additive — it never
replaces the agent's prompt). Providers are read-only and best-effort: one that
raises, returns nothing, or overruns its deadline is logged and skipped, and
never blocks the turn.

Provider contract::

    async def provider(ctx: ContextProviderInput) -> str | None: ...

A synchronous callable returning ``str | None`` is also accepted; it runs in a
worker thread so provider I/O (memory recall is the motivating case) never
stalls the runner's event loop. The ``path`` may name the provider directly, or
a factory (when ``arguments`` are given in the spec, it is called with those
kwargs to produce the provider).

Providers run concurrently under a per-provider deadline and an overall budget,
so turn latency is bounded by the slowest provider rather than their sum. Their
outputs are still joined in declaration order.

Only the *upload* boundary decides which providers a spec may name
(``omnigent.server.bundles.validate_agent_bundle``). A refusal raised here is a
platform decision, not a provider fault, so it propagates instead of being
swallowed with the best-effort failures.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any

from omnigent.errors import OmnigentError
from omnigent.policies.function import _resolve_dotted_path
from omnigent.spec.types import AgentSpec, FunctionRef

_logger = logging.getLogger(__name__)

# Wall-clock ceiling for a single provider. Long enough for a memory or
# retrieval round trip, short enough that a wedged provider does not hold
# the pre-stream turn setup open while the user stares at nothing.
_PROVIDER_TIMEOUT_SEC = 10.0

# Backstop across all providers. They run concurrently, so this only bites
# when resolution itself (a blocking import) drags past the per-provider
# deadlines, which ``asyncio.wait_for`` cannot interrupt.
_PROVIDERS_BUDGET_SEC = 15.0

# Providers that failed already, as ``(conversation_id, path)``. A broken
# dotted path fails identically on every turn; without this the runner
# would log a traceback per turn forever.
_WARNED: set[tuple[str | None, str]] = set()
_WARNED_CAP = 1024


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


def _warn_once(
    ctx: ContextProviderInput,
    agent: str | None,
    path: str,
    reason: str,
    exc: BaseException | None,
) -> None:
    """Log a provider failure at WARNING, once per conversation and path.

    :param ctx: The turn context the provider ran under.
    :param agent: The declaring agent's name, for operators reading logs.
    :param path: The provider's dotted path.
    :param reason: Short description of the failure mode.
    :param exc: The underlying exception, when there was one.
    """
    key = (ctx.conversation_id, path)
    if key in _WARNED:
        return
    if len(_WARNED) >= _WARNED_CAP:
        _WARNED.clear()
    _WARNED.add(key)
    _logger.warning(
        "context provider %r on agent %r %s; skipping it for this session "
        "(the turn continues without its text)",
        path,
        agent or "<unnamed>",
        reason,
        exc_info=exc,
    )


async def _run_one(ref: FunctionRef, ctx: ContextProviderInput) -> Any:
    """Resolve and invoke one provider, keeping sync work off the event loop.

    :param ref: The declared provider reference.
    :param ctx: The turn context passed to the provider.
    :returns: Whatever the provider returned, awaited if awaitable.
    """
    target = _resolve_dotted_path(ref.path)
    provider = target(**ref.arguments) if ref.arguments is not None else target
    if inspect.iscoroutinefunction(provider):
        result = await provider(ctx)
    else:
        result = await asyncio.to_thread(provider, ctx)
    # A plain function may still hand back an awaitable (e.g. it returns a
    # coroutine rather than being declared ``async def``).
    if inspect.isawaitable(result):
        result = await result
    return result


async def run_context_providers(spec: AgentSpec, ctx: ContextProviderInput) -> str | None:
    """Invoke each declared context provider for this turn.

    Providers run concurrently, each under :data:`_PROVIDER_TIMEOUT_SEC`, with
    :data:`_PROVIDERS_BUDGET_SEC` as an overall backstop. Output order follows
    the spec's declaration order regardless of completion order.

    :param spec: The agent spec whose ``context_providers:`` to run.
    :param ctx: The turn context handed to every provider.
    :returns: The providers' outputs joined by blank lines, or ``None`` when the
        spec declares no providers or none produced text. Best-effort — a
        provider that errors, times out, or breaks the ``str | None`` contract
        is logged and skipped so memory never breaks a turn.
    :raises OmnigentError: If a provider is refused by the platform. A refusal
        is not a provider fault, and swallowing it is what let an unvetted
        provider run unnoticed, so it is never treated as best-effort.
    """
    refs = spec.context_providers
    if not refs:
        return None

    pending = [asyncio.wait_for(_run_one(ref, ctx), _PROVIDER_TIMEOUT_SEC) for ref in refs]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            _PROVIDERS_BUDGET_SEC,
        )
    except TimeoutError:
        _logger.warning(
            "context providers for agent %r exceeded the %.0fs overall budget; "
            "continuing the turn without their text",
            spec.name or "<unnamed>",
            _PROVIDERS_BUDGET_SEC,
        )
        return None

    parts: list[str] = []
    for ref, result in zip(refs, results, strict=True):
        if isinstance(result, OmnigentError):
            raise result
        if isinstance(result, TimeoutError):
            _warn_once(
                ctx,
                spec.name,
                ref.path,
                f"exceeded its {_PROVIDER_TIMEOUT_SEC:.0f}s deadline",
                None,
            )
            continue
        if isinstance(result, BaseException):
            _warn_once(ctx, spec.name, ref.path, "failed", result)
            continue
        if result is None:
            continue
        if not isinstance(result, str):
            # Stringifying would splice e.g. ``"{'a': 1}"`` or a repr of the
            # factory itself into the system prompt, unnoticed.
            _warn_once(
                ctx,
                spec.name,
                ref.path,
                f"returned {type(result).__name__}, not the contracted str | None",
                None,
            )
            continue
        text = result.strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts) or None
