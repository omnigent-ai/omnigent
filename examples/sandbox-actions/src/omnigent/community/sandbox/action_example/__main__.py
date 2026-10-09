"""Run the installed provider's contract checks without creating real resources."""

from dataclasses import replace

from omnigent.onboarding.sandboxes.experimental import (
    SandboxActionRequest,
    SandboxActionResult,
    SandboxActionTarget,
    SandboxChildResult,
    invoke_sandbox_action,
)
from omnigent.onboarding.sandboxes.registry import instantiate


def main() -> None:
    launcher = instantiate("example")
    assert not launcher.capabilities.managed_launch
    source_id = launcher.provision("source")
    target = SandboxActionTarget(
        session_id="demo-session",
        host_id="demo-host",
        provider="example",
        sandbox_id=source_id,
    )
    inspect_request = SandboxActionRequest(
        action="example.inspect.v1",
        operation_id="inspect-source",
        targets={"source": target},
        parameters={},
    )
    inspection = invoke_sandbox_action(launcher, inspect_request)
    assert isinstance(inspection, SandboxActionResult)
    assert inspection.data == {"name": "source", "running": True}

    request = replace(
        inspect_request,
        action="example.create_child.v1",
        operation_id="create-child",
        parameters={"name": "candidate"},
    )
    child = invoke_sandbox_action(launcher, request)
    assert isinstance(child, SandboxChildResult)
    assert child.child.sandbox_id != source_id
    assert launcher.is_running(child.child.sandbox_id)
    assert invoke_sandbox_action(instantiate("example"), request) == child

    try:
        invoke_sandbox_action(launcher, replace(request, parameters={"name": "changed"}))
    except ValueError as error:
        assert "different request" in str(error)
    else:
        raise AssertionError("Conflicting operation reuse was accepted")

    launcher.terminate(child.child.sandbox_id)
    launcher.terminate(child.child.sandbox_id)
    assert not launcher.is_running(child.child.sandbox_id)
    assert launcher.is_running(source_id)
    launcher.terminate(source_id)
    print("Passed: installed discovery, actions, replay, conflict rejection, and scoped cleanup")


if __name__ == "__main__":
    main()
