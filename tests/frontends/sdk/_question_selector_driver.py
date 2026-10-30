"""
TerminalHost driver for the inline question selector PTY test
(:mod:`tests.frontends.sdk.test_question_selector`).

Boots a :class:`TerminalHost` and asks a fixed sequence of questions through
:meth:`TerminalHost.ask_questions`, printing each result as one
``ANSWER <n> <json>`` line so the test can assert what real keystrokes chose.
Lines submitted as chat are printed as ``CHAT <json>``; the selector must never
produce any.

Run directly with ``python _question_selector_driver.py <history-file>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from typing import Any

from omnigent_ui_sdk import TerminalHost
from omnigent_ui_sdk.terminal import QuestionSpec
from rich.text import Text

QUESTIONS: list[list[QuestionSpec]] = [
    [
        QuestionSpec(
            key="Env?",
            question="Which environment?",
            header="Environment",
            options=(("dev", "Development"), ("staging", "Pre-production"), ("prod", "")),
        )
    ],
    [
        QuestionSpec(
            key="regions",
            question="Which regions?",
            options=(("us-east", ""), ("eu-west", ""), ("ap-south", "")),
            multi_select=True,
        )
    ],
    [QuestionSpec(key="Name?", question="Which name?", options=(("alpha", ""), ("beta", "")))],
    [QuestionSpec(key="0", question="Release notes?", text_default="draft notes")],
    [QuestionSpec(key="Skip?", question="Dismiss me", options=(("a", ""),))],
    [QuestionSpec(key="Comment?", question="Comment me", options=(("a", ""),))],
    [QuestionSpec(key="Discard?", question="Discard me", options=(("a", ""),))],
]


async def _main(history_file: str) -> None:
    """Run the host and ask :data:`QUESTIONS` one after another."""
    host = TerminalHost(model_name="test", history_file=history_file)

    async def _chat(text: str, files: list[Any]) -> None:
        host.output(Text(f"CHAT {json.dumps(text)}"))

    async def _ask_all() -> None:
        await asyncio.sleep(0.5)  # let the prompt draw first
        for number, questions in enumerate(QUESTIONS, start=1):
            answers = await host.ask_questions(questions)
            host.output(Text(f"ANSWER {number} {json.dumps(answers, sort_keys=True)}"))

    async with host:
        asker = asyncio.create_task(_ask_all())
        try:
            await host.run(_chat)
        finally:
            asker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asker


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1]))
