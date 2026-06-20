"""
HermesExecutor: run agent turns through the Hermes Agent CLI.

Spawns ``hermes chat -q`` as a subprocess for each turn.  Hermes manages its
own session state via a persistent session store (SQLite under
``~/.hermes/``), so the executor uses ``--resume <session_id>`` on subsequent
turns to maintain conversational context across the Omnigent session without
re-serialising the full history.

Each turn yields text output as ``TextChunk`` / ``TurnComplete`` events.
Tool-call bridging (Omnigent tools → Hermes tool execution) is
future work — the initial harness runs Hermes in its default tool-enabled
configuration.

Requirements:
    The ``hermes`` CLI must be installed and on PATH (or set via
    ``HARNESS_HERMES_PATH``).

Env vars read at construction:

- ``HARNESS_HERMES_MODEL`` — model identifier, e.g. ``"deepseek/deepseek-chat"``
  or ``"anthropic/claude-sonnet-4"``.  ``None`` falls back to Hermes' own
  configured default model.
- ``HARNESS_HERMES_CWD`` — working directory the subprocess runs in.
  ``None`` falls back to ``os.getcwd()``.
- ``HARNESS_HERMES_PATH`` — absolute path to the ``hermes`` CLI binary.
  ``None`` searches ``PATH``.
- ``HARNESS_HERMES_OS_ENV`` — JSON-encoded :class:`OSEnvSpec`.  When unset,
  defaults to ``caller_process + sandbox=none``.
- ``HARNESS_HERMES_SKILLS_FILTER`` — JSON-encoded ``str | list[str]``
  carrying the agent spec's ``skills_filter``.  When unset, falls back to
  ``"all"``.
- ``HARNESS_HERMES_BUNDLE_DIR`` — absolute path to the agent bundle's
  extracted root.  Unset for agents without a bundled-skills directory.
- ``HARNESS_HERMES_AGENT_NAME`` — agent display name (reserved for future use).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator
from typing import Any

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnComplete,
)

_logger = logging.getLogger(__name__)

# Maximum seconds to wait for a Hermes subprocess to complete a single turn.
# Complex tasks (multi-tool-calling loops) may take several minutes.
_HERMES_TURN_TIMEOUT_S = 600.0

# Regex to extract the session_id from Hermes' quiet-mode output line.
# Matches lines like ``session_id: 20260620_142506_c51451``.
_RE_SESSION_ID = re.compile(r"^session_id:\s+(\S+)")

# Regex to detect the resume notice line emitted by ``--resume``.
# Matches lines like ``↻ Resumed session 20260620_142506_c51451 ...``.
_RE_RESUME_NOTICE = re.compile(r"^↻\s+Resumed\s+session\s+\S+")

# Regex to detect the "continue" notice line.
# Matches lines like ``↻ Resumed session NAME ...``.
_RE_CONTINUE_NOTICE = re.compile(r"^↻\s+Resumed\s+session")

# Prefix for Hermes warning messages that should be stripped from output.
_WARNING_PREFIX = "Warning:"


def _strip_hermes_metadata(output: str) -> str:
    r"""
    Strip Hermes metadata lines from subprocess stdout, leaving only
    the agent's response text.

    Hermes' quiet mode (``-Q``) emits a small number of info lines
    alongside the actual response:

    - ``session_id: <id>``
    - ``↻ Resumed session <id> ...``
    - ``Warning: ...``

    :param output: Raw stdout from ``hermes chat -q``.
    :returns: The agent's response text with metadata lines removed.
    """
    lines = output.split("\n")
    filtered: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _RE_SESSION_ID.match(stripped):
            continue
        if _RE_RESUME_NOTICE.match(stripped):
            continue
        if _RE_CONTINUE_NOTICE.match(stripped):
            continue
        if stripped.startswith(_WARNING_PREFIX):
            continue
        filtered.append(line)
    return "\n".join(filtered).strip()


def _parse_session_id(output: str) -> str | None:
    """
    Extract the Hermes session ID from a subprocess response.

    :param output: Raw stdout from ``hermes chat -q``.
    :returns: The session ID string, or ``None`` if no session_id
        line was found.
    """
    for line in output.split("\n"):
        match = _RE_SESSION_ID.match(line.strip())
        if match:
            return match.group(1)
    return None


def _extract_last_user_message(messages: list[Message]) -> str:
    """
    Extract the text of the most recent user message from the
    Omnigent message list.

    :param messages: The conversation message list passed to
        ``run_turn``.
    :returns: The user message text, or ``""`` if none found.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                if parts:
                    return "\n".join(parts)
            elif isinstance(content, str):
                return content
    return ""


def _build_hermes_args(
    hermes_path: str,
    message: str,
    *,
    model: str | None = None,
    session_id: str | None = None,
    cwd: str | None = None,
) -> list[str]:
    """
    Build the argument list for a Hermes subprocess call.

    :param hermes_path: Path to the Hermes CLI binary.
    :param message: The user message text.
    :param model: Optional model override (``-m`` flag).
    :param session_id: Optional session ID to resume (``--resume``).
    :param cwd: Not used in arg building (passed to subprocess as cwd).
    :returns: A list of CLI arguments.
    """
    args = [
        hermes_path,
        "chat",
        "-q",
        message,
        "-Q",  # quiet mode: suppress banner, spinner, tool previews
        "--source",
        "tool",  # tag sessions as tool/integration-originated
    ]
    if model:
        args.extend(["-m", model])
    if session_id:
        args.extend(["--resume", session_id])
    return args


class HermesExecutor(Executor):
    """
    Executor that drives the Hermes Agent CLI as a subprocess.

    Hermes manages its own session persistence (SQLite).  The executor
    captures the ``session_id`` from the first turn and passes
    ``--resume <session_id>`` on subsequent turns so conversational
    history is maintained without Omnigent re-serializing the full
    message list.

    Each turn runs ``hermes chat -q "<message>" -Q --source tool`` as an
    ``asyncio.create_subprocess_exec`` subprocess, streams text output,
    and yields ``TextChunk`` / ``TurnComplete`` events.
    """

    def __init__(
        self,
        hermes_path: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        os_env: OSEnvSpec | None = None,
        skills_filter: str | list[str] | None = None,
        bundle_dir: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """
        :param hermes_path: Path to the ``hermes`` CLI binary.
            ``None`` searches ``PATH``.
        :param cwd: Working directory for the subprocess.
            ``None`` uses ``os.getcwd()``.
        :param model: Model identifier override.
            ``None`` uses Hermes' configured default.
        :param os_env: OS environment spec for the subprocess.
            ``None`` defaults to ``caller_process + sandbox=none``.
        :param skills_filter: Skills filter forwarded to Hermes.
            ``None`` means "no filter" (Hermes' default).
        :param bundle_dir: Agent bundle directory (reserved).
        :param agent_name: Agent display name (reserved).
        """
        self._hermes_path = hermes_path or shutil.which("hermes") or "hermes"
        self._cwd = cwd or os.getcwd()
        self._model = model
        self._os_env = os_env or OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="none"),
        )
        self._skills_filter = skills_filter
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        # Per-session state: maps session_key -> hermes_session_id
        self._session_map: dict[str, str] = {}

    def _hermes_session_id(self, session_key: str) -> str | None:
        """Return the stored Hermes session ID for an Omnigent session key."""
        return self._session_map.get(session_key)

    def supports_streaming(self) -> bool:
        """Return True — Hermes streams text output."""
        return True

    def handles_tools_internally(self) -> bool:
        """Return True — Hermes executes tools inside its own agent loop.

        The Hermes Agent CLI manages its own tool-calling loop internally.
        Tool-call requests/results are handled by Hermes, not bridged
        through Omnigent's tool dispatch.  This means Omnigent-level
        tool policies do not apply to tools Hermes calls internally.
        """
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Run one agent turn by spawning ``hermes chat -q``.

        :param messages: Conversation history from Omnigent.
        :param tools: Tool schemas (Hermes uses its own tools internally).
        :param system_prompt: System prompt (used by Hermes internally).
        :param config: Per-turn config (model override, etc.).
        :yields: ``TextChunk`` and ``TurnComplete`` events.
        :yields: ``ExecutorError`` on subprocess failure or timeout.
        """
        _logger.debug(
            "HermesExecutor.run_turn: %d messages, tools=%d, prompt_len=%d",
            len(messages),
            len(tools),
            len(system_prompt),
        )

        # Extract the latest user message
        user_text = _extract_last_user_message(messages)
        if not user_text:
            # Nothing to respond to — short-circuit
            yield TurnComplete(response=None)
            return

        # Resolve model from config override, then instance default
        model = (config.model if config else None) or self._model

        # Determine session key for this conversation
        session_key = self._session_key(messages)
        hermes_sid = self._hermes_session_id(session_key)

        # Build the command-line arguments
        args = _build_hermes_args(
            hermes_path=self._hermes_path,
            message=user_text,
            model=model,
            session_id=hermes_sid,
        )

        _logger.debug("Hermes subprocess: %s", " ".join(args))

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=_HERMES_TURN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _logger.warning("Hermes subprocess timed out after %ss", _HERMES_TURN_TIMEOUT_S)
            yield ExecutorError(
                message=f"Hermes subprocess timed out after {_HERMES_TURN_TIMEOUT_S}s",
                retryable=True,
            )
            return
        except FileNotFoundError:
            yield ExecutorError(
                message=(
                    f"Hermes CLI not found at '{self._hermes_path}'. "
                    "Install Hermes Agent (curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh)"
                ),
                retryable=False,
            )
            return
        except OSError as exc:
            yield ExecutorError(
                message=f"Failed to spawn Hermes subprocess: {exc}",
                retryable=True,
            )
            return

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            _logger.warning(
                "Hermes exited with code %d: %s",
                proc.returncode,
                error_msg[:500],
            )
            yield ExecutorError(
                message=f"Hermes exited with code {proc.returncode}: {error_msg[:500]}",
                retryable=True,
            )
            return

        # Store the session_id for subsequent turns
        parsed_sid = _parse_session_id(stdout)
        if parsed_sid and not hermes_sid:
            _logger.debug("Captured Hermes session_id: %s", parsed_sid)
            self._session_map[session_key] = parsed_sid

        # Strip metadata lines to get the clean response
        response_text = _strip_hermes_metadata(stdout)

        if response_text:
            yield TextChunk(text=response_text)

        yield TurnComplete(response=response_text or None)

    def _session_key(self, messages: list[Message]) -> str:
        """
        Derive a stable Omnigent session key from the message list.

        Uses the ``session_id`` stamped on the first message if available,
        otherwise falls back to a hash of the conversation content.
        """
        for msg in messages:
            sid = msg.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        # Fallback: hash the serialised messages for a stable key
        return str(hash(tuple(
            (m.get("role", ""), str(m.get("content", ""))[:200])
            for m in messages
        )))

    async def close_session(self, session_key: str) -> None:
        """
        Release resources for a specific session.

        Removes the Hermes session mapping — the Hermes session
        persists in its own SQLite store and can be resumed later
        via `hermes --resume` outside Omnigent.
        """
        self._session_map.pop(session_key, None)
        await super().close_session(session_key)

    async def close(self) -> None:
        """Release executor-wide resources."""
        self._session_map.clear()
        await super().close()
