"""Runner-owned background session title generation."""

from omnigent.runner.background_titles.service import (
    BackgroundTitleContext,
    BackgroundTitleHarnessError,
    background_title_model,
    generate_background_title,
    generator_spec_for_harness,
)

__all__ = [
    "BackgroundTitleContext",
    "BackgroundTitleHarnessError",
    "background_title_model",
    "generate_background_title",
    "generator_spec_for_harness",
]
