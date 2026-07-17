"""Expose Omnigent's builtin tools to an ACP agent via ``session/new.mcpServers``.

Shared by the ACP executors (``acp`` generic, ``goose``, ``qwen``). Reuses the
*same* stdio ``serve-mcp`` relay the native harnesses use
(:mod:`omnigent.claude_native_bridge`): the ACP agent spawns
``python -Im omnigent.claude_native_bridge serve-mcp --bridge-dir <dir>`` as an
MCP server, which proxies each Omnigent tool call back through ``tool_executor``
(→ :meth:`TurnContext.dispatch_tool` → the Omnigent server, where TOOL_CALL /
TOOL_RESULT policy is enforced). The agent keeps its own filesystem/shell tools;
this only *adds* Omnigent's builtin tools (``sys_session_*``, ``sys_agent_*``,
``load_skill``, ``web_fetch``, policy tools, …).

The relay is a localhost HTTP server started inside the harness subprocess and
lives for the session (the agent connects to ``serve-mcp`` once at
``session/new``); tool calls only fire during an active turn, when
``_stable_tool_executor`` has a live ``TurnContext`` to dispatch into.

Never fatal: any setup failure (missing bridge helper, no tool executor, the
``OMNIGENT_ACP_MCP=0`` kill switch) yields an empty ``mcpServers`` — the agent
just runs without Omnigent tools, exactly as before this feature.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Global kill switch (any of "0"/"false"/"no" disables). Per-executor config may
# also disable it (the generic ``acp`` harness exposes a per-agent knob).
_ENV_KILL_SWITCH = "OMNIGENT_ACP_MCP"


def _mcp_enabled() -> bool:
    return os.environ.get(_ENV_KILL_SWITCH, "1").strip().lower() not in ("0", "false", "no")


def _to_acp_mcp_servers(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert :func:`claude_native_bridge.build_mcp_config` → ACP ``mcpServers``.

    ``build_mcp_config`` returns ``{"mcpServers": {"<name>": {command, args,
    env(dict)}}}`` (the Claude/native shape). ACP's ``session/new.mcpServers`` is
    an array of stdio entries ``{name, command, args, env:[{name,value}]}`` (no
    ``type`` discriminator for stdio) — so the env dict is flattened to the
    ``[{name, value}]`` list ACP requires.
    """
    servers = config.get("mcpServers", {})
    out: list[dict[str, Any]] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        env_dict = spec.get("env") or {}
        out.append(
            {
                "name": name,
                "command": spec["command"],
                "args": list(spec.get("args", [])),
                "env": [{"name": str(k), "value": str(v)} for k, v in env_dict.items()],
            }
        )
    return out


class OmnigentAcpMcp:
    """Lazily-started Omnigent-tool relay + its ACP ``mcpServers`` entry.

    One instance per ACP executor. Call :meth:`session_new_servers` when building
    ``session/new`` params, and :meth:`close` on executor teardown.
    """

    def __init__(self, label: str = "acp") -> None:
        self._label = label
        self._relay: Any | None = None
        self._bridge_dir: Path | None = None
        # ``None`` = not yet resolved; a list (possibly empty) = resolved+cached.
        self._acp_servers: list[dict[str, Any]] | None = None

    def session_new_servers(
        self,
        *,
        tools: list[Any],
        tool_executor: Any | None,
        loop: Any,
        enabled: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the ACP ``mcpServers`` array for ``session/new`` (may be empty).

        Starts the relay once (cached thereafter). Returns ``[]`` — without
        caching — when the inputs aren't ready yet (no ``tool_executor`` / no
        ``tools``), so a later turn can retry; caches ``[]`` when disabled or on
        failure so it isn't retried every turn.

        :param tools: Omnigent tool schemas to advertise (each ``{"name", …}``).
        :param tool_executor: The adapter-injected ``_tool_executor`` bridge, or
            ``None`` (standalone / unit tests) → no relay.
        :param loop: The running event loop (owns ``tool_executor``).
        :param enabled: Per-executor enable (ANDed with the global kill switch).
        """
        if self._acp_servers is not None:
            return self._acp_servers
        if not enabled or not _mcp_enabled():
            self._acp_servers = []
            return []
        if tool_executor is None or not tools:
            return []  # not ready — retry on a later turn, don't cache
        try:
            from omnigent.claude_native_bridge import (
                build_mcp_config,
                prepare_acp_mcp_bridge_dir,
                start_tool_relay,
            )

            # Secure per-relay bridge dir under the allow-listed ACP-MCP root,
            # carrying a token-only bridge.json → serve-mcp serves ONLY the relay
            # tools (no raw sys_os_* fs tools; the ACP agent owns those).
            self._bridge_dir = prepare_acp_mcp_bridge_dir()
            self._relay = start_tool_relay(
                bridge_dir=self._bridge_dir,
                tools=list(tools),
                tool_executor=tool_executor,
                loop=loop,
            )
            self._acp_servers = _to_acp_mcp_servers(build_mcp_config(self._bridge_dir))
            logger.info(
                "acp[%s] Omnigent MCP relay ready (%d builtin tools bridged)",
                self._label,
                len(tools),
            )
            return self._acp_servers
        except Exception as exc:  # noqa: BLE001 — MCP is additive; never break a turn
            logger.warning(
                "acp[%s] Omnigent MCP bridge setup failed; agent runs without Omnigent tools: %s",
                self._label,
                exc,
            )
            self._acp_servers = []
            self._cleanup()
            return []

    def close(self) -> None:
        """Tear down the relay HTTP server and remove the bridge dir."""
        self._cleanup()

    def _cleanup(self) -> None:
        if self._relay is not None:
            with contextlib.suppress(Exception):
                self._relay.close()
            self._relay = None
        if self._bridge_dir is not None:
            shutil.rmtree(self._bridge_dir, ignore_errors=True)
            self._bridge_dir = None
