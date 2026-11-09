from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

NATIVE_SKILL_INVOCATION_HEADER = "X-Omnigent-Native-Skill-Invocation"
SKILL_CLIENT_ID_PREFIX = "omnigent-skill:"


class ClaudeSkillInvocation(BaseModel):
    harness: Literal["claude-native"] = "claude-native"
    command: str = Field(pattern=r"^/[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+(?: [^\x00-\x1f\x7f]*)?$")
    bridge_dir: str
    socket_path: str


class CodexSkillInvocation(BaseModel):
    harness: Literal["codex-native"] = "codex-native"
    command: str = Field(pattern=r"^\$[a-zA-Z0-9_-]+(?: [^\x00-\x1f\x7f]*)?$")
    name: str
    path: str
    thread_id: str
    socket_path: str


NativeSkillInvocation = Annotated[
    ClaudeSkillInvocation | CodexSkillInvocation, Field(discriminator="harness")
]
native_skill_invocation_adapter = TypeAdapter(NativeSkillInvocation)
_invocation: ContextVar[NativeSkillInvocation | None] = ContextVar(
    "native_skill_invocation", default=None
)


@contextmanager
def native_skill_delivery(invocation: NativeSkillInvocation | None) -> Iterator[None]:
    token = _invocation.set(invocation)
    try:
        yield
    finally:
        _invocation.reset(token)


def current_native_skill_invocation() -> NativeSkillInvocation | None:
    return _invocation.get()
