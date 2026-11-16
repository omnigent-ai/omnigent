"""Built-in tools for triggered tasks.

A triggered task starts an agent session for each new matching item in Jira, Slack,
GitHub, Linear or PagerDuty. The server stores it as a scheduled task whose prompt is
rendered from a trigger, exactly as the web form's trigger templates save it (see
:mod:`omnigent.tools.builtins.trigger_prompts`). These tools take the trigger itself, so
an agent never handles that prompt or the schedule it checks on; the runner compiles each
call into ``/v1/scheduled-tasks`` requests.

* ``sys_triggered_task_create`` — create a triggered task.
* ``sys_triggered_task_list`` — list the caller's triggered tasks.
* ``sys_triggered_task_update`` — change a triggered task.
* ``sys_triggered_task_delete`` — delete a triggered task.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, cast

from omnigent.native.permission_modes import (
    AGENT_DEFAULT_PERMISSION_MODE_HINT,
    APPROVAL_SKIPPING_PERMISSION_MODES,
)
from omnigent.tools.base import Tool
from omnigent.tools.builtins.trigger_prompts import (
    MAX_INSTRUCTIONS_CHARS,
    TRIGGER_SOURCES,
    TRIGGER_TEMPLATES,
    VALUELESS_OPERATORS,
    TriggerCondition,
    TriggerConfig,
    TriggerSource,
    TriggerWorkerConfig,
    empty_trigger_fields,
    instructions_length,
    parse_github_reviewer,
    trigger_condition_fields,
    trigger_event,
    trigger_fields_complete,
    trigger_scope,
    trigger_scope_operators,
    with_trigger_event,
)

TRIGGERED_TASK_CREATE = "sys_triggered_task_create"
TRIGGERED_TASK_LIST = "sys_triggered_task_list"
TRIGGERED_TASK_UPDATE = "sys_triggered_task_update"
TRIGGERED_TASK_DELETE = "sys_triggered_task_delete"
TRIGGERED_TASK_TOOL_NAMES: frozenset[str] = frozenset(
    {TRIGGERED_TASK_CREATE, TRIGGERED_TASK_LIST, TRIGGERED_TASK_UPDATE, TRIGGERED_TASK_DELETE}
)

# Resent on every create and update so the approval shows the effective values.
TRIGGERED_TASK_REVIEW_FIELDS = frozenset({"instructions", "permission_mode"})

# The most automations one list call reads, the server's page limit.
LIST_PAGE_MAX = 100

# Arguments that describe the trigger, as opposed to where and how the task runs.
TRIGGER_ARGUMENTS = (
    "provider",
    "event",
    "scope",
    "scope_match",
    "target",
    "conditions",
    "link_back",
    "session_per_match",
    "worker",
)
# What a trigger watches. An update passes the whole search or none of it, so its
# approval can show the search it compiles to.
SEARCH_ARGUMENTS = ("provider", "event", "scope", "scope_match", "target", "conditions")
_RESTATED_SEARCH_ARGUMENTS = ("provider", "event", "scope", "conditions")
_WORKER_ARGUMENTS = ("agent_id", "model", "reasoning_effort", "permission_mode", "placement")
_WORKER_PLACEMENTS = ("same_host", "managed")
_SCOPE_MATCHES = ("exact", "contains")
_EXECUTION_TARGETS = ("connected_host", "managed_sandbox")

# The polling interval a minute-based rule names, e.g. "FREQ=MINUTELY;INTERVAL=10".
_MINUTELY_RRULE = re.compile(r"FREQ=MINUTELY;INTERVAL=(\d+)")

_PERMISSION_MODE_DESC = (
    "Permission mode for this task and, by default, the sessions it starts, in the "
    "selected harness's vocabulary: Claude Code (e.g. 'acceptEdits'), Codex ('default', "
    "'ask-for-approval', 'approve-for-me', 'read-only'), Cursor (e.g. 'auto-review'), "
    "Antigravity ('default', 'skip'), or Devin (e.g. 'smart'). Other harnesses, such as "
    "claude-sdk, have no automation permission modes. It runs unattended, so a prompting "
    "mode ('default'/'plan') stalls waiting for approval — prefer an auto-running mode. "
    f"Send {AGENT_DEFAULT_PERMISSION_MODE_HINT}, and always for a harness without modes. "
    "Required so the user can review the effective setting."
)

_EVENT_DESC = (
    "What to react to; defaults to the provider's first event. "
    "jira: 'created' (new ticket) or 'assigned' (assignee changed, including reassignment). "
    "slack: 'posted' (new top-level message in a channel), 'dm' (new direct message to "
    "you) or 'group_dm' (new top-level message in a group DM). "
    "github: 'issue' (issue opened), 'pr' (pull request opened), 'any' (either) or "
    "'review' (review requested on an open, non-draft pull request, including "
    "re-requests). linear: 'created' (new issue). pagerduty: 'triggered' (new incident)."
)

_SCOPE_DESC = (
    "What to watch; an item from any entry matches. jira: project keys, e.g. ['ES']. "
    "slack 'posted': channel names or IDs, e.g. ['eng-support']; 'dm': the senders, and "
    "'group_dm': everyone in the group DM besides you (at least two), as handles, display "
    "names or emails. github: repositories as 'owner/repo'. linear: team keys. "
    "pagerduty: service names or IDs."
)

_SCOPE_MATCH_DESC = (
    "'exact' (default) matches the scope entries as given. 'contains' (pagerduty only) "
    "watches every service whose name contains the single scope entry, ignoring case."
)

_TARGET_DESC = (
    "Who the event names. jira 'assigned': the new assignee — 'me' (default), 'anyone' "
    "(or ''), 'unassigned', or a name or email. github 'review': the requested reviewer — 'me' "
    "(default), a username, or 'org/team'. Other events take no target."
)

_LINK_BACK_DESC = (
    "Whether to post each session's link on its item: a ticket comment, a thread reply, "
    "a GitHub or Linear comment, or an incident note. Defaults to true, except false for "
    "github 'review'."
)

_SESSION_PER_MATCH_DESC = (
    "true (default) starts a new session for each match. false sends every match to one "
    "long-lived task session, named after this task, which keeps its context across "
    "matches."
)

_INSTRUCTIONS_DESC = (
    "What each session should do with its item, e.g. 'Investigate the ticket and propose "
    "a fix.' The item's details are added after these instructions. At most "
    f"{MAX_INSTRUCTIONS_CHARS} characters."
)

_MCP_SERVICES_DESC = (
    "Optional MCP services this task and the sessions it starts begin with, as the "
    "deployment's service names, such as the one that gives the agent the provider's "
    "tools. The owner must already be connected to each one."
)


def _conditions_catalog() -> str:
    """Each provider's condition fields and operators, as the tool description lists them.

    Built from the trigger templates, so the description can't drift from what the
    renderer accepts. Consecutive fields with the same operators and unavailable events
    share one entry, e.g. ``label, issuetype (is, is_not)``.
    """
    providers: list[str] = []
    for source in TRIGGER_SOURCES:
        template = TRIGGER_TEMPLATES[source]
        groups: list[tuple[list[str], tuple[str, ...], tuple[str, ...]]] = []
        for field in template.fields:
            hidden = tuple(
                f"'{event.id}'" for event in template.events if field.id in event.hidden_fields
            )
            operators = tuple(field.operators)
            if groups and groups[-1][1] == operators and groups[-1][2] == hidden:
                groups[-1][0].append(field.id)
            else:
                groups.append(([field.id], operators, hidden))
        entries = []
        for fields, operators, hidden in groups:
            note = f"; not with {', '.join(hidden)}" if hidden else ""
            entries.append(f"{', '.join(fields)} ({', '.join(operators)}{note})")
        providers.append(f"{source}: {'; '.join(entries)}")
    return ". ".join(providers) + "."


_CONDITIONS_DESC = (
    "Optional extra filters; every one must match. Each is {field, operator, value}; "
    "'me' as a person means you, and empty/not_empty take no value. "
    f"{_conditions_catalog()} jira custom_field also needs name: the field's name or ID, "
    "e.g. 'cf[10010]'. jira jql and github qualifiers take raw query text; slack topic "
    "takes a plain-language description of what the message is about."
)


def _condition_schema() -> dict[str, Any]:
    """The JSON schema of one ``conditions`` entry."""
    return {
        "type": "object",
        "properties": {
            "field": {"type": "string", "description": "A field the provider offers."},
            "operator": {
                "type": "string",
                "enum": sorted(
                    {
                        operator
                        for template in TRIGGER_TEMPLATES.values()
                        for field in template.fields
                        for operator in field.operators
                    }
                ),
            },
            "value": {"type": "string", "description": "What to compare with."},
            "name": {
                "type": "string",
                "description": "jira custom_field only: the field's name or ID.",
            },
        },
        "required": ["field", "operator"],
        "additionalProperties": False,
    }


def _worker_schema(*, update: bool) -> dict[str, Any]:
    """The JSON schema of the ``worker`` settings; an update refers to the create tool's."""
    nullable = {"type": ["string", "null"]}
    if update:
        return {
            "type": "object",
            "description": "Change settings of the sessions it starts, as in create.",
            "properties": {
                **{key: nullable for key in _WORKER_ARGUMENTS if key != "placement"},
                "placement": {"type": "string", "enum": list(_WORKER_PLACEMENTS)},
            },
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "description": (
            "Optional settings for the sessions it starts. By default they use agent_id, "
            "model_override, reasoning_effort and permission_mode (except a mode that "
            "skips approval), and start where this task runs: beside it on the same "
            "host, or each in a new managed sandbox with execution_target "
            "'managed_sandbox'. null means the default; a permission_mode that skips "
            "approval is not allowed."
        ),
        "properties": {
            **{key: nullable for key in _WORKER_ARGUMENTS if key != "placement"},
            "placement": {
                "type": "string",
                "enum": list(_WORKER_PLACEMENTS),
                "description": (
                    "'same_host' starts each session beside this task; 'managed' starts "
                    "each in a new managed sandbox."
                ),
            },
        },
        "additionalProperties": False,
    }


def _trigger_properties(*, update: bool) -> dict[str, Any]:
    """The schema properties that describe the trigger.

    The update tool refers to the create tool's descriptions instead of repeating
    them, since every tool's schema is sent on every turn.
    """

    def described(create_text: str, update_text: str) -> str:
        return update_text if update else create_text

    return {
        "provider": {"type": "string", "enum": list(TRIGGER_SOURCES)},
        "event": {
            "type": "string",
            "description": described(_EVENT_DESC, "As in create; part of the search."),
        },
        "scope": {
            "type": "array",
            "items": {"type": "string"},
            "description": described(_SCOPE_DESC, "As in create; part of the search."),
        },
        "scope_match": {
            "type": "string",
            "enum": list(_SCOPE_MATCHES),
            "description": described(
                _SCOPE_MATCH_DESC, "As in create; part of the search, defaulting to 'exact'."
            ),
        },
        "target": {
            "type": "string",
            "description": described(
                _TARGET_DESC, "As in create; part of the search, defaulting to 'me'."
            ),
        },
        "conditions": {
            "type": "array",
            "items": _condition_schema(),
            "description": described(
                _CONDITIONS_DESC,
                "Every condition, as in create ([] for none); part of the search.",
            ),
        },
        "instructions": {
            "type": "string",
            "description": described(
                _INSTRUCTIONS_DESC, "What each session does with its item, as in create."
            ),
        },
        "link_back": {
            "type": "boolean",
            "description": described(_LINK_BACK_DESC, "As in create."),
        },
        "session_per_match": {
            "type": "boolean",
            "description": described(_SESSION_PER_MATCH_DESC, "As in create."),
        },
        "worker": _worker_schema(update=update),
    }


def _placement_properties(*, update: bool) -> dict[str, Any]:
    """The schema properties for where the task runs and how its sessions are filed."""
    return {
        "workspace": {
            "type": ["string", "null"] if update else "string",
            "description": (
                "For connected_host: an existing absolute start path on the host; omit for "
                "the host's home directory. For managed_sandbox: a repository URL, "
                "optionally with #branch; omit for the provider default."
                + (" Null restores the default." if update else "")
            ),
        },
        "host_id": {
            "type": "string",
            "description": (
                "Optional connected host to run on, from the current workspace's host "
                "list. Omit to run on your online host when it checks; a check fails if "
                "none is online."
            ),
        },
        "execution_target": {
            "type": "string",
            "enum": list(_EXECUTION_TARGETS),
            "description": (
                "'connected_host' (default) runs on a connected host. 'managed_sandbox' "
                "runs in a server-managed sandbox; do not set host_id with it. Requires "
                "managed sandboxes configured on the server."
            ),
        },
        "project_name": {
            "type": "string",
            "description": (
                "Optional project to file its sessions into, by name: your existing "
                "project with this name, or a new one. Do not set project_id with it."
            ),
        },
        "project_id": {
            "type": ["string", "null"] if update else "string",
            "description": (
                "Optional existing project id to file its sessions into. Do not set "
                "project_name with it." + (" Null stops filing them." if update else "")
            ),
        },
    }


def _function_schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Wrap ``parameters`` in the OpenAI function-tool envelope."""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


class SysTriggeredTaskCreateTool(Tool):
    """Create a triggered task. Runner-dispatched to ``POST /v1/scheduled-tasks``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_triggered_task_create"``."""
        return TRIGGERED_TASK_CREATE

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Create a triggered task: it starts an agent session for each new item that "
            "matches a trigger in Jira, Slack, GitHub, Linear or PagerDuty, such as a "
            "ticket created in a Jira project or a review requested from you on GitHub. "
            "Give the provider, what to watch (event, scope, conditions) and the "
            "instructions each session gets; the item's details are added for it. New "
            "matches usually start a session within about 10 minutes. The first check "
            "also picks up items from about the hour before; for assignments and review "
            "requests, only changes after that check count. The agent needs the "
            "provider's tools where the task runs (an MCP server for Jira, Slack, Linear "
            "or PagerDuty, which mcp_services can start, or the GitHub CLI or a GitHub MCP "
            "server); without them it reports what is missing. What it posts uses your "
            "accounts and ends with 'Sent with Omnigent'. The task also appears on the "
            "Automations page. "
            "Manage it with sys_triggered_task_list, sys_triggered_task_update and "
            "sys_triggered_task_delete."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Task name, e.g. 'Triage new ES tickets'.",
                    },
                    **_trigger_properties(update=False),
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "The agent that watches for matches and, unless "
                            "worker.agent_id is set, runs the sessions it starts, e.g. "
                            "'ag_abc123' — from sys_agent_list or sys_agent_get."
                        ),
                    },
                    "model_override": {
                        "type": "string",
                        "description": (
                            "Optional model for this task and, by default, its sessions. "
                            "Omit for the agent default."
                        ),
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": (
                            "Optional reasoning-effort hint for this task and, by default, "
                            "its sessions, e.g. 'high'."
                        ),
                    },
                    "permission_mode": {
                        "type": ["string", "null"],
                        "description": _PERMISSION_MODE_DESC,
                    },
                    "mcp_services": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _MCP_SERVICES_DESC + " Omit for none.",
                    },
                    **_placement_properties(update=False),
                },
                "required": [
                    "name",
                    "provider",
                    "scope",
                    "instructions",
                    "agent_id",
                    "permission_mode",
                ],
                "additionalProperties": False,
            },
        )


class SysTriggeredTaskListTool(Tool):
    """List the caller's triggered tasks. Dispatched to ``GET /v1/scheduled-tasks``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_triggered_task_list"``."""
        return TRIGGERED_TASK_LIST

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List your triggered tasks: each one's trigger, instructions, settings and "
            "state, when it last checked for matches (last_checked_at, "
            "last_check_status), and watch_session_id, the session that checks and "
            "whose replies say which sessions it started. Results come a page of your "
            "automations at a time, oldest first: while next_cursor is set, call again "
            "with it as cursor. A page can hold fewer triggered tasks than limit, even none."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": LIST_PAGE_MAX,
                        "description": (
                            f"Automations to read for this page, 1-{LIST_PAGE_MAX}; "
                            f"default {LIST_PAGE_MAX}."
                        ),
                    },
                    "cursor": {
                        "type": "string",
                        "description": "The next_cursor of the previous page.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        )


class SysTriggeredTaskUpdateTool(Tool):
    """Update a triggered task. Dispatched to ``PATCH /v1/scheduled-tasks/{id}``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_triggered_task_update"``."""
        return TRIGGERED_TASK_UPDATE

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Change a triggered task (from sys_triggered_task_list). Resend instructions "
            "and permission_mode even when unchanged; anything else changes only if you "
            "pass it. To change what it watches, pass the whole search as the list shows "
            "it, with your changes: provider, event, scope and conditions, and target or "
            "scope_match where they apply. A different provider starts the task's checks "
            "over. Pass state 'paused' to stop it without deleting it, or 'active' to "
            "resume. Sessions it already started are not changed."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "type": "object",
                "properties": {
                    "triggered_task_id": {
                        "type": "string",
                        "description": "The task to change (from sys_triggered_task_list).",
                    },
                    "name": {"type": "string", "description": "New task name."},
                    **_trigger_properties(update=True),
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Switch the agent that watches for matches (and runs the "
                            "sessions, unless worker.agent_id is set), from sys_agent_list. "
                            "Model, effort and permission mode do not carry across a "
                            "switch: resend any you want set."
                        ),
                    },
                    "model_override": {"type": "string", "description": "New model."},
                    "reasoning_effort": {
                        "type": "string",
                        "description": "New reasoning-effort hint.",
                    },
                    "permission_mode": {
                        "type": ["string", "null"],
                        "description": "As in create; resend the current value when unchanged.",
                    },
                    "mcp_services": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "As in create; an empty list removes them.",
                    },
                    **_placement_properties(update=True),
                    "state": {"type": "string", "enum": ["active", "paused"]},
                },
                "required": ["triggered_task_id", "instructions", "permission_mode"],
                "additionalProperties": False,
            },
        )


class SysTriggeredTaskDeleteTool(Tool):
    """Delete a triggered task. Dispatched to ``DELETE /v1/scheduled-tasks/{id}``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_triggered_task_delete"``."""
        return TRIGGERED_TASK_DELETE

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Delete a triggered task so it stops starting sessions. Sessions it already "
            "started are kept."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "type": "object",
                "properties": {
                    "triggered_task_id": {
                        "type": "string",
                        "description": "The task to delete (from sys_triggered_task_list).",
                    },
                },
                "required": ["triggered_task_id"],
                "additionalProperties": False,
            },
        )


TRIGGERED_TASK_TOOLS: tuple[type[Tool], ...] = (
    SysTriggeredTaskCreateTool,
    SysTriggeredTaskListTool,
    SysTriggeredTaskUpdateTool,
    SysTriggeredTaskDeleteTool,
)


def triggered_task_call_error(args: Mapping[str, object], *, action: str) -> str | None:
    """What makes a create or update invalid before it reaches the server, or ``None``.

    The approval policy and the runner both check it, so nobody is asked to approve a
    call the runner would refuse.

    :param args: Parsed tool arguments.
    :param action: ``"create"`` or ``"update"``.
    :returns: An error message, or ``None``.
    """
    missing = sorted(TRIGGERED_TASK_REVIEW_FIELDS.difference(args))
    if missing:
        hint = (
            "List the task and retry with its instructions and permission_mode."
            if action == "update"
            else f"Retry with instructions and permission_mode: a mode name, or "
            f"{AGENT_DEFAULT_PERMISSION_MODE_HINT}."
        )
        return f"Missing review fields ({', '.join(missing)}). {hint}"
    mode = args["permission_mode"]
    if mode is not None and (not isinstance(mode, str) or mode.strip().lower() in ("", "null")):
        return f"permission_mode must be a mode name, or {AGENT_DEFAULT_PERMISSION_MODE_HINT}."
    if action == "update" and not args.get("triggered_task_id"):
        return "Provide the triggered_task_id of the task to change."
    name = args.get("name")
    if (action == "create" or "name" in args) and (not isinstance(name, str) or not name.strip()):
        return "name must be a non-empty string"
    if "project_name" in args and "project_id" in args:
        return "set project_id or project_name, not both"
    if "project_name" in args and not isinstance(args["project_name"], str):
        return "project_name must be a string; use project_id: null to clear"
    instructions = args["instructions"]
    if not isinstance(instructions, str) or not instructions.strip():
        return "instructions must be a non-empty string"
    if instructions_length(instructions.strip()) > MAX_INSTRUCTIONS_CHARS:
        return f"instructions can be at most {MAX_INSTRUCTIONS_CHARS} characters"
    for key in ("link_back", "session_per_match"):
        if key in args and not isinstance(args[key], bool):
            return f"{key} must be true or false"
    if "worker" in args:
        # The saved worker it merges into can't change whether these values are valid.
        worker = _parse_worker(args["worker"], _NO_WORKER_OVERRIDES)
        if isinstance(worker, str):
            return worker
    if action == "update":
        return _partial_search_error(args)
    return None


_NO_WORKER_OVERRIDES: TriggerWorkerConfig = {
    "placement": "same_host",
    "agentId": None,
    "model": "",
    "effort": "",
    "permissionMode": "",
}


def _partial_search_error(args: Mapping[str, object]) -> str | None:
    """Why an update's search is only part of one, or ``None``."""
    if not any(key in args for key in SEARCH_ARGUMENTS):
        return None
    absent = [key for key in _RESTATED_SEARCH_ARGUMENTS if key not in args]
    if not absent:
        return None
    return (
        f"to change what it watches, pass the whole search: add {', '.join(absent)} "
        "(sys_triggered_task_list shows the current values)"
    )


def default_worker(
    *,
    execution_target: object,
    model_override: object,
    reasoning_effort: object,
    permission_mode: object,
    worker_permission_modes: bool,
) -> TriggerWorkerConfig:
    """The worker settings a new trigger starts with, as the web form picks them.

    Workers run this task's agent, model and effort, and start where the task runs. Its
    permission mode carries over unless it skips approval, which the server refuses for
    a session an agent starts, or the server can't set a session's mode.

    :param execution_target: The task's ``execution_target``, e.g. ``"managed_sandbox"``.
    :param model_override: The task's model override, or ``None``.
    :param reasoning_effort: The task's reasoning effort, or ``None``.
    :param permission_mode: The task's permission mode, or ``None``.
    :param worker_permission_modes: Whether session creates accept a permission mode.
    :returns: The worker settings.
    """
    mode = permission_mode if isinstance(permission_mode, str) else ""
    if not worker_permission_modes or mode in APPROVAL_SKIPPING_PERMISSION_MODES:
        mode = ""
    return {
        "placement": "managed" if execution_target == "managed_sandbox" else "same_host",
        "agentId": None,
        "model": model_override if isinstance(model_override, str) else "",
        "effort": reasoning_effort if isinstance(reasoning_effort, str) else "",
        "permissionMode": mode,
    }


def _scope_entries(scope: str) -> list[str]:
    """The comma-separated entries of a saved scope, e.g. ``"ES, OPS"`` → ``["ES", "OPS"]``."""
    return [entry.strip() for entry in scope.split(",") if entry.strip()]


def _scope_operator(
    source: TriggerSource, event: str, entries: list[str], scope_match: str
) -> str:
    """The saved scope operator for ``entries``, as the web form would choose it."""
    if scope_match == "contains":
        return "contains"
    if trigger_scope(source, event).is_group:
        return "in"
    return "is" if len(entries) == 1 else "in"


def _parse_scope(source: TriggerSource, event: str, raw: object) -> list[str] | str:
    """The scope entries from a tool call, without the event's ``#``/``@`` prefix."""
    if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
        return "scope must be a list of strings, e.g. ['ES']"
    prefix = trigger_scope(source, event).prefix
    entries: list[str] = []
    for entry in cast(list[str], raw):
        value = entry.strip()
        while prefix and value.startswith(prefix):
            value = value[len(prefix) :].strip()
        if "," in value:
            return f"scope entries can't contain commas: {entry!r}; give each as its own entry"
        if value:
            entries.append(value)
    if not entries:
        return "scope needs at least one entry"
    return entries


def _parse_conditions(
    source: TriggerSource, event: str, raw: object
) -> list[TriggerCondition] | str:
    """The condition rows from a tool call, checked against the event's fields."""
    if not isinstance(raw, list):
        return "conditions must be a list of {field, operator, value} objects"
    fields = {field.id: field for field in trigger_condition_fields(source, event)}
    conditions: list[TriggerCondition] = []
    for index, item in enumerate(cast(list[object], raw)):
        where = f"conditions[{index}]"
        if not isinstance(item, dict):
            return f"{where} must be an object with field, operator and value"
        row = cast(dict[str, object], item)
        unknown = sorted(set(row).difference({"field", "operator", "value", "name"}))
        if unknown:
            return f"{where} has unknown keys: {', '.join(unknown)}"
        field_id = row.get("field")
        field = fields.get(field_id) if isinstance(field_id, str) else None
        if field is None:
            return (
                f"{where}: field {field_id!r} isn't available for {source} {event!r}; "
                f"use one of: {', '.join(fields)}"
            )
        operator = row.get("operator")
        if operator not in field.operators:
            return (
                f"{where}: {source} {field.id} takes the operators "
                f"{', '.join(field.operators)}, not {operator!r}"
            )
        value = row.get("value", "")
        if not isinstance(value, str):
            return f"{where}: value must be a string"
        value = value.strip()
        if operator in VALUELESS_OPERATORS:
            if value:
                return f"{where}: {operator} takes no value"
        elif not value:
            return f"{where}: {field.id} {operator} needs a value"
        if field.id == "reviewer" and parse_github_reviewer(value) is None:
            return f"{where}: a reviewer is 'me', a GitHub username, or 'org/team'"
        condition: TriggerCondition = {
            "field": field.id,
            "operator": cast(Any, operator),
            "value": value,
        }
        name = row.get("name")
        if field.id == "custom_field":
            if not isinstance(name, str) or not name.strip():
                return f"{where}: custom_field needs name, the field's name or ID"
            condition["name"] = name.strip()
        elif name is not None:
            return f"{where}: only custom_field takes a name"
        conditions.append(condition)
    return conditions


def _parse_worker(raw: object, base: TriggerWorkerConfig) -> TriggerWorkerConfig | str:
    """``base`` with the worker settings a tool call passes."""
    if not isinstance(raw, dict):
        return "worker must be an object"
    settings = cast(dict[str, object], raw)
    unknown = sorted(set(settings).difference(_WORKER_ARGUMENTS))
    if unknown:
        return f"worker has unknown keys: {', '.join(unknown)}"
    values: dict[str, str | None] = {}
    for key in ("agent_id", "model", "reasoning_effort", "permission_mode"):
        if key not in settings:
            continue
        value = settings[key]
        if value is not None and not isinstance(value, str):
            return f"worker.{key} must be a string or null"
        values[key] = value.strip() if isinstance(value, str) else None

    def setting(key: str, saved: str) -> str:
        return (values[key] or "") if key in values else saved

    worker: TriggerWorkerConfig = {
        "placement": base["placement"],
        # A blank agent means the task's own, like null.
        "agentId": (values["agent_id"] or None) if "agent_id" in values else base["agentId"],
        "model": setting("model", base["model"]),
        "effort": setting("reasoning_effort", base["effort"]),
        "permissionMode": setting("permission_mode", base.get("permissionMode", "")),
    }
    if worker["permissionMode"] in APPROVAL_SKIPPING_PERMISSION_MODES:
        return (
            f"worker.permission_mode {worker['permissionMode']!r} skips approval, which "
            "sessions an automation starts can't use"
        )
    if "placement" in settings:
        placement = settings["placement"]
        if placement == "same_host":
            worker["placement"] = "same_host"
        elif placement == "managed":
            worker["placement"] = "managed"
        else:
            return "worker.placement must be 'same_host' or 'managed'"
    return worker


def _build_search(args: Mapping[str, object]) -> dict[str, Any] | str:
    """The fields a call's search sets, from the provider's empty form, or the problem.

    :param args: Parsed tool arguments with the search, e.g. ``{"provider": "jira", ...}``.
    :returns: The trigger fields, with ``source`` and ``event``, or an error message.
    """
    raw_source = args.get("provider")
    if raw_source not in TRIGGER_SOURCES:
        return f"provider must be one of: {', '.join(TRIGGER_SOURCES)}"
    source = cast(Any, raw_source)
    events = [event.id for event in TRIGGER_TEMPLATES[source].events]
    raw_event = args.get("event", events[0])
    if raw_event not in events:
        return f"{source} event must be one of: {', '.join(events)}"
    event = cast(str, raw_event)
    fields: dict[str, Any] = dict(empty_trigger_fields(source))
    if event != fields["event"]:
        # Like the form, an event with a target starts at "me".
        fields = dict(with_trigger_event(source, cast(Any, fields), event))

    scope_match = args.get("scope_match", "exact")
    if scope_match not in _SCOPE_MATCHES:
        return "scope_match must be 'exact' or 'contains'"
    if scope_match == "contains" and "contains" not in trigger_scope_operators(source):
        return f"scope_match 'contains' is for pagerduty, not {source}"
    if "scope" not in args:
        return "scope is required"
    entries = _parse_scope(source, event, args["scope"])
    if isinstance(entries, str):
        return entries
    if scope_match == "contains" and len(entries) != 1:
        return "scope_match 'contains' takes exactly one scope entry"
    if trigger_scope(source, event).is_group and len(entries) < 2:
        return "a group DM scope names at least two people besides you"
    fields["scope"] = ", ".join(entries)
    fields["scopeOperator"] = _scope_operator(source, event, entries, cast(str, scope_match))

    event_spec = trigger_event(source, event)
    if "target" in args:
        if event_spec.target is None:
            return f"{source} {event!r} takes no target"
        target = args["target"]
        if not isinstance(target, str):
            return "target must be a string"
        fields["target"] = target.strip()
    if event_spec.target is None:
        fields.pop("target", None)
    else:
        fields.setdefault("target", "me")
        if event_spec.target.required and not fields["target"]:
            return f"{source} {event!r} needs a target"
        if source == "github" and parse_github_reviewer(fields["target"]) is None:
            return "target must be 'me', a GitHub username, or 'org/team'"

    if "conditions" in args:
        conditions = _parse_conditions(source, event, args["conditions"])
        if isinstance(conditions, str):
            return conditions
        fields["conditions"] = conditions
    return {**fields, "source": source, "event": event}


def build_trigger_config(
    args: Mapping[str, object],
    *,
    current: TriggerConfig | None,
    worker_defaults: TriggerWorkerConfig,
) -> TriggerConfig | str:
    """The trigger a create or update call describes, or the reason it is invalid.

    A create builds its search from the provider's empty form, as the web form does. An
    update keeps the saved search unless the call passes a whole new one (see
    :func:`triggered_task_call_error`), which is built the same way, so the approval can
    compile it from the call alone. Link-back, where matches go and the worker settings
    change only when the call passes them.

    :param args: Parsed tool arguments, e.g. ``{"provider": "jira", "scope": ["ES"], ...}``.
    :param current: The task's saved trigger on update, ``None`` on create.
    :param worker_defaults: Worker settings for a create; an update keeps ``current``'s.
    :returns: The complete trigger, or an error message.
    """
    worker: TriggerWorkerConfig = {
        **(current["worker"] if current is not None else worker_defaults)
    }
    fields: dict[str, Any]
    if current is None or any(key in args for key in SEARCH_ARGUMENTS):
        partial = _partial_search_error(args) if current is not None else None
        if partial is not None:
            return partial
        search = _build_search(args)
        if isinstance(search, str):
            return search
        fields = search
        if current is not None:
            for key in ("linkBack", "linkBackTouched", "taskSessionMode"):
                if key in current:
                    fields[key] = current[key]
                else:
                    fields.pop(key, None)
    else:
        fields = dict(current)
    source = cast(Any, fields["source"])
    event = cast(str, fields["event"])

    instructions = args.get("instructions", fields.get("instructions", ""))
    if not isinstance(instructions, str) or not instructions.strip():
        return "instructions must be a non-empty string"
    instructions = instructions.strip()
    if instructions_length(instructions) > MAX_INSTRUCTIONS_CHARS:
        return f"instructions can be at most {MAX_INSTRUCTIONS_CHARS} characters"
    fields["instructions"] = instructions

    if "link_back" in args:
        if not isinstance(args["link_back"], bool):
            return "link_back must be true or false"
        fields["linkBack"] = args["link_back"]
        fields["linkBackTouched"] = True
    if "session_per_match" in args:
        if not isinstance(args["session_per_match"], bool):
            return "session_per_match must be true or false"
        if args["session_per_match"]:
            fields.pop("taskSessionMode", None)
        else:
            fields["taskSessionMode"] = "continue"
    if "worker" in args:
        parsed_worker = _parse_worker(args["worker"], worker)
        if isinstance(parsed_worker, str):
            return parsed_worker
        worker = parsed_worker

    config = cast(TriggerConfig, {**fields, "source": source, "event": event, "worker": worker})
    if not trigger_fields_complete(config):
        return "the trigger is incomplete; check scope, target and conditions"
    return config


def polling_minutes(rrule: object) -> int:
    """How far back a trigger's first check looks, from its schedule, as the form passes it.

    :param rrule: The task's rule, e.g. ``"FREQ=MINUTELY;INTERVAL=10"``.
    :returns: The minute interval for a minute-based rule, else 60.
    """
    match = _MINUTELY_RRULE.fullmatch(rrule) if isinstance(rrule, str) else None
    return int(match.group(1)) if match else 60


def trigger_rrule(info: Mapping[str, object]) -> str:
    """The schedule a new trigger checks on, as the web form picks it from ``/v1/info``.

    :param info: The server's ``/v1/info`` payload.
    :returns: E.g. ``"FREQ=MINUTELY;INTERVAL=10"``, or hourly where the server can't
        schedule minute rules.
    """
    features = info.get("features")
    anchored = isinstance(features, dict) and features.get("automations_v2") is True
    minutes = info.get("automation_trigger_poll_minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        minutes = 10
    if anchored and (info.get("scheduled_task_subhour") is True or minutes >= 60):
        return f"FREQ=MINUTELY;INTERVAL={minutes}"
    return "FREQ=HOURLY;BYMINUTE=0"


def triggered_task_view(task: Mapping[str, Any], config: TriggerConfig) -> dict[str, Any]:
    """A triggered task as the tools report it: the trigger, not the prompt it renders.

    :param task: A ``/v1/scheduled-tasks`` task response.
    :param config: The trigger decoded from the task's prompt.
    :returns: The task with the same trigger fields the create tool takes.
    """
    worker = config["worker"]
    view: dict[str, Any] = {
        "triggered_task_id": task.get("id"),
        "name": task.get("name"),
        "state": task.get("state"),
        "provider": config["source"],
        "event": config["event"],
        "scope": _scope_entries(config["scope"]),
        "scope_match": "contains" if config.get("scopeOperator") == "contains" else "exact",
    }
    if trigger_event(config["source"], config["event"]).target is not None:
        # The form can save no target, which watches anyone; '' restates that.
        view["target"] = config.get("target") or ""
    view.update(
        {
            "conditions": [
                {
                    key: condition[key]
                    for key in ("field", "operator", "value", "name")
                    if key in condition
                }
                for condition in config["conditions"]
            ],
            "instructions": config["instructions"],
            "link_back": config["linkBack"],
            "session_per_match": config.get("taskSessionMode") != "continue",
            "worker": {
                "agent_id": worker["agentId"],
                "model": worker["model"] or None,
                "reasoning_effort": worker["effort"] or None,
                "permission_mode": worker.get("permissionMode") or None,
                "placement": worker["placement"],
            },
        }
    )
    for key in (
        "agent_id",
        "model_override",
        "reasoning_effort",
        "permission_mode",
        "mcp_services",
        "workspace",
        "host_id",
        "execution_target",
        "project_id",
        "created_at",
        "updated_at",
    ):
        if key in task:
            view[key] = task[key]
    view["last_checked_at"] = task.get("last_run_at")
    view["last_check_status"] = task.get("last_run_status")
    view["watch_session_id"] = task.get("session_id") or task.get("last_run_conversation_id")
    return view


def describe_trigger_arguments(args: Mapping[str, object]) -> list[str]:
    """Review lines for the trigger arguments a call passes, each value shown in full.

    Values are JSON-encoded so a multi-line or quoted value can't pass for the review's
    own text. Conditions and raw query text run with the owner's credentials, so none is
    shortened.

    :param args: Parsed tool arguments.
    :returns: One line per trigger argument present, e.g. ``'provider: "jira"'``.
    """
    lines = []
    for key in TRIGGER_ARGUMENTS:
        if key not in args:
            continue
        value = args[key]
        if key == "conditions" and isinstance(value, list) and value:
            rows = [json.dumps(row, ensure_ascii=False) for row in cast(list[object], value)]
            lines.append("conditions (all must match):")
            lines.extend(f"  - {row}" for row in rows)
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return lines
