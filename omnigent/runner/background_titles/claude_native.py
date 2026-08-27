"""Background title generation through isolated Claude Code print mode."""

from __future__ import annotations

from omnigent.runner.background_titles.service import (
    BackgroundTitleContext,
    background_title_prompt,
)
from omnigent.runner.isolated_inference import run_isolated_claude_print


async def generate_background_title(context: BackgroundTitleContext) -> str | None:
    """Generate a title with an isolated Claude Code print-mode process."""
    return await run_isolated_claude_print(
        background_title_prompt(context),
        spawn_env=context.spawn_env,
        cwd=context.cwd,
        model_override=context.model_override,
    )
