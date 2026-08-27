"""Background title generation through isolated native Codex exec."""

from __future__ import annotations

from omnigent.runner.background_titles.service import (
    BackgroundTitleContext,
    background_title_prompt,
)
from omnigent.runner.isolated_inference import run_isolated_codex_exec


async def generate_background_title(context: BackgroundTitleContext) -> str | None:
    """Generate a title with an isolated native Codex exec process."""
    return await run_isolated_codex_exec(
        background_title_prompt(context),
        model_override=context.model_override,
        # Thread the spec so a title exec honors spec-level auth too (#2744).
        session_spec=context.session_spec,
        temp_dir_prefix="omnigent-codex-title-",
    )
