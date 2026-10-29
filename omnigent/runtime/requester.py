"""Requester metadata carried by trusted transports, never by model arguments."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Literal, ParamSpec, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REQUESTER_FIELD = "_omnigent_requester"
REQUESTER_HEADER = "X-Omnigent-Requester"
DelegationPolicy = Literal["requester", "no_edit_sharing"]


class InputRequester(BaseModel):
    """The authenticated origin of one accepted input, separate from attribution.

    Authentication is established by the transport that supplies this value,
    not by parsing it. An unknown user never acquires the runner owner's identity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    input_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    user_id: str | None = Field(default=None, min_length=1)
    account_generation: str | None = Field(default=None, min_length=1)

    @classmethod
    def from_body(cls, body: Mapping[str, object]) -> InputRequester:
        """Read only the protected transport envelope; missing input is unknown."""
        try:
            return cls.model_validate(body.get(REQUESTER_FIELD))
        except ValidationError:
            return cls()

    def same_account(self, other: InputRequester) -> bool:
        """Unknown origins cannot establish a shared requester."""
        return self.user_id is not None and (
            self.user_id,
            self.account_generation,
        ) == (other.user_id, other.account_generation)


class ToolRequester(BaseModel):
    """Origin and delegation policy captured by the producing harness, not tool arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    origin: InputRequester
    delegation_policy: DelegationPolicy = "requester"


current_tool_requester: ContextVar[ToolRequester | None] = ContextVar(
    "tool_requester", default=None
)
_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")


def bind_tool_requester(
    function: Callable[_Parameters, Coroutine[Any, Any, _Result]],
) -> Callable[_Parameters, Coroutine[Any, Any, _Result]]:
    """Preserve a call's snapshot through nested and asynchronous tool dispatch."""

    @wraps(function)
    async def scoped(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Result:
        supplied = kwargs.get("requester")
        requester = (
            supplied if isinstance(supplied, ToolRequester) else current_tool_requester.get()
        )
        token = current_tool_requester.set(requester)
        try:
            return await function(*args, **kwargs)
        finally:
            current_tool_requester.reset(token)

    return scoped


def requester_headers() -> dict[str, str]:
    """Return protected metadata for an authenticated runner request."""
    requester = current_tool_requester.get()
    return {REQUESTER_HEADER: requester.model_dump_json()} if requester is not None else {}


@dataclass
class ExecutionRequester:
    """Inputs consumed by one execution; queued messages remain separate.

    Mixed or unknown input permanently blocks delegation for this execution.
    A fresh execution has its own identity and cannot rehabilitate old calls.
    """

    origin: InputRequester
    execution_id: str = field(default_factory=lambda: uuid4().hex)
    _pending: dict[str, InputRequester] = field(default_factory=dict, init=False)

    def snapshot(self) -> ToolRequester:
        """Block during an injection whose consumption is not yet confirmed."""
        origin = InputRequester() if self._pending else self.origin
        return ToolRequester(execution_id=self.execution_id, origin=origin)

    def consume(self, origin: InputRequester) -> None:
        if not self.origin.same_account(origin):
            self.origin = InputRequester()

    def begin_injection(self, injection_id: str, origin: InputRequester) -> None:
        self._pending[injection_id] = origin

    def finish_injection(self, injection_id: str, *, consumed: bool) -> None:
        origin = self._pending.pop(injection_id, None)
        if consumed:
            self.consume(origin if origin is not None else InputRequester())
