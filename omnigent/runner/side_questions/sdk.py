"""Side-question answering through an isolated SDK harness process."""

from __future__ import annotations

from omnigent.runner.isolated_inference import run_isolated_sdk_inference
from omnigent.runner.side_questions.service import (
    SideQuestionContext,
    SideQuestionHarnessError,
    side_question_prompt,
)


async def generate_side_question_answer(context: SideQuestionContext) -> str | None:
    """Answer a side question with a synthetic tool-free SDK harness session."""
    return await run_isolated_sdk_inference(
        side_question_prompt(context),
        harness=context.harness,
        spawn_env=context.spawn_env,
        process_manager=context.process_manager,
        error_cls=SideQuestionHarnessError,
    )
