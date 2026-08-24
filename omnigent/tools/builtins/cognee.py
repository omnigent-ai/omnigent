"""Built-in tools: cognee knowledge-graph long-term memory.

Exposes cognee (https://github.com/topoteretes/cognee) search / remember as
built-in tools so an agent can persist and recall memory across runs, backed
by an embedded local store under the Omnigent data dir. cognee is an optional
dependency (``omnigent[cognee]``); all calls go through the framework-owned
boundary in :mod:`omnigent.runtime.memory` (gate, timeouts, circuit breaker).

Each agent's memory is isolated in its own dataset by default (resolved from
the spec config, falling back to the run's identity in ``ToolContext``).
Cross-agent access is grant-based (see
:class:`omnigent.runtime.memory.MemoryGrants`), layered narrowest to
broadest: **agent** (private) → **user** → **team** → **org** tier pools,
plus a generic ``shared_dataset`` exchange and per-peer read/write grants.
The tools can only touch datasets the grants expose.

Usage in config.yaml::

    tools:
      builtins:
        - name: cognee_search
          team_dataset: platform_team
          read_datasets: ag_researcher, ag_writer
        - name: cognee_remember
          team_dataset: platform_team
          write_datasets: ag_researcher

Config keys (all optional):

- ``dataset``: Private dataset name. Defaults to the agent id (falling back
  to the conversation id), so memory isolates per agent out of the box.
- ``user_dataset`` / ``team_dataset`` / ``org_dataset``: Tier pools —
  read/write for every agent granted the same name. Each falls back to the
  same key in the global ``cognee:`` config block, so a deployment can set
  its tiers once and agents inherit them.
- ``shared_dataset``: Ad-hoc cross-agent exchange dataset outside the tier
  hierarchy — read/write for every agent granted the same name.
- ``read_datasets``: Comma-separated peer datasets (agent ids or dataset
  names) this agent may search.
- ``write_datasets``: Comma-separated peer datasets this agent may publish
  into. List a peer in both keys for full read/write access.
- ``search_type``: cognee search type (default ``GRAPH_COMPLETION``).
- ``top_k``: max search results (default 10).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)


class _CogneeToolBase(Tool):
    """Shared grant/settings resolution for the cognee memory tools.

    The name starts with an underscore so the builtin-discovery test
    (``_all_builtin_tool_subclasses``) skips it — only the concrete tools
    below are user-facing.

    :param config: Spec-level config from config.yaml (see module docstring).
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        self._config = config or {}

    def _grants(self, ctx: ToolContext) -> Any:
        """Resolve this agent's :class:`MemoryGrants` from config + context.

        Passes the effective ``cognee:`` settings so tier datasets granted
        globally (``user_dataset`` / ``team_dataset`` / ``org_dataset``)
        are inherited without per-agent config.
        """
        from omnigent.runtime.memory import resolve_grants

        return resolve_grants(
            self._config,
            agent_id=ctx.agent_id,
            conversation_id=ctx.conversation_id,
            settings=self._settings(),
        )

    @staticmethod
    def _tier_not_granted(tier: str) -> str:
        return (
            f"No {tier}-level memory is granted to this agent (set "
            f"'{tier}_dataset' in the tool config or the global 'cognee:' "
            "config block)."
        )

    def _settings(self) -> dict[str, Any]:
        """Effective cognee settings with spec-level search overrides applied."""
        from omnigent.runtime.memory import cognee_settings

        settings = dict(cognee_settings())
        if self._config.get("search_type"):
            settings["search_type"] = self._config["search_type"]
        return settings

    def _top_k(self) -> int:
        return int(self._config.get("top_k", "10"))

    @staticmethod
    def _not_granted(action: str, dataset: str, granted: tuple[str, ...]) -> str:
        granted_list = ", ".join(granted)
        return (
            f"Dataset {dataset!r} is not granted for {action} (granted: "
            f"{granted_list}). Grants come from the tool config: "
            "'shared_dataset', 'read_datasets', 'write_datasets'."
        )


class CogneeSearchTool(_CogneeToolBase):
    """Search cognee long-term memory (own + granted shared/peer datasets)."""

    @classmethod
    def name(cls) -> str:
        return "cognee_search"

    @classmethod
    def description(cls) -> str:
        return (
            "Search long-term memory (a cognee knowledge graph) for relevant "
            "information — previously stored facts, decisions, preferences, "
            "and context, including memories from shared or peer-agent "
            "datasets this agent is granted. Call this BEFORE answering "
            "anything that may depend on what you already know from past "
            "sessions. Returns the matching memories, or a note that none "
            "were found."
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
                        "scope": {
                            "type": "string",
                            "enum": ["all", "agent", "user", "team", "org", "shared", "peers"],
                            "description": (
                                "Which memory to search: 'agent' (this agent's "
                                "private memory), 'user' / 'team' / 'org' (the "
                                "granted tier pools, narrowest to broadest), "
                                "'shared' (the ad-hoc exchange dataset), 'peers' "
                                "(granted peer-agent datasets), or 'all' "
                                "(everything granted; the default)."
                            ),
                        },
                        "dataset": {
                            "type": "string",
                            "description": (
                                "Search one specific granted dataset (e.g. a "
                                "peer agent's id). Overrides 'scope'."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        from omnigent.runtime.memory import memory_search, sanitize_dataset_name

        try:
            args = json.loads(arguments) if arguments else {}
            query = args.get("query")
            if not query:
                return "Error: 'query' parameter is required."
            grants = self._grants(ctx)
            if args.get("dataset"):
                target = sanitize_dataset_name(args["dataset"])
                if not grants.can_read(target):
                    return self._not_granted("reading", target, grants.readable())
                datasets = [target]
            else:
                scope = args.get("scope") or "all"
                if scope == "agent":
                    datasets = [grants.private]
                elif scope in ("user", "team", "org", "shared"):
                    tier_dataset = grants.tier(scope)
                    if not tier_dataset:
                        if scope == "shared":
                            return (
                                "No shared memory is granted to this agent (set "
                                "'shared_dataset' in the tool config to exchange "
                                "memory with other agents)."
                            )
                        return self._tier_not_granted(scope)
                    datasets = [tier_dataset]
                elif scope == "peers":
                    if not grants.readable_peers:
                        return (
                            "No peer-agent memory is granted to this agent "
                            "(set 'read_datasets' in the tool config to read "
                            "other agents' datasets)."
                        )
                    datasets = list(grants.readable_peers)
                else:
                    datasets = list(grants.readable())
            results = memory_search(
                query,
                datasets,
                settings=self._settings(),
                top_k=self._top_k(),
            )
            if not results:
                return "No relevant memories found."
            return "\n".join(f"- {r}" for r in results)
        except Exception as e:
            _logger.error("cognee search failed: %s", e)
            return f"cognee search failed: {e}"


class CogneeRememberTool(_CogneeToolBase):
    """Store information in cognee long-term memory."""

    @classmethod
    def name(cls) -> str:
        return "cognee_remember"

    @classmethod
    def description(cls) -> str:
        return (
            "Persist information to long-term memory (a cognee knowledge "
            "graph) so it survives across conversations and sessions. Call "
            "this whenever a durable fact, preference, or decision emerges, "
            "or the user asks you to remember something — conversation "
            "context alone is lost between sessions. Use scope='user', "
            "'team', or 'org' to store into a granted tier pool, "
            "scope='shared' for the ad-hoc exchange dataset, or 'dataset' "
            "to publish into a granted peer agent's memory."
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
                        "scope": {
                            "type": "string",
                            "enum": ["agent", "user", "team", "org", "shared"],
                            "description": (
                                "Where to store: 'agent' (this agent's private "
                                "memory; the default), 'user' / 'team' / 'org' "
                                "(the granted tier pools), or 'shared' (the "
                                "ad-hoc cross-agent exchange dataset)."
                            ),
                        },
                        "dataset": {
                            "type": "string",
                            "description": (
                                "Store into one specific granted dataset (e.g. "
                                "a peer agent's id, when write access is "
                                "granted). Overrides 'scope'."
                            ),
                        },
                    },
                    "required": ["content"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        from omnigent.runtime.memory import memory_add, sanitize_dataset_name

        try:
            args = json.loads(arguments) if arguments else {}
            content = args.get("content")
            if not content:
                return "Error: 'content' parameter is required."
            grants = self._grants(ctx)
            scope = args.get("scope") or "agent"
            if args.get("dataset"):
                dataset = sanitize_dataset_name(args["dataset"])
                if not grants.can_write(dataset):
                    return self._not_granted("writing", dataset, grants.writable())
            elif scope in ("user", "team", "org", "shared"):
                tier_dataset = grants.tier(scope)
                if not tier_dataset:
                    if scope == "shared":
                        return (
                            "No shared memory is granted to this agent (set "
                            "'shared_dataset' in the tool config to exchange "
                            "memory with other agents)."
                        )
                    return self._tier_not_granted(scope)
                dataset = tier_dataset
            else:
                dataset = grants.private
            node_set = [ctx.conversation_id] if ctx.conversation_id else None
            stored = memory_add(
                content,
                dataset,
                settings=self._settings(),
                node_set=node_set,
            )
            if not stored:
                return "cognee remember failed: the memory store is unavailable."
            if dataset != grants.private:
                return (
                    f"Stored to long-term memory dataset {dataset!r} "
                    "(visible to agents granted access to it)."
                )
            return "Stored to long-term memory."
        except Exception as e:
            _logger.error("cognee remember failed: %s", e)
            return f"cognee remember failed: {e}"
