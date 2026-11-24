"""Who receives a project's routed events: the owner's choice, the Lead, the first persona."""

from __future__ import annotations

from omnigent.entities import Project
from omnigent.entities.project import ProjectIdentity
from omnigent.server.project_router import LEAD_IDENTITY_ID, choose_router


def _project(**fields: object) -> Project:
    return Project(id="p", name="P", user_id=None, created_at=0, main_session_id="lead", **fields)


def _persona(identity_id: str, name: str) -> ProjectIdentity:
    return ProjectIdentity(
        id=identity_id,
        project_id="p",
        name=name,
        instructions="",
        created_at=0,
        active_session_id=f"{identity_id}-session",
    )


PM = _persona("pm", "PM")
QA = _persona("qa", "QA")


def test_router_falls_back_from_the_choice_to_the_lead_to_the_first_persona() -> None:
    cases = [
        (_project(), [PM, QA], (LEAD_IDENTITY_ID, "lead")),
        (_project(router_identity_id="qa"), [PM, QA], ("qa", "qa-session")),
        (_project(router_identity_id="gone"), [PM, QA], (LEAD_IDENTITY_ID, "lead")),
        (_project(lead_removed=True), [PM, QA], ("pm", "pm-session")),
        (_project(router_identity_id="qa", lead_removed=True), [PM, QA], ("qa", "qa-session")),
        (
            _project(router_identity_id=LEAD_IDENTITY_ID, lead_removed=True),
            [QA],
            ("qa", "qa-session"),
        ),
        # A project with no persona keeps its Lead even if it was removed.
        (_project(lead_removed=True), [], (LEAD_IDENTITY_ID, "lead")),
    ]
    for project, personas, expected in cases:
        choice = choose_router(project, personas)
        assert (choice.identity_id, choice.session_id) == expected, (project, personas)
