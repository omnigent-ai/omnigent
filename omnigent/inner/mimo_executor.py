"""MimoExecutor: run agents through a persistent ``mimo acp`` session.

Mimo exposes an ACP (Agent Client Protocol) server via ``mimo acp``. This
executor mirrors :mod:`omnigent.inner.cursor_executor`: it keeps one ACP
session per Omnigent conversation, sends prompts turn by turn, and maps ACP
``session/update`` notifications into Omnigent executor events.

Requirements:
    The ``mimo`` CLI must be installed and on PATH (or ``HARNESS_MIMO_PATH``
    set).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .cursor_acp import AcpClient, AcpError
from .cursor_executor import _build_cursor_prompt, _update_to_event
from .datamodel import OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnComplete,
)

logger = logging.getLogger(__name__)

_MIMO_ENV_ALLOW_PREFIXES: tuple[str, ...] = (
    "MIMO_",
    "MIMOCODE_",
    "HTTP_",
    "HTTPS_",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_",
    "NODE_",
    "XDG_",
    "LANG",
    "LC_",
)

_MIMO_ENV_ALLOW_EXACT: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "TZ",
    }
)


def _find_mimo() -> str | None:
    """Find the ``mimo`` CLI on PATH."""
    return shutil.which("mimo")


def _clean_mimo_env(extra_allowed: Sequence[str] | None = None) -> dict[str, str]:
    """Build a filtered subprocess environment for ``mimo acp``.

    Mirrors the Cursor/Pi deny-by-default posture: Mimo config, proxy/TLS,
    Node/runtime, locale, and basic POSIX process vars pass through; unrelated
    cloud/API secrets do not. ``MIMOCODE_*`` is allowed because Mimo's own help
    documents settings such as ``MIMOCODE_SERVER_PASSWORD``.
    """
    allow_exact = set(_MIMO_ENV_ALLOW_EXACT)
    if extra_allowed is not None:
        allow_exact.update(extra_allowed)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allow_exact or key.startswith(_MIMO_ENV_ALLOW_PREFIXES)
    }


@dataclass
class _MimoSessionState:
    """Per-Omnigent-conversation ACP session state."""

    client: AcpClient | None = None
    session_id: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    has_sent_prompt: bool = False


class MimoExecutor(Executor):
    """Execute agent turns via a persistent ``mimo acp`` session."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        mimo_path: str | None = None,
        bundle_dir: Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        resolved = mimo_path or _find_mimo()
        if not resolved:
            raise ImportError(
                "MimoExecutor requires the 'mimo' CLI on PATH. "
                "Install Mimo Code, or set HARNESS_MIMO_PATH to the binary."
            )
        self._mimo_path = resolved
        self._cwd = cwd or (os_env.cwd if os_env is not None else None)
        self._os_env_spec = os_env
        self._model_override = model
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        passthrough = (
            os_env.sandbox.env_passthrough if os_env is not None and os_env.sandbox else None
        )
        self._env: dict[str, str] = _clean_mimo_env(passthrough)
        self._session_states: dict[str, _MimoSessionState] = {}

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        return False

    def _session_key(self, messages: list[Message]) -> str:
        if messages:
            last = messages[-1]
            if last.get("session_id"):
                return str(last["session_id"])
            meta = last.get("metadata", {})
            if isinstance(meta, dict) and meta.get("session_id"):
                return str(meta["session_id"])
        return "__default__"

    def _resolve_model(self, config: ExecutorConfig | None) -> str | None:
        cfg = config or ExecutorConfig()
        return cfg.model or self._model_override

    async def _close_state(self, state: _MimoSessionState) -> None:
        if state.client is not None:
            await state.client.close()
            state.client = None

    async def close_session(self, session_key: str) -> None:
        state = self._session_states.pop(session_key, None)
        if state is not None:
            await self._close_state(state)

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._session_states.get(session_key)
        if state is None:
            return False
        if state.client is not None and state.session_id is not None:
            with contextlib.suppress(Exception):
                await state.client.cancel(state.session_id)
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("MimoExecutor: close after interrupt failed: %s", exc)
            return False

    async def close(self) -> None:
        for key in list(self._session_states.keys()):
            await self.close_session(key)

    async def _ensure_session(self, state: _MimoSessionState, model: str | None) -> None:
        if state.client is not None and state.client.running:
            return
        cwd = self._cwd or os.getcwd()
        client = AcpClient(
            self._mimo_path,
            env=self._env,
            cwd=cwd,
            extra_args=["--cwd", cwd],
        )
        await client.start()
        state.session_id = await client.new_session(cwd=cwd, model=model, mcp_servers=[])
        state.client = client

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],  # noqa: ARG002 - Mimo uses its own native tools in v1.
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        session_key = self._session_key(messages)
        model = self._resolve_model(config)
        state = self._session_states.setdefault(session_key, _MimoSessionState())
        if state.client is not None and (
            state.system_prompt != system_prompt or state.model != model
        ):
            await self._close_state(state)
            state = _MimoSessionState()
            self._session_states[session_key] = state
        is_first_turn = not state.has_sent_prompt
        state.system_prompt = system_prompt
        state.model = model

        try:
            await self._ensure_session(state, model)
        except (AcpError, OSError, ImportError) as exc:
            await self.close_session(session_key)
            yield ExecutorError(message=f"Failed to start mimo acp: {exc}")
            return

        prompt = _build_cursor_prompt(
            messages, is_first_turn=is_first_turn, system_prompt=system_prompt
        )
        if not prompt:
            yield TurnComplete(response=None)
            return

        assert state.client is not None and state.session_id is not None
        state.has_sent_prompt = True
        response_text = ""
        try:
            async for kind, payload in state.client.prompt_stream(
                state.session_id, [{"type": "text", "text": prompt}]
            ):
                if kind == "update":
                    event = _update_to_event(payload)
                    if event is not None:
                        if isinstance(event, TextChunk):
                            response_text += event.text
                        yield event
                elif kind == "error":
                    yield ExecutorError(message=f"mimo acp error: {payload}", retryable=True)
                    return
                else:
                    yield TurnComplete(response=response_text or None, usage=None)
                    return
        except AcpError as exc:
            stderr = state.client.stderr_tail() if state.client is not None else ""
            await self.close_session(session_key)
            suffix = f" Stderr: {stderr}" if stderr else ""
            yield ExecutorError(message=f"mimo acp turn failed: {exc}{suffix}", retryable=True)
