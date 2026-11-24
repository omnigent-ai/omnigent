"""Personal saved views and durable work tasks, independent of agent sessions."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EntityType = Literal["tasks", "projects", "sessions"]
TaskStatus = Literal["backlog", "todo", "in_progress", "review", "done", "canceled"]
TASK_STATUS_CODES = {
    "backlog": 0,
    "todo": 1,
    "in_progress": 2,
    "review": 3,
    "done": 4,
    "canceled": 5,
}
RecordId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
GroupBy = Literal["none", "status", "project", "priority"]
ViewProperty = Literal["status", "project", "priority", "created_at", "updated_at"]


def _default_properties() -> list[ViewProperty]:
    return ["status", "project", "priority", "updated_at"]


class ViewFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: str = Field(default="", max_length=200)
    statuses: list[str] = Field(default_factory=list, max_length=10)
    project_ids: list[RecordId] = Field(default_factory=list, max_length=50)
    no_project: bool = False
    priorities: list[int] = Field(default_factory=list, max_length=5)
    updated_after: int | None = Field(default=None, ge=0)
    include_archived: bool = False

    @model_validator(mode="after")
    def valid_priorities(self) -> ViewFilters:
        if any(priority < 0 or priority > 4 for priority in self.priorities):
            raise ValueError("priorities must be between 0 and 4")
        return self


class ViewDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layout: Literal["list", "board"] = "list"
    group_by: GroupBy = "status"
    order_by: Literal["created_at", "updated_at", "title", "priority"] = "updated_at"
    direction: Literal["asc", "desc"] = "desc"
    show_empty_groups: bool = False
    properties: list[ViewProperty] = Field(default_factory=_default_properties, max_length=5)


class ViewDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: EntityType = "tasks"
    filters: ViewFilters = Field(default_factory=ViewFilters)
    display: ViewDisplay = Field(default_factory=ViewDisplay)

    @model_validator(mode="after")
    def compatible_fields(self) -> ViewDefinition:
        if self.entity_type == "projects":
            if self.filters.statuses or self.filters.priorities or self.filters.no_project:
                raise ValueError(
                    "projects do not have task status, priority or project membership"
                )
            if self.display.group_by != "none":
                raise ValueError("projects use no grouping")
        if self.entity_type != "tasks":
            if self.filters.priorities or self.display.order_by == "priority":
                raise ValueError("priority is a task property")
            if self.display.group_by == "priority":
                raise ValueError("priority is a task property")
        if self.entity_type == "sessions" and self.display.order_by == "title":
            raise ValueError("sessions can be ordered by created or updated time")
        statuses = (
            set(TASK_STATUS_CODES)
            if self.entity_type == "tasks"
            else {"idle", "running", "waiting", "failed", "needs_input", "offline", "archived"}
        )
        if any(status not in statuses for status in self.filters.statuses):
            raise ValueError("status does not belong to this view's item type")
        return self


class SaveView(ViewDefinition):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    favorite: bool = False

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value.strip()


class ViewQuery(ViewDefinition):
    cursor: RecordId | None = None
    limit: int = Field(default=100, ge=1, le=200)


class SaveWorkTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=12000)
    status: TaskStatus = "todo"
    priority: int = Field(default=0, ge=0, le=4)
    project_id: RecordId | None = None

    @field_validator("title")
    @classmethod
    def nonempty_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be empty")
        return value.strip()


class LinkWorkSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: RecordId
    involvement: str = Field(default="", max_length=300)
