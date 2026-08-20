"""Background title generation through isolated Claude Code print mode."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from omnigent.runner.background_titles.service import (
    BACKGROUND_TITLE_INFERENCE_TIMEOUT_SECONDS,
    BACKGROUND_TITLE_INSTRUCTIONS,
    BackgroundTitleContext,
)

_logger = logging.getLogger("omnigent.runner.background_titles.claude_native")


async def generate_background_title(context: BackgroundTitleContext) -> str | None:
    """Generate a title with an isolated Claude Code print-mode process."""
    from omnigent.claude_launcher import resolve_claude_launch
    from omnigent.claude_native import (
        build_native_claude_terminal_env,
        resolve_native_claude_config,
    )
    from omnigent.runner.native.orchestration import _claude_terminal_env_unset

    try:
        claude_config = resolve_native_claude_config(spec=None)
    except Exception:  # noqa: BLE001 - match the native terminal's auth fallback
        _logger.warning(
            "background Claude Code title could not resolve provider config; "
            "falling back to Claude Code's native login",
            exc_info=True,
        )
        claude_config = None
    effective_model = (
        context.spawn_env.get("HARNESS_CLAUDE_SDK_MODEL")
        or context.model_override
        or (claude_config.model if claude_config is not None else None)
    )
    args = [
        "--safe-mode",
        "--system-prompt",
        BACKGROUND_TITLE_INSTRUCTIONS,
        "-p",
        f"<user_message>\n{context.prompt}\n</user_message>",
        "--tools",
        "",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--effort",
        "low",
    ]
    if effective_model:
        args.extend(("--model", effective_model))
    if claude_config is not None and claude_config.api_key_helper:
        args.extend(("--settings", json.dumps({"apiKeyHelper": claude_config.api_key_helper})))

    command, launch_args = resolve_claude_launch("claude", args)
    env = dict(os.environ)
    env.update(build_native_claude_terminal_env(claude_config))
    for name in _claude_terminal_env_unset(claude_config):
        env.pop(name, None)

    process = await asyncio.create_subprocess_exec(
        command,
        *launch_args,
        cwd=str(context.cwd) if context.cwd is not None else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(BACKGROUND_TITLE_INFERENCE_TIMEOUT_SECONDS):
            stdout, stderr = await process.communicate()
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise

    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        _logger.warning(
            "background Claude Code title failed returncode=%s detail=%s",
            process.returncode,
            detail[-1000:],
        )
        return None
    return stdout.decode(errors="replace").strip()
