"""Built-in tools: Hindsight long-term memory.

Exposes Hindsight's retain / recall / reflect operations as three built-in
tools so an agent can persist and recall memory across runs. Hindsight
(https://github.com/vectorize-io/hindsight) is an open-source agent-memory
system; the client SDK is an optional dependency (``omnigent[hindsight]``).

The memory bank is resolved per invocation from the agent spec config, falling
back to the run's identity in :class:`ToolContext` — so a single declaration
isolates memory per agent (or per conversation) out of the box.

Usage in config.yaml::

    tools:
      builtins:
        - name: hindsight_recall
          api_key: ${HINDSIGHT_API_KEY}
        - name: hindsight_retain
          api_key: ${HINDSIGHT_API_KEY}
        - name: hindsight_reflect
          api_key: ${HINDSIGHT_API_KEY}

Config keys (all optional except ``api_key``):

- ``api_key``: Hindsight API key (or set it via ``${HINDSIGHT_API_KEY}``).
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

# Banks already ensured-to-exist this process, so ``retain`` doesn't issue a
# redundant create_bank on every call. Module-level (not per-instance) because
# ToolManager builds a fresh tool instance per agent load.
_CREATED_BANKS: set[str] = set()


def _csv(value: str | None) -> list[str] | None:
    """Parse a comma-separated config string into a tag list, or None."""
    if not value:
        return None
    tags = [t.strip() for t in value.split(",") if t.strip()]
    return tags or None


class _HindsightToolBase(Tool):
    """Shared client/bank resolution for the Hindsight memory tools.

    The name starts with an underscore so the builtin-discovery test
    (``_all_builtin_tool_subclasses``) skips it — only the three concrete
    tools below are user-facing.

    :param config: Spec-level config from config.yaml (see module docstring).
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        self._config = config or {}
        self._cached_client: Hindsight | None = None

    def _client(self) -> Hindsight:
        """Build (and cache) a Hindsight client from the spec config.

        Imports ``hindsight_client`` lazily so merely importing this module
        (e.g. for ``description()`` during tool discovery) never requires the
        optional dependency.
        """
        if self._cached_client is not None:
            return self._cached_client

        api_key = self._config.get("api_key")
        if not api_key:
            raise ValueError(
                "Hindsight memory tools require an 'api_key' in the tool config "
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
        """Resolve the memory bank: config override → agent id → conversation id."""
        bank = self._config.get("bank_id") or ctx.agent_id or ctx.conversation_id
        if not bank:
            raise ValueError(
                "No Hindsight bank could be resolved (no bank_id, agent_id, or conversation_id)."
            )
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


class HindsightRetainTool(_HindsightToolBase):
    """Store information in Hindsight long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "hindsight_retain"

    @classmethod
    def description(cls) -> str:
        return (
            "Store information in long-term memory (Hindsight) for later "
            "retrieval — facts, preferences, decisions, or anything that "
            "should be remembered across conversations."
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
            _logger.error("Hindsight retain failed: %s", e)
            return f"Hindsight retain failed: {e}"


class HindsightRecallTool(_HindsightToolBase):
    """Search Hindsight long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "hindsight_recall"

    @classmethod
    def description(cls) -> str:
        return (
            "Search long-term memory (Hindsight) for relevant information — "
            "previously stored facts, preferences, or context. Returns the "
            "matching memories, or a note that none were found."
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
            _logger.error("Hindsight recall failed: %s", e)
            return f"Hindsight recall failed: {e}"


class HindsightReflectTool(_HindsightToolBase):
    """Synthesize a reasoned answer from Hindsight long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "hindsight_reflect"

    @classmethod
    def description(cls) -> str:
        return (
            "Synthesize a reasoned answer from long-term memory (Hindsight). "
            "Use this for a coherent summary or reasoned response about what "
            "is known, rather than raw memory facts."
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
            _logger.error("Hindsight reflect failed: %s", e)
            return f"Hindsight reflect failed: {e}"
