"""Schema-only tools for listing projects and updating the owner's sessions."""

from typing import Any

from omnigent.tools.base import Tool

MAX_PROJECT_NAME_LENGTH = 100
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
            "sessions only. A session with moves_with is in a family and shares that "
            "session's project. Use sys_session_update to file a session into a project."
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


class SysSessionUpdateTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_session_update"

    @classmethod
    def description(cls) -> str:
        return (
            "Update one of the user's own sessions. project files the session into a "
            "project by name, the same as moving it in the sidebar; a project that does "
            'not exist yet is created, and "" removes the session from its project. '
            "Changing the project only changes the folder: a running session, including "
            "this one, keeps working. Sub-agent sessions follow their parent's project, "
            "and a family shares the project of the session its members list as "
            "moves_with. Call once per session."
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
                        "session_id": {
                            "type": "string",
                            "description": "ID of the session to update.",
                        },
                        "project": {
                            "type": "string",
                            "maxLength": MAX_PROJECT_NAME_LENGTH,
                            "description": 'Project name to file the session into; "" '
                            "removes it from its project.",
                        },
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        }
