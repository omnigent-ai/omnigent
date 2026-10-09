"""Contract checks for opt-in sandbox actions, without creating runtimes."""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError, SandboxLifecycle
from omnigent.onboarding.sandboxes.experimental import (
    SandboxAction,
    SandboxActionRequest,
    SandboxActionResult,
    SandboxActionTarget,
    SandboxChildResult,
    invoke_sandbox_action,
    sandbox_actions,
)
from omnigent.onboarding.sandboxes.types import SandboxInfo


class _Parameters(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    limit: int = Field(default=1, ge=1)


class _PermissiveParameters(BaseModel):
    limit: int = 1


class _Mode(str, Enum):
    PREVIEW = "preview"


class _EnumParameters(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    mode: _Mode


_ACTION = SandboxAction("example.inspect.v1", _Parameters, target_roles=("source",))
_TARGET = SandboxActionTarget("session-source", "host-source", "example", "sb-source")


class _LegacyLauncher(SandboxLifecycle):
    provider: ClassVar[str] = "example"

    def prepare(self) -> None:
        pass

    def provision(self, name: str) -> str:
        raise AssertionError("action dispatch must not provision a sandbox")


class _Launcher(_LegacyLauncher):
    def __init__(
        self,
        actions: tuple[SandboxAction, ...] = (_ACTION,),
        result: SandboxActionResult | SandboxChildResult | None = None,
    ) -> None:
        self.actions = actions
        self.result = result if result is not None else SandboxActionResult()
        self.calls: list[SandboxActionRequest] = []

    @property
    def experimental_actions(self) -> tuple[SandboxAction, ...]:
        return self.actions

    def invoke_experimental_action(
        self, request: SandboxActionRequest
    ) -> SandboxActionResult | SandboxChildResult:
        self.calls.append(request)
        return self.result


def _request() -> SandboxActionRequest:
    return SandboxActionRequest(
        action=_ACTION.name,
        operation_id="operation-1",
        targets={"source": _TARGET},
        parameters={},
    )


def test_legacy_provider_does_not_support_actions() -> None:
    launcher = _LegacyLauncher()
    assert sandbox_actions(launcher) == ()
    with pytest.raises(SandboxCapabilityError):
        launcher.invoke_experimental_action(_request())
    with pytest.raises(SandboxCapabilityError):
        invoke_sandbox_action(launcher, _request())


def test_valid_action_normalizes_defaults_before_provider_callback() -> None:
    launcher = _Launcher(result=SandboxActionResult(data={"items": [True, None, 1.5]}))
    request = replace(_request(), operation_id="x" * 128)

    assert sandbox_actions(launcher) == (_ACTION,)
    assert _ACTION.parameters_model.model_json_schema()["properties"]["limit"]["type"] == "integer"
    result = invoke_sandbox_action(launcher, request)

    assert result == launcher.result
    assert launcher.calls == [replace(request, parameters={"limit": 1})]
    assert request.parameters == {}


def test_enum_parameters_accept_json_values_and_reject_unknown_values() -> None:
    launcher = _Launcher((replace(_ACTION, parameters_model=_EnumParameters),))
    request = replace(_request(), parameters={"mode": "preview"})

    invoke_sandbox_action(launcher, request)

    assert launcher.calls == [request]
    assert type(launcher.calls[0].parameters["mode"]) is str
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, replace(request, parameters={"mode": "unknown"}))
    assert launcher.calls == [request]


@pytest.mark.parametrize(
    "actions",
    [
        (replace(_ACTION, name="inspect.v1"),),
        (replace(_ACTION, name="other.inspect.v1"),),
        (replace(_ACTION, name="example.inspect.v0"),),
        (_ACTION, _ACTION),
        (replace(_ACTION, target_roles=("source", "source")),),
        (replace(_ACTION, target_roles=("source-id",)),),
        (replace(_ACTION, parameters_model=dict),),
        (replace(_ACTION, parameters_model=_PermissiveParameters),),
    ],
)
def test_invalid_declarations_fail_before_provider_callback(
    actions: tuple[SandboxAction, ...],
) -> None:
    launcher = _Launcher(actions)
    with pytest.raises(ValueError):
        sandbox_actions(launcher)
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, _request())
    assert launcher.calls == []


def test_undeclared_action_is_not_dispatched() -> None:
    launcher = _Launcher()
    with pytest.raises(SandboxCapabilityError):
        invoke_sandbox_action(launcher, replace(_request(), action="example.unknown.v1"))
    assert launcher.calls == []


@pytest.mark.parametrize("operation_id", ["", " \t", "x" * 129])
def test_invalid_operation_id_fails_before_provider_callback(operation_id: str) -> None:
    launcher = _Launcher()
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, replace(_request(), operation_id=operation_id))
    assert launcher.calls == []


@pytest.mark.parametrize(
    "targets",
    [
        {},
        {"source": _TARGET, "child": _TARGET},
        {"source": replace(_TARGET, provider="other")},
        {"source": replace(_TARGET, session_id="")},
        {"source": replace(_TARGET, host_id=" ")},
        {"source": replace(_TARGET, sandbox_id="")},
    ],
)
def test_invalid_targets_fail_before_provider_callback(
    targets: dict[str, SandboxActionTarget],
) -> None:
    launcher = _Launcher()
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, replace(_request(), targets=targets))
    assert launcher.calls == []


@pytest.mark.parametrize("parameters", [{"limit": "2"}, {"limit": 0}, {"unexpected": True}])
def test_parameter_schema_is_enforced_before_provider_callback(parameters: object) -> None:
    launcher = _Launcher()
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, replace(_request(), parameters=parameters))
    assert launcher.calls == []


@pytest.mark.parametrize(
    "value",
    [
        [],
        {1: "non-string key"},
        {"value": (1, 2)},
        {"value": {1, 2}},
        {"value": float("nan")},
        {"value": float("inf")},
    ],
)
def test_parameters_and_results_require_finite_json_objects(value: object) -> None:
    launcher = _Launcher()
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, replace(_request(), parameters=value))
    assert launcher.calls == []

    launcher.result = replace(SandboxActionResult(), data=value)
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, _request())
    assert len(launcher.calls) == 1


def test_child_result_preserves_runtime_reference_and_provider_data() -> None:
    child = SandboxInfo("sb-child", workspace_path="/workspace", metadata={"revision": 2})
    launcher = _Launcher(
        actions=(replace(_ACTION, creates_child=True),),
        result=SandboxChildResult(child, data={"ready": False}),
    )

    result = invoke_sandbox_action(launcher, _request())

    assert result == launcher.result
    assert len(launcher.calls) == 1


@pytest.mark.parametrize(
    ("creates_child", "result"),
    [(True, SandboxActionResult()), (False, SandboxChildResult(SandboxInfo("sb-child")))],
)
def test_result_kind_must_match_declared_action(
    creates_child: bool, result: SandboxActionResult | SandboxChildResult
) -> None:
    launcher = _Launcher((replace(_ACTION, creates_child=creates_child),), result)
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, _request())
    assert len(launcher.calls) == 1


@pytest.mark.parametrize(
    "child",
    [
        SandboxInfo(" "),
        SandboxInfo(_TARGET.sandbox_id),
        SandboxInfo("sb-child", metadata={"score": float("nan")}),
    ],
)
def test_child_result_rejects_reused_ids_and_invalid_metadata(child: SandboxInfo) -> None:
    launcher = _Launcher((replace(_ACTION, creates_child=True),), SandboxChildResult(child))
    with pytest.raises(ValueError):
        invoke_sandbox_action(launcher, _request())
    assert len(launcher.calls) == 1
