"""AiderExecutor: run agents through Aider's one-shot CLI mode.

`Aider <https://aider.chat>`_ is a terminal AI pair-programmer. Unlike the
native qwen/codex/claude harnesses it exposes no streaming agent protocol, and
its Python ``Coder`` API is explicitly *"not officially supported or documented
and could change without backwards compatibility"*. So this executor drives the
CLI in **one-shot** mode, once per turn, in the session workspace::

    aider --message "<prompt>" --yes-always --no-stream --no-pretty \\
          --no-auto-commits --no-dirty-commits --no-check-update \\
          --analytics-disable [--model <model>] [--restore-chat-history]

Aider runs its own agent loop (file edits, shell, repo map, tool use)
internally and prints its reply to stdout; this executor streams stdout lines
as :class:`TextChunk` events and emits a final :class:`TurnComplete`. Because
Aider routes models through LiteLLM, it honours ``OPENAI_BASE_URL`` /
``OPENAI_API_KEY``, so Omnigent's provider/gateway routing works unchanged
(mirroring :class:`omnigent.inner.qwen_executor.QwenExecutor`). Otherwise auth
is bring-your-own provider key via the ambient environment.

Multi-turn continuity: each turn is a fresh subprocess, so the system prompt is
folded into the first turn only and subsequent turns pass
``--restore-chat-history`` to reload Aider's persisted workspace chat history.

Requirements:
    The ``aider`` CLI must be installed (``python -m pip install aider-chat``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
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

# Kill the aider subprocess if it produces no output for this long. Bounds a
# wedged turn without capping a legitimately long edit (reset implicitly by
# reading the next stdout line).
_TURN_IDLE_TIMEOUT_SECONDS = 600.0

# 16 MiB per-line cap for the stdout StreamReader so a long, unbroken aider
# line (e.g. a big diff) doesn't hit the default 64 KiB limit.
_STREAM_LIMIT = 16 * 1024 * 1024


def _inline_text_file_data(file_data: Any) -> str:  # type: ignore[explicit-any]
    """Decode a text ``input_file`` ``file_data`` data URI into inline text.

    Mirrors :func:`omnigent.inner.qwen_executor._inline_text_file_data`:
    ``input_file`` blocks may carry a ``data:<mime>;base64,<payload>`` URI. Text
    files are decoded so the model sees their content; binary files (PDF,
    images) can't be inlined as text and return ``""``. A bare, non-data-URI
    string is treated as already-inline text.

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


class AiderExecutor(Executor):
    """Executor that drives Aider via its one-shot ``--message`` CLI mode."""

    def __init__(
        self,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        aider_path: str | None = None,
        gateway_base_url: str | None = None,
        gateway_auth_command: str | None = None,
    ) -> None:
        """Initialize the Aider executor.

        :param cwd: Working directory the aider subprocess runs in. When
            ``None``, inherits the caller's cwd.
        :param os_env: Environment / sandbox spec. When its ``sandbox`` is not
            ``"none"``, the aider process tree is wrapped in the platform
            sandbox (bwrap/seatbelt) at spawn — see :meth:`_sandbox_launch_path`.
        :param model: Model identifier passed via ``--model`` (e.g.
            ``"claude-3-5-sonnet"``, ``"gpt-4o"``). ``None`` uses aider's default.
        :param aider_path: Absolute path to the ``aider`` CLI binary. Defaults to
            ``"aider"`` (PATH lookup).
        :param gateway_base_url: OpenAI-compatible base URL of an Omnigent
            provider/gateway (from ``HARNESS_AIDER_GATEWAY_BASE_URL``). When set
            with *gateway_auth_command*, the executor exports ``OPENAI_BASE_URL``
            / ``OPENAI_API_KEY`` / ``OPENAI_MODEL`` into the aider subprocess so
            the spec's ``auth:`` / ``providers:`` routing takes effect instead of
            aider's ambient auth.
        :param gateway_auth_command: Shell command that prints a bearer token to
            stdout (from ``HARNESS_AIDER_GATEWAY_AUTH_COMMAND``); run once per
            turn to snapshot ``OPENAI_API_KEY``.
        """
        self._cwd = cwd or os.getcwd()
        self._os_env = os_env
        self._model = model
        self._aider_path = aider_path or "aider"
        self._gateway_base_url = gateway_base_url
        self._gateway_auth_command = gateway_auth_command

        # Aider has no system-prompt flag, so the persona is folded into the
        # first turn's message only; later turns reuse it via restored history.
        self._system_prompt_sent = False
        # After the first turn has run, aider has persisted chat history to the
        # workspace, so subsequent turns pass --restore-chat-history.
        self._has_history = False

    # ------------------------------------------------------------------
    # Executor capability flags
    # ------------------------------------------------------------------

    def supports_streaming(self) -> bool:
        """Aider output is streamed back line-by-line as it arrives."""
        return True

    def supports_tool_calling(self) -> bool:
        """Aider does not surface tool calls to Omnigent (it runs its own loop)."""
        return False

    def handles_tools_internally(self) -> bool:
        """Aider executes file edits / shell / tools inside its own agent loop."""
        return True

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _text_from_blocks(blocks: list[Any]) -> str:  # type: ignore[explicit-any]
        """Fold a Responses-API content-block list into a single prompt string.

        Mirrors :meth:`omnigent.inner.qwen_executor.QwenExecutor._text_from_blocks`.
        The harness adapter passes a content **list** whenever a message carries
        a non-text block (e.g. a file attachment). Aider's ``--message`` is
        text-only, so each block is folded into text:

        - ``input_text`` / ``output_text`` / ``text`` → the text verbatim.
        - ``input_file`` → the file's inlined content, fenced with a labeled
          header/footer when the runner resolved it into a text ``file_data``
          data URI; otherwise a ``[attached file: <name>]`` marker.
        - ``input_image`` → a ``[attached image: <name>]`` marker (aider's
          one-shot CLI can't take inline image blocks).

        :param blocks: The message ``content`` list.
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
                    parts.append(
                        f"--- attached file: {name} ---\n{inlined}\n--- end of {name} ---"
                    )
                else:
                    parts.append(f"[attached file: {name}]")
            elif btype == "input_image":
                name = block.get("filename") or block.get("file_id")
                parts.append(f"[attached image: {name}]" if name else "[attached image]")
        return "\n".join(parts)

    def _latest_user_text(self, messages: list[Message]) -> str:
        """Extract prompt text from the most recent user message."""
        for msg in reversed(messages):
            role = msg.get("role", "") if isinstance(msg, dict) else ""
            if role != "user":
                continue
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return self._text_from_blocks(content)
            return ""
        return ""

    def _build_argv(self, launch_path: str, prompt: str, *, restore: bool) -> list[str]:
        """Assemble the one-shot aider argv for a turn.

        :param launch_path: argv[0] — the aider binary or a sandbox launcher.
        :param prompt: The single user message to send.
        :param restore: When ``True``, pass ``--restore-chat-history`` so aider
            reloads the workspace chat history from earlier turns.
        :returns: The full argv list.
        """
        argv = [
            launch_path,
            "--message",
            prompt,
            "--yes-always",  # auto-confirm every prompt (non-interactive)
            "--no-stream",  # return the full reply at once
            "--no-pretty",  # plain output for clean capture
            "--no-auto-commits",  # Omnigent owns workspace change-tracking
            "--no-dirty-commits",
            "--no-check-update",  # no network version check / upgrade prompt
            "--analytics-disable",
        ]
        if self._model:
            argv += ["--model", self._model]
        if restore:
            argv.append("--restore-chat-history")
        return argv

    # ------------------------------------------------------------------
    # Environment / sandbox plumbing (mirrors QwenExecutor)
    # ------------------------------------------------------------------

    async def _resolve_gateway_env(self) -> dict[str, str]:
        """Build the OpenAI-compatible env aider reads from the gateway config.

        When a provider/gateway is wired (base URL + a bearer-token command),
        run the command once to snapshot a token and return ``OPENAI_BASE_URL``
        / ``OPENAI_API_KEY`` / ``OPENAI_MODEL``. Returns an empty dict when no
        gateway is configured (aider's ambient auth path). Mirrors
        :meth:`QwenExecutor._resolve_gateway_env`.

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
                f"aider gateway auth command failed (exit {proc.returncode}): {detail}"
            )
        token = out.decode("utf-8", errors="replace").strip()
        if not token:
            raise RuntimeError("aider gateway auth command produced an empty token")
        env: dict[str, str] = {
            "OPENAI_BASE_URL": self._gateway_base_url,
            "OPENAI_API_KEY": token,
        }
        if self._model:
            env["OPENAI_MODEL"] = self._model
        return env

    def _sandbox_launch_path(self, spawn_env_names: Sequence[str]) -> str:
        """Return the path to spawn for aider — sandbox launcher or bare binary.

        Mirrors :meth:`QwenExecutor._sandbox_launch_path`. When ``os_env.sandbox``
        requests confinement, wraps the aider binary in the platform sandbox so
        its file/shell tools run confined to the spec's read/write roots. Falls
        back to the bare binary (never blocks startup) when no sandbox is
        requested, the resolved policy is inactive, or the backend is
        unavailable. Aider is a pip CLI, so the Python interpreter prefixes are
        added as read roots and ``~/.aider`` + ``/tmp`` as write roots.

        :param spawn_env_names: Env-var names set on the subprocess ``env=``;
            baked into the policy so the launcher prunes anything else.
        :returns: The path to pass as argv[0] to ``create_subprocess_exec``.
        """
        os_env = self._os_env
        if os_env is None:
            return self._aider_path
        sandbox_spec = os_env.sandbox or OSEnvSandboxSpec()
        if sandbox_spec.type == "none":
            return self._aider_path
        try:
            from .sandbox import (
                create_exec_launcher,
                resolve_sandbox,
                with_additional_read_roots,
                with_additional_write_roots,
                with_spawn_env_allowlist,
            )

            cwd = Path(self._cwd or os.getcwd()).resolve(strict=False)
            sandbox = resolve_sandbox(os_env, cwd)
            if not sandbox.active:
                return self._aider_path
            aider_real = shutil.which(self._aider_path) or self._aider_path
            # aider is a pip CLI: it must read the Python interpreter + its
            # site-packages and the binary's own tree, and write its config /
            # history dir (~/.aider) and /tmp, or it can't start inside the jail.
            read_roots = [
                Path(sys.base_prefix),
                Path(sys.prefix),
                Path(aider_real).resolve().parent.parent,
            ]
            sandbox = with_additional_read_roots(sandbox, read_roots)
            sandbox = with_additional_write_roots(sandbox, [Path.home() / ".aider", Path("/tmp")])
            sandbox = with_spawn_env_allowlist(sandbox, spawn_env_names)
            return create_exec_launcher(aider_real, sandbox)
        except (OSError, ImportError, NotImplementedError) as exc:
            logger.warning("Could not apply sandbox for aider; running unsandboxed: %s", exc)
            return self._aider_path

    # ------------------------------------------------------------------
    # Executor interface
    # ------------------------------------------------------------------

    @staticmethod
    async def _drain(stream: asyncio.StreamReader | None, sink: list[str]) -> None:
        """Continuously drain a subprocess pipe into *sink*.

        Draining stderr concurrently with the stdout read prevents a chatty
        aider from filling the OS pipe buffer (~64 KiB) and wedging mid-turn.
        """
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            sink.append(line.decode("utf-8", errors="replace"))

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[Any],  # type: ignore[explicit-any]  # noqa: ARG002 — aider runs its own tool loop; required by the Executor interface
        system_prompt: str,
        config: ExecutorConfig | None = None,  # noqa: ARG002 — unused; required by the Executor interface
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn by invoking aider once in ``--message`` mode.

        :param messages: Conversation history; only the latest user message is
            sent (aider carries prior context via its restored workspace history).
        :param tools: Tool specs (ignored — aider uses its own tool registry).
        :param system_prompt: Agent instructions, folded into the first turn.
        :param config: Optional executor config (unused).
        """
        aider_bin = shutil.which(self._aider_path)
        if aider_bin is None and not os.path.isfile(self._aider_path):
            yield ExecutorError(
                message=(
                    f"aider CLI not found ({self._aider_path!r}). "
                    "Install it with `python -m pip install aider-chat`."
                ),
                retryable=False,
            )
            return

        user_text = self._latest_user_text(messages)
        if not self._system_prompt_sent and system_prompt:
            user_text = f"{system_prompt}\n\n{user_text}" if user_text else system_prompt
            self._system_prompt_sent = True
        if not user_text.strip():
            yield TurnComplete(response="")
            return

        try:
            env = os.environ.copy()
            env.update(await self._resolve_gateway_env())
        except Exception as exc:  # noqa: BLE001 — surface gateway failures as a clean error
            yield ExecutorError(message=str(exc), retryable=False)
            return

        launch_path = self._sandbox_launch_path(tuple(env.keys()))
        argv = self._build_argv(launch_path, user_text, restore=self._has_history)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._cwd,
                limit=_STREAM_LIMIT,
            )
        except (OSError, ValueError) as exc:
            yield ExecutorError(message=f"failed to launch aider: {exc}", retryable=False)
            return

        stderr_chunks: list[str] = []
        stderr_task = asyncio.create_task(self._drain(proc.stderr, stderr_chunks))
        accumulated: list[str] = []
        assert proc.stdout is not None
        timed_out = False
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=_TURN_IDLE_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    proc.kill()
                    break
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace")
                accumulated.append(text)
                if text.strip():
                    yield TextChunk(text=text)
        finally:
            with contextlib.suppress(Exception):
                await stderr_task

        await proc.wait()
        # aider has now written workspace chat history; later turns restore it.
        self._has_history = True

        if timed_out:
            secs = int(_TURN_IDLE_TIMEOUT_SECONDS)
            yield ExecutorError(
                message=f"aider produced no output for {secs}s; turn aborted",
                retryable=True,
            )
            return
        if proc.returncode != 0:
            detail = ("".join(stderr_chunks).strip() or "".join(accumulated).strip())[:800]
            yield ExecutorError(
                message=f"aider exited with code {proc.returncode}: {detail}",
                retryable=False,
            )
            return
        yield TurnComplete(response="".join(accumulated).strip(), usage=None)

    async def close_session(self, session_key: str) -> None:
        """No-op: each turn is its own short-lived subprocess."""

    async def close(self) -> None:
        """No-op: the executor holds no long-lived subprocess."""
