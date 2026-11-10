"""Schema-only tools for listing projects and filing the owner's sessions into them."""

from typing import Any

from omnigent.tools.base import Tool

MAX_PROJECT_NAME_LENGTH = 100
MAX_SESSIONS_PER_MOVE = 100
MAX_LISTED_SESSIONS = 200


class SysProjectListTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_project_list"

    @classmethod
    def description(cls) -> str:
        return (
            "List the user's projects, oldest first, and their own sessions with each "
            "session's current project, most recently updated first. Both lists are "
            "paged by limit: pass next_cursor as after for more sessions and "
            "projects_next_cursor as projects_after for more projects. A session whose "
            "project is null is unfiled; this_session marks the session calling the "
            'tool. Filter sessions by project name, or pass project "" for unfiled '
            "sessions only. A session with moves_with is in a family and moves with "
            "that session. Use sys_session_move to file sessions into a project."
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
                        "project": {
                            "type": "string",
                            "maxLength": MAX_PROJECT_NAME_LENGTH,
                            "description": (
                                'Only list sessions in this project; "" lists unfiled '
                                "sessions. Omit to list every session."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_LISTED_SESSIONS,
                            "description": (
                                "Maximum sessions, and separately projects, to return "
                                "(default 50)."
                            ),
                        },
                        "after": {
                            "type": "string",
                            "description": "next_cursor from a previous call, for more sessions.",
                        },
                        "projects_after": {
                            "type": "string",
                            "description": (
                                "projects_next_cursor from a previous call, for more projects."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }


class SysSessionMoveTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_session_move"

    @classmethod
    def description(cls) -> str:
        return (
            "File one or more of the user's own sessions into a project by name, the "
            "same as moving them in the sidebar. A project that does not exist yet is "
            'created. Pass project "" to remove the sessions from their project. Moving '
            "only changes the folder: running sessions, including this one, keep "
            "working. Sub-agent sessions follow their parent, and a family moves "
            "together through the session its members list as moves_with. Each "
            "session gets its own result; a refused session does not stop the others."
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
                        "session_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": MAX_SESSIONS_PER_MOVE,
                            "uniqueItems": True,
                            "description": "IDs of the sessions to move.",
                        },
                        "project": {
                            "type": "string",
                            "maxLength": MAX_PROJECT_NAME_LENGTH,
                            "description": 'Target project name; "" removes the sessions '
                            "from their project.",
                        },
                    },
                    "required": ["session_ids", "project"],
                    "additionalProperties": False,
                },
            },
        }
