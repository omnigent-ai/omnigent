"""Background title generation through an isolated native ``opencode run``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import tempfile
from pathlib import Path

from omnigent.debug_logging import runner_primary_session_id
from omnigent.runner.background_titles.service import (
    BACKGROUND_TITLE_INFERENCE_TIMEOUT_SECONDS,
    BackgroundTitleContext,
    build_background_title_instructions,
)

_logger = logging.getLogger("omnigent.runner.background_titles.opencode_native")

#: Title runs answer from the prompt alone: every tool OpenCode could reach
#: for is denied, so a prompt-injected first message cannot act.
_TITLE_OPENCODE_CONFIG = {
    "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
}


def _title_from_json_events(stdout: str) -> str | None:
    """Join the assistant text parts of ``opencode run --format json`` output."""
    parts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        text = part.get("text") if isinstance(part, dict) else event.get("text")
        if isinstance(text, str):
            parts.append(text)
    title = "".join(parts).strip()
    return title or None


async def generate_background_title(context: BackgroundTitleContext) -> str | None:
    """Generate a title with an isolated, tool-free native OpenCode run.

    Mirrors the session's own launch: per-run XDG dirs (never the user's
    global OpenCode config or project), the user's ``auth.json`` seeded in so
    the session's provider and model work, and the session's model.
    """
    from omnigent.harnesses.opencode_native.app_server import (
        filtered_server_env,
        find_opencode_cli,
    )
    from omnigent.harnesses.opencode_native.bridge import seed_opencode_auth
    from omnigent.inner import _proc
    from omnigent.runner.native.orchestration import _opencode_native_model_from_spec

    model = (
        context.title_model
        or context.model_override
        or _opencode_native_model_from_spec(context.session_spec)
    )
    opencode_path = find_opencode_cli()
    with tempfile.TemporaryDirectory(prefix="omnigent-opencode-title-") as temp_dir:
        temp_root = Path(temp_dir)
        bridge_dir = temp_root / "bridge"
        title_workdir = temp_root / "workspace"
        bridge_dir.mkdir(mode=0o700)
        title_workdir.mkdir()
        seed_opencode_auth(bridge_dir)
        env = filtered_server_env(
            bridge_dir=bridge_dir,
            auth_secret=secrets.token_urlsafe(16),
            extra_env=context.spawn_env,
        )
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(_TITLE_OPENCODE_CONFIG)

        instructions = build_background_title_instructions(context.additional_instructions)
        args = ["run", "--pure", "--format", "json", "--dir", str(title_workdir)]
        if model:
            args.extend(("--model", model))
        args.append(
            f"{instructions} Do not use tools.\n<user_message>\n{context.prompt}\n</user_message>"
        )
        process = await asyncio.create_subprocess_exec(
            opencode_path,
            *args,
            cwd=str(title_workdir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_proc.spawn_kwargs(),
        )
        try:
            async with asyncio.timeout(BACKGROUND_TITLE_INFERENCE_TIMEOUT_SECONDS):
                stdout, stderr = await process.communicate()
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                _proc.kill_tree(process)
            with contextlib.suppress(Exception):
                await process.wait()
            raise

    if process.returncode != 0:
        detail = (stderr or stdout).decode(errors="replace").strip()
        _logger.warning(
            "background native OpenCode title failed returncode=%s detail=%s",
            process.returncode,
            detail[-1000:],
            extra={"session_id": runner_primary_session_id()},
        )
        return None
    return _title_from_json_events(stdout.decode(errors="replace"))
