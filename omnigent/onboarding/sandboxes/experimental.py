"""Experimental provider actions; no public API or session orchestration.

Only trusted callers may construct target references, after authorizing each
runtime. These helpers validate the SDK contract, not the caller's permissions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from pydantic import BaseModel, JsonValue, TypeAdapter

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.onboarding.sandboxes.types import SandboxInfo

if TYPE_CHECKING:
    from omnigent.onboarding.sandboxes.base import SandboxLifecycle


@dataclass(frozen=True)
class SandboxAction:
    """A provider-qualified action with its own parameter schema and version."""

    name: str
    parameters_model: type[BaseModel]
    target_roles: tuple[str, ...] = ()
    creates_child: bool = False


@dataclass(frozen=True)
class SandboxActionTarget:
    """An authorized, generation-specific reference supplied by the caller."""

    session_id: str
    host_id: str
    provider: str
    sandbox_id: str


@dataclass(frozen=True)
class SandboxActionRequest:
    """A retry identity, authorized targets, and provider-owned parameters."""

    action: str
    operation_id: str
    targets: dict[str, SandboxActionTarget]
    parameters: dict[str, JsonValue]


@dataclass(frozen=True)
class SandboxActionResult:
    """Provider data; never an instruction to rebind or retire a session."""

    data: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxChildResult:
    """A new runtime awaiting adoption; it is not yet a ready child session.

    The provider must fence copied control processes until fresh credentials
    arrive, preserve the workspace on startup, and allow independent cleanup.
    """

    child: SandboxInfo
    data: dict[str, JsonValue] = field(default_factory=dict)


_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_ROLE = re.compile(r"[a-z][a-z0-9_]*")


def _json_object(value: object) -> dict[str, JsonValue]:
    """Validate and copy JSON, rejecting non-finite floats and non-string keys."""
    validated = _JSON_OBJECT.validate_python(value, strict=True)
    return json.loads(json.dumps(validated, allow_nan=False))


def sandbox_actions(launcher: SandboxLifecycle) -> tuple[SandboxAction, ...]:
    """Discover and validate an installed provider's opt-in action declarations."""
    actions = launcher.experimental_actions
    names: set[str] = set()
    pattern = re.compile(rf"{re.escape(launcher.provider)}\.[a-z][a-z0-9_]*\.v[1-9][0-9]*")
    for action in actions:
        if not pattern.fullmatch(action.name):
            raise ValueError("action name must be provider.action.vN with a positive version")
        if action.name in names:
            raise ValueError(f"duplicate sandbox action: {action.name}")
        names.add(action.name)
        if len(set(action.target_roles)) != len(action.target_roles) or any(
            not _ROLE.fullmatch(role) for role in action.target_roles
        ):
            raise ValueError("action target roles must be unique lowercase identifiers")
        if not isinstance(action.parameters_model, type) or not issubclass(
            action.parameters_model, BaseModel
        ):
            raise ValueError("action parameters_model must be a Pydantic model")
        if action.parameters_model.model_config.get("extra") != "forbid":
            raise ValueError("action parameters_model must forbid extra fields")
    return tuple(actions)


def invoke_sandbox_action(
    launcher: SandboxLifecycle,
    request: SandboxActionRequest,
) -> SandboxActionResult | SandboxChildResult:
    """Validate and invoke a trusted provider; no authorization or retry engine.

    The caller must durably reserve operation_id before invoking this helper.
    The provider must return the same outcome on retry and reject key reuse
    with different actions, targets, or normalized parameters.
    """
    action = next((a for a in sandbox_actions(launcher) if a.name == request.action), None)
    if action is None:
        raise SandboxCapabilityError(f"unsupported sandbox action: {request.action}")
    if not request.operation_id.strip() or len(request.operation_id) > 128:
        raise ValueError("operation_id must be nonblank and at most 128 characters")
    if set(request.targets) != set(action.target_roles):
        raise ValueError("request targets must exactly match the action's target roles")
    for target in request.targets.values():
        if target.provider != launcher.provider:
            raise ValueError("all targets must belong to the selected sandbox provider")
        if not all(
            value.strip() for value in (target.session_id, target.host_id, target.sandbox_id)
        ):
            raise ValueError("target session, host, and sandbox IDs must be nonblank")
    parameters = action.parameters_model.model_validate_json(
        json.dumps(_json_object(request.parameters), allow_nan=False), strict=True
    )
    normalized = replace(
        request,
        targets=dict(request.targets),
        parameters=_json_object(parameters.model_dump(mode="json")),
    )
    result = launcher.invoke_experimental_action(normalized)
    if action.creates_child:
        if not isinstance(result, SandboxChildResult):
            raise ValueError("child-creating action must return SandboxChildResult")
        if not result.child.sandbox_id.strip() or any(
            result.child.sandbox_id == target.sandbox_id for target in request.targets.values()
        ):
            raise ValueError("child sandbox ID must be nonblank and distinct from all targets")
        return replace(
            result,
            child=replace(result.child, metadata=_json_object(result.child.metadata)),
            data=_json_object(result.data),
        )
    if not isinstance(result, SandboxActionResult):
        raise ValueError("data action must return SandboxActionResult")
    return replace(result, data=_json_object(result.data))
