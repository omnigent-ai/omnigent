"""Content-free observations at native failed-status intake boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from omnigent.runner.transports.ws_tunnel.registry import RunnerSession, TunnelRegistry


def _field_state(data: Mapping[str, object], key: str, *, mapping: bool = False) -> str:
    if key not in data:
        return "missing"
    value = data[key]
    if value is None:
        return "null"
    if mapping:
        return ("present" if value else "empty") if isinstance(value, dict) else "invalid"
    if not isinstance(value, str):
        return "invalid"
    return "present" if value.strip() else "empty"


def bound_runner_connection(registry: object, runner_id: str | None) -> RunnerSession | None:
    """Observe the current binding without asserting it produced an HTTP status event."""
    if not isinstance(registry, TunnelRegistry) or runner_id is None:
        return None
    return registry.get(runner_id)


def failed_status_intake_attributes(
    data: Mapping[str, object],
    *,
    runner_id: str | None,
    connection: object,
    source: Literal["relay_stream", "bound_runner_at_intake"],
) -> dict[str, str]:
    """Record field availability and observed connection identity, never event content."""
    error = data.get("error")
    error_fields = error if isinstance(error, dict) else {}
    attributes = {
        "failure_intake_schema": "1",
        "failure_wire_error": _field_state(data, "error", mapping=True),
        "failure_wire_code": _field_state(error_fields, "code"),
        "failure_wire_message": _field_state(error_fields, "message"),
        "failure_wire_output": _field_state(data, "output"),
        "failure_wire_detail": _field_state(data, "failure_detail"),
        "failure_wire_context": _field_state(data, "failure_context", mapping=True),
        "failure_wire_response_id": _field_state(data, "response_id"),
        "failure_connection_source": source,
        "failure_connection_state": "unavailable" if runner_id else "no_runner_binding",
    }
    if isinstance(connection, RunnerSession):
        connection = {
            "runner_id": connection.runner_id,
            "runner_version": connection.hello.runner_version,
            "connection_id": connection.hello.connection_id,
            "generation": connection.generation,
        }
    if runner_id and isinstance(connection, dict) and connection.get("runner_id") == runner_id:
        attributes["failure_connection_state"] = "observed"
        for key in ("runner_version", "connection_id"):
            value = connection.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:+-]{1,128}", value):
                attributes[f"failure_{key}"] = value
        generation = connection.get("generation")
        if type(generation) is int and generation > 0:
            attributes["failure_connection_generation"] = str(generation)
    if runner_id and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", runner_id):
        attributes["failure_runner_id"] = runner_id
    return attributes
