"""Automatic long-term memory for the turn lifecycle.

Deterministic recall/retain wired into the runner's turn dispatch and driven
by the agent spec's ``memory:`` block (:class:`~omnigent.spec.types.MemoryConfig`).
This is the *non-tool* path: unlike the ``hindsight_*`` built-in tools, the
model does not have to elect to call anything for memory to work.

- **Recall** runs before the model sees the turn and returns a block of relevant
  memories. The runner injects it as a system prompt for executor harnesses, or
  prepends it to the user message for native harnesses (which fix their system
  prompt at spawn) — see :func:`prepend_memory_to_content`.
- **Retain** persists the user's turn after dispatch.

``memory:`` is a generic feature; the backend is selected by
``MemoryConfig.provider`` and dispatched through the :data:`_BACKENDS` registry
(mirroring the ``web_search`` tool's ``_Backend`` idiom). Hindsight is the
default and, today, only backend; its ``hindsight_client`` SDK is an optional
dependency (``omnigent[hindsight]``) imported lazily so this module loads even
when memory is unconfigured.

Both phases are best effort — a slow or unreachable backend must never fail or
stall a turn, so every call is guarded and recall is time-bounded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hindsight_client import Hindsight

    from omnigent.spec.types import MemoryConfig

_logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "https://api.hindsight.vectorize.io"

# Background retain tasks, kept referenced so the event loop does not GC them
# mid-flight (asyncio holds only a weak reference to a bare task). Each task
# removes itself on completion via the done callback below.
_RETAIN_TASKS: set[asyncio.Task[None]] = set()

# Banks ensured-to-exist this process, so retain does not issue a redundant
# create_bank on every turn. Module-level to mirror the ``hindsight_*`` tools'
# cache and share the effect across sessions.
_CREATED_BANKS: set[str] = set()


def resolve_bank(
    cfg: MemoryConfig,
    agent_id: object,
    conversation_id: object,
) -> str | None:
    """Resolve the memory bank: config ``bank_id`` → agent id → conversation id.

    Matches the ``hindsight_*`` tools' rule so tool-based and automatic memory
    share a bank. Returns the first non-empty *string* candidate; empty strings
    and non-string values (the runner threads these from loosely-typed request
    dicts) fall through rather than resolving to an empty bank that would
    silently co-mingle every agent's memory.

    :param cfg: The agent's memory configuration.
    :param agent_id: The run's agent id, if any.
    :param conversation_id: The run's conversation id, if any.
    :returns: The resolved bank id, or ``None`` when none could be determined.
    """
    for candidate in (cfg.bank_id, agent_id, conversation_id):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


# Fallback HTTP timeout for the retain path, whose fact-extraction call can
# legitimately run longer than a recall. Recall passes its own (shorter)
# ``recall_timeout`` instead — see ``_hindsight_recall``.
_DEFAULT_CLIENT_TIMEOUT = 30.0


def _build_client(cfg: MemoryConfig, *, timeout: float = _DEFAULT_CLIENT_TIMEOUT) -> Hindsight:
    """Construct a Hindsight client from *cfg* (lazy SDK import).

    :param timeout: HTTP timeout in seconds. Recall passes ``cfg.recall_timeout``
        so the blocking SDK call unwinds close to when the turn stops waiting on
        it (``asyncio.wait_for``) — otherwise a timed-out recall would keep a
        worker thread busy for the full default timeout under a slow backend.
    """
    import hindsight_client

    return hindsight_client.Hindsight(
        base_url=cfg.api_url or _DEFAULT_API_URL,
        api_key=cfg.api_key,
        timeout=timeout,
    )


def _close_client(client: Hindsight) -> None:
    """Best-effort close of a per-call client to free its HTTP session.

    A fresh client is built per recall/retain, so it must be closed or the
    long-lived runner leaks an ``aiohttp`` session/connector each turn.
    """
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception as e:
            _logger.debug("Hindsight client close failed: %s", e)


def extract_latest_user_text(content: object) -> str | None:
    """Pull the most recent user message's text from harness content items.

    ``content`` is the list assembled for ``harness_body["content"]``: message
    items carry a ``role`` and either a plain-string ``content`` or a list of
    blocks each with a ``text`` field. Keys on ``role == "user"`` (not the
    optional ``type`` field), mirroring the native executors' own
    ``_latest_user_text`` so both the persisted-history shape
    (``{"type": "message", "role": ...}``) and the inbound-turn shape
    (``{"role": ...}``) are handled. Returns the concatenated text of the last
    user message, or ``None`` when no user text is present. Defensive against
    unexpected shapes so a malformed item can never raise into the turn path.

    :param content: The harness content items (expected ``list[dict]``).
    :returns: The latest user message text, or ``None``.
    """
    if not isinstance(content, list):
        return None
    for item in reversed(content):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        blocks = item.get("content")
        if isinstance(blocks, str):
            return blocks.strip() or None
        if not isinstance(blocks, list):
            continue
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if parts:
            return "\n".join(parts)
    return None


# Content block type carrying recalled memory injected into a native turn's
# user message. Native harnesses fix the system prompt at spawn, so the user
# message content is the only per-turn channel that reaches the model. The
# native prompt builder accepts both ``input_text`` and ``text``; ``input_text``
# matches how user turns are otherwise encoded (see entities/conversation.py).
_MEMORY_BLOCK_TYPE = "input_text"


def prepend_memory_to_content(content: object, memory_block: str) -> object:
    """Prepend recalled *memory_block* to the latest user message in *content*.

    Used for native harnesses, whose per-turn system prompt is fixed at spawn —
    the user message content is the only channel that reaches the model each
    turn (executor harnesses instead take an injected system prompt). Returns a
    new content list with the memory as a leading block on the most recent user
    message. Returns *content* unchanged when its shape is unrecognised, so a
    malformed turn simply proceeds without injected memory.

    :param content: The harness content items (expected ``list[dict]``).
    :param memory_block: The formatted memory text to inject.
    :returns: A new content list, or *content* unchanged.
    """
    if not isinstance(content, list):
        return content
    block = {"type": _MEMORY_BLOCK_TYPE, "text": memory_block}
    new_content = list(content)
    for idx in range(len(new_content) - 1, -1, -1):
        item = new_content[idx]
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        blocks = item.get("content")
        if isinstance(blocks, str):
            new_blocks: list[object] = [block, {"type": _MEMORY_BLOCK_TYPE, "text": blocks}]
        elif isinstance(blocks, list):
            new_blocks = [block, *blocks]
        else:
            return content
        new_content[idx] = {**item, "content": new_blocks}
        return new_content
    return content


def _hindsight_recall(cfg: MemoryConfig, bank: str, query: str) -> list[str]:
    """Blocking Hindsight recall; run off-loop via :func:`asyncio.to_thread`."""
    client = _build_client(cfg, timeout=cfg.recall_timeout)
    try:
        response = client.recall(
            bank_id=bank,
            query=query,
            budget=cfg.budget,
            max_tokens=cfg.max_tokens,
        )
        return [r.text for r in (response.results or []) if getattr(r, "text", None)]
    finally:
        _close_client(client)


def _hindsight_retain(cfg: MemoryConfig, bank: str, content: str) -> None:
    """Blocking Hindsight retain; run off-loop via :func:`asyncio.to_thread`."""
    client = _build_client(cfg)
    try:
        if bank not in _CREATED_BANKS:
            try:
                client.create_bank(bank_id=bank, name=bank)
            except Exception as e:
                # Bank likely already exists; a real auth/network failure
                # surfaces on the retain call below.
                _logger.debug("create_bank(%r) failed (assuming it exists): %s", bank, e)
            _CREATED_BANKS.add(bank)
        client.retain(bank_id=bank, content=content)
    finally:
        _close_client(client)


@dataclass(frozen=True)
class _MemoryBackend:
    """A selectable memory backend in the :data:`_BACKENDS` registry.

    Mirrors the ``web_search`` tool's ``_Backend`` idiom: the generic ``memory:``
    feature dispatches recall/retain to the backend named by
    ``MemoryConfig.provider``. Both callables are blocking and are run off the
    event loop via :func:`asyncio.to_thread`.

    :param recall: ``(cfg, bank, query) -> list[str]`` of recalled memory texts.
    :param retain: ``(cfg, bank, content) -> None`` persisting the user's turn.
    """

    recall: Callable[[MemoryConfig, str, str], list[str]]
    retain: Callable[[MemoryConfig, str, str], None]


# Backend registry keyed by ``MemoryConfig.provider``. Keep in sync with
# ``_MEMORY_PROVIDERS`` in ``omnigent/spec/parser.py`` — the parser validates the
# provider name at load time, so a missing entry here would be a bug, not user
# error. Hindsight is the default and, today, only backend.
_BACKENDS: dict[str, _MemoryBackend] = {
    "hindsight": _MemoryBackend(recall=_hindsight_recall, retain=_hindsight_retain),
}


def _backend_for(cfg: MemoryConfig) -> _MemoryBackend | None:
    """Return the backend for ``cfg.provider``, or ``None`` if unregistered."""
    return _BACKENDS.get(cfg.provider)


async def recall_instructions(cfg: MemoryConfig, bank: str, query: str) -> str | None:
    """Recall memories for *query* and format them as a system-prompt block.

    Returns ``None`` (never raises) on timeout, backend error, or no hits, so
    the turn always proceeds. Bounded by ``cfg.recall_timeout`` so a stalled
    backend adds at most that many seconds to a turn.

    :param cfg: The agent's memory configuration.
    :param bank: The resolved memory bank.
    :param query: The recall query (typically the latest user message).
    :returns: A ready-to-inject instructions block, or ``None``.
    """
    backend = _backend_for(cfg)
    if backend is None:
        _logger.warning("Unknown memory provider %r; skipping recall.", cfg.provider)
        return None
    try:
        memories = await asyncio.wait_for(
            asyncio.to_thread(backend.recall, cfg, bank, query),
            timeout=cfg.recall_timeout,
        )
    except TimeoutError:
        _logger.warning(
            "Memory recall timed out after %ss (provider=%s, bank=%s); "
            "dispatching without memory.",
            cfg.recall_timeout,
            cfg.provider,
            bank,
        )
        return None
    except Exception as e:
        _logger.warning("Memory recall failed (provider=%s, bank=%s): %s", cfg.provider, bank, e)
        return None
    if not memories:
        return None
    body = "\n".join(f"- {m}" for m in memories)
    return (
        "## Relevant long-term memory\n"
        "The following facts were recalled from earlier sessions and may be "
        "relevant to this turn. Treat them as background you already know; do "
        "not mention that they came from memory unless asked.\n"
        f"{body}"
    )


def schedule_retain(cfg: MemoryConfig, bank: str, content: str) -> None:
    """Persist *content* to memory in the background (fire-and-forget).

    Retain triggers server-side fact extraction and can be slow, so it must
    not block the turn. Runs on a worker thread; failures are logged, never
    raised. The task is tracked to survive garbage collection and cleared on
    completion.

    :param cfg: The agent's memory configuration.
    :param bank: The resolved memory bank.
    :param content: The text to persist (typically the user's turn).
    """
    backend = _backend_for(cfg)
    if backend is None:
        _logger.warning("Unknown memory provider %r; skipping retain.", cfg.provider)
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(backend.retain, cfg, bank, content)
        except Exception as e:
            _logger.warning(
                "Memory retain failed (provider=%s, bank=%s): %s", cfg.provider, bank, e
            )

    task = asyncio.create_task(_run())
    _RETAIN_TASKS.add(task)
    task.add_done_callback(_RETAIN_TASKS.discard)
