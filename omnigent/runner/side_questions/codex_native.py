"""Side-question answering through isolated native Codex exec."""

from __future__ import annotations

from omnigent.runner.isolated_inference import run_isolated_codex_exec
from omnigent.runner.side_questions.service import (
    SideQuestionContext,
    side_question_prompt,
)


async def generate_side_question_answer(context: SideQuestionContext) -> str | None:
    """Answer a side question with an isolated native Codex exec process."""
    return await run_isolated_codex_exec(
        side_question_prompt(context),
        model_override=context.model_override,
        session_spec=context.session_spec,
        temp_dir_prefix="omnigent-codex-side-question-",
    )
