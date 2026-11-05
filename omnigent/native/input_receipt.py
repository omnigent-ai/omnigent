"""Versioned native-input receipt metadata, kept separate from diagnostics."""

from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from typing import Any

from omnigent.native.input_diagnostics import input_attributes
from omnigent.session_event_batch import MAX_SESSION_EVENT_REQUEST_BYTES

NATIVE_INPUT_RECEIPT_VERSION = 1
NATIVE_INPUT_RECEIPT_EVENT = "external_native_input_receipt"
_RECEIPT_IDENTITY_FIELDS = frozenset({"pending_id", "delivery_attempt_id", "input_stable_id"})
_receipt_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "native_input_receipt", default=None
)


def native_input_identity(source: Mapping[str, object]) -> dict[str, str] | None:
    """Validate a server-issued receipt identity without accepting partial IDs."""
    identity = {key: value for key, value in source.items() if key in _RECEIPT_IDENTITY_FIELDS}
    if not {"pending_id", "delivery_attempt_id"} <= identity.keys():
        return None
    validated = input_attributes(identity)
    if validated != identity or any(not isinstance(value, str) for value in validated.values()):
        return None
    return {key: str(value) for key, value in validated.items()}


def validate_native_input_receipt(value: object) -> dict[str, Any] | None:
    """Accept the exact versioned server contract, including original content blocks."""
    if not isinstance(value, dict) or set(value) != {"version", "identity", "content"}:
        return None
    if type(value["version"]) is not int or value["version"] != NATIVE_INPUT_RECEIPT_VERSION:
        return None
    raw_identity = value["identity"]
    if not isinstance(raw_identity, dict) or set(raw_identity) - _RECEIPT_IDENTITY_FIELDS:
        return None
    identity = native_input_identity(raw_identity)
    content = value["content"]
    if (
        identity is None
        or not isinstance(content, list)
        or not all(isinstance(block, dict) for block in content)
    ):
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        return None
    if len(encoded) > MAX_SESSION_EVENT_REQUEST_BYTES:
        return None
    return {
        "version": NATIVE_INPUT_RECEIPT_VERSION,
        "identity": identity,
        "content": copy.deepcopy(content),
    }


def build_native_input_receipt(
    content: object, identity: Mapping[str, object] | None
) -> dict[str, Any] | None:
    """Capture unresolved server content before attachments are hydrated."""
    if identity is None:
        return None
    validated_identity = native_input_identity(identity)
    if validated_identity is None:
        return None
    return validate_native_input_receipt(
        {
            "version": NATIVE_INPUT_RECEIPT_VERSION,
            "identity": validated_identity,
            "content": content,
        }
    )


@contextlib.contextmanager
def native_input_receipt_scope(value: object) -> Iterator[None]:
    """Bind one authored input without exposing it through content-free log attributes."""
    token = _receipt_context.set(validate_native_input_receipt(value))
    try:
        yield
    finally:
        _receipt_context.reset(token)


def current_native_input_receipt() -> dict[str, Any] | None:
    """Return an independent copy of the current input's receipt context."""
    return copy.deepcopy(_receipt_context.get())
