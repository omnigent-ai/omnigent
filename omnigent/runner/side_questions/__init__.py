"""Runner-owned ``/btw`` side-question answering."""

from omnigent.runner.side_questions.service import (
    SIDE_QUESTION_MAX_EXCERPT_CHARS,
    SIDE_QUESTION_MAX_QUESTION_CHARS,
    SideQuestionContext,
    SideQuestionHarnessError,
    generate_side_question_answer,
    generator_spec_for_harness,
)

__all__ = [
    "SIDE_QUESTION_MAX_EXCERPT_CHARS",
    "SIDE_QUESTION_MAX_QUESTION_CHARS",
    "SideQuestionContext",
    "SideQuestionHarnessError",
    "generate_side_question_answer",
    "generator_spec_for_harness",
]
