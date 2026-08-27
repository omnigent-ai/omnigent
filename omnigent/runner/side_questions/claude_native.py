"""Side-question answering through isolated Claude Code print mode."""

from __future__ import annotations

from omnigent.runner.isolated_inference import run_isolated_claude_print
from omnigent.runner.side_questions.service import (
    SideQuestionContext,
    side_question_prompt,
)


async def generate_side_question_answer(context: SideQuestionContext) -> str | None:
    """Answer a side question with an isolated Claude Code print-mode process."""
    return await run_isolated_claude_print(
        side_question_prompt(context),
        spawn_env=context.spawn_env,
        cwd=context.cwd,
        model_override=context.model_override,
    )
