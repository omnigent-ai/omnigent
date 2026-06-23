"""QwenExecutor: run agents through Qwen Code's ACP mode.

Spawns Qwen (``qwen --acp``) as a subprocess and communicates via the
Agent Communication Protocol (ACP) — a JSON-RPC 2.0 protocol over
newline-delimited JSON on stdin/stdout.

Protocol flow:
  1. ``initialize``  — handshake, learn capabilities.
  2. ``session/new`` — create a session, get back the server-assigned sessionId.
  3. ``session/prompt`` — send a user turn; wait for streaming
     ``session/update`` notifications and the final response.
  4. Repeat step 3 for subsequent turns (``session/load`` or just re-use the
     same sessionId if the server keeps it alive across prompts).

Qwen manages its own agent loop, tool execution, context window, and
compaction internally.  This executor translates the ACP event stream into
Omnigent ExecutorEvents.

Requirements:
    The ``qwen`` CLI (v0.18+) must be installed and on PATH.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any

from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    TurnComplete,
)

logger = logging.getLogger(__name__)

# ACP protocol constants (JSON-RPC 2.0 method names)
_AGENT_METHOD_INITIALIZE = "initialize"
_AGENT_METHOD_SESSION_NEW = "session/new"
_AGENT_METHOD_SESSION_PROMPT = "session/prompt"

# Notifications sent *from* the agent to the client
_CLIENT_NOTIFICATION_SESSION_UPDATE = "session/update"

# session/update.update.sessionUpdate values we care about
_UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
_UPDATE_TOOL_CALL = "tool_call"
_UPDATE_TOOL_CALL_UPDATE = "tool_call_update"

# How long (seconds) to wait for qwen to respond to a JSON-RPC request
# before treating the turn as timed out.
_PROMPT_TIMEOUT_SECONDS = 300.0
_INIT_TIMEOUT_SECONDS = 30.0

# ACP protocol version this executor targets.
_PROTOCOL_VERSION = 1


def _inline_text_file_data(file_data: Any) -> str:  # type: ignore[explicit-any]
    """Decode a text ``input_file`` ``file_data`` data URI into inline text.

    Mirrors the codex executor: ``input_file`` blocks may carry a
    ``data:<mime>;base64,<payload>`` URI. Text files are decoded so the model
    sees their content; binary files (PDF, images) can't be inlined as text and
    return ``""`` (the caller falls back to an attachment marker). A bare,
    non-data-URI string is treated as already-inline text.

    :param file_data: The block's ``file_data`` value (or ``None``).
    :returns: Decoded text, or ``""`` when absent/binary/undecodable.
    """
    if not isinstance(file_data, str) or not file_data:
        return ""
    if not file_data.startswith("data:"):
        return file_data
    try:
        import base64

        meta, b64 = file_data.split(",", 1)
        mime = meta.split(";")[0].replace("data:", "")
        if not mime.startswith("text/"):
            return ""  # binary payloads can't be inlined as prompt text
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — best-effort; never break a turn on a bad URI
        return ""


def _parse_image_data_uri(data_uri: Any) -> tuple[str, str] | None:  # type: ignore[explicit-any]
    """Split an ``image/*`` ``data:`` URI into ``(mime_type, base64_payload)``.

    ACP's ``image`` content block carries the raw base64 payload plus its media
    type separately (``{"type": "image", "mimeType": ..., "data": ...}``), so we
    peel those out of the ``data:image/png;base64,<payload>`` URI the runner
    resolves a ``file_id`` into. Returns ``None`` for anything that isn't an
    inline ``image/*`` data URI (external URLs are never fetched — SSRF).

    :param data_uri: A block's ``image_url`` / ``file_data`` value (or ``None``).
    :returns: ``(mime_type, base64_payload)`` for an image data URI, else ``None``.
    """
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        return None
    try:
        meta, payload = data_uri.split(",", 1)
    except ValueError:
        return None
    mime = meta.split(";")[0].replace("data:", "")
    if not mime.startswith("image/") or not payload:
        return None
    return mime, payload


class QwenExecutor(Executor):
    """Executor that drives Qwen Code via its ACP (``--acp``) mode.

    Spawns a ``qwen --acp`` subprocess and manages sessions through the
    ACP JSON-RPC 2.0 protocol over newline-delimited stdin/stdout.
    """

    def __init__(
        self,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        qwen_path: str | None = None,
        gateway_base_url: str | None = None,
        gateway_auth_command: str | None = None,
    ) -> None:
        """Initialize the Qwen executor.

        :param cwd: Working directory for the qwen subprocess.  When
            ``None``, the subprocess inherits the caller's cwd.
        :param os_env: Environment spec for sandboxing (currently unused
            by this executor but accepted for API parity).
        :param model: Model identifier to pass in ``session/new``.
        :param qwen_path: Absolute path to qwen CLI binary.
            Defaults to ``"qwen"`` (PATH lookup).
        :param gateway_base_url: OpenAI-compatible base URL of an Omnigent
            provider/gateway (from ``HARNESS_QWEN_GATEWAY_BASE_URL``). When set
            with *gateway_auth_command*, the executor exports ``OPENAI_BASE_URL``
            / ``OPENAI_API_KEY`` / ``OPENAI_MODEL`` into the ``qwen`` subprocess
            so the spec's ``auth:`` / ``providers:`` routing takes effect instead
            of qwen's ambient CLI auth.
        :param gateway_auth_command: Shell command that prints a bearer token to
            stdout (from ``HARNESS_QWEN_GATEWAY_AUTH_COMMAND``); run once at
            process start to snapshot ``OPENAI_API_KEY``.
        """
        self._cwd = cwd or os.getcwd()
        self._os_env = os_env
        self._model = model
        self._qwen_path = qwen_path or "qwen"
        self._gateway_base_url = gateway_base_url
        self._gateway_auth_command = gateway_auth_command

        # Asyncio subprocess (created on first run_turn call).
        self._proc: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]

        # Queue fed by the stdout-reader coroutine.
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()  # type: ignore[explicit-any]
        self._reader_task: asyncio.Task[None] | None = None
        # Drains qwen stderr so a chatty CLI can't fill the pipe buffer
        # (~64 KiB) and wedge the subprocess mid-turn — see _read_stderr.
        self._stderr_task: asyncio.Task[None] | None = None

        # Monotonically increasing JSON-RPC request id.
        self._rpc_id: int = 0

        # Pending RPC responses keyed by request id.
        # When _reader_task receives a response (has "id" + "result"/"error"),
        # it places it here for the awaiting coroutine.
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}  # type: ignore[explicit-any]

        # ACP session id assigned by qwen (returned in session/new response).
        self._session_id: str | None = None

        # Whether initialize has been sent already.
        self._initialized: bool = False

        # Whether qwen accepts ``image`` prompt blocks, learned from the
        # ``initialize`` handshake (``agentCapabilities.promptCapabilities.image``).
        # When True we forward attached images as ACP ``image`` content blocks so
        # vision-capable models can see them; when False they degrade to a text
        # ``[attached image: <name>]`` marker rather than vanishing.
        self._image_supported: bool = False

        # Whether the system prompt has been folded into a turn already.
        # ACP has no dedicated system-prompt field, so we prepend it to the
        # first user turn only (subsequent turns reuse the same session and
        # qwen retains the earlier context).
        self._system_prompt_sent: bool = False

        # Bridges the ExecutorAdapter installs (best-effort, via
        # ``getattr(..., None) is None``) so qwen's mid-turn
        # ``session/request_permission`` routes through Omnigent's TOOL_CALL
        # policy + human-consent elicitation instead of blind auto-approve —
        # mirrors ClaudeSDKExecutor. Declared here so the install check sees
        # them and the intent is explicit. ``None`` means "no bridge wired"
        # (standalone use / unit tests), in which case permission falls back
        # to allow. See _decide_permission.
        self._policy_evaluator: Any | None = None  # type: ignore[explicit-any]
        self._elicitation_handler: Any | None = None  # type: ignore[explicit-any]

    # ------------------------------------------------------------------
    # Low-level ACP helpers
    # ------------------------------------------------------------------

    async def _start_process(self) -> None:
        """Start ``qwen --acp`` as an asyncio subprocess.

        The StreamReader limit is set to 16 MiB so that qwen's large
        ``session/new`` responses (which can list dozens of available
        models) don't hit the default 64 KiB per-line cap and raise
        "Separator is not found, and chunk exceed the limit".
        """
        env = os.environ.copy()
        # Translate Omnigent's provider/gateway routing into the OpenAI-compatible
        # env vars qwen reads (overriding any ambient values). No-op when no
        # gateway is wired (the CLI's own ambient auth is used).
        env.update(await self._resolve_gateway_env())
        # 16 MiB per-line limit for the stdout StreamReader.
        _STREAM_LIMIT = 16 * 1024 * 1024
        self._proc = await asyncio.create_subprocess_exec(
            self._qwen_path,
            "--acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self._cwd,
            limit=_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _resolve_gateway_env(self) -> dict[str, str]:
        """Build the OpenAI-compatible env qwen reads from the gateway config.

        When a provider/gateway is wired (base URL + a bearer-token command),
        run the command **once** to snapshot a token and return
        ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``OPENAI_MODEL``. Returns an
        empty dict when no gateway is configured (the CLI's ambient auth path).

        The token is captured at process start; qwen has no token-refresh hook,
        so a short-lived rotating token (e.g. the Databricks gateway) can expire
        over a long session — restart the subprocess to refresh. See
        docs/QWEN_FOLLOWUPS.md.

        :returns: The OPENAI_* overrides, or ``{}`` when no gateway is wired.
        :raises RuntimeError: If the auth command fails or yields no token.
        """
        if not self._gateway_base_url or not self._gateway_auth_command:
            return {}
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            self._gateway_auth_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            detail = err.decode("utf-8", errors="replace").strip()[:200]
            raise RuntimeError(
                f"qwen gateway auth command failed (exit {proc.returncode}): {detail}"
            )
        token = out.decode("utf-8", errors="replace").strip()
        if not token:
            raise RuntimeError("qwen gateway auth command produced an empty token")
        env: dict[str, str] = {
            "OPENAI_BASE_URL": self._gateway_base_url,
            "OPENAI_API_KEY": token,
        }
        if self._model:
            env["OPENAI_MODEL"] = self._model
        return env

    async def _read_stderr(self) -> None:
        """Continuously drain qwen stderr, logging each line at debug.

        With ``stderr=PIPE`` and no reader, a chatty ``qwen`` process can
        fill the OS pipe buffer (~64 KiB), block on its next stderr write,
        and stall the whole turn until the prompt timeout fires. Draining
        keeps the pipe clear; the lines are logged so diagnostics aren't lost.
        """
        assert self._proc and self._proc.stderr
        try:
            while True:
                raw_line = await self._proc.stderr.readline()
                if not raw_line:
                    break  # EOF — process exited
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.debug("qwen stderr: %s", line)
        except asyncio.CancelledError:
            # Expected on shutdown (close() cancels this task); exit quietly.
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("qwen stderr reader stopped: %s", exc)

    async def _read_stdout(self) -> None:
        """Continuously read NDJSON lines from qwen stdout.

        Decoded messages are dispatched:
        - Responses (have ``"id"`` key + ``"result"``/``"error"``) are
          resolved into the matching ``_pending`` future.
        - Notifications (have ``"method"`` key, no ``"id"``) are put on
          ``_queue`` for ``run_turn`` to consume.

        Uses ``readline()`` directly instead of ``async for line in
        stdout`` to benefit from the raised StreamReader limit set at
        process creation time (the iteration protocol falls back to the
        chunk limit rather than the configured per-line limit in some
        Python versions).
        """
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw_line = await self._proc.stdout.readline()
                if not raw_line:
                    break  # EOF — process exited
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg: dict[str, Any] = json.loads(line)  # type: ignore[explicit-any]
                except json.JSONDecodeError:
                    logger.debug("qwen: non-JSON stdout line: %r", line[:200])
                    continue

                msg_id = msg.get("id")
                # Match a response by "id + no method": qwen's own requests
                # (e.g. session/request_permission) also carry an id from a
                # counter that can collide with ours, so without the method
                # check a colliding request would mis-resolve our prompt future
                # and hang the turn.
                if msg_id is not None and "method" not in msg and msg_id in self._pending:
                    # Response to one of our requests.
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
                else:
                    # Notification or server-initiated request → run_turn queue.
                    await self._queue.put(msg)
        except (asyncio.CancelledError, EOFError):
            # Expected on shutdown (close() cancels this task) or stream EOF;
            # exit quietly without surfacing an error.
            pass
        except Exception as exc:
            logger.exception("qwen stdout reader error: %s", exc)
            # Wake any pending futures with an error so callers don't block
            # forever when the process dies unexpectedly.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            await self._queue.put({"type": "error", "message": str(exc)})

    async def _send(self, msg: dict[str, Any]) -> None:  # type: ignore[explicit-any]
        """Write one newline-terminated JSON message to qwen stdin."""
        assert self._proc and self._proc.stdin
        encoded = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(encoded)
        await self._proc.stdin.drain()

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],  # type: ignore[explicit-any]
        timeout: float = _INIT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:  # type: ignore[explicit-any]
        """Send a JSON-RPC 2.0 request and await its response.

        :param method: RPC method name, e.g. ``"initialize"``.
        :param params: Request parameters.
        :param timeout: Maximum seconds to wait for the response.
        :returns: The full response message dict (containing ``"result"`` or
            ``"error"``).
        :raises asyncio.TimeoutError: If no response arrives within *timeout*.
        :raises RuntimeError: If the process is not running.
        """
        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()  # type: ignore[explicit-any]
        self._pending[req_id] = fut

        request: dict[str, Any] = {  # type: ignore[explicit-any]
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        await self._send(request)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    async def _notify(self, method: str, params: dict[str, Any]) -> None:  # type: ignore[explicit-any]
        """Send a JSON-RPC 2.0 notification (no response expected)."""
        notification: dict[str, Any] = {  # type: ignore[explicit-any]
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._send(notification)

    # ------------------------------------------------------------------
    # ACP handshake helpers
    # ------------------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        """Perform the ``initialize`` handshake if not already done."""
        if self._initialized:
            return
        resp = await self._rpc(
            _AGENT_METHOD_INITIALIZE,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "clientInfo": {"name": "omnigent", "version": "1.0"},
            },
            timeout=_INIT_TIMEOUT_SECONDS,
        )
        if "error" in resp:
            raise RuntimeError(
                f"qwen ACP initialize failed: {resp['error'].get('message', resp['error'])}"
            )
        prompt_caps = (
            (resp.get("result") or {}).get("agentCapabilities", {}).get("promptCapabilities", {})
        )
        self._image_supported = bool(prompt_caps.get("image"))
        self._initialized = True

    async def _ensure_session(self) -> str:
        """Create (or reuse) an ACP session, returning its server-assigned id.

        :returns: The session id string assigned by qwen.
        """
        if self._session_id is not None:
            return self._session_id

        params: dict[str, Any] = {  # type: ignore[explicit-any]
            "sessionId": secrets.token_urlsafe(16),
            "cwd": self._cwd,
            "mcpServers": [],
        }
        if self._model:
            params["model"] = self._model

        resp = await self._rpc(
            _AGENT_METHOD_SESSION_NEW,
            params,
            timeout=_INIT_TIMEOUT_SECONDS,
        )
        if "error" in resp:
            raise RuntimeError(
                f"qwen ACP session/new failed: {resp['error'].get('message', resp['error'])}"
            )

        # Qwen assigns (possibly remaps) the session id — always use what
        # the server returns, not what we sent.
        result = resp.get("result", {})
        server_session_id = result.get("sessionId")
        if not server_session_id:
            raise RuntimeError(
                "qwen ACP session/new response missing sessionId: " + json.dumps(resp)[:200]
            )
        self._session_id = server_session_id
        return self._session_id

    # ------------------------------------------------------------------
    # Server-initiated requests (agent → client)
    # ------------------------------------------------------------------

    async def _respond_to_agent_request(self, request: dict[str, Any]) -> None:  # type: ignore[explicit-any]
        """Answer a server-initiated ACP request from qwen.

        qwen can drive the client mid-turn (e.g. permission prompts). A blanket
        ``{"result": {}}`` reply would be wrong, so we branch on the method:

        - ``session/request_permission`` — decide via Omnigent's TOOL_CALL
          policy + human-consent elicitation (:meth:`_decide_permission`),
          then select the matching allow/reject option. NOT a blind approve.
        - anything else — reply with a JSON-RPC ``method not found`` error
          rather than a bogus success, so qwen fails loudly instead of acting
          on empty data.

        NB: we do **not** advertise ``clientCapabilities.fs`` in ``initialize``,
        so qwen never delegates file ops to us — it uses its own file tools, and
        any ``fs/*`` request would hit the ``method not found`` branch. To route
        file I/O through Omnigent, advertise the capability and add
        ``fs/read_text_file`` / ``fs/write_text_file`` handlers (see
        docs/QWEN_FOLLOWUPS.md).

        :param request: The decoded JSON-RPC request object (has ``id`` and
            ``method``).
        """
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {}) or {}
        # Log every server-initiated request so DEBUG reveals whether qwen
        # actually delegates permissions/fs to us (vs handling them itself).
        logger.debug("qwen agent request: method=%s id=%s", method, req_id)

        result: dict[str, Any] | None = None  # type: ignore[explicit-any]
        error: dict[str, Any] | None = None  # type: ignore[explicit-any]

        try:
            if method == "session/request_permission":
                allow = await self._decide_permission(params)
                result = {"outcome": self._permission_outcome(params, allow=allow)}
            else:
                error = {
                    "code": -32601,
                    "message": f"omnigent: unsupported ACP request method {method!r}",
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("qwen agent request %s failed: %s", method, exc)
            error = {"code": -32603, "message": f"{method} failed: {exc}"}

        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}  # type: ignore[explicit-any]
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result
        await self._send(reply)

    @staticmethod
    def _extract_tool_call(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:  # type: ignore[explicit-any]
        """Pull ``(tool_name, tool_input)`` from a ``session/request_permission``.

        Qwen's payload carries a ``toolCall`` with ``_meta.toolName`` (e.g.
        ``"run_shell_command"``) and a ``rawInput`` dict (e.g.
        ``{"command": "rm -f …", …}``). Falls back to the tool-call ``kind``
        (e.g. ``"execute"``) and an empty dict when fields are absent.
        """
        tool_call = params.get("toolCall") or {}
        meta = tool_call.get("_meta") or {}
        name = meta.get("toolName") or tool_call.get("kind") or "tool"
        args = tool_call.get("rawInput")
        if not isinstance(args, dict):
            args = {}
        return str(name), args

    async def _decide_permission(self, params: dict[str, Any]) -> bool:  # type: ignore[explicit-any]
        """Decide allow/deny for a permission request — policy then elicitation.

        Mirrors ``ClaudeSDKExecutor``'s ``can_use_tool`` gate, composed of two
        independent checks read from the adapter-installed bridges:

        1. **TOOL_CALL policy** (:attr:`_policy_evaluator`): a hard
           ``POLICY_ACTION_DENY`` denies; ``POLICY_ACTION_ASK`` defers to
           elicitation (and **fails closed** — deny — when no elicitation
           handler is wired); ``ALLOW`` / unspecified falls through.
        2. **Human-consent elicitation** (:attr:`_elicitation_handler`): routes
           to the user via ``ctx.elicit`` and returns their accept/deny.

        When neither bridge is wired (standalone use / unit tests), falls back
        to allow so direct use of the executor isn't blocked. In normal runner
        operation the adapter always installs both, so destructive actions are
        gated rather than blindly approved.

        :param params: The ``session/request_permission`` params.
        :returns: ``True`` to allow the tool call, ``False`` to deny it.
        """
        tool_name, tool_input = self._extract_tool_call(params)
        handler = getattr(self, "_elicitation_handler", None)
        policy_eval = getattr(self, "_policy_evaluator", None)

        if policy_eval is not None:
            action: str | None
            try:
                verdict = await policy_eval(
                    "PHASE_TOOL_CALL", {"name": tool_name, "arguments": tool_input}
                )
                action = getattr(verdict, "action", None)
            except Exception as exc:  # noqa: BLE001 — fail open to elicitation
                logger.warning("qwen TOOL_CALL policy eval failed for %s: %s", tool_name, exc)
                action = None
            if action == "POLICY_ACTION_DENY":
                logger.info("qwen permission denied by policy: tool=%s", tool_name)
                return False
            if action == "POLICY_ACTION_ASK":
                if handler is None:
                    logger.warning(
                        "qwen TOOL_CALL policy ASK with no elicitation handler; denying tool=%s",
                        tool_name,
                    )
                    return False
                allowed = bool(await handler(tool_name, tool_input))
                logger.info(
                    "qwen permission %s by user (policy ASK): tool=%s",
                    "allowed" if allowed else "denied",
                    tool_name,
                )
                return allowed
            # ALLOW / UNSPECIFIED / unknown → fall through to elicitation.

        if handler is not None:
            allowed = bool(await handler(tool_name, tool_input))
            logger.info(
                "qwen permission %s by user: tool=%s",
                "allowed" if allowed else "denied",
                tool_name,
            )
            return allowed

        # No gates wired (standalone / tests) — allow.
        logger.debug("qwen permission allowed (no policy/elicitation wired): tool=%s", tool_name)
        return True

    @staticmethod
    def _permission_outcome(  # type: ignore[explicit-any]
        params: dict[str, Any], *, allow: bool
    ) -> dict[str, Any]:
        """Map an allow/deny decision to an ACP permission ``outcome``.

        On allow, prefer a once-scoped grant (``allow_once``) over
        ``allow_always`` so we never persist a blanket "always allow". On deny,
        pick a ``reject_*`` option, or ``cancelled`` when none is offered.
        """
        options = [o for o in (params.get("options") or []) if isinstance(o, dict)]

        def _pick(*kinds: str) -> dict[str, Any] | None:  # type: ignore[explicit-any]
            for kind in kinds:  # exact-kind preference order
                for opt in options:
                    if opt.get("kind") == kind:
                        return opt
            return None

        if allow:
            chosen = _pick("allow_once", "allow_always") or next(
                (o for o in options if "allow" in str(o.get("kind", ""))), None
            )
            if chosen is None:  # no allow option offered — fail safe
                return {"outcome": "cancelled"}
        else:
            chosen = _pick("reject_once", "reject_always") or next(
                (o for o in options if "reject" in str(o.get("kind", ""))), None
            )
            if chosen is None:  # no explicit reject option — cancel
                return {"outcome": "cancelled"}
        return {"outcome": "selected", "optionId": chosen.get("optionId")}

    @staticmethod
    def _image_blocks_from_content(content: Any) -> list[dict[str, Any]]:  # type: ignore[explicit-any]
        """Build ACP ``image`` prompt blocks from a message's ``input_image`` blocks.

        The runner resolves a ``file_id`` into an inline ``image_url`` (or
        ``file_data``) ``data:image/…;base64,…`` URI before the message reaches
        us; we peel that into ACP's ``{"type": "image", "mimeType", "data"}``
        shape. External URLs and non-image payloads are skipped (they're handled
        as text / markers by :meth:`_text_from_blocks`).

        :param content: A message's ``content`` (block list, or non-list → none).
        :returns: A list of ACP image content blocks (possibly empty).
        """
        out: list[dict[str, Any]] = []  # type: ignore[explicit-any]
        if not isinstance(content, list):
            return out
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "input_image":
                continue
            parsed = _parse_image_data_uri(block.get("image_url") or block.get("file_data"))
            if parsed:
                mime, data = parsed
                out.append({"type": "image", "mimeType": mime, "data": data})
        return out

    @staticmethod
    def _text_from_blocks(
        blocks: list[Any],
        *,
        emit_image_marker: bool = False,  # type: ignore[explicit-any]
    ) -> str:
        """Extract prompt text from a Responses-API content-block list.

        The harness adapter passes a content **list** (rather than a plain
        string) whenever a message carries a non-text block — e.g. a file
        attachment becomes ``[{"type": "input_text", …}, {"type":
        "input_file", …}]``. ACP's ``session/prompt`` is text-only, so we
        fold each block into text:

        - ``input_text`` / ``output_text`` / ``text`` → the text verbatim.
        - ``input_file`` → the file's inlined content (fenced with a labeled
          ``--- attached file: <name> ---`` header/footer) when the runner
          resolved it into a text ``file_data`` data URI; otherwise a
          ``[attached file: <name>]`` marker so the attachment isn't silently
          dropped. The fence keeps the action request and the file body
          separate — bare-appending raw content derails weaker models into
          narrating tool calls as prose (full file delivery via ``file_id`` and
          audio input are deferred — see docs/QWEN_FOLLOWUPS.md).
        - ``input_image`` → handled out-of-band as a real ACP ``image`` prompt
          block (:meth:`_image_blocks_from_content`); this fold emits a
          ``[attached image: <name>]`` marker only when *emit_image_marker* is
          set (qwen lacks image capability), so the image isn't silently lost.

        Crucially, the block ``type`` is ``input_text`` (not ``text``): the
        previous ``type == "text"`` filter matched nothing, dropping the whole
        message — text and file alike — whenever an attachment was present.

        :param blocks: The message ``content`` list.
        :param emit_image_marker: When ``True``, append a ``[attached image:
            …]`` marker for each ``input_image`` (used when qwen can't accept a
            real image block); when ``False``, skip images here (the caller
            forwards them as ACP image blocks).
        :returns: The concatenated prompt text (may be empty).
        """
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("input_text", "output_text", "text"):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif btype == "input_file":
                name = block.get("filename") or block.get("file_id") or "file"
                inlined = _inline_text_file_data(block.get("file_data"))
                if inlined:
                    # Fence the content with a labeled header/footer so the model
                    # reads it as an *attachment*, not as instructions. Bare-
                    # appending the raw text derails weaker models (notably
                    # qwen3-coder:free): they lose the tool-calling thread and
                    # narrate the shell command as prose instead of emitting a
                    # structured call. Fencing keeps the action request and the
                    # file body visually separate. See docs/QWEN_FOLLOWUPS.md.
                    parts.append(
                        f"--- attached file: {name} ---\n{inlined}\n--- end of {name} ---"
                    )
                else:
                    parts.append(f"[attached file: {name}]")
            elif btype == "input_image" and emit_image_marker:
                # Only when qwen can't take a real image block (capability off):
                # leave a marker so the image isn't silently dropped. When it
                # can, the image is sent via _image_blocks_from_content instead.
                name = block.get("filename") or block.get("file_id")
                parts.append(f"[attached image: {name}]" if name else "[attached image]")
            # input_audio: no audio over the ACP text prompt yet (deferred —
            # see docs/QWEN_FOLLOWUPS.md).
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Executor interface
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[Any],  # type: ignore[explicit-any]  # noqa: ARG002 — qwen runs its own tool registry; param required by the Executor interface
        system_prompt: str,
        config: ExecutorConfig | None = None,  # noqa: ARG002 — unused; required by the Executor interface
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn of the Qwen agent loop via ACP.

        Sends a ``session/prompt`` request and yields events until the
        turn completes (``stopReason`` present in the response) or an
        error occurs.

        :param messages: Conversation history.
        :param tools: Tool specs (not passed directly to Qwen; Qwen uses
            its own tool registry — MCP bridging is TODO).
        :param system_prompt: Instructions for the session.
        :param config: Optional executor config (model override etc.).
        """
        try:
            # Lazily boot the subprocess. A missing/unspawnable ``qwen`` binary
            # raises here (FileNotFoundError / OSError) — surface it as a clean
            # ExecutorError instead of letting it escape the generator.
            if self._proc is None or self._proc.returncode is not None:
                await self._start_process()
            await self._ensure_initialized()
            session_id = await self._ensure_session()
        except Exception as exc:  # noqa: BLE001
            yield ExecutorError(message=str(exc), retryable=False)
            return

        # Build the prompt payload from the most recent user message.
        user_text = ""
        image_blocks: list[dict[str, Any]] = []  # type: ignore[explicit-any]
        for msg in reversed(messages):
            role = msg.get("role", "") if isinstance(msg, dict) else ""
            if role == "user":
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                if isinstance(content, str):
                    user_text = content
                elif isinstance(content, list):
                    # Forward attached images as real ACP image blocks when qwen
                    # supports them (vision models can then see them); otherwise
                    # fall back to a text marker so they aren't silently dropped.
                    if self._image_supported:
                        image_blocks = self._image_blocks_from_content(content)
                    user_text = self._text_from_blocks(
                        content, emit_image_marker=not self._image_supported
                    )
                break

        # ACP has no system-prompt field, so fold it into the first turn's
        # user text. Without this the agent's persona / instructions (the
        # spec ``prompt:``) never reach qwen and it runs uninstructed.
        if not self._system_prompt_sent and system_prompt:
            user_text = f"{system_prompt}\n\n{user_text}" if user_text else system_prompt
            self._system_prompt_sent = True

        # Text first, then any image blocks (ACP prompt is an ordered array).
        prompt_blocks: list[dict[str, Any]] = []  # type: ignore[explicit-any]
        if user_text or not image_blocks:
            prompt_blocks.append({"type": "text", "text": user_text})
        prompt_blocks.extend(image_blocks)

        # Drain stale items from a prior turn. Notifications (no ``id``) are
        # safe to drop, but a server-initiated *request* left in the queue is
        # still awaiting a reply — answer it instead of discarding it, or qwen
        # blocks forever on a response that never comes.
        while not self._queue.empty():
            try:
                stale = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(stale, dict) and stale.get("id") is not None and stale.get("method"):
                await self._respond_to_agent_request(stale)

        # Send the turn — this is a JSON-RPC *request*, so we wait for
        # both streaming notifications AND the final response.
        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()  # type: ignore[explicit-any]
        self._pending[req_id] = fut

        prompt_request: dict[str, Any] = {  # type: ignore[explicit-any]
            "jsonrpc": "2.0",
            "id": req_id,
            "method": _AGENT_METHOD_SESSION_PROMPT,
            "params": {
                "sessionId": session_id,
                "prompt": prompt_blocks,
            },
        }
        await self._send(prompt_request)

        # Idle-based deadline: reset on every inbound message (bottom of loop),
        # so it bounds time-without-progress, not total turn length — a long
        # human approval or slow stream won't trip a spurious timeout.
        deadline = loop.time() + _PROMPT_TIMEOUT_SECONDS
        accumulated_text: list[str] = []

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                yield ExecutorError(message="Timeout waiting for qwen response", retryable=True)
                return

            # Complete only once the future is resolved AND the queue is drained.
            # The reader resolves the future directly but enqueues chunks, and
            # the response trails the chunk stream — a bare fut.done() check
            # could return with chunks still buffered, truncating the response.
            if fut.done() and self._queue.empty():
                try:
                    response = fut.result()
                except Exception as exc:  # noqa: BLE001
                    # The stdout reader sets an exception on the future when the
                    # subprocess dies. Surface it as a clean retryable error
                    # rather than letting it raise out of the generator.
                    self._session_id = None
                    self._system_prompt_sent = False
                    yield ExecutorError(message=f"qwen process error: {exc}", retryable=True)
                    return
                if "error" in response:
                    error_msg = response["error"].get("message", "Unknown ACP error")
                    # If the session was lost, reset so next turn creates a new
                    # one — and re-send the system prompt into that fresh session.
                    if "Session not found" in error_msg:
                        self._session_id = None
                        self._system_prompt_sent = False
                    yield ExecutorError(message=error_msg, retryable=True)
                    return
                # Successful completion.
                final_text = "".join(accumulated_text)
                if final_text:
                    yield TurnComplete(response=final_text)
                else:
                    yield TurnComplete(response="")
                return

            # Otherwise consume queued notifications.
            try:
                notification = await asyncio.wait_for(
                    self._queue.get(), timeout=min(remaining, 2.0)
                )
            except asyncio.TimeoutError:
                continue

            method = notification.get("method", "")
            params = notification.get("params", {})

            if method == _CLIENT_NOTIFICATION_SESSION_UPDATE:
                update = params.get("update", {})
                update_type = update.get("sessionUpdate", "")

                if update_type == _UPDATE_AGENT_MESSAGE_CHUNK:
                    content = update.get("content", {})
                    if isinstance(content, dict):
                        text = content.get("text", "")
                    else:
                        text = ""
                    if text:
                        accumulated_text.append(text)
                        yield TextChunk(text=text)

                elif update_type == _UPDATE_TOOL_CALL:
                    # Qwen is executing a built-in tool — surface it as info.
                    tool_title = update.get("title", "tool_call")
                    logger.debug("qwen tool_call: %s", tool_title)

                elif update_type == _UPDATE_TOOL_CALL_UPDATE:
                    # Status update on an in-progress tool call — skip.
                    pass

            elif notification.get("id") is not None and notification.get("method"):
                # Server-initiated request (e.g. session/request_permission):
                # permission goes through policy + elicitation; anything else
                # gets method-not-found. Blocks while the human decides.
                await self._respond_to_agent_request(notification)

            # Inbound message = progress; reset the idle deadline. Runs after the
            # human-approval block above so a slow approval doesn't time out.
            deadline = loop.time() + _PROMPT_TIMEOUT_SECONDS

    async def close_session(self, session_key: str) -> None:
        """Close a named session (no-op; sessions are per-process)."""

    async def close(self) -> None:
        """Terminate the qwen subprocess and clean up."""
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

        if self._proc:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()  # type: ignore[union-attr]
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    self._proc.kill()
            finally:
                self._proc = None
