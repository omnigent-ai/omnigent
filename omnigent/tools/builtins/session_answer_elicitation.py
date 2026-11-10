"""Tool letting a session answer a pending elicitation of its own direct child."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionAnswerElicitationTool(Tool):
    """
    Resolve a pending elicitation (approval prompt) raised by a direct child.

    Schema-only: like ``sys_session_send`` and ``sys_session_get_info``,
    execution is runner-dispatched (``omnigent/runner/tool_dispatch.py``)
    because the runner proxies the Omnigent server's REST API rather than
    sharing an in-process ``ConversationStore``. The runner-side handler
    reuses the exact server endpoint
    (``POST /v1/sessions/{id}/elicitations/{eid}/resolve``) the web UI's
    ApprovalCard already calls, so every harness that supports UI answering
    — including antigravity-native's ``agy_permission`` prompts — resolves
    identically regardless of who answers.

    **Direct children only**: the target session's ``parent_session_id``
    must equal the calling session's own id. A grandchild, sibling, or the
    caller's own elicitation is refused with ``session_out_of_tree``, even
    though the caller may otherwise have read/edit access to it (orchestrator
    sessions routinely do, since they own the whole spawn tree) — answering a
    prompt is strictly scoped to the direct parent/child edge.

    No "always allow" / persist option: only a one-shot ``allow``/``deny``
    verdict for the named ``elicitation_id``.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_answer_elicitation"``."""
        return "sys_session_answer_elicitation"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Answer a pending elicitation (approval/permission prompt) raised by one "
            "of your DIRECT child sessions — e.g. an antigravity-native child's "
            "agy_permission prompt that would otherwise stall waiting for a human to "
            "click approve. List a child's outstanding prompts via "
            "sys_session_get_info's pending_elicitations. Only the direct parent of "
            "the session that raised the prompt may answer it; a grandchild, sibling, "
            "or your own elicitation returns session_out_of_tree. Returns "
            "session_not_found for an unknown session_id, and "
            "elicitation_not_pending when elicitation_id is unknown or already "
            "resolved. This is a one-shot allow/deny verdict — it never sets an "
            "'always allow' persistent policy."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The direct child session's conversation_id that "
                                "raised the prompt. Get this from sys_session_list "
                                "or a prior sys_session_send handle."
                            ),
                        },
                        "elicitation_id": {
                            "type": "string",
                            "description": (
                                "The elicitation id from that child's "
                                "sys_session_get_info pending_elicitations list."
                            ),
                        },
                        "decision": {
                            "type": "string",
                            "enum": ["allow", "deny"],
                            "description": (
                                "'allow' approves the prompt; 'deny' refuses it. "
                                "Always a one-shot verdict — never persisted."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional free-text reason, for audit context.",
                            "maxLength": 2000,
                        },
                    },
                    "required": ["session_id", "elicitation_id", "decision"],
                    "additionalProperties": False,
                },
            },
        }
