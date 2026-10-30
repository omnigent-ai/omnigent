"""Installed provider example backed by an in-memory fake controller."""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError, SandboxHostLauncher
from omnigent.onboarding.sandboxes.experimental import (
    SandboxAction,
    SandboxActionRequest,
    SandboxActionResult,
    SandboxChildResult,
)
from omnigent.onboarding.sandboxes.registry import (
    SandboxProviderContribution,
    SandboxProviderMetadata,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities, SandboxInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from omnigent.onboarding.sandboxes.types import RepoWorkspace


class InspectParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChildParameters(InspectParameters):
    name: str = Field(min_length=1, max_length=80)


# ponytail: single-threaded, process-local state; production needs durable operation records.
_handles: dict[str, str] = {}
_operations: dict[str, tuple[str, SandboxActionResult | SandboxChildResult]] = {}
_handle_ids = itertools.count(1)


class ExampleSandboxLauncher(SandboxHostLauncher):
    provider = "example"

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(programmatic_terminate=True)

    @property
    def experimental_actions(self) -> tuple[SandboxAction, ...]:
        return (
            SandboxAction(
                name="example.inspect.v1",
                parameters_model=InspectParameters,
                target_roles=("source",),
            ),
            SandboxAction(
                name="example.create_child.v1",
                parameters_model=ChildParameters,
                target_roles=("source",),
                creates_child=True,
            ),
        )

    def prepare(self) -> None:
        pass

    def provision(self, name: str) -> str:
        sandbox_id = f"example-{next(_handle_ids)}"
        _handles[sandbox_id] = name
        return sandbox_id

    def is_running(self, sandbox_id: str) -> bool:
        return sandbox_id in _handles

    def terminate(self, sandbox_id: str) -> None:
        _handles.pop(sandbox_id, None)

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        del sandbox_id, token, host_id, host_name, server_url, repos, host_config, on_stage
        raise SandboxCapabilityError("The example provider has no real hosts")

    def invoke_experimental_action(
        self, request: SandboxActionRequest
    ) -> SandboxActionResult | SandboxChildResult:
        signature = json.dumps(asdict(request), sort_keys=True, allow_nan=False)
        previous = _operations.get(request.operation_id)
        if previous is not None:
            if previous[0] != signature:
                raise ValueError("Operation ID was already used with a different request")
            return previous[1]

        source_id = request.targets["source"].sandbox_id
        if source_id not in _handles:
            raise ValueError("Source handle does not exist")

        result: SandboxActionResult | SandboxChildResult
        if request.action == "example.inspect.v1":
            result = SandboxActionResult(data={"name": _handles[source_id], "running": True})
        elif request.action == "example.create_child.v1":
            parameters = ChildParameters.model_validate(request.parameters)
            child_id = self.provision(parameters.name)
            result = SandboxChildResult(
                child=SandboxInfo(sandbox_id=child_id),
                data={"source_id": source_id},
            )
        else:
            raise SandboxCapabilityError(f"Unsupported example action: {request.action}")
        _operations[request.operation_id] = (signature, result)
        return result


def get_contribution() -> SandboxProviderContribution:
    return SandboxProviderContribution(
        name="omnigent-sandbox-actions-example",
        providers={
            "example": SandboxProviderMetadata(
                name="example",
                launcher_class=(
                    "omnigent.community.sandbox.action_example:ExampleSandboxLauncher"
                ),
            ),
        },
    )
