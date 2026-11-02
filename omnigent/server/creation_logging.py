"""Debug-log contract for server-received session creation requests."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Literal

from omnigent.debug_logging import (
    add_audit_attrs,
    current_request_audit_attrs,
    debug_event,
    set_current_runner_id,
    set_current_session_id,
)
from omnigent.server.session_metadata_logging import harness_attributes

_logger = logging.getLogger("omnigent.server.creation")

CreationStageAttribute = Literal[
    "create_persistence_ms",
    "create_identity_ms",
    "create_acl_ms",
]


def _safe_identifier(value: str, *, maximum: int = 64) -> str:
    return value if value.isascii() and value.isidentifier() and len(value) <= maximum else "other"


def record_creation_failure(
    exc: BaseException, *, validation_errors: Sequence[Mapping[str, object]] = ()
) -> None:
    """Retain bounded failure location and schema fields without exception text or input."""
    with contextlib.suppress(Exception):
        attributes = current_request_audit_attrs()
        if "creation_phase" not in attributes:
            return
        if "creation_failure_phase" not in attributes:
            site = "external"
            traceback = exc.__traceback__
            while traceback is not None:
                frame = traceback.tb_frame
                module = frame.f_globals.get("__name__", "")
                if (
                    isinstance(module, str)
                    and module.startswith("omnigent.")
                    and len(module) <= 160
                    and all(_safe_identifier(part) == part for part in module.split("."))
                ):
                    site = (
                        f"{module}:{_safe_identifier(frame.f_code.co_name)}:{traceback.tb_lineno}"
                    )
                traceback = traceback.tb_next
            phase = attributes["creation_phase"]
            add_audit_attrs(
                creation_failure_phase=phase
                if phase
                in {
                    "request_validation",
                    "validation",
                    "persistence",
                    "identity",
                    "acl",
                    "post_persistence",
                }
                else "unknown",
                creation_exception_type=_safe_identifier(type(exc).__name__),
                creation_cause_type=_safe_identifier(type(exc.__cause__).__name__)
                if exc.__cause__
                else None,
                creation_failure_site=site,
            )
        if validation_errors:
            from omnigent.server.schemas import SessionCreateRequest

            fields: set[str] = set()
            kinds: set[str] = set()
            for error in validation_errors[:32]:
                location = error.get("loc")
                if isinstance(location, (list, tuple)):
                    fields.update(
                        value
                        for value in location
                        if isinstance(value, str) and value in SessionCreateRequest.model_fields
                    )
                kind = error.get("type")
                kinds.add(
                    kind
                    if isinstance(kind, str)
                    and kind
                    in {
                        "missing",
                        "string_type",
                        "int_type",
                        "int_parsing",
                        "bool_type",
                        "literal_error",
                        "extra_forbidden",
                        "enum",
                        "value_error",
                        "json_invalid",
                        "union_tag_invalid",
                        "union_tag_not_found",
                    }
                    else "other"
                )
            add_audit_attrs(
                creation_validation_fields=",".join(sorted(fields)[:8]) or "other",
                creation_validation_types=",".join(sorted(kinds)[:8]),
                creation_validation_count=len(validation_errors),
            )


@contextlib.contextmanager
def creation_stage(attribute: CreationStageAttribute) -> Iterator[None]:
    """Accumulate one create-stage duration on the request audit row.

    A stage may occur more than once in one request (for example, the
    conversation insert and a post-initial-items refresh are both persistence
    work), so repeated measurements add to the existing value.

    :param attribute: Bounded audit attribute that receives elapsed
        milliseconds.
    """
    started_at = time.perf_counter()
    previous_phase = current_request_audit_attrs().get("creation_phase", "validation")
    add_audit_attrs(creation_phase=attribute.removeprefix("create_").removesuffix("_ms"))
    try:
        yield
    except BaseException as exc:
        record_creation_failure(exc)
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        existing = current_request_audit_attrs().get(attribute)
        total_ms = elapsed_ms + (float(existing) if existing is not None else 0.0)
        add_audit_attrs(**{attribute: total_ms})
        add_audit_attrs(creation_phase=previous_phase)


def creation_metadata(*, parent_session_id: str | None, host_type: str) -> None:
    """Classify validated creates before persistence or launch can fail."""
    add_audit_attrs(
        creation_kind="child" if parent_session_id else "top_level",
        host_type=host_type,
        creation_phase="validation",
    )


def session_created(
    session_id: str,
    runner_id: str | None = None,
    parent_session_id: str | None = None,
    *,
    agent_id: str | None = None,
    harness: str | None = None,
) -> None:
    """Publish the request-to-session link immediately after persistence.

    ``parent_session_id`` is the spawning parent for a sub-agent child (``None``
    for a top-level session); logging it on the creation row is what lets a
    debug-log query join a child back to its parent session.
    """
    set_current_session_id(session_id)
    set_current_runner_id(runner_id)
    add_audit_attrs(session_id=session_id, runner_id=runner_id, creation_phase="post_persistence")
    _logger.info(
        "Session persisted",
        extra=debug_event(
            "session_created",
            session_id=session_id,
            runner_id=runner_id,
            agent_id=agent_id,
            session_kind="sub_agent" if parent_session_id else "default",
            parent_session_id=parent_session_id,
            creation_kind=current_request_audit_attrs().get("creation_kind", "unknown"),
            **harness_attributes(harness, source="create_selection"),
        ),
    )
