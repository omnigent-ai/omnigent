"""GeminiExecutor: run agents through a persistent ``gemini --acp`` session.

Drives Google's ``gemini`` CLI over the Agent Client Protocol (ACP). Unlike
``cursor-agent`` / ``mimo`` (which enter ACP via an ``acp`` subcommand), the
Gemini CLI uses a ``--acp`` flag, so the shared
:class:`omnigent.inner.cursor_acp.AcpClient` is given ``subcommand=("--acp",)``.

Per Omnigent conversation it holds one ACP session (``session/new``) that stays
open across turns; each ``run_turn`` sends one ``session/prompt`` and translates
the streamed ``session/update`` notifications into ExecutorEvents (reusing the
cursor wrap's :func:`_update_to_event` / :func:`_build_cursor_prompt`).

Model: this harness is pinned to a single Gemini model
(:data:`GEMINI_PINNED_MODEL`). The model is passed authoritatively as a
``--model`` CLI argument at launch; any spec / ``/model`` override is ignored so
the worker only ever runs that one model. The launch also passes ``--yolo``
(auto-approve all actions — headless workers can't answer permission prompts)
and ``--skip-trust`` (trust the worktree for the session).

Requirements:
    The ``gemini`` CLI must be installed and on PATH (or ``HARNESS_GEMINI_PATH``
    set).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

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

# The single model this harness runs. Passed as ``--model`` at launch so the
# worker only ever talks to this model regardless of any spec / ``/model``
# override.
GEMINI_PINNED_MODEL = "gemini-3.1-pro-preview"

# Prefix-matched env var names allowed into the gemini subprocess. Only
# known-safe categories pass: Gemini/Google CLI config, proxy/TLS settings,
# Node.js runtime knobs (gemini bundles a Node runtime), and locale. Unrelated
# credential families do NOT match — mirrors the cursor / mimo posture.
_GEMINI_ENV_ALLOW_PREFIXES: tuple[str, ...] = (
    "GEMINI_",
    "GOOGLE_",
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

# Exact-matched env var names allowed into the gemini subprocess: the minimal
# set a POSIX CLI reasonably expects (HOME carries ~/.gemini login state).
_GEMINI_ENV_ALLOW_EXACT: frozenset[str] = frozenset(
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


def _find_gemini() -> str | None:
    """Find the ``gemini`` CLI on PATH."""
    return shutil.which("gemini")


def _clean_gemini_env(extra_allowed: Sequence[str] | None = None) -> dict[str, str]:
    """Build a filtered copy of ``os.environ`` for the gemini subprocess.

    Deny-by-default allowlist mirroring :func:`cursor_executor._clean_cursor_env`:
    only the known-safe prefixes / exact names pass, so host secrets never reach
    the gemini process. ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` are allowed via
    the ``GEMINI_`` / ``GOOGLE_`` prefixes; the executor also injects an API key
    explicitly from config when provided.

    :param extra_allowed: Extra exact names to pass through, e.g. a spec's
        ``os_env.sandbox.env_passthrough`` entries. ``None`` means no extras.
    :returns: Filtered environment dict.
    """
    allow_exact = set(_GEMINI_ENV_ALLOW_EXACT)
    if extra_allowed is not None:
        allow_exact.update(extra_allowed)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allow_exact or key.startswith(_GEMINI_ENV_ALLOW_PREFIXES)
    }


def _sandbox_enabled(os_env: OSEnvSpec | None) -> bool:
    """Whether to pass gemini's ``--sandbox`` flag.

    A restrictive sandbox enables gemini's own sandbox; ``"none"`` / unset (the
    headless default) disables it for full access. Enforcement is gemini's, not
    Omnigent's bwrap.
    """
    sandbox = os_env.sandbox if os_env is not None else None
    return not (sandbox is None or sandbox.type == "none")


@dataclass
class _GeminiSessionState:
    """Per-Omnigent-conversation ACP session state."""

    client: AcpClient | None = None
    session_id: str | None = None
    system_prompt: str | None = None
    has_sent_prompt: bool = False


class GeminiExecutor(Executor):
    """Execute agent turns via a persistent ``gemini --acp`` session."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        gemini_path: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Create a GeminiExecutor.

        :param cwd: Working directory the ACP session operates in. ``None``
            falls back to ``os_env.cwd`` then the process cwd.
        :param os_env: Optional OS environment / sandbox spec (its ``cwd`` is
            used when *cwd* is unset).
        :param gemini_path: Absolute path to ``gemini``. ``None`` searches PATH.
        :param api_key: Gemini API key injected as ``GEMINI_API_KEY``. ``None``
            relies on an inherited key or a prior ``gemini`` login.
        :raises ImportError: When ``gemini`` is not found.
        """
        resolved = gemini_path or _find_gemini()
        if not resolved:
            raise ImportError(
                "GeminiExecutor requires the 'gemini' CLI on PATH. "
                "Install it with: npm install -g @google/gemini-cli"
            )
        self._gemini_path = resolved
        self._cwd = cwd or (os_env.cwd if os_env is not None else None)
        self._os_env_spec = os_env
        passthrough = (
            os_env.sandbox.env_passthrough if os_env is not None and os_env.sandbox else None
        )
        self._env: dict[str, str] = _clean_gemini_env(passthrough)
        if api_key:
            self._env["GEMINI_API_KEY"] = api_key
        self._session_states: dict[str, _GeminiSessionState] = {}

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        # ACP exposes no confirmed mid-turn steer, so a message can't be
        # injected into a running turn (parity with cursor / mimo).
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

    async def _close_state(self, state: _GeminiSessionState) -> None:
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
        # Always drop the session so the next turn starts fresh — same rationale
        # as the cursor / pi executors (a resumed turn would bypass the runner's
        # interrupt marker).
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface as False
            logger.debug("GeminiExecutor: close after interrupt failed: %s", exc)
            return False

    async def close(self) -> None:
        for key in list(self._session_states.keys()):
            await self.close_session(key)

    async def _ensure_session(self, state: _GeminiSessionState) -> None:
        """Spawn the ACP server and open a session if one isn't live."""
        if state.client is not None and state.client.running:
            return
        cwd = self._cwd or os.getcwd()
        # The model is pinned authoritatively via ``--model``. ``--yolo``
        # auto-approves every action (headless workers can't answer permission
        # prompts); ``--skip-trust`` trusts the worktree for the session.
        extra_args = ["--model", GEMINI_PINNED_MODEL, "--yolo", "--skip-trust"]
        if _sandbox_enabled(self._os_env_spec):
            extra_args.append("--sandbox")
        client = AcpClient(
            self._gemini_path,
            env=self._env,
            cwd=cwd,
            extra_args=extra_args,
            subcommand=("--acp",),
        )
        await client.start()
        # The model is pinned via ``--model`` above, so the session is left to
        # the CLI's resolved model (no ``session/new`` model field).
        state.session_id = await client.new_session(cwd=cwd, model=None, mcp_servers=[])
        state.client = client

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],  # noqa: ARG002 — gemini uses its own native tools
        system_prompt: str,
        config: ExecutorConfig | None = None,  # noqa: ARG002 — model is pinned
    ) -> AsyncIterator[ExecutorEvent]:
        session_key = self._session_key(messages)
        state = self._session_states.setdefault(session_key, _GeminiSessionState())

        # The system prompt is baked into the first turn, so a change to it means
        # a fresh ACP session. (The model never changes — it is pinned.)
        if state.client is not None and state.system_prompt != system_prompt:
            await self._close_state(state)
            state = _GeminiSessionState()
            self._session_states[session_key] = state
        is_first_turn = not state.has_sent_prompt
        state.system_prompt = system_prompt

        try:
            await self._ensure_session(state)
        except (AcpError, OSError, ImportError) as exc:
            await self.close_session(session_key)
            yield ExecutorError(message=f"Failed to start gemini --acp: {exc}")
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
                    yield ExecutorError(message=f"gemini --acp error: {payload}", retryable=True)
                    return
                else:  # "result"
                    # stopReason in payload; ACP reports no token usage, so the
                    # turn is left unpriced (usage=None).
                    yield TurnComplete(response=response_text or None, usage=None)
                    return
        except AcpError as exc:
            # The ACP server died mid-turn — drop the session so the next turn
            # respawns it, and surface a retryable error.
            stderr = state.client.stderr_tail() if state.client is not None else ""
            await self.close_session(session_key)
            suffix = f" Stderr: {stderr}" if stderr else ""
            yield ExecutorError(message=f"gemini --acp turn failed: {exc}{suffix}", retryable=True)
