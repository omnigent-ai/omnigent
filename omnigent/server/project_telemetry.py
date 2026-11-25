"""Product telemetry derived from data Omnigent already keeps: how people work in a project.

Every figure is computed on request from sessions, their messages, ticket activity
and roles, over the Summary's UTC windows. Nothing is recorded for telemetry here.

- **People** are signed-in users (``"local"`` in single-user mode); agent activity
  is counted separately, never as a person.
- **Sessions started by people** are top-level sessions that aren't the Lead's, a
  role's, a side chat, an agent worker's or a sub-agent, credited to their owner.
- **Messages to the Lead or a role** come from the usage log, which records a
  message at the moment a person sends it. Stored messages can't tell that: the
  server's own notices are stored the same way as a person's words. With the log
  off, these counts are ``None`` (not measured).
- **Roles by the Lead vs by hand:** a role an agent created records its session;
  the owner's records none. Deleted roles aren't counted.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationLabel,
    SqlConversationMetadata,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.project_status import (
    MemberKey,
    StatusPeriod,
)
from omnigent.util.uuid import uuid_to_bytes

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.entities.project import ProjectIdentity
    from omnigent.stores import ConversationStore, PermissionStore

SessionKind = Literal["personal", "lead", "role", "side_chat", "worker", "sub_agent"]
#: A ticket in a working stage with no activity for this long counts as stalled.
STALLED_AFTER_SECONDS = 3 * 86_400
#: An edit this long after a role's creation counts as adapting it.
ROLE_EDIT_GRACE_SECONDS = 60


@dataclass(frozen=True)
class ProjectSession:
    """A project session's facts telemetry needs.

    :param side_chat_of: For a side chat, the kind of session it's a side chat of.
    """

    id: str
    created_at: int
    kind: SessionKind
    owner_user_id: str | None
    side_chat_of: SessionKind | None = None


@dataclass(frozen=True)
class PersonMessage:
    """A message a person wrote, in a Lead, role or side-chat session."""

    session_id: str
    user_id: str
    created_at: int


@dataclass
class PersonRow:
    """One person's activity in the window."""

    user_id: str
    sessions_started: int = 0
    tickets_filed: int = 0
    #: ``None`` while people's messages aren't measured.
    messages_to_lead: int | None = 0
    messages_to_roles: int | None = 0
    side_chats_opened: int = 0
    ticket_comments: int = 0


@dataclass(frozen=True)
class TelemetryInputs:
    """What one project's telemetry is computed from; readers run lazily."""

    project: Project
    period: StatusPeriod
    sessions: Callable[[], list[ProjectSession]]
    #: ``None`` when people's messages aren't measured.
    person_messages: Callable[[list[ProjectSession]], list[PersonMessage] | None]
    activity: Callable[[], list[dict[str, Any]]]
    identities: Callable[[], list[ProjectIdentity]]
    member_of_move: Callable[[dict[str, Any]], MemberKey]
    extra: dict[str, Any] = field(default_factory=dict)


def classify_session(
    project: Project,
    session_id: str,
    *,
    root_id: str | None,
    family_id: str | None,
    labels: dict[str, str],
) -> SessionKind:
    """What a project session is: the Lead's, a role's, a worker's, a sub-agent or a person's."""
    if root_id not in (None, session_id):
        return "sub_agent"
    if session_id == project.main_session_id or session_id in project.retired_main_session_ids:
        return "lead"
    if labels.get(PROJECT_IDENTITY_LABEL_KEY):
        return "role"
    if family_id is not None and family_id != session_id:
        return "worker"
    return "personal"


def _person(user_id: str | None, project: Project) -> str:
    return user_id or project.user_id or "local"


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def compute_telemetry(inputs: TelemetryInputs) -> dict[str, Any]:
    """Every derived KPI for one project and window, with per-person rows."""
    project, period = inputs.project, inputs.period
    in_window = period.contains
    sessions = inputs.sessions()
    people: dict[str, PersonRow] = {}

    def row(user_id: str) -> PersonRow:
        return people.setdefault(user_id, PersonRow(user_id))

    started = [s for s in sessions if s.kind == "personal" and in_window(s.created_at)]
    for session in started:
        row(_person(session.owner_user_id, project)).sessions_started += 1
    side_chats = [s for s in sessions if s.kind == "side_chat" and in_window(s.created_at)]
    for session in side_chats:
        row(_person(session.owner_user_id, project)).side_chats_opened += 1

    filed_direct = filed_from_chat = filed_by_agents = 0
    comments = 0
    for event in inputs.activity():
        if not in_window(event["created_at"]):
            continue
        member = inputs.member_of_move(event)
        is_person = member.startswith("user:")
        if event["kind"] == "created":
            if event["session_id"] is None:
                filed_direct += 1
            elif is_person:
                filed_from_chat += 1
            else:
                filed_by_agents += 1
            if is_person:
                row(member.removeprefix("user:")).tickets_filed += 1
        elif event["session_id"] is None and event["user_id"] is not None:
            comments += 1
            row(event["user_id"]).ticket_comments += 1

    messages = inputs.person_messages(sessions)
    kind_of = {s.id: (s.side_chat_of if s.kind == "side_chat" else s.kind) for s in sessions}
    lead_messages = role_messages = None
    if messages is not None:
        lead_messages = role_messages = 0
        for message in messages:
            if not in_window(message.created_at):
                continue
            target = kind_of.get(message.session_id)
            if target == "lead":
                lead_messages += 1
                row(message.user_id).messages_to_lead += 1
            elif target == "role":
                role_messages += 1
                row(message.user_id).messages_to_roles += 1

    def chatted(person: PersonRow) -> bool:
        return bool(
            person.messages_to_lead or person.messages_to_roles or person.side_chats_opened
        )

    rows = sorted(people.values(), key=lambda person: person.user_id)
    if messages is None:
        for person in rows:
            person.messages_to_lead = person.messages_to_roles = None
    mix = {"session_first": 0, "ticket_first": 0, "both": 0}
    for person in rows:
        if person.sessions_started and person.tickets_filed:
            mix["both"] += 1
        elif person.sessions_started:
            mix["session_first"] += 1
        elif person.tickets_filed:
            mix["ticket_first"] += 1

    identities = inputs.identities()
    by_lead = [identity for identity in identities if identity.created_by_session_id]
    edited_hours = [
        (identity.updated_at - identity.created_at) / 3600
        for identity in identities
        if identity.updated_at
        and identity.updated_at - identity.created_at > ROLE_EDIT_GRACE_SECONDS
    ]
    created_in_window = [identity for identity in identities if in_window(identity.created_at)]
    return {
        "work_start": {
            "sessions_started_by_people": len(started),
            "tickets_filed_directly": filed_direct,
            "tickets_filed_from_a_chat": filed_from_chat,
            "tickets_filed_by_agents": filed_by_agents,
            "people": mix,
        },
        "role_interaction": {
            "messages_to_lead": lead_messages,
            "messages_to_roles": role_messages,
            "people_messaging_roles": None
            if messages is None
            else sum(1 for person in rows if person.messages_to_roles),
            "side_chats_opened": len(side_chats),
            "side_chats_with_roles": sum(1 for s in side_chats if s.side_chat_of == "role"),
            "ticket_comments_by_people": comments,
            "people_ticket_only": None
            if messages is None
            else sum(1 for person in rows if person.ticket_comments and not chatted(person)),
        },
        "team_building": {
            "roles": len(identities),
            "roles_created_by_lead": len(by_lead),
            "roles_created_by_hand": len(identities) - len(by_lead),
            "roles_created_in_window": len(created_in_window),
            "roles_created_in_window_by_lead": sum(
                1 for identity in created_in_window if identity.created_by_session_id
            ),
            "roles_edited_after_creation": len(edited_hours),
            "median_hours_to_last_role_edit": _median(edited_hours),
        },
        "people_rows": [vars(person) for person in rows],
        **inputs.extra,
    }


def add_usage_log_counts(
    result: dict[str, Any],
    events: Iterable[Any],
    sessions: Iterable[ProjectSession],
    period: StatusPeriod,
    measured_since: int | None,
) -> None:
    """Add the usage log's figures: sessions by client and role edits in the window."""
    personal = {session.id for session in sessions if session.kind == "personal"}
    by_client: dict[str, int] = {}
    edits = 0
    edited: set[str] = set()
    for event in events:
        if not period.contains(event.created_at):
            continue
        if event.name == "session.started" and event.session_id in personal:
            client = event.surface or "unknown"
            by_client[client] = by_client.get(client, 0) + 1
        elif event.name == "role.updated":
            edits += 1
            if event.subject_id:
                edited.add(event.subject_id)
    result["work_start"]["sessions_by_client"] = dict(sorted(by_client.items()))
    result["role_interaction"]["messages_measured_since"] = measured_since
    result["team_building"]["role_edits_in_window"] = edits
    result["team_building"]["roles_edited_in_window"] = len(edited)


def review_rounds_and_stalled(
    histories: dict[str, list[dict[str, Any]]],
    stages: list[dict[str, Any]],
    stage_ages: Iterable[tuple[str, str | None, int]],
    period: StatusPeriod,
) -> dict[str, Any]:
    """Moves into a review stage per shipped ticket, and tickets stalled in a working stage."""
    review_ids = {
        str(stage["id"])
        for stage in stages
        if "review" in str(stage["id"]).lower() or "review" in str(stage.get("name", "")).lower()
    }
    complete_ids = {str(stage["id"]) for stage in stages if stage.get("is_complete")}
    first_stage = str(stages[0]["id"]) if stages else None
    rounds: list[int] = []
    for history in histories.values():
        shipped = [
            entry
            for entry in history
            if entry["is_complete"] and not entry.get("created") and period.contains(entry["at"])
        ]
        if shipped:
            rounds.append(
                sum(
                    1
                    for entry in history
                    if entry["stage_id"] in review_ids and not entry.get("created")
                )
            )
    stalled = sum(
        1
        for _, stage_id, updated_at in stage_ages
        if stage_id not in complete_ids
        and stage_id != first_stage
        and period.until_epoch - updated_at >= STALLED_AFTER_SECONDS
    )
    return {
        "review_rounds_per_shipped_ticket": round(sum(rounds) / len(rounds), 2)
        if rounds and review_ids
        else None,
        "stalled_tickets": stalled,
    }


class ProjectTelemetryReader:
    """Reads a project's sessions and people's messages for :func:`compute_telemetry`."""

    def __init__(
        self, conversations: ConversationStore, permissions: PermissionStore | None
    ) -> None:
        location = conversations.storage_location
        items_location = getattr(conversations, "conversation_storage_location", None) or location
        self._metadata_db = make_named_managed_session_maker(
            get_or_create_engine(location), query_name_prefix="omnigent.project_telemetry"
        )
        self._items_db = make_named_managed_session_maker(
            get_or_create_engine(items_location), query_name_prefix="omnigent.project_telemetry"
        )
        self._permissions = permissions

    def sessions(self, project: Project) -> list[ProjectSession]:
        """The project's sessions, classified, each with its owner."""
        project_hex = uuid_to_bytes(project.id).hex()
        with self._metadata_db("list_project_sessions_for_telemetry") as session:
            meta = session.execute(
                select(
                    SqlConversationMetadata.id,
                    SqlConversationMetadata.family_id,
                    SqlConversationMetadata.side_chat_parent_id,
                ).where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.project_id == project_hex,
                )
            ).all()
        ids = [row[0] for row in meta]
        if not ids:
            return []
        with self._items_db("list_project_session_rows_for_telemetry") as session:
            conversations = {
                row[0]: row
                for row in session.execute(
                    select(
                        SqlConversation.id,
                        SqlConversation.created_at,
                        SqlConversation.root_conversation_id,
                    ).where(
                        SqlConversation.workspace_id == current_workspace_id(),
                        SqlConversation.id.in_(ids),
                    )
                )
            }
            identity_labels = dict(
                session.execute(
                    select(SqlConversationLabel.conversation_id, SqlConversationLabel.value).where(
                        SqlConversationLabel.workspace_id == current_workspace_id(),
                        SqlConversationLabel.conversation_id.in_(ids),
                        SqlConversationLabel.key == PROJECT_IDENTITY_LABEL_KEY,
                    )
                ).all()
            )
        owners = self._owners(ids)
        kinds: dict[str, SessionKind] = {}
        parents: dict[str, str] = {}
        for session_id, family_id, side_chat_parent_id in meta:
            row = conversations.get(session_id)
            if row is None:
                continue
            labels = (
                {PROJECT_IDENTITY_LABEL_KEY: identity_labels[session_id]}
                if session_id in identity_labels
                else {}
            )
            kinds[session_id] = classify_session(
                project, session_id, root_id=row[2], family_id=family_id, labels=labels
            )
            if side_chat_parent_id:
                parents[session_id] = side_chat_parent_id
        result = []
        for session_id, kind in kinds.items():
            parent = parents.get(session_id)
            result.append(
                ProjectSession(
                    id=session_id,
                    created_at=conversations[session_id][1],
                    kind="side_chat" if parent else kind,
                    owner_user_id=owners.get(session_id),
                    side_chat_of=kinds.get(parent, "personal") if parent else None,
                )
            )
        return result

    def _owners(self, session_ids: list[str]) -> dict[str, str]:
        if self._permissions is None:
            return {}
        grants = self._permissions.list_for_sessions(session_ids)
        owners: dict[str, str] = {}
        for session_id, entries in grants.items():
            owner = next((grant.user_id for grant in entries if grant.level >= LEVEL_OWNER), None)
            if owner is not None:
                owners[session_id] = owner
        return owners


def visible_people(
    rows: list[dict[str, Any]], *, viewer: str | None, owner: bool, agent: bool
) -> list[dict[str, Any]] | None:
    """Per-person rows the viewer may see: all for the owner, a member's own, none for agents."""
    if agent:
        return None
    if owner:
        return rows
    return [row for row in rows if row["user_id"] == (viewer or "local")]
