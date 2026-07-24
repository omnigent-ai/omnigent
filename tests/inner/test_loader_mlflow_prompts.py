"""MLflow Prompt Registry resolution on the inner-loader parse path.

The inner loader (``omnigent.inner.loader``) is the second of the two parser
copies. It must resolve ``instructions:`` MLflow references through the same
shared helper as ``omnigent.spec.parser`` and honor the same
``expand_env=False`` network-fetch gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mlflow
import pytest
import yaml
from mlflow.genai import register_prompt, set_prompt_alias

from omnigent.inner.loader import load_agent_def


@pytest.fixture()
def greeting_prompt(tmp_path: Path) -> None:
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    mlflow.set_registry_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    pv = register_prompt(name="greeting", template="Hello {{product}} user!")
    set_prompt_alias(name="greeting", alias="production", version=pv.version)


def _write_yaml(root: Path, instructions: object) -> Path:
    data: dict[str, Any] = {"name": "test-agent", "instructions": instructions}
    path = root / "agent.yaml"
    path.write_text(yaml.dump(data))
    return path


def test_loader_resolves_mlflow_shorthand(tmp_path: Path, greeting_prompt: None) -> None:
    path = _write_yaml(tmp_path, "mlflow+prompts:/greeting@production")
    agent = load_agent_def(path)
    assert agent.instructions == "Hello {{product}} user!"


def test_loader_resolves_mlflow_structured_with_vars(
    tmp_path: Path, greeting_prompt: None
) -> None:
    path = _write_yaml(
        tmp_path,
        {
            "source": "mlflow",
            "reference": "prompts:/greeting/1",
            "vars": {"product": "Acme"},
        },
    )
    agent = load_agent_def(path)
    assert agent.instructions == "Hello Acme user!"


def test_loader_guard_no_fetch_when_expand_env_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untrusted/validation load must not contact the registry."""

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("registry must not be contacted when expand_env=False")

    monkeypatch.setattr("omnigent.spec.mlflow_prompts.resolve_mlflow_prompt", _boom)
    path = _write_yaml(tmp_path, "mlflow+prompts:/greeting@production")
    agent = load_agent_def(path, expand_env=False)
    assert agent.instructions == "prompts:/greeting@production"


def test_loader_uses_shared_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader path delegates to the single canonical spec-layer helper."""
    seen: dict[str, Any] = {}

    def _fake_resolve(reference: str, **kwargs: Any) -> str:
        seen["reference"] = reference
        return "RESOLVED"

    monkeypatch.setattr("omnigent.spec.mlflow_prompts.resolve_mlflow_prompt", _fake_resolve)
    path = _write_yaml(tmp_path, "mlflow+prompts:/greeting/1")
    agent = load_agent_def(path)
    assert agent.instructions == "RESOLVED"
    assert seen["reference"] == "prompts:/greeting/1"
