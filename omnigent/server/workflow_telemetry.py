"""Content-free workflow adoption and execution facts in the structured debug log."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging

from omnigent.debug_logging import debug_event
from omnigent.entities import Conversation
from omnigent.entities.session_journal import JournalEntry
from omnigent.entities.workflow import Workflow
from omnigent.server.journal_markers import plan_change
from omnigent.server.session_provenance import SessionProvenance, session_provenance
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
)

_logger = logging.getLogger(__name__)


def log_workflow_saved(
    workflow: Workflow, *, created: bool, plan_saved: bool, source_session_id: str | None
) -> None:
    """Count persisted workflows and saves separately: an empty draft isn't a saved plan.

    Saves can repeat on edits or retries. Adoption counts distinct workflow IDs,
    using their first save; no read-before-write race decides that transition.
    """
    attributes = {
        "workflow_id": workflow.id,
        "workflow_created_at": workflow.created_at,
        "step_count": len(workflow.steps),
        "has_plan": bool(workflow.steps),
        "creation_source": "agent" if source_session_id else "api",
    }
    with contextlib.suppress(Exception):
        for event in (["workflow_created"] if created else []) + (
            ["workflow_saved"] if plan_saved and workflow.steps else []
        ):
            _logger.info(
                "Workflow persisted",
                extra=debug_event(
                    event,
                    session_id=source_session_id,
                    turn_id=None,
                    user_id=workflow.user_id,
                    **attributes,
                ),
            )


def _step_key(session_id: str, step: str) -> str:
    # Stable across replans in this session, without logging agent-authored step IDs.
    return hashlib.sha256(f"{session_id}:{step}".encode()).hexdigest()[:32]


def log_journal_entry(
    conversations: ConversationStore,
    session: Conversation,
    entry: JournalEntry,
    previous_plan: JournalEntry | None,
    *,
    user_id: str | None,
) -> None:
    """Observe a newly committed plan or one of its steps, never a retried append.

    ``previous_plan`` is read before the entry's position, not the current head:
    another append may already have replaced it by the time telemetry runs.
    """
    plan = entry if entry.kind == "plan" else previous_plan
    if plan is None or entry.kind not in {"plan", "status"}:
        return
    steps = plan.payload["steps"]
    step_ids = [step["id"] for step in steps]
    if entry.kind == "status" and entry.step not in step_ids:
        return
    with contextlib.suppress(Exception):
        provenance = SessionProvenance("unknown")
        # A failed optional lookup must not lose the committed workflow fact.
        with contextlib.suppress(Exception):
            provenance = session_provenance(conversations, session)
        automation = provenance.automation_session
        attributes: dict[str, object] = {
            "journal_entry_id": entry.id,
            "journal_position": entry.position,
            "entry_created_at": entry.created_at,
            "plan_id": plan.id,
            "plan_version": plan.payload["version"],
            "plan_created_at": plan.created_at,
            "workflow_id": plan.workflow_id,
            "workflow_kind": "saved" if plan.workflow_id else "ad_hoc",
            "execution_source": provenance.execution_source,
            "root_session_id": session.root_conversation_id,
            "parent_session_id": session.parent_conversation_id,
            "created_by_session_id": session.labels.get(CREATED_BY_SESSION_LABEL_KEY),
            "automation_session_id": automation.id if automation else None,
            "task_id": automation.labels.get(SCHEDULED_TASK_ORIGIN_LABEL_KEY)
            if automation
            else None,
            # An ancestor's *current* run can be newer than the worker it created.
            "scheduled_task_run_id": entry.scheduled_task_run_id,
            "step_count": len(steps),
        }
        if entry.kind == "plan":
            change = plan_change(previous_plan.payload if previous_plan else None, plan.payload)
            attributes.update(
                previous_plan_id=previous_plan.id if previous_plan else None,
                plan_change=change["change"]
                if change
                else "association_changed"
                if previous_plan and previous_plan.workflow_id != plan.workflow_id
                else "unchanged",
                step_keys=json.dumps([_step_key(session.id, step) for step in step_ids]),
            )
            event = "workflow_plan_written"
        else:
            assert entry.step is not None
            attributes.update(
                step_key=_step_key(session.id, entry.step),
                step_index=step_ids.index(entry.step),
                status=entry.payload["status"],
                plan_elapsed_seconds=max(0, entry.created_at - plan.created_at),
            )
            event = "workflow_step_status_written"
        _logger.info(
            "Workflow journal entry persisted",
            extra=debug_event(
                event, session_id=session.id, turn_id=None, user_id=user_id, **attributes
            ),
        )
