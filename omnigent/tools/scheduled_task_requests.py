"""Request identity and honest errors shared by native relays and the runner."""

from __future__ import annotations

import uuid

from omnigent.util.json_types import JsonObject

MUTATION_TOOLS = frozenset({"sys_scheduled_task_create", "sys_scheduled_task_update"})


def mutation_arguments(name: str, arguments: JsonObject) -> JsonObject:
    """Allocate identity before dispatch, preserving the caller's ID on retry."""
    if name not in MUTATION_TOOLS:
        return arguments
    request_id = arguments.get("request_id")
    if request_id is None and "request_id" not in arguments:
        request_id = uuid.uuid4().hex
    if not isinstance(request_id, str):
        raise ValueError("request_id must be a UUID string")
    try:
        request_id = uuid.UUID(request_id).hex
    except ValueError as exc:
        raise ValueError("request_id must be a UUID string") from exc
    return {**arguments, "request_id": request_id}


def mutation_failure(
    name: str,
    arguments: JsonObject,
    message: str,
    *,
    outcome: str = "unknown",
) -> JsonObject:
    """Never interpret a timeout or cancellation as proof of rollback."""
    result: JsonObject = {"error": message}
    if name in MUTATION_TOOLS:
        result.update(outcome=outcome)
        request_id = arguments.get("request_id")
        if isinstance(request_id, str):
            result.update(
                request_id=request_id,
                reconciliation={"tool": "sys_scheduled_task_list", "request_id": request_id},
                retry="Reuse this request_id and the same inputs. A new ID can create a duplicate "
                "task or reapply an older update. Cancellation cannot undo a committed write.",
            )
    return result
