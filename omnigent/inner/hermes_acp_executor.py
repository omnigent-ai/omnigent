"""Streaming Hermes executor via ``hermes acp`` -- a thin AcpExecutor layer.

The batch ``hermes chat -q`` path (:mod:`omnigent.inner.hermes_executor`) only
returns stdout when a turn ends, so Omnigent sees a single end-of-turn
``TextChunk``. This executor drives Hermes' ACP mode (``hermes acp``, JSON-RPC
over stdio) through the generic :class:`omnigent.inner.acp_executor.AcpExecutor`
client, adding only what Hermes needs on top:

- **Per-session ``HERMES_HOME``** (using the shared Hermes bridge helper):
  merges the user's model/provider auth and wires the Omnigent
  ``pre_tool_call`` shell hook. The hook is the complete policy gate -- Hermes
  raises ACP ``session/request_permission`` for only a subset of its tools, so
  the generic permission -> policy/elicitation route alone would leave gaps
  (and gating both ways would double-prompt); permission requests are therefore
  auto-allowed beneath the hook.
- **Tool-card fidelity**: inherited from :class:`AcpExecutor` -- bridge calls
  pass through unchanged; Hermes' self-executed tools are marked
  ``self_executed`` so their cards render and persist without entering
  dispatch correlation.
- **Usage normalization**: Hermes reports camelCase usage with a
  cache-inclusive ``inputTokens``; the cached portion is split out.
- ``session/new`` extras: a model override and a restrictive skills filter,
  retried without the extra params if the agent rejects them.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from omnigent.hermes_native_bridge import bridge_dir_for_session_id, write_policy_hook_config
from omnigent.inner.acp_executor import (
    _INIT_TIMEOUT_SECONDS,
    AcpAgentConfig,
    AcpExecutor,
)
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.hermes_executor import _get_conversation_id

logger = logging.getLogger(__name__)

_ACP_METHOD_SESSION_NEW = "session/new"


class HermesAcpExecutor(AcpExecutor):
    """Drives Hermes Agent via ``hermes acp`` for streaming turns."""

    def __init__(
        self,
        hermes_path: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        os_env: OSEnvSpec | None = None,
        skills_filter: str | list[str] | None = None,
        bundle_dir: str | None = None,  # noqa: ARG002 -- reserved
        agent_name: str | None = None,  # noqa: ARG002 -- reserved
    ) -> None:
        binary = hermes_path or shutil.which("hermes") or "hermes"
        super().__init__(
            AcpAgentConfig(
                # AcpAgentConfig.command is shlex-split; quote the binary so a
                # path containing spaces stays one argv entry.
                command=f"{shlex.quote(binary)} acp --accept-hooks",
                name="Hermes Agent",
                model=model,
            ),
            cwd=cwd,
            os_env=os_env,
        )
        self._skills_filter = skills_filter
        # Per-session HERMES_HOME with policy hook config (matches the batch path).
        self._hermes_home: Path | None = None
        self._setup_hermes_home()

    def _setup_hermes_home(self) -> None:
        """Create a per-session ``HERMES_HOME`` with the Omnigent policy hook.

        Mirrors :meth:`HermesExecutor._setup_hermes_home`: the ``HERMES_HOME``
        env var makes Hermes read this config instead of the user's
        ``~/.hermes/``, wiring the ``pre_tool_call`` policy hook.
        """
        server_url = os.environ.get("RUNNER_SERVER_URL", "")
        conv_id = _get_conversation_id()
        if not server_url or not conv_id:
            logger.warning(
                "hermes acp policy hooks disabled: RUNNER_SERVER_URL=%r, conv_id=%r",
                server_url or "(unset)",
                conv_id or "(unset)",
            )
            return
        self._hermes_home = Path(tempfile.mkdtemp(prefix="hermes_home_"))
        write_policy_hook_config(
            bridge_dir_for_session_id(conv_id),
            server_url,
            conv_id,
            hermes_home=self._hermes_home,
        )
        logger.debug("hermes acp per-session home: %s", self._hermes_home)

    def _spawn_env(self) -> dict[str, str]:
        env = super()._spawn_env()
        if self._hermes_home is not None:
            env["HERMES_HOME"] = str(self._hermes_home)
        return env

    async def _decide_permission(self, params: dict[str, Any]) -> bool:
        """Allow when the ``pre_tool_call`` hook is wired -- it already gated
        this call, and routing the (subset of) permission requests through
        policy again would evaluate the same call twice and park two approval
        cards. When the hook could NOT be wired (no server URL / conversation
        id), fall back to the generic policy/elicitation route rather than
        approving unguarded.
        """
        if self._hermes_home is not None:
            return True
        return await super()._decide_permission(params)

    async def _ensure_session(self) -> str:
        """``session/new`` with Hermes extras (model, restrictive skills filter).

        ``hermes acp`` has no ``-m``/``-s`` flags, so the overrides go in
        ``session/new`` params. If the agent rejects the extra params, retry
        without them and warn rather than failing the session -- the session
        then uses the agent's configured defaults.
        """
        if self._session_id is not None:
            return self._session_id

        mcp_servers = self._session_mcp_servers()
        base_params: dict[str, Any] = {"cwd": self._cwd, "mcpServers": mcp_servers}
        extras: dict[str, Any] = {}
        if self._config.model:
            extras["model"] = self._config.model
        # Only send a skills filter when it actually restricts; "all"/None is
        # the default and the agent may reject the redundant param.
        skills = self._skills_filter
        if isinstance(skills, list) or (isinstance(skills, str) and skills not in ("all", "")):
            extras["skills"] = skills

        params = {**base_params, **extras} if extras else base_params
        resp = await self._rpc(_ACP_METHOD_SESSION_NEW, params, timeout=_INIT_TIMEOUT_SECONDS)
        if "error" in resp and "model" in extras:
            # Model fallback is safe (the agent's configured default applies);
            # keep any skills restriction in the retry.
            retry = {**base_params, **{k: v for k, v in extras.items() if k != "model"}}
            logger.warning(
                "hermes acp: session/new rejected params %s (%s); retrying without model",
                sorted(extras),
                resp["error"].get("message", resp["error"]),
            )
            resp = await self._rpc(_ACP_METHOD_SESSION_NEW, retry, timeout=_INIT_TIMEOUT_SECONDS)
        if "error" in resp and "skills" in extras:
            # A restrictive skills filter must not be silently widened: launching
            # with every skill enabled is a policy change, not a degraded default.
            raise RuntimeError(
                "hermes acp session/new rejected the restrictive skills filter: "
                f"{resp['error'].get('message', resp['error'])}"
            )
        if "error" in resp:
            raise RuntimeError(
                f"hermes acp session/new failed: {resp['error'].get('message', resp['error'])}"
            )
        session_id = (resp.get("result") or {}).get("sessionId")
        if not session_id:
            raise RuntimeError("hermes acp session/new returned no sessionId")
        self._session_id = session_id
        return self._session_id

    @staticmethod
    def _usage_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
        """Hermes usage: camelCase keys, cache-inclusive ``inputTokens``.

        ``input_tokens`` becomes the non-cached prompt tokens; the cached
        portion maps to ``cache_read_input_tokens`` so the cost accumulator
        prices it correctly.
        """
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        out: dict[str, Any] = {}
        cached = usage.get("cachedReadTokens")
        cached = cached if isinstance(cached, int) else 0
        inp = usage.get("inputTokens")
        inp = inp if isinstance(inp, int) else 0
        if inp or cached:
            out["input_tokens"] = max(inp - cached, 0)
        if cached:
            out["cache_read_input_tokens"] = cached
        if isinstance(usage.get("outputTokens"), int):
            out["output_tokens"] = usage["outputTokens"]
        if isinstance(usage.get("totalTokens"), int):
            out["total_tokens"] = usage["totalTokens"]
        return out or None

    async def close(self) -> None:
        await super().close()
        if self._hermes_home is not None:
            shutil.rmtree(self._hermes_home, ignore_errors=True)
            self._hermes_home = None


__all__ = ["HermesAcpExecutor"]
