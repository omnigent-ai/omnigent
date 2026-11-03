"""Client-declared launch context for joining server audit requests."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import click

from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER

_LAUNCH_HEADERS = {
    "X-Omnigent-Launch-Id": "client_launch_id",
    "X-Omnigent-Launch-Mode": "client_launch_mode",
    "X-Omnigent-Launch-Command": "client_launch_command",
    "X-Omnigent-Caller-Host-Id": "client_host_id",
}


def launch_audit_headers(*, host_id: str, fresh: bool) -> dict[str, str]:
    """Identify one launch attempt without capturing CLI arguments or prompts."""
    context = click.get_current_context(silent=True)
    command = (context.command.name if context else None) or "unknown"
    if len(command) > 256 or not command.isascii() or not command.isprintable():
        command = "unknown"
    return {
        "X-Omnigent-Launch-Id": uuid.uuid4().hex,
        "X-Omnigent-Launch-Mode": "fresh" if fresh else "ensure_existing",
        "X-Omnigent-Launch-Command": command,
        "X-Omnigent-Caller-Host-Id": host_id,
    }


def request_client_audit_attributes(headers: Mapping[str, str]) -> dict[str, str]:
    """Read bounded diagnostic hints; they are never authentication or routing inputs."""
    normalized = {key.lower(): value for key, value in headers.items()}
    fields = {
        **_LAUNCH_HEADERS,
        "User-Agent": "client_user_agent",
        OMNIGENT_SLICE_KEY_HEADER: "request_slice_key",
    }
    return {
        attribute: value
        for header, attribute in fields.items()
        if (value := normalized.get(header.lower()))
        and len(value) <= 256
        and value.isascii()
        and value.isprintable()
    }
