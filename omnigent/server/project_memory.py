"""Project memory: memory a project owns, with a shared area and one area per role.

A project's memory is one :class:`MemoryActor` of type ``project``. Inside it:

* ``/project/...`` is shared by everyone on the project;
* ``/identities/<identity id>/...`` belongs to one role, ``orchestrator`` for the Lead.

People who can edit the project write anywhere in that layout. An agent writes only
from a session holding one of the project's roles, and only to the shared area and its
own role's area. Everyone on the project, and every agent session filed in it, reads
all of it. The project and role always come from the URL and the calling session.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.memory_store import ACTOR_TYPE_PROJECT, MemoryActor, path_in_prefix

SHARED_AREA = "/project"
IDENTITIES_AREA = "/identities"


def project_memory_actor(project_id: str) -> MemoryActor:
    """The owner of *project_id*'s memory."""
    return MemoryActor(ACTOR_TYPE_PROJECT, project_id)


def identity_area(identity_id: str) -> str:
    """The area one role writes, e.g. ``/identities/orchestrator`` for the Lead."""
    return f"{IDENTITIES_AREA}/{identity_id}"


@dataclass(frozen=True)
class ProjectMemoryCaller:
    """Who is reading or writing a project's memory.

    :param project_id: The project whose memory it is.
    :param is_agent: Whether an agent session made the call.
    :param identity_id: The role the calling session holds, or ``None`` for a person
        or an agent session that holds no role.
    :param may_edit: Whether a person caller may change the project.
    """

    project_id: str
    is_agent: bool
    identity_id: str | None
    may_edit: bool

    @property
    def actor(self) -> MemoryActor:
        return project_memory_actor(self.project_id)

    def writable_areas(self) -> list[str]:
        """The areas this caller may write, shared area first."""
        if self.is_agent:
            if self.identity_id is None:
                return []
            return [SHARED_AREA, identity_area(self.identity_id)]
        return [SHARED_AREA, IDENTITIES_AREA] if self.may_edit else []


def _in_layout(path: str) -> bool:
    """Whether *path* is in the shared area or inside one role's area."""
    if path_in_prefix(path, SHARED_AREA):
        return True
    return path.startswith(IDENTITIES_AREA + "/") and path.count("/") >= 2


def require_project_memory_write(caller: ProjectMemoryCaller, path: str) -> None:
    """Refuse a write or delete *caller* may not make at *path*.

    :raises OmnigentError: ``INVALID_INPUT`` outside the layout, ``FORBIDDEN`` when
        the caller may not write there.
    """
    if not _in_layout(path):
        raise OmnigentError(
            f"Project memory paths start with {SHARED_AREA}/ or {IDENTITIES_AREA}/<role>/",
            code=ErrorCode.INVALID_INPUT,
        )
    areas = caller.writable_areas()
    if any(path_in_prefix(path, area) for area in areas):
        return
    if caller.is_agent and caller.identity_id is None:
        message = "Only a session holding one of the project's roles writes project memory"
    elif caller.is_agent:
        message = (
            f"A role writes only {SHARED_AREA}/ and its own area, "
            f"{identity_area(caller.identity_id or '')}/"
        )
    else:
        message = "You can read this project's memory but not change it"
    raise OmnigentError(message, code=ErrorCode.FORBIDDEN)
