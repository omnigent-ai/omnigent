"""Schema-only tools for organizing the owner's projects and sessions."""

from typing import Any

from omnigent.entities.conversation import USER_SESSION_TITLE_MAX_CHARS
from omnigent.tools.base import Tool

MAX_PROJECT_NAME_LENGTH = 100
# Matches the web icon picker's bound on one emoji grapheme.
MAX_PROJECT_ICON_LENGTH = 16
MAX_LISTED_SESSIONS = 200
MAX_SEARCH_LENGTH = 200


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
            "sessions only; search matches titles and transcript text, pinned lists "
            "only pinned sessions, and include_archived adds archived ones. A session "
            "with moves_with is in a family and shares that session's project. Use "
            "sys_session_update to file, rename, pin or archive a session, and "
            "sys_project_update to rename a project or change its icon."
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
                        "search": {
                            "type": "string",
                            "maxLength": MAX_SEARCH_LENGTH,
                            "description": (
                                "Only list sessions whose title or transcript contains "
                                "this text, case-insensitively."
                            ),
                        },
                        "pinned": {
                            "type": "boolean",
                            "description": "true lists only the sessions the user pinned.",
                        },
                        "include_archived": {
                            "type": "boolean",
                            "description": (
                                "true also lists archived sessions, each marked archived."
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
            "Update one of the user's own sessions, the same as the sidebar's row "
            "actions; pass only the fields to change. project files the session into a "
            'project by name; a project that does not exist yet is created, and "" '
            "removes the session from its project. title renames it. pinned pins or "
            "unpins it for the user. archived: true archives it, which stops its runner "
            "and unpins it, so a running session's turn ends; archived: false restores "
            "it. Filing, renaming and pinning only change how the session is listed: a "
            "running session, including this one, keeps working. This session can archive "
            "itself, for example when its task is done: its runner stops once the current "
            "turn ends. It cannot archive a session it runs under. Sub-agent sessions "
            "follow their parent, and a family shares "
            "the project of the session its members list as moves_with. Call once per "
            "session."
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
                        "title": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": USER_SESSION_TITLE_MAX_CHARS,
                            "description": "New session title.",
                        },
                        "pinned": {
                            "type": "boolean",
                            "description": "true pins the session for the user; false unpins it.",
                        },
                        "archived": {
                            "type": "boolean",
                            "description": (
                                "true archives the session and stops its runner; false "
                                "restores it."
                            ),
                        },
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysProjectUpdateTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_project_update"

    @classmethod
    def description(cls) -> str:
        return (
            "Rename one of the user's projects or change its emoji icon, the same as the "
            "sidebar's project menu. Name the project as sys_project_list shows it, and "
            "pass only the fields to change. Its sessions stay in it, and running "
            "sessions keep working; status partial lists sessions_not_moved that "
            "stayed under the old name."
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
                            "minLength": 1,
                            "maxLength": MAX_PROJECT_NAME_LENGTH,
                            "description": "Current name of the project to update.",
                        },
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_PROJECT_NAME_LENGTH,
                            "description": "New project name.",
                        },
                        "icon": {
                            "type": "string",
                            "maxLength": MAX_PROJECT_ICON_LENGTH,
                            "description": 'One emoji to show for the project; "" removes it.',
                        },
                    },
                    "required": ["project"],
                    "additionalProperties": False,
                },
            },
        }


class SysSessionLinkPrTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_session_link_pr"

    @classmethod
    def description(cls) -> str:
        return (
            "Link or unlink a GitHub pull request in a session's PR panel. "
            "Defaults to the current session; another session requires edit access. "
            "Its host or runner must be online and able to read the PR. "
            "Requires scripted sessions enabled on the server and a private or "
            "read-only-shared calling session. This updates the association only."
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
                            "description": "Target session; omit for this session.",
                        },
                        "url": {"type": "string", "description": "GitHub pull request URL."},
                        "action": {
                            "type": "string",
                            "enum": ["attach", "remove"],
                            "description": "Defaults to attach.",
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        }
