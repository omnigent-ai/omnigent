"""Gadget actions: declared requests that each viewer's gadget session carries out.

A revision may declare ``actions`` (a name, a description for the agent, and an optional
JSON Schema for ``params``). A viewer who can edit the gadget requests one from the gadget's
frontend; the request is queued in the gadget's action ledger and posted into that viewer's
gadget session, one long-lived agent session per viewer and gadget. The session coordinates:
it reads the ledger, handles each action inline or in a child session, and records progress
and the result here, so the gadget can show them and a later session can resume from them.
"""

from __future__ import annotations

import re
from typing import Any

import jsonschema

from omnigent.stores.gadget_store import InvalidInput

MAX_ACTIONS = 16
MAX_ACTION_DESCRIPTION = 2000
MAX_ACTION_SUMMARY = 4000
ACTION_STATES = ("queued", "running", "done", "failed", "cancelled")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def validate_actions(value: Any, validate_schema) -> list[dict]:
    """Normalize declared actions; ``validate_schema`` checks a params schema."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ACTIONS:
        raise InvalidInput(f"Actions must be a list of at most {MAX_ACTIONS} entries")
    actions: list[dict] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "description", "params"}:
            raise InvalidInput(
                "Each action has a name, a description and an optional params schema"
            )
        name, description = item.get("name"), item.get("description")
        if not isinstance(name, str) or _IDENTIFIER.fullmatch(name) is None or name in names:
            raise InvalidInput("Each action needs a unique name like run_card")
        names.add(name)
        if not isinstance(description, str) or not 0 < len(description) <= MAX_ACTION_DESCRIPTION:
            raise InvalidInput(
                f"Action {name} needs a description of at most {MAX_ACTION_DESCRIPTION} characters"
            )
        action: dict[str, Any] = {"name": name, "description": description}
        if item.get("params") is not None:
            validate_schema(item["params"])
            action["params"] = item["params"]
        actions.append(action)
    return actions


def validate_request(actions: list[dict], name: Any, params: Any) -> tuple[str, dict]:
    declared = next((action for action in actions if action["name"] == name), None)
    if declared is None:
        raise InvalidInput("This gadget does not declare that action")
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise InvalidInput("Action params must be an object")
    schema = declared.get("params")
    if schema is not None:
        error = jsonschema.exceptions.best_match(
            jsonschema.Draft202012Validator(schema).iter_errors(params)
        )
        if error is not None:
            where = "".join(f"[{part!r}]" for part in error.absolute_path)
            raise InvalidInput(
                f"Params do not match action {name} at params{where}: {error.message[:200]}"
            )
    return name, params


def validate_update(current: dict, body: dict) -> dict:
    """The ledger fields an agent may change; who asked and for what stays as requested."""
    if set(body) - {"state", "summary", "session_id"}:
        raise InvalidInput("Only state, summary and session_id can change")
    value = dict(current)
    if "state" in body:
        if body["state"] not in ACTION_STATES:
            raise InvalidInput(f"state must be one of {', '.join(ACTION_STATES)}")
        value["state"] = body["state"]
    if "summary" in body:
        summary = body["summary"]
        if summary is not None and (
            not isinstance(summary, str) or len(summary) > MAX_ACTION_SUMMARY
        ):
            raise InvalidInput(
                f"summary must be Markdown of at most {MAX_ACTION_SUMMARY} characters"
            )
        value["summary"] = summary
    if "session_id" in body:
        value["session_id"] = validate_session_id(body["session_id"], allow_none=True)
    return value


def validate_session_id(value: Any, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or _SESSION_ID.fullmatch(value) is None:
        raise InvalidInput("Invalid session ID")
    return value
