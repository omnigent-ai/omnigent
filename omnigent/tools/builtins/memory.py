"""Built-in tools: long-term memory.

Exposes retain / recall / reflect as three built-in memory tools so an agent
can persist and recall information across runs. The tools are vendor-neutral at
the agent-facing layer (``memory_retain`` / ``memory_recall`` /
``memory_reflect``); the backend is selected per declaration via the
``provider`` config key. Today the only provider is Hindsight
(https://github.com/vectorize-io/hindsight), an open-source agent-memory
system whose client SDK is an optional dependency (``omnigent[memory]``).

The memory bank is resolved per invocation from the agent spec config, falling
back to the run's identity in :class:`ToolContext` — so a single declaration
isolates memory per agent (or per conversation) out of the box.

Usage in config.yaml::

    tools:
      builtins:
        - name: memory_recall
          api_key: ${HINDSIGHT_API_KEY}
        - name: memory_retain
          api_key: ${HINDSIGHT_API_KEY}
        - name: memory_reflect
          api_key: ${HINDSIGHT_API_KEY}

Config keys (all optional except ``api_key``):

- ``provider``: memory backend. Defaults to ``hindsight`` (the only one today).
- ``api_key``: backend API key (or set it via ``${HINDSIGHT_API_KEY}``).
- ``api_url``: API base URL. Defaults to Hindsight Cloud.
- ``bank_id``: Memory bank to read/write. Defaults to ``ctx.agent_id``.
- ``budget``: recall/reflect budget level — ``low`` / ``mid`` / ``high``.
- ``max_tokens``: max tokens for recall results.
- ``tags`` / ``recall_tags``: comma-separated tags for retain / recall.
- ``recall_tags_match``: ``any`` / ``all`` / ``any_strict`` / ``all_strict``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from omnigent.tools.base import Tool, ToolContext

if TYPE_CHECKING:
    from hindsight_client import Hindsight

_logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "https://api.hindsight.vectorize.io"

# The only memory backend wired up today. The ``provider`` config key exists so
# the agent-facing tool names stay vendor-neutral (``memory_*``) and another
# backend can be added behind the same tools later without renaming anything.
_SUPPORTED_PROVIDERS = frozenset({"hindsight"})

# Banks already ensured-to-exist this process, so ``retain`` doesn't issue a
# redundant create_bank on every call. Module-level (not per-instance) because
# ToolManager builds a fresh tool instance per agent load.
_CREATED_BANKS: set[str] = set()

# Banks whose resolution has already been logged, so we emit the "which bank am
# I using" line once per bank per process rather than on every invocation.
_LOGGED_BANKS: set[str] = set()


def _csv(value: str | None) -> list[str] | None:
    """Parse a comma-separated config string into a tag list, or None."""
    if not value:
        return None
    tags = [t.strip() for t in value.split(",") if t.strip()]
    return tags or None


class _MemoryToolBase(Tool):
    """Shared client/bank resolution for the memory tools.

    The name starts with an underscore so the builtin-discovery test
    (``_all_builtin_tool_subclasses``) skips it — only the three concrete
    tools below are user-facing.

    :param config: Spec-level config from config.yaml (see module docstring).
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        self._config = config or {}
        self._cached_client: Hindsight | None = None

    def _client(self) -> Hindsight:
        """Build (and cache) a memory-backend client from the spec config.

        Imports ``hindsight_client`` lazily so merely importing this module
        (e.g. for ``description()`` during tool discovery) never requires the
        optional dependency.
        """
        if self._cached_client is not None:
            return self._cached_client

        provider = (self._config.get("provider") or "hindsight").lower()
        if provider not in _SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported memory provider {provider!r}. "
                f"Supported providers: {', '.join(sorted(_SUPPORTED_PROVIDERS))}."
            )

        api_key = self._config.get("api_key")
        if not api_key:
            raise ValueError(
                "Memory tools require an 'api_key' in the tool config "
                "(e.g. api_key: ${HINDSIGHT_API_KEY})."
            )

        import hindsight_client

        self._cached_client = hindsight_client.Hindsight(
            base_url=self._config.get("api_url") or _DEFAULT_API_URL,
            api_key=api_key,
            timeout=30.0,
        )
        return self._cached_client

    def _bank(self, ctx: ToolContext) -> str:
        """Resolve the memory bank: config override → agent id → conversation id.

        ``ctx.agent_id`` / ``ctx.conversation_id`` may be empty strings, not
        just None; ``or`` skips those too, so an empty ``agent_id`` falls
        through to the conversation scope rather than resolving to an empty
        bank that would silently co-mingle every agent's memory. The resolved
        bank (and which source it came from) is logged once per bank so an
        operator can see exactly where memory is being read/written.
        """
        bank = self._config.get("bank_id") or ctx.agent_id or ctx.conversation_id
        if not bank:
            raise ValueError(
                "No memory bank could be resolved (no bank_id, agent_id, or conversation_id)."
            )
        if bank not in _LOGGED_BANKS:
            if self._config.get("bank_id"):
                source = "config bank_id"
            elif ctx.agent_id:
                source = "agent_id"
            else:
                source = "conversation_id"
            _logger.info("Memory tools resolved bank %r (source: %s).", bank, source)
            _LOGGED_BANKS.add(bank)
        return bank

    def _budget(self) -> str:
        return self._config.get("budget", "mid")

    def _max_tokens(self) -> int:
        return int(self._config.get("max_tokens", "4096"))

    def _ensure_bank(self, client: Hindsight, bank: str) -> None:
        """Create the bank once per process; tolerate it already existing."""
        if bank in _CREATED_BANKS:
            return
        try:
            client.create_bank(bank_id=bank, name=bank)
        except Exception as e:
            # Bank likely already exists; treat as created either way. Logged at
            # debug so a real auth/network failure is visible here rather than
            # only surfacing later on the retain call.
            _logger.debug("create_bank(%r) failed (assuming it exists): %s", bank, e)
        _CREATED_BANKS.add(bank)


class MemoryRetainTool(_MemoryToolBase):
    """Store information in long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "memory_retain"

    @classmethod
    def description(cls) -> str:
        return (
            "Persist information to long-term memory so it survives across "
            "conversations and sessions. Call this whenever the user shares a "
            "durable fact, preference, or decision, or asks you to remember "
            "something — conversation context alone is lost between sessions, "
            "so acknowledging a fact in chat does NOT save it."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The information to store in long-term memory.",
                        },
                    },
                    "required": ["content"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        try:
            content = json.loads(arguments).get("content") if arguments else None
            if not content:
                return "Error: 'content' parameter is required."
            client = self._client()
            bank = self._bank(ctx)
            self._ensure_bank(client, bank)
            kwargs: dict[str, Any] = {"bank_id": bank, "content": content}
            tags = _csv(self._config.get("tags"))
            if tags:
                kwargs["tags"] = tags
            client.retain(**kwargs)
            return "Stored to long-term memory."
        except Exception as e:
            _logger.error("Memory retain failed: %s", e)
            return f"Memory retain failed: {e}"


class MemoryRecallTool(_MemoryToolBase):
    """Search long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "memory_recall"

    @classmethod
    def description(cls) -> str:
        return (
            "Search long-term memory for relevant information — previously "
            "stored facts, preferences, or context. Call this BEFORE answering "
            "anything that may depend on what you already know about the user "
            "or past sessions. Returns the matching memories, or a note that "
            "none were found."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query to find relevant memories.",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        try:
            query = json.loads(arguments).get("query") if arguments else None
            if not query:
                return "Error: 'query' parameter is required."
            client = self._client()
            bank = self._bank(ctx)
            kwargs: dict[str, Any] = {
                "bank_id": bank,
                "query": query,
                "budget": self._budget(),
                "max_tokens": self._max_tokens(),
            }
            recall_tags = _csv(self._config.get("recall_tags"))
            if recall_tags:
                kwargs["tags"] = recall_tags
                kwargs["tags_match"] = self._config.get("recall_tags_match", "any")
            response = client.recall(**kwargs)
            memories = [r.text for r in (response.results or [])]
            if not memories:
                return "No relevant memories found."
            return "\n".join(f"- {m}" for m in memories)
        except Exception as e:
            _logger.error("Memory recall failed: %s", e)
            return f"Memory recall failed: {e}"


class MemoryReflectTool(_MemoryToolBase):
    """Synthesize a reasoned answer from long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "memory_reflect"

    @classmethod
    def description(cls) -> str:
        return (
            "Synthesize a reasoned answer from long-term memory. Use this for a "
            "coherent summary or reasoned response about what is known, rather "
            "than raw memory facts."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The question to reflect on using stored memories.",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        try:
            query = json.loads(arguments).get("query") if arguments else None
            if not query:
                return "Error: 'query' parameter is required."
            client = self._client()
            bank = self._bank(ctx)
            response = client.reflect(bank_id=bank, query=query, budget=self._budget())
            return response.text or "No relevant memories found."
        except Exception as e:
            _logger.error("Memory reflect failed: %s", e)
            return f"Memory reflect failed: {e}"
