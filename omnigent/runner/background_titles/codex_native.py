"""Background title generation through isolated native Codex exec."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from pathlib import Path

from omnigent.debug_logging import runner_primary_session_id
from omnigent.runner.background_titles.service import (
    BACKGROUND_TITLE_INFERENCE_TIMEOUT_SECONDS,
    BackgroundTitleContext,
    build_background_title_instructions,
)

_logger = logging.getLogger("omnigent.runner.background_titles.codex_native")


async def generate_background_title(context: BackgroundTitleContext) -> str | None:
    """Generate a title with an isolated native Codex exec process."""
    from omnigent.codex_native_app_server import (
        build_codex_native_server,
        resolve_native_codex_launch,
    )
    from omnigent.inner import _proc
    from omnigent.inner.codex_executor import (
        _codex_home_config_source_from_env,
        _populate_codex_home_config,
        materialize_codex_provider_config,
    )
    from omnigent.runner.native.orchestration import _codex_native_model_from_spec

    model = (
        context.title_model
        or context.model_override
        or _codex_native_model_from_spec(context.session_spec)
    )
    # Thread the spec so a title exec honors spec-level auth too (#2744).
    launch = resolve_native_codex_launch(model=model, spec=context.session_spec)
    with tempfile.TemporaryDirectory(prefix="omnigent-codex-title-") as temp_dir:
        temp_root = Path(temp_dir)
        codex_home = temp_root / "codex-home"
        title_workdir = temp_root / "workspace"
        codex_home.mkdir(mode=0o700)
        title_workdir.mkdir()
        _populate_codex_home_config(
            codex_home,
            _codex_home_config_source_from_env(),
            minimal_config=True,
        )
        native_server = build_codex_native_server(
            socket_path=temp_root / "unused.sock",
            codex_home=codex_home,
            cwd=title_workdir,
            model=launch.model,
            profile=launch.profile,
            bridge_dir=temp_root / "bridge",
            extra_config_overrides=launch.config_overrides,
        )
        native_server.config_overrides = materialize_codex_provider_config(
            codex_home,
            native_server.config_overrides,
        )
        output_path = temp_root / "title.txt"
        args = [
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-last-message",
            str(output_path),
            "--color",
            "never",
        ]
        for override in native_server.config_overrides:
            args.extend(("--config", override))
        for override in (
            'approval_policy="never"',
            "features.unified_exec=false",
            "features.shell_tool=false",
            'web_search="disabled"',
            "features.apps=false",
            "features.browser_use=false",
            "features.computer_use=false",
            "features.image_generation=false",
            "features.multi_agent=false",
            "features.plugins=false",
            "features.tool_search=false",
        ):
            args.extend(("--config", override))
        if launch.model:
            args.extend(("--model", launch.model))
        instructions = build_background_title_instructions(context.additional_instructions)
        args.append(
            f"{instructions} Do not use tools.\n<user_message>\n{context.prompt}\n</user_message>"
        )
        env = {**native_server.env, "CODEX_HOME": str(codex_home)}
        process = await asyncio.create_subprocess_exec(
            native_server.codex_path,
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
                _stdout, stderr = await process.communicate()
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                _proc.kill_tree(process)
            with contextlib.suppress(Exception):
                await process.wait()
            raise

        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            _logger.warning(
                "background native Codex title failed returncode=%s detail=%s",
                process.returncode,
                detail[-1000:],
                extra={"session_id": runner_primary_session_id()},
            )
            return None
        if not output_path.is_file():
            return None
        return output_path.read_text(errors="replace").strip()
