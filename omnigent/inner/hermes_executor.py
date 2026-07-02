"""
HermesExecutor: run agent turns through the Hermes Agent CLI.

Spawns ``hermes chat -q`` as a subprocess for each turn.  Hermes manages its
own session state via a persistent session store (SQLite under
``~/.hermes/``), so the executor uses ``--resume <session_id>`` on subsequent
turns to maintain conversational context across the Omnigent session without
re-serialising the full history.

Each turn yields text output as ``TextChunk`` / ``TurnComplete`` events.
Omnigent policies are enforced on Hermes' native tool calls via Hermes'
``pre_tool_call`` shell hook mechanism: a per-session ``HERMES_HOME``
directory is created with a ``config.yaml`` that registers a policy hook
script, matching how Codex uses a per-session ``CODEX_HOME``.

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
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

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

# Prefixes for Hermes warning/notice messages that should be stripped.
_WARNING_PREFIXES = ("Warning:", "⚠")


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
        if stripped.startswith(_WARNING_PREFIXES):
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


def _get_conversation_id() -> str | None:
    """Extract the ``--conversation-id`` value from the CLI args.

    The harness subprocess is launched by :mod:`process_manager` with
    ``--conversation-id conv_<hex>`` on the command line.
    """
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--conversation-id" and i + 1 < len(argv):
            return argv[i + 1]
    return None


# Keys from the user's ``~/.hermes/config.yaml`` that the per-session
# HERMES_HOME needs in order to authenticate with the inference provider.
# Everything else (secrets, security, agent tuning, terminal, etc.) is
# either irrelevant to a headless Omnigent turn or actively harmful
# (e.g. ``secrets.bitwarden`` referencing an unset ``BWS_ACCESS_TOKEN``).
_USER_CONFIG_KEYS = frozenset(
    {
        "model",
        "providers",
        "fallback_providers",
        "credential_pool_strategies",
    }
)


def _load_user_hermes_config() -> dict:
    """Load inference-relevant keys from the user's ``~/.hermes/config.yaml``.

    Returns a dict containing only the keys Hermes needs to resolve a
    model and authenticate (see :data:`_USER_CONFIG_KEYS`), or ``{}``
    when the file is missing or malformed.
    """
    user_config = Path.home() / ".hermes" / "config.yaml"
    if not user_config.is_file():
        return {}
    try:
        import yaml

        full = yaml.safe_load(user_config.read_text()) or {}
        return {k: v for k, v in full.items() if k in _USER_CONFIG_KEYS}
    except Exception:  # noqa: BLE001 — catch YAML parse errors, permission errors, etc.
        _logger.debug("Failed to load user Hermes config at %s", user_config, exc_info=True)
        return {}


def _populate_hermes_home(
    hermes_home: Path,
    hook_script_path: str,
    server_url: str,
    session_id: str,
) -> None:
    """Populate a per-session ``HERMES_HOME`` with policy hook config.

    Creates a ``config.yaml`` that registers the Omnigent policy hook
    as a ``pre_tool_call`` shell hook, and writes a wrapper script
    that exports the server env vars before exec-ing the Python hook.

    The user's ``~/.hermes/config.yaml`` model/provider settings are
    merged into the per-session config so Hermes can authenticate with
    the inference provider the user configured via ``hermes model``.

    This mirrors how Codex creates a per-session ``CODEX_HOME`` with
    its own ``config.toml`` — Hermes scopes all state (config, sessions,
    hooks, allowlist) to ``HERMES_HOME``.

    :param hermes_home: The per-session HERMES_HOME directory.
    :param hook_script_path: Absolute path to ``hermes_policy_hook.py``.
    :param server_url: Omnigent server URL.
    :param session_id: Conversation / session ID for policy evaluation.
    """
    hermes_home.mkdir(parents=True, exist_ok=True)

    # Write the wrapper shell script that sets env vars and execs the hook. It
    # bakes a one-shot auth token + workspace-routing header, so it is
    # owner-only (0o700) — the secret is never world-readable.
    from omnigent.native_policy_hook import policy_hook_wrapper_script

    wrapper = hermes_home / "omnigent-policy-hook.sh"
    wrapper.write_text(policy_hook_wrapper_script(server_url, session_id, hook_script_path))
    wrapper.chmod(0o700)

    # Start from the user's config so model/provider/auth settings carry over.
    # Hermes scopes everything to HERMES_HOME, so without this merge it won't
    # find the inference provider the user configured via ``hermes model``.
    user_cfg = _load_user_hermes_config()
    config: dict = {**user_cfg}

    # Layer Omnigent's policy hook config on top.
    config["hooks_auto_accept"] = True
    config["hooks"] = {
        **config.get("hooks", {}),
        "pre_tool_call": [
            {
                "command": str(wrapper),
                # One day: must match the server's ``ask_timeout`` so
                # the hook stays alive while the human responds to the
                # web-UI approval card (ASK policy).
                "timeout": 86400,
            },
        ],
    }

    config_path = hermes_home / "config.yaml"
    # Use JSON for YAML-compatible output (JSON is valid YAML).
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    # Copy the user's .env file if present (carries API keys like
    # OPENROUTER_API_KEY, OPENAI_API_KEY, etc.).
    user_env = Path.home() / ".hermes" / ".env"
    if user_env.is_file():
        shutil.copy2(user_env, hermes_home / ".env")

    # Copy the user's auth.json if present (carries provider credentials
    # stored by ``hermes auth`` / ``hermes model``).
    user_auth = Path.home() / ".hermes" / "auth.json"
    if user_auth.is_file():
        shutil.copy2(user_auth, hermes_home / "auth.json")

    # Pre-populate the allowlist so Hermes never prompts for consent.
    # Hermes' allowlist format is {"approvals": [{"event": ..., "command": ...}]}.
    allowlist_path = hermes_home / "shell-hooks-allowlist.json"
    allowlist_data = {
        "approvals": [
            {"event": "pre_tool_call", "command": str(wrapper)},
        ],
    }
    allowlist_path.write_text(json.dumps(allowlist_data, indent=2) + "\n")


def _build_hermes_args(
    hermes_path: str,
    message: str,
    *,
    model: str | None = None,
    session_id: str | None = None,
    skills_filter: str | list[str] | None = None,
) -> list[str]:
    """
    Build the argument list for a Hermes subprocess call.

    :param hermes_path: Path to the Hermes CLI binary.
    :param message: The user message text.
    :param model: Optional model override (``-m`` flag).
    :param session_id: Optional session ID to resume (``--resume``).
    :param skills_filter: Skills to preload. A non-empty list preloads
        those named skills (``-s a,b``); ``"none"`` skips preloaded
        skills (``--ignore-rules``); ``"all"``/``None``/empty adds nothing.
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
    if isinstance(skills_filter, list) and skills_filter:
        args.extend(["-s", ",".join(skills_filter)])
    elif skills_filter == "none":
        args.append("--ignore-rules")
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

    A per-session ``HERMES_HOME`` directory is created with a
    ``config.yaml`` that registers an Omnigent policy hook as a
    Hermes ``pre_tool_call`` shell hook, enforcing ``PHASE_TOOL_CALL``
    policies on all native Hermes tool calls.
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
        # Per-session HERMES_HOME with policy hook config.
        self._hermes_home: Path | None = None
        self._setup_hermes_home()

    def _setup_hermes_home(self) -> None:
        """Create a per-session ``HERMES_HOME`` with Omnigent policy hooks.

        When the Omnigent server URL and conversation ID are available,
        creates a temp directory with a ``config.yaml`` that registers the
        Omnigent policy hook as a Hermes ``pre_tool_call`` shell hook.
        The ``HERMES_HOME`` env var is passed to the subprocess so Hermes
        reads this config instead of the user's ``~/.hermes/``.

        Mirrors how Codex creates a per-session ``CODEX_HOME``.
        """
        server_url = os.environ.get("RUNNER_SERVER_URL", "")
        conv_id = _get_conversation_id()
        if not server_url or not conv_id:
            _logger.warning(
                "Hermes policy hooks disabled: RUNNER_SERVER_URL=%r, conv_id=%r",
                server_url or "(unset)",
                conv_id or "(unset)",
            )
            return
        self._hermes_home = Path(tempfile.mkdtemp(prefix="hermes_home_"))
        hook_script = str(Path(__file__).with_name("hermes_policy_hook.py"))
        _populate_hermes_home(self._hermes_home, hook_script, server_url, conv_id)
        _logger.debug("Hermes per-session home: %s", self._hermes_home)

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
        through Omnigent's tool dispatch.  Omnigent policies are enforced
        via Hermes' native ``pre_tool_call`` shell hook that evaluates
        ``PHASE_TOOL_CALL`` against the Omnigent server before each tool
        execution.
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
            skills_filter=self._skills_filter,
        )

        # Build subprocess env with per-session HERMES_HOME for policy hooks.
        proc_env: dict[str, str] | None = None
        if self._hermes_home is not None:
            proc_env = {**os.environ, "HERMES_HOME": str(self._hermes_home)}
            _logger.info("Hermes using per-session HERMES_HOME=%s", self._hermes_home)
        else:
            _logger.warning("Hermes running WITHOUT per-session HERMES_HOME (no policy hooks)")

        _logger.debug("Hermes subprocess: %s", " ".join(args))

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=proc_env,
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
                    "Install: curl -fsSL https://hermes-agent.nousresearch.com"
                    "/install.sh | sh"
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
        return str(
            hash(tuple((m.get("role", ""), str(m.get("content", ""))[:200]) for m in messages))
        )

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
        # Best-effort cleanup of the per-session HERMES_HOME.
        if self._hermes_home is not None:
            shutil.rmtree(self._hermes_home, ignore_errors=True)
            self._hermes_home = None
        await super().close()
