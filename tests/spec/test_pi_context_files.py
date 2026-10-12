"""Pi prompt settings survive YAML loading and CLI bundle transport."""

from pathlib import Path

import pytest
import yaml

from omnigent.chat import _bundle_agent
from omnigent.errors import OmnigentError
from omnigent.spec import load
from omnigent.spec.omnigent import agent_def_to_agent_spec, agent_spec_to_agent_def


def _write_agent(tmp_path: Path, *, directory: bool, executor: dict) -> Path:
    source = tmp_path / "agent"
    source.mkdir()
    if directory:
        executor_config = dict(executor)
        model = executor_config.pop("model", None)
        config = {
            "spec_version": 1,
            "name": "pi-context-files",
            "executor": {"type": "omnigent", "model": model, "config": executor_config},
            "instructions": "AGENTS.md",
        }
        (source / "AGENTS.md").write_text("Explicit agent instructions.")
        (source / "config.yaml").write_text(yaml.safe_dump(config))
        return source
    config = {
        "name": "pi-context-files",
        "executor": executor,
        "prompt": "Explicit agent instructions.",
    }
    path = source / "agent.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


@pytest.mark.parametrize("directory", [False, True], ids=["yaml", "bundle"])
@pytest.mark.parametrize("enabled", [None, True, False], ids=["default", "enabled", "disabled"])
def test_context_files_survives_bundle_transport(
    tmp_path: Path, directory: bool, enabled: bool | None
) -> None:
    executor: dict[str, object] = {"harness": "pi"}
    if enabled is not None:
        executor["context_files"] = enabled
    path = _write_agent(tmp_path, directory=directory, executor=executor)

    spec = load(_bundle_agent(path), dest=tmp_path / "uploaded")

    assert spec.executor.config.get("context_files", True) is (enabled is not False)
    assert spec.instructions == "Explicit agent instructions."


@pytest.mark.parametrize("directory", [False, True], ids=["yaml", "bundle"])
@pytest.mark.parametrize("value", ["false", "true", "off", 0, 1, None, [], {}])
def test_context_files_rejects_non_boolean_values(
    tmp_path: Path, directory: bool, value: object
) -> None:
    path = _write_agent(
        tmp_path, directory=directory, executor={"harness": "pi", "context_files": value}
    )

    with pytest.raises((OmnigentError, ValueError), match=r"context_files.*must be a boolean"):
        load(path)


@pytest.mark.parametrize("harness", ["pi-native", "codex"])
def test_context_files_rejects_unsupported_harness(tmp_path: Path, harness: str) -> None:
    path = _write_agent(
        tmp_path, directory=False, executor={"harness": harness, "context_files": False}
    )

    with pytest.raises(OmnigentError, match=r"context_files.*only supported.*pi"):
        load(path)


@pytest.mark.parametrize("directory", [False, True], ids=["yaml", "bundle"])
@pytest.mark.parametrize("mode", [None, "append", "replace"], ids=["default", "append", "replace"])
def test_system_prompt_mode_survives_bundle_transport(
    tmp_path: Path, directory: bool, mode: str | None
) -> None:
    executor: dict[str, object] = {"harness": "pi", "model": "mock-pi"}
    if mode is not None:
        executor["system_prompt_mode"] = mode
    path = _write_agent(tmp_path, directory=directory, executor=executor)
    spec = load(_bundle_agent(path), dest=tmp_path / "uploaded")

    for candidate in (spec, agent_def_to_agent_spec(agent_spec_to_agent_def(spec))):
        assert candidate.executor.config.get("system_prompt_mode", "append") == (mode or "append")
        assert candidate.instructions == "Explicit agent instructions."


@pytest.mark.parametrize("directory", [False, True], ids=["yaml", "bundle"])
@pytest.mark.parametrize("mode", ["unknown", "REPLACE", "", True, 1, None, [], {}])
def test_system_prompt_mode_rejects_invalid_values(
    tmp_path: Path, directory: bool, mode: object
) -> None:
    path = _write_agent(
        tmp_path, directory=directory, executor={"harness": "pi", "system_prompt_mode": mode}
    )
    with pytest.raises((OmnigentError, ValueError), match=r"system_prompt_mode.*append.*replace"):
        load(path)


@pytest.mark.parametrize("harness", ["pi-native", "codex"])
def test_system_prompt_mode_rejects_unsupported_harness(tmp_path: Path, harness: str) -> None:
    path = _write_agent(
        tmp_path, directory=False, executor={"harness": harness, "system_prompt_mode": "replace"}
    )
    with pytest.raises(OmnigentError, match=r"system_prompt_mode.*only supported.*pi"):
        load(path)


def test_inline_subagent_preserves_system_prompt_mode(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "parent",
                "prompt": "Parent instructions.",
                "executor": {"harness": "pi", "model": "mock-pi", "system_prompt_mode": "replace"},
                "tools": {
                    "worker": {
                        "type": "agent",
                        "prompt": "Worker instructions.",
                        "executor": {"system_prompt_mode": "append"},
                    }
                },
            }
        )
    )
    spec = load(_bundle_agent(path), dest=tmp_path / "uploaded")
    for candidate in (spec, agent_def_to_agent_spec(agent_spec_to_agent_def(spec))):
        assert candidate.executor.config["system_prompt_mode"] == "replace"
        assert candidate.sub_agents[0].executor.config["system_prompt_mode"] == "append"


def test_inline_subagents_preserve_independent_context_files(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        """name: parent
executor:
  harness: pi
  model: mock-pi
  context_files: false
prompt: Parent instructions.
tools:
  worker:
    type: agent
    executor:
      context_files: true
    prompt: Worker instructions.
    tools:
      nested:
        type: agent
        executor:
          context_files: false
        prompt: Nested instructions.
"""
    )
    spec = load(_bundle_agent(path), dest=tmp_path / "uploaded")
    assert spec.sub_agents[0].sub_agents[0].executor.config["context_files"] is False
    for candidate in (spec, agent_def_to_agent_spec(agent_spec_to_agent_def(spec))):
        assert candidate.executor.config["context_files"] is False
        worker = candidate.sub_agents[0]
        assert worker.executor.config["context_files"] is True
