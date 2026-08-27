"""Background title generation through an isolated SDK harness process."""

from __future__ import annotations

from omnigent.runner.background_titles.service import (
    BackgroundTitleContext,
    BackgroundTitleHarnessError,
    background_title_prompt,
)
from omnigent.runner.isolated_inference import run_isolated_sdk_inference


async def generate_background_title(context: BackgroundTitleContext) -> str | None:
    """Generate a title with a synthetic tool-free SDK harness session."""
    return await run_isolated_sdk_inference(
        background_title_prompt(context),
        harness=context.harness,
        spawn_env=context.spawn_env,
        process_manager=context.process_manager,
        error_cls=BackgroundTitleHarnessError,
    )
