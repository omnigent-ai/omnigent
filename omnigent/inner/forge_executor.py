"""ForgeCode CLI executor.

Drives the upstream ``forge`` CLI from https://forgecode.dev with one
``forge -p <prompt> -C <cwd>`` subprocess per Omnigent turn.

Verified in the local spike:

- ``-p/--prompt`` is the headless one-shot prompt mode.
- ``-C/--directory`` selects the working directory.
- ``--agent`` selects an agent.
- ``--conversation-id`` exists and accepts UUIDs, but semantic two-turn
  resume could not be verified without provider credentials.
- ``FORGE_CONFIG`` points to a config directory containing
  ``.forge.toml``.

Forge returned exit code 0 while printing credential/auth errors in the
spike, so this executor treats empty cleaned stdout as an error even when
the subprocess exits successfully.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.executor import (
    EnqueuedContent,
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

_FORGE_TURN_TIMEOUT_S = 600.0
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FORGE_ERROR_RE = re.compile(r"(?:^|\n)\s*[●!]\s+\[[^\n]*\]\s+ERROR:", re.IGNORECASE)


def _resolve_forge_binary() -> str:
    explicit = os.environ.get("HARNESS_FORGE_PATH", "").strip()
    if explicit:
        return explicit
    return "forge"


def _latest_user_text(messages: list[Message]) -> str:
    dropped_blocks = 0
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in ("text", "input_text") and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block_type in ("input_image", "input_file", "input_audio"):
                    dropped_blocks += 1
            if dropped_blocks:
                _logger.warning(
                    "forge harness: dropped %d non-text content block(s) on the latest user "
                    "message (multimodal input not yet wired)",
                    dropped_blocks,
                )
            return "".join(text_parts)
    return ""


def _strip_terminal_metadata(output: str) -> str:
    text = _ANSI_RE.sub("", output).replace("\r", "\n")
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "Ctrl+C to interrupt" in stripped:
            continue
        if _FORGE_ERROR_RE.search(stripped):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _split_model_override(model: str) -> tuple[str | None, str]:
    """Split ``provider:model`` or ``provider/model`` into config fields."""
    for separator in (":", "/"):
        provider, sep, model_id = model.partition(separator)
        if sep and provider and model_id:
            return provider, model_id
    return None, model


def _write_model_config(config_dir: Path, model: str) -> None:
    """Write the minimal Forge session model config.

    Forge docs verify ``[session].provider_id`` + ``model_id``. A bare
    model id has no verified provider, so keep that as model-only and let
    Forge merge/resolve provider state from its own defaults.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    provider_id, model_id = _split_model_override(model)
    lines = ["[session]", f'model_id = "{model_id}"']
    if provider_id:
        lines.insert(1, f'provider_id = "{provider_id}"')
    else:
        lines.insert(0, "# UNVERIFIED: bare HARNESS_FORGE_MODEL has no provider_id.")
    (config_dir / ".forge.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class ForgeExecutor(Executor):
    """Drive ``forge -p`` per Omnigent turn."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        binary_path: str | None = None,
        agent: str | None = None,
        config_dir: str | None = None,
    ) -> None:
        self._cwd = cwd
        self._os_env = os_env
        self._model = model
        self._binary_path = binary_path or _resolve_forge_binary()
        self._agent = agent
        self._config_dir = Path(config_dir) if config_dir else None
        self._temp_config_dir: Path | None = None
        self._active_process: asyncio.subprocess.Process | None = None
        self._warned_tools_without_bridge = False

    def handles_tools_internally(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return False

    def _build_argv(self, *, prompt_text: str) -> list[str]:
        argv = [self._binary_path, "-p", prompt_text]
        if self._cwd:
            argv.extend(["-C", self._cwd])
        if self._agent:
            argv.extend(["--agent", self._agent])
        return argv

    def _config_path(self) -> Path | None:
        if self._config_dir is not None:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            return self._config_dir
        if self._model:
            if self._temp_config_dir is None:
                self._temp_config_dir = Path(tempfile.mkdtemp(prefix="forge_config_"))
            return self._temp_config_dir
        return None

    def _build_spawn_env(self) -> dict[str, str]:
        env = os.environ.copy()
        config_dir = self._config_path()
        if config_dir is not None:
            if self._model:
                _write_model_config(config_dir, self._model)
            env["FORGE_CONFIG"] = str(config_dir)
        return env

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,  # noqa: ARG002 - Forge owns its agent instructions
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        if tools and not self._warned_tools_without_bridge:
            _logger.warning(
                "forge executor received %d declared tool(s) but Omnigent has no "
                "tool-injection bridge for ForgeCode yet. Forge will use its native tools only.",
                len(tools),
            )
            self._warned_tools_without_bridge = True

        if shutil.which(self._binary_path) is None and not Path(self._binary_path).exists():
            yield ExecutorError(
                message=(
                    f"forge harness: binary {self._binary_path!r} not found on PATH. "
                    "Install via `curl -fsSL https://forgecode.dev/cli | sh` or set "
                    "HARNESS_FORGE_PATH to its absolute location."
                ),
                retryable=False,
            )
            return

        prompt_text = _latest_user_text(messages)
        if not prompt_text:
            yield TurnComplete(response=None)
            return

        model = (config.model if config else None) or self._model
        old_model = self._model
        self._model = model
        try:
            argv = self._build_argv(prompt_text=prompt_text)
            env = self._build_spawn_env()
        finally:
            self._model = old_model

        process: asyncio.subprocess.Process | None = None
        try:
            process = await _create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd or None,
                env=env,
            )
            self._active_process = process
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=_FORGE_TURN_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            yield ExecutorError(
                message=f"forge subprocess timed out after {_FORGE_TURN_TIMEOUT_S}s",
                retryable=True,
            )
            return
        except FileNotFoundError:
            yield ExecutorError(
                message=(
                    f"forge harness: binary {self._binary_path!r} not found. "
                    "Install via `curl -fsSL https://forgecode.dev/cli | sh`."
                ),
                retryable=False,
            )
            return
        except OSError as exc:
            yield ExecutorError(message=f"failed to spawn forge subprocess: {exc}", retryable=True)
            return
        finally:
            self._active_process = None

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        cleaned = _strip_terminal_metadata(stdout)
        raw_error = stderr.strip() or stdout.strip()

        if process.returncode not in (None, 0):
            yield ExecutorError(
                message=f"forge exited with code {process.returncode}: {raw_error[:500]}",
                retryable=False,
            )
            return
        if not cleaned:
            yield ExecutorError(
                message=f"forge produced no assistant output. stderr/stdout: {raw_error[:500]}",
                retryable=False,
            )
            return

        yield TextChunk(text=cleaned)
        yield TurnComplete(response=cleaned)

    async def close_session(self, session_key: str) -> None:  # noqa: ARG002
        return None

    async def close(self) -> None:
        if self._temp_config_dir is not None:
            shutil.rmtree(self._temp_config_dir, ignore_errors=True)
            self._temp_config_dir = None
        await super().close()

    async def interrupt_session(self, session_key: str) -> bool:  # noqa: ARG002
        process = self._active_process
        if process is None or process.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
            return True
        return False

    async def enqueue_session_message(
        self,
        session_key: str,  # noqa: ARG002
        content: EnqueuedContent,  # noqa: ARG002
    ) -> bool:
        return False


async def _create_subprocess_exec(
    *args: Any,  # type: ignore[explicit-any]
    **kwargs: Any,  # type: ignore[explicit-any]
) -> asyncio.subprocess.Process:
    """Indirection point for tests."""
    return await asyncio.create_subprocess_exec(*args, **kwargs)
