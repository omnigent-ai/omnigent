import pytest
from fastapi import Request

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.project_main_session import seed_orchestrator_access
from omnigent.server.schemas import SessionCreateMetadata
from omnigent.stores.conversation_store import CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY


@pytest.mark.parametrize(
    ("harness", "expected_args", "expected_labels"),
    [
        ("claude-native", ["--permission-mode", "bypassPermissions"], {}),
        ("unknown-harness", None, {}),
        ("codex-native", [], {CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY: "1"}),
        ("cursor-native", ["--yolo"], {}),
        ("antigravity-native", ["--dangerously-skip-permissions"], {}),
        ("devin-native", ["--permission-mode", "dangerous"], {}),
        ("pi-native", None, {}),
        ("claude-sdk", None, {}),
        ("codex", None, {}),
        (None, None, {}),
    ],
)
def test_orchestrator_access_only_defaults_supported_harnesses(
    harness, expected_args, expected_labels
):
    request = Request({"type": "http", "headers": []})
    metadata = SessionCreateMetadata(
        project_id="project", project_main_session=True, expected_main_session_id=None
    )
    seed_orchestrator_access(request, metadata, harness)
    assert metadata.terminal_launch_args == expected_args
    assert metadata.labels == expected_labels


@pytest.mark.parametrize("scenario", ["ordinary", "agent", "explicit"])
def test_orchestrator_access_does_not_replace_normal_launch_selection(scenario):
    headers = (
        [(ORCHESTRATION_SOURCE_HEADER.lower().encode(), b"source")] if scenario == "agent" else []
    )
    request = Request({"type": "http", "headers": headers})
    request.state.project_main_explicit_access = scenario == "explicit"
    metadata = SessionCreateMetadata(
        project_id="project",
        project_main_session=scenario != "ordinary",
        expected_main_session_id=None,
    )
    seed_orchestrator_access(request, metadata, "claude-native")
    assert metadata.terminal_launch_args is None
    assert metadata.labels == {}


def test_a_rolled_over_lead_takes_the_projects_defaults_over_its_predecessors() -> None:
    """A model or effort set for the Lead in Team beats the old Lead's launch choice."""
    from types import SimpleNamespace

    from omnigent.server.project_orchestrator_rollover import carried_over_session_choices

    previous = SimpleNamespace(
        model_override="old-model", reasoning_effort="low", harness_override="codex"
    )
    assert carried_over_session_choices(previous, {}) == {
        "model_override": "old-model",
        "reasoning_effort": "low",
        "harness_override": "codex",
    }
    assert carried_over_session_choices(previous, {"model": "m2", "effort": "high"}) == {
        "harness_override": "codex"
    }
    assert carried_over_session_choices(None, {}) == {}
