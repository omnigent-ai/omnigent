"""Roll the project orchestrator over to a fresh session seeded with a handoff."""

from typing import Any

from omnigent.tools.base import Tool


class SysProjectOrchestratorRolloverTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_project_orchestrator_rollover"

    @classmethod
    def description(cls) -> str:
        return (
            "Hand your role as project orchestrator to a fresh session with a small context. "
            "Only the project's current orchestrator can call it. After your turn ends, the "
            "server archives this session as read-only history and starts a new orchestrator "
            "seeded with a handoff: board, active workers, human requests, recent worker "
            "handoffs and owner messages, plus your notes. Messages to this session's ID are "
            "forwarded to the successor. Put anything the board can't show in notes, such as "
            "branches awaiting landing and pending decisions. Use it when the user asks or "
            "your context is long."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "notes": {
                            "type": "string",
                            "maxLength": 8000,
                            "description": "Handoff notes for your successor.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }
