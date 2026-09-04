"""Schema-only ``sys_ask_user_question`` builtin tool class.

Lets any sub-agent harness pause its turn and present the human operator
with 1-4 structured multiple-choice questions, then resume with the
human's structured answers. Modeled closely on Claude Code's own
``AskUserQuestionInput`` / ``AskUserQuestionOutput`` contract (see
``sdk-tools.d.ts`` in the ``@anthropic-ai/claude-code`` package), with
one deliberate addition: a boolean ``recommended`` flag on individual
options, marking the suggested default — a field that exists in neither
Claude Code's own tool nor this platform's existing
``ElicitationRequestParams`` / ``ElicitationResult`` schemas.

This class is the **tool surface only** — ``name()``, ``description()``
and ``get_schema()``. It deliberately does NOT implement ``invoke()``:
execution needs the session's elicitation machinery (publish a
``response.elicitation_request`` event, park until a human answers),
which lives on the server and needs the runner's ``server_client`` —
``ToolContext`` carries neither. Execution lives in the runner dispatch
layer (``omnigent/runner/tool_dispatch.py`` — the
``_ASK_USER_QUESTION_TOOLS`` branch of ``execute_tool()``), which POSTs
to ``/v1/sessions/{id}/ask_user_question`` (see
``omnigent/server/routes/_ask_user_question.py`` for the request/response
shaping and ``omnigent/server/routes/sessions/routes_elicitations.py``
for the route itself). Any call that reaches ``Tool.invoke`` here means
the tool was misrouted to the server-side path — the base class raises
``NotImplementedError`` loudly in that case.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysAskUserQuestionTool(Tool):
    """Pause the turn and ask the human 1-4 structured questions (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_ask_user_question"``."""
        return "sys_ask_user_question"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Pause your turn and ask the human operator 1-4 structured "
            "multiple-choice questions, then resume with their answers. "
            "Each question offers 2-4 options with a label, a description "
            "of what choosing it means, and an optional longer preview "
            "shown when the option is focused. Mark exactly one option per "
            "question 'recommended' when you have a suggested default — "
            "the human sees it called out and pre-selected, but can pick "
            "differently. Set 'multiSelect' when a question's options "
            "aren't mutually exclusive. The human may also type a free-text "
            "answer instead of picking an option. Works across every "
            "harness this platform supports — use it whenever you'd "
            "otherwise have to guess at an ambiguous choice."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        option_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": (
                        "The display text for this option. Concise (1-5 words), "
                        "clearly describing the choice."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Explanation of what this option means or what happens if chosen."
                    ),
                },
                "preview": {
                    "type": "string",
                    "description": (
                        "Optional longer snippet (a mockup, code, or a fuller "
                        "comparison) shown when this option is focused."
                    ),
                },
                "recommended": {
                    "type": "boolean",
                    "description": (
                        "Marks this as the suggested default. Set true on at most "
                        "one option per question."
                    ),
                },
            },
            "required": ["label", "description"],
            "additionalProperties": False,
        }
        question_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": ("The complete question to ask, ending with a question mark."),
                },
                "header": {
                    "type": "string",
                    "description": "Very short label displayed as a chip/tag (max 12 chars).",
                },
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 4,
                    "items": option_schema,
                    "description": (
                        "2-4 distinct, mutually exclusive choices (unless multiSelect)."
                    ),
                },
                "multiSelect": {
                    "type": "boolean",
                    "description": "True to let the human select multiple options.",
                },
            },
            "required": ["question", "header", "options", "multiSelect"],
            "additionalProperties": False,
        }
        return {
            "type": "function",
            "function": {
                "name": SysAskUserQuestionTool.name(),
                "description": SysAskUserQuestionTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "questions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": question_schema,
                            "description": "1-4 questions to ask the human, in order.",
                        },
                    },
                    "required": ["questions"],
                    "additionalProperties": False,
                },
            },
        }
