"""Tests for the ``sys_triggered_task_*`` tool definitions and trigger arguments.

Rendering and the saved header are covered by ``test_trigger_prompts.py``; these
cover what the tools accept: their schemas, how a call's arguments become the
trigger the web form saves, and how a saved trigger is reported back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from omnigent.tools.builtins.trigger_prompts import (
    MAX_INSTRUCTIONS_CHARS,
    TRIGGER_TEMPLATES,
    TriggerConfig,
    TriggerWorkerConfig,
    decode_trigger_prompt,
    describe_trigger_watch,
    trigger_fields_complete,
)
from omnigent.tools.builtins.triggered_tasks import (
    TRIGGERED_TASK_TOOLS,
    SysTriggeredTaskCreateTool,
    SysTriggeredTaskUpdateTool,
    build_trigger_config,
    default_worker,
    describe_trigger_arguments,
    polling_minutes,
    review_json,
    trigger_rrule,
    triggered_task_call_error,
    triggered_task_view,
)

_WORKER: TriggerWorkerConfig = {
    "placement": "same_host",
    "agentId": None,
    "model": "",
    "effort": "",
    "permissionMode": "",
}


def _create(**args: Any) -> TriggerConfig | str:
    return build_trigger_config(
        {"instructions": "Investigate it.", **args}, current=None, worker_defaults=_WORKER
    )


def _created(**args: Any) -> TriggerConfig:
    config = _create(**args)
    assert not isinstance(config, str), config
    assert trigger_fields_complete(config)
    return config


@pytest.mark.parametrize("tool", TRIGGERED_TASK_TOOLS, ids=lambda tool: tool.name())
def test_schemas_are_valid_json_schema(tool: Any) -> None:
    schema = tool().get_schema()
    assert schema["function"]["name"] == tool.name()
    jsonschema.Draft202012Validator.check_schema(schema["function"]["parameters"])


def test_create_schema_requires_the_trigger_and_review_fields() -> None:
    parameters = SysTriggeredTaskCreateTool().get_schema()["function"]["parameters"]
    assert set(parameters["required"]) == {
        "name",
        "provider",
        "scope",
        "instructions",
        "agent_id",
        "permission_mode",
    }
    # Schedules and session modes are implementation details of a trigger.
    assert not {"rrule", "timezone", "session_mode", "session_id"} & set(parameters["properties"])
    jsonschema.validate(
        {
            "name": "Triage new tickets",
            "provider": "jira",
            "event": "created",
            "scope": ["ES"],
            "conditions": [{"field": "custom_field", "operator": "is", "value": "x", "name": "y"}],
            "instructions": "Investigate it.",
            "agent_id": "ag_example",
            "permission_mode": None,
            "worker": {"model": None, "placement": "managed"},
        },
        parameters,
    )


def test_update_schema_requires_only_the_id_and_review_fields() -> None:
    parameters = SysTriggeredTaskUpdateTool().get_schema()["function"]["parameters"]
    assert set(parameters["required"]) == {"triggered_task_id", "instructions", "permission_mode"}
    assert "rrule" not in parameters["properties"]


def test_create_description_lists_every_condition_field() -> None:
    conditions = SysTriggeredTaskCreateTool().get_schema()["function"]["parameters"]["properties"][
        "conditions"
    ]["description"]
    for source, template in TRIGGER_TEMPLATES.items():
        line = conditions.split(f"{source}: ", 1)[1].split(". ", 1)[0]
        for field in template.fields:
            assert field.id in line, (source, field.id)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            {"provider": "jira", "scope": ["ES"]},
            {"event": "created", "scope": "ES", "scopeOperator": "is", "linkBack": True},
        ),
        (
            {"provider": "jira", "scope": [" ES ", "OPS", ""]},
            {"scope": "ES, OPS", "scopeOperator": "in"},
        ),
        (
            {"provider": "slack", "scope": ["#eng-support", "C0123ABCD"]},
            {"event": "posted", "scope": "eng-support, C0123ABCD"},
        ),
        (
            {"provider": "slack", "event": "dm", "scope": ["@alice"]},
            {"event": "dm", "scope": "alice", "scopeOperator": "is"},
        ),
        (
            {"provider": "slack", "event": "group_dm", "scope": ["alice", "bob"]},
            {"scopeOperator": "in"},
        ),
        (
            {"provider": "github", "event": "review", "scope": ["octo-org/octo-repo"]},
            {"target": "me", "linkBack": False},
        ),
        (
            {
                "provider": "github",
                "event": "review",
                "scope": ["octo-org/octo-repo"],
                "target": "octo-org/reviewers",
                "link_back": True,
            },
            {"target": "octo-org/reviewers", "linkBack": True, "linkBackTouched": True},
        ),
        (
            {"provider": "jira", "event": "assigned", "scope": ["ES"], "target": ""},
            {"target": ""},
        ),
        (
            {"provider": "pagerduty", "scope": ["Checkout"], "scope_match": "contains"},
            {"event": "triggered", "scopeOperator": "contains"},
        ),
        (
            {"provider": "linear", "scope": ["ENG"], "session_per_match": False},
            {"taskSessionMode": "continue"},
        ),
    ],
    ids=[
        "jira-default-event",
        "several-projects",
        "slack-channel-prefix",
        "slack-dm-prefix",
        "group-dm",
        "github-review-defaults",
        "github-team-reviewer",
        "jira-anyone",
        "pagerduty-contains",
        "one-task-session",
    ],
)
def test_create_builds_the_trigger_the_form_saves(
    args: dict[str, Any], expected: dict[str, Any]
) -> None:
    config = _created(**args)
    assert config["source"] == args["provider"]
    for key, value in expected.items():
        assert config.get(key) == value, key
    assert config["instructions"] == "Investigate it."


def test_create_reads_conditions() -> None:
    config = _created(
        provider="jira",
        scope=["ES"],
        conditions=[
            {"field": "label", "operator": "is_not", "value": " wontfix "},
            {"field": "assignee", "operator": "empty"},
            {"field": "custom_field", "operator": "is", "value": "Backend", "name": "Team"},
            {"field": "jql", "operator": "matches", "value": 'summary ~ "crash"'},
        ],
    )
    assert config["conditions"] == [
        {"field": "label", "operator": "is_not", "value": "wontfix"},
        {"field": "assignee", "operator": "empty", "value": ""},
        {"field": "custom_field", "operator": "is", "value": "Backend", "name": "Team"},
        {"field": "jql", "operator": "matches", "value": 'summary ~ "crash"'},
    ]


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"provider": "asana", "scope": ["X"]}, "provider must be one of"),
        ({"provider": "jira", "event": "closed", "scope": ["ES"]}, "event must be one of"),
        ({"provider": "jira", "scope": []}, "scope needs at least one entry"),
        ({"provider": "jira", "scope": "ES"}, "scope must be a list"),
        ({"provider": "jira", "scope": ["ES,OPS"]}, "can't contain commas"),
        ({"provider": "jira", "scope": ["ES"], "scope_match": "contains"}, "is for pagerduty"),
        (
            {"provider": "pagerduty", "scope": ["A", "B"], "scope_match": "contains"},
            "exactly one scope entry",
        ),
        ({"provider": "slack", "event": "group_dm", "scope": ["alice"]}, "at least two people"),
        ({"provider": "jira", "scope": ["ES"], "target": "me"}, "takes no target"),
        (
            {"provider": "github", "event": "review", "scope": ["o/r"], "target": ""},
            "needs a target",
        ),
        (
            {"provider": "github", "event": "review", "scope": ["o/r"], "target": "not a login"},
            "GitHub username",
        ),
        (
            {"provider": "jira", "scope": ["ES"], "conditions": [{"field": "status"}]},
            "isn't available for jira 'created'",
        ),
        (
            {
                "provider": "jira",
                "event": "assigned",
                "scope": ["ES"],
                "conditions": [{"field": "assignee", "operator": "empty"}],
            },
            "isn't available for jira 'assigned'",
        ),
        (
            {
                "provider": "jira",
                "scope": ["ES"],
                "conditions": [{"field": "label", "operator": "contains", "value": "x"}],
            },
            "takes the operators is, is_not",
        ),
        (
            {
                "provider": "jira",
                "scope": ["ES"],
                "conditions": [{"field": "label", "operator": "is"}],
            },
            "needs a value",
        ),
        (
            {
                "provider": "jira",
                "scope": ["ES"],
                "conditions": [{"field": "assignee", "operator": "empty", "value": "me"}],
            },
            "takes no value",
        ),
        (
            {
                "provider": "jira",
                "scope": ["ES"],
                "conditions": [{"field": "custom_field", "operator": "is", "value": "x"}],
            },
            "custom_field needs name",
        ),
        (
            {
                "provider": "jira",
                "scope": ["ES"],
                "conditions": [{"field": "label", "operator": "is", "value": "x", "name": "y"}],
            },
            "only custom_field takes a name",
        ),
        (
            {
                "provider": "github",
                "event": "pr",
                "scope": ["o/r"],
                "conditions": [{"field": "reviewer", "operator": "is", "value": "a b"}],
            },
            "a reviewer is",
        ),
        ({"provider": "jira", "scope": ["ES"], "instructions": "  "}, "non-empty"),
        (
            {"provider": "jira", "scope": ["ES"], "worker": {"permission_mode": "bypass"}},
            "skips approval",
        ),
        # Saved, these would stop every session it starts; the string isn't JSON null.
        (
            {"provider": "jira", "scope": ["ES"], "worker": {"permission_mode": "null"}},
            "is not a permission mode",
        ),
        (
            {"provider": "jira", "scope": ["ES"], "worker": {"permission_mode": "acceptedits"}},
            "is not a permission mode",
        ),
        (
            {"provider": "jira", "scope": ["ES"], "worker": {"placement": "cloud"}},
            "worker.placement",
        ),
        ({"provider": "jira", "scope": ["ES"], "link_back": "yes"}, "link_back must be"),
    ],
)
def test_create_explains_an_invalid_trigger(args: dict[str, Any], error: str) -> None:
    result = _create(**args)
    assert isinstance(result, str)
    assert error in result


def test_instructions_limit_counts_like_the_web_form() -> None:
    # The form counts UTF-16 code units, so an astral emoji takes two.
    fits = "x" * (MAX_INSTRUCTIONS_CHARS - 2) + "\U0001f600"
    assert not isinstance(_create(provider="jira", scope=["ES"], instructions=fits), str)
    result = _create(provider="jira", scope=["ES"], instructions=fits + "x")
    assert isinstance(result, str) and "at most" in result


@pytest.mark.parametrize(
    ("worker", "expected"),
    [
        # Another agent starts without the task agent's harness-specific settings.
        ({"agent_id": "ag_codex"}, {"agentId": "ag_codex", "model": "", "effort": ""}),
        (
            {"agent_id": "ag_codex", "model": "gpt-5"},
            {"agentId": "ag_codex", "model": "gpt-5", "effort": ""},
        ),
        # The task's own agent keeps them.
        ({"agent_id": None}, {"agentId": None, "model": "claude-x", "effort": "high"}),
    ],
)
def test_another_worker_agent_starts_without_the_task_agents_settings(
    worker: dict[str, Any], expected: dict[str, Any]
) -> None:
    defaults = default_worker(
        execution_target=None,
        model_override="claude-x",
        reasoning_effort="high",
        permission_mode="acceptEdits",
        worker_permission_modes=True,
    )
    config = build_trigger_config(
        {"provider": "jira", "scope": ["ES"], "instructions": "x", "worker": worker},
        current=None,
        worker_defaults=defaults,
    )
    assert not isinstance(config, str)
    mode = "acceptEdits" if expected["agentId"] is None else ""
    assert config["worker"] == {**expected, "placement": "same_host", "permissionMode": mode}


def test_worker_settings_merge_over_the_defaults() -> None:
    config = _created(
        provider="jira",
        scope=["ES"],
        worker={"agent_id": "ag_worker", "model": "", "reasoning_effort": "high"},
    )
    assert config["worker"] == {**_WORKER, "agentId": "ag_worker", "effort": "high"}


@pytest.mark.parametrize(
    ("task", "worker_permission_modes", "expected"),
    [
        (
            {"execution_target": None, "permission_mode": "acceptEdits"},
            True,
            {"placement": "same_host", "permissionMode": "acceptEdits"},
        ),
        (
            {"execution_target": "managed_sandbox", "permission_mode": "bypassPermissions"},
            True,
            {"placement": "managed", "permissionMode": ""},
        ),
        (
            {"execution_target": "connected_host", "permission_mode": "acceptEdits"},
            False,
            {"placement": "same_host", "permissionMode": ""},
        ),
    ],
    ids=["inherits-mode", "never-skips-approval", "server-without-session-modes"],
)
def test_default_worker_follows_the_task(
    task: dict[str, Any], worker_permission_modes: bool, expected: dict[str, Any]
) -> None:
    worker = default_worker(
        model_override="model-a",
        reasoning_effort=None,
        worker_permission_modes=worker_permission_modes,
        **task,
    )
    assert worker == {"agentId": None, "model": "model-a", "effort": "", **expected}


def _update(current: TriggerConfig, **args: Any) -> TriggerConfig | str:
    return build_trigger_config(
        {"instructions": current["instructions"], **args},
        current=current,
        worker_defaults=current["worker"],
    )


def test_update_keeps_what_the_call_leaves_out() -> None:
    current = _created(
        provider="jira",
        scope=["ES"],
        conditions=[{"field": "label", "operator": "is", "value": "bug"}],
        link_back=False,
        worker={"agent_id": "ag_worker"},
    )
    # An "in" scope with one project stays "in" so an unrelated update renders the same.
    current["scopeOperator"] = "in"
    assert _update(current) == current
    updated = _update(current, instructions="Fix it.")
    assert updated == {**current, "instructions": "Fix it."}


def test_update_with_a_new_search_builds_it_from_the_call() -> None:
    current = _created(
        provider="github",
        event="pr",
        scope=["octo-org/octo-repo"],
        conditions=[{"field": "reviewer", "operator": "is", "value": "me"}],
        link_back=False,
        session_per_match=False,
        worker={"agent_id": "ag_worker"},
    )
    search = {
        "provider": "github",
        "event": "issue",
        "scope": ["octo-org/octo-repo"],
        "conditions": [{"field": "label", "operator": "is", "value": "bug"}],
    }
    switched = _update(current, **search)
    # The search is the call's, as a create builds it; delivery and workers stay.
    expected = _created(**search, link_back=False, session_per_match=False)
    assert switched == {**expected, "worker": current["worker"]}


@pytest.mark.parametrize(
    ("link_back", "expected"),
    [
        # Never chosen, it follows the new event, whose default is off for review requests.
        (None, False),
        (True, True),
    ],
    ids=["unchosen", "chosen"],
)
def test_a_new_search_keeps_only_a_chosen_link_back(
    link_back: bool | None, expected: bool
) -> None:
    current = _created(
        provider="jira", scope=["ES"], **({} if link_back is None else {"link_back": link_back})
    )
    switched = _update(
        current,
        provider="github",
        event="review",
        scope=["octo-org/octo-repo"],
        target="me",
        conditions=[],
    )
    assert not isinstance(switched, str) and switched["linkBack"] is expected


def test_update_needs_the_whole_search_to_change_it() -> None:
    current = _created(provider="slack", scope=["eng-support"])
    error = _update(current, event="dm", scope=["alice"])
    assert error == (
        "to change what it watches, pass the whole search: add provider, conditions "
        "(sys_triggered_task_list shows the current values)"
    )
    dm = _update(current, provider="slack", event="dm", scope=["alice"], conditions=[])
    assert not isinstance(dm, str) and (dm["event"], dm["scope"]) == ("dm", "alice")


@pytest.mark.parametrize(
    ("search", "absent"),
    [
        # Left out, they would fall back to "me" and "exact" rather than the saved values.
        ({"provider": "jira", "event": "assigned", "scope": ["ES"]}, "target"),
        ({"provider": "pagerduty", "event": "triggered", "scope": ["Payments"]}, "scope_match"),
    ],
)
def test_a_new_search_names_its_target_and_scope_match_where_they_apply(
    search: dict[str, Any], absent: str
) -> None:
    current = _created(provider="jira", scope=["ES"])
    assert _update(current, **search, conditions=[]) == (
        f"to change what it watches, pass the whole search: add {absent} "
        "(sys_triggered_task_list shows the current values)"
    )


def test_update_to_another_provider_starts_from_its_empty_form() -> None:
    current = _created(
        provider="jira",
        scope=["ES"],
        conditions=[{"field": "label", "operator": "is", "value": "bug"}],
        session_per_match=False,
        worker={"agent_id": "ag_worker"},
    )
    search = {"provider": "linear", "event": "created", "scope": ["ENG"], "conditions": []}
    switched = _update(current, **search)
    assert not isinstance(switched, str)
    assert (switched["source"], switched["event"], switched["conditions"]) == (
        "linear",
        "created",
        [],
    )
    # Like the form's source switch, the sessions it starts and where matches go stay.
    assert switched["worker"]["agentId"] == "ag_worker"
    assert switched["taskSessionMode"] == "continue"
    per_match = _update(current, **search, session_per_match=True)
    assert not isinstance(per_match, str) and "taskSessionMode" not in per_match


def test_an_unrelated_update_keeps_a_trigger_that_watches_anyone() -> None:
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "trigger_prompts.json").read_text(
            encoding="utf-8"
        )
    )
    [case] = [c for c in fixture["renders"] if c["name"] == "jira-assigned-without-target"]
    current = decode_trigger_prompt(case["prompt"])
    assert current is not None and "target" not in current
    watch = describe_trigger_watch(current)
    assert "assigned to anyone" in watch
    updated = _update(current, instructions="Fix it.")
    assert not isinstance(updated, str)
    assert describe_trigger_watch(updated) == watch
    # The list restates the saved search, so resending it watches the same tickets.
    view = triggered_task_view({"id": "t1"}, current)
    assert view["target"] == ""
    resent = _update(
        current,
        **{key: view[key] for key in ("provider", "event", "scope", "target", "conditions")},
    )
    assert not isinstance(resent, str)
    assert describe_trigger_watch(resent) == watch


@pytest.mark.parametrize(
    "args",
    [
        {"provider": "jira", "scope": ["ES", "OPS"], "link_back": False},
        {"provider": "slack", "event": "group_dm", "scope": ["alice", "bob"]},
        {
            "provider": "github",
            "event": "review",
            "scope": ["octo-org/octo-repo"],
            "target": "@octocat",
            "conditions": [{"field": "label", "operator": "is_not", "value": "draft"}],
        },
        {"provider": "pagerduty", "scope": ["Payments"], "scope_match": "contains"},
        {
            "provider": "linear",
            "scope": ["ENG"],
            "session_per_match": False,
            "worker": {"model": "model-b", "placement": "managed"},
        },
    ],
    ids=["jira", "slack-group-dm", "github-review", "pagerduty", "linear-task-session"],
)
def test_view_round_trips_through_create(args: dict[str, Any]) -> None:
    """What the list reports can be passed back to create the same trigger."""
    config = _created(**args)
    view = triggered_task_view({"id": "t1", "name": "n"}, config)
    trigger = {
        key: view[key]
        for key in (
            "provider",
            "event",
            "scope",
            "scope_match",
            "target",
            "conditions",
            "link_back",
            "session_per_match",
        )
        if key in view
    }
    worker = {key: value for key, value in view["worker"].items() if value is not None}
    rebuilt = _created(instructions=view["instructions"], worker=worker, **trigger)
    # Passing link_back explicitly marks it as the owner's choice; nothing else changes.
    rebuilt.pop("linkBackTouched", None)
    config.pop("linkBackTouched", None)
    assert rebuilt == config


def test_view_reports_the_task_without_its_schedule() -> None:
    config = _created(provider="jira", scope=["ES"])
    view = triggered_task_view(
        {
            "id": "t1",
            "name": "Jira tickets",
            "state": "active",
            "prompt": "rendered",
            "rrule": "FREQ=MINUTELY;INTERVAL=10",
            "timezone": "UTC",
            "next_run_at": 1,
            "session_mode": "continue",
            "session_id": "conv_watch",
            "agent_id": "ag_example",
            "last_run_at": 5,
            "last_run_status": "succeeded",
        },
        config,
    )
    assert view["triggered_task_id"] == "t1"
    assert (view["last_checked_at"], view["last_check_status"]) == (5, "succeeded")
    assert view["watch_session_id"] == "conv_watch"
    assert not {"prompt", "rrule", "timezone", "next_run_at", "session_mode"} & set(view)


@pytest.mark.parametrize(
    ("rrule", "minutes"),
    [
        ("FREQ=MINUTELY;INTERVAL=10", 10),
        ("FREQ=MINUTELY;INTERVAL=90", 90),
        ("FREQ=HOURLY;BYMINUTE=0", 60),
        ("FREQ=MINUTELY;INTERVAL=10;COUNT=3", 60),
        (None, 60),
    ],
)
def test_polling_minutes_matches_the_form(rrule: str | None, minutes: int) -> None:
    assert polling_minutes(rrule) == minutes


@pytest.mark.parametrize(
    ("info", "rrule"),
    [
        (
            {
                "features": {"automations_v2": True},
                "scheduled_task_subhour": True,
                "automation_trigger_poll_minutes": 15,
            },
            "FREQ=MINUTELY;INTERVAL=15",
        ),
        (
            {"features": {"automations_v2": True}, "automation_trigger_poll_minutes": 120},
            "FREQ=MINUTELY;INTERVAL=120",
        ),
        (
            {"features": {"automations_v2": True}, "automation_trigger_poll_minutes": 10},
            "FREQ=HOURLY;BYMINUTE=0",
        ),
        ({"scheduled_task_subhour": True}, "FREQ=HOURLY;BYMINUTE=0"),
        # Like the form, a value outside 10-240 minutes means the default.
        (
            {
                "features": {"automations_v2": True},
                "scheduled_task_subhour": True,
                "automation_trigger_poll_minutes": 5,
            },
            "FREQ=MINUTELY;INTERVAL=10",
        ),
        (
            {
                "features": {"automations_v2": True},
                "scheduled_task_subhour": True,
                "automation_trigger_poll_minutes": 300,
            },
            "FREQ=MINUTELY;INTERVAL=10",
        ),
    ],
    ids=["subhour", "hours", "no-subhour", "no-database-scheduler", "too-often", "too-rarely"],
)
def test_trigger_rrule_matches_the_form_default(info: dict[str, Any], rrule: str) -> None:
    assert trigger_rrule(info) == rrule


_REVIEWED = {"instructions": "x", "permission_mode": None}
_NAMED = {**_REVIEWED, "name": "n", "agent_id": "ag_1"}


@pytest.mark.parametrize(
    ("args", "action", "error"),
    [
        ({"permission_mode": None}, "create", "Missing review fields (instructions)"),
        ({"instructions": "x"}, "update", "Missing review fields (permission_mode)"),
        ({"instructions": "x", "permission_mode": "null"}, "create", "permission_mode must"),
        (_REVIEWED, "update", "triggered_task_id"),
        # The runner can't address these, so they are refused before anyone is asked.
        ({**_REVIEWED, "triggered_task_id": "../x"}, "update", "triggered_task_id"),
        ({**_REVIEWED, "triggered_task_id": ["t1"]}, "update", "triggered_task_id"),
        (_REVIEWED, "create", "name must be a non-empty string"),
        ({**_REVIEWED, "name": "n"}, "create", "agent_id must name the agent"),
        (
            {**_REVIEWED, "triggered_task_id": "t1", "agent_id": " "},
            "update",
            "agent_id must name the agent",
        ),
        # Harnesses don't all enforce the schema, and a name can hold anything.
        ({**_NAMED, "x\nApprove?": 1}, "create", 'unknown arguments: "x\\nApprove?"'),
        ({**_REVIEWED, "triggered_task_id": "t1", "rrule": "x"}, "update", "unknown arguments"),
        ({**_REVIEWED, "name": " "}, "create", "name must be a non-empty string"),
        (
            {**_REVIEWED, "triggered_task_id": "t1", "name": ""},
            "update",
            "name must be a non-empty string",
        ),
        (
            {**_NAMED, "project_name": "p", "project_id": "q"},
            "create",
            "not both",
        ),
        ({**_NAMED, "project_name": 3}, "create", "project_name must"),
        (
            {**_NAMED, "execution_target": "managed_sandbox", "host_id": "h1"},
            "create",
            "omit host_id",
        ),
        (
            {
                **_REVIEWED,
                "triggered_task_id": "t1",
                "execution_target": "connected_host",
                "workspace": None,
            },
            "update",
            "restores the default only for a managed_sandbox task",
        ),
        (
            {**_REVIEWED, "triggered_task_id": "t1", "conditions": []},
            "update",
            "pass the whole search: add provider, event, scope",
        ),
        ({**_NAMED, "instructions": " "}, "create", "instructions must be"),
        ({**_NAMED, "link_back": "yes"}, "create", "link_back must be"),
        (
            {**_NAMED, "worker": {"permission_mode": "null"}},
            "create",
            "is not a permission mode",
        ),
        (
            {**_REVIEWED, "triggered_task_id": "t1", "session_per_match": 1},
            "update",
            "session_per_match must be",
        ),
        (
            {**_REVIEWED, "triggered_task_id": "t1", "worker": {"permission_mode": "bypass"}},
            "update",
            "skips approval",
        ),
    ],
)
def test_call_error(args: dict[str, Any], action: str, error: str) -> None:
    assert error in (triggered_task_call_error(args, action=action) or "")


@pytest.mark.parametrize(
    ("args", "action"),
    [
        ({**_REVIEWED, "triggered_task_id": "t1"}, "update"),
        ({**_NAMED, "project_id": None}, "create"),
    ],
)
def test_call_error_accepts_complete_values(args: dict[str, Any], action: str) -> None:
    assert triggered_task_call_error(args, action=action) is None


def test_review_json_keeps_every_line_break_inside_its_value() -> None:
    breaks = [chr(code) for code in range(0x110000) if len(f"a{chr(code)}b".splitlines()) > 1]
    assert all(len(review_json(f"a{char}b").splitlines()) == 1 for char in breaks)


def test_review_json_escapes_what_could_lay_out_as_other_lines() -> None:
    value = {"scope": "ES\u2028Approve?", "name": "r\u00e9sum\u00e9 \u202eevil"}
    encoded = review_json(value)
    assert "\u2028" not in encoded and "\u202e" not in encoded
    assert "\\u2028" in encoded and "r\u00e9sum\u00e9" in encoded
    assert json.loads(encoded) == value


def test_describe_trigger_arguments_shows_every_value_in_full() -> None:
    jql = 'summary ~ "crash" AND ' + "labels = x AND " * 40 + "END"
    lines = describe_trigger_arguments(
        {
            "provider": "jira",
            "scope": ["ES"],
            "conditions": [{"field": "jql", "operator": "matches", "value": jql}],
            "instructions": "not a trigger argument",
        }
    )
    assert lines[0] == 'provider: "jira"'
    assert lines[1] == 'scope: ["ES"]'
    assert lines[2] == "conditions (all must match):"
    # JSON-encoded, so quotes and line breaks stay inside the value.
    assert lines[3].endswith('END"}') and '\\"crash\\"' in lines[3]
    assert len(lines) == 4
