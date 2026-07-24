"""Tests for MLflow Prompt Registry resolution of agent ``instructions:``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mlflow
import pytest
import yaml
from mlflow.genai import register_prompt, set_prompt_alias

from omnigent.errors import OmnigentError
from omnigent.spec.mlflow_prompts import (
    parse_mlflow_instructions,
    resolve_mlflow_prompt,
)
from omnigent.spec.parser import parse


@pytest.fixture()
def oss_registry(tmp_path: Path) -> None:
    """Point MLflow at a throwaway sqlite-backed registry for the test."""
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    mlflow.set_registry_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")


@pytest.fixture()
def greeting_prompt(oss_registry: None) -> None:
    """Register a text prompt ``greeting`` v1 with a ``production`` alias."""
    pv = register_prompt(name="greeting", template="Hello {{product}} user!")
    set_prompt_alias(name="greeting", alias="production", version=pv.version)


def _agent_dir(root: Path, instructions: object) -> Path:
    config: dict[str, Any] = {"spec_version": 1, "name": "test-agent"}
    if instructions is not None:
        config["instructions"] = instructions
    (root / "config.yaml").write_text(yaml.dump(config))
    return root


# --- parse_mlflow_instructions detection -------------------------------------


def test_detect_shorthand_string() -> None:
    ref = parse_mlflow_instructions("mlflow+prompts:/greeting@production")
    assert ref is not None
    assert ref.reference == "prompts:/greeting@production"
    assert ref.vars is None


def test_detect_structured_mapping() -> None:
    ref = parse_mlflow_instructions(
        {
            "source": "mlflow",
            "reference": "prompts:/greeting/1",
            "vars": {"product": "Acme"},
        }
    )
    assert ref is not None
    assert ref.reference == "prompts:/greeting/1"
    assert ref.vars == {"product": "Acme"}


def test_plain_string_is_not_mlflow() -> None:
    assert parse_mlflow_instructions("You are a helpful agent.") is None
    assert parse_mlflow_instructions("AGENTS.md") is None
    assert parse_mlflow_instructions(None) is None


def test_structured_missing_reference_raises() -> None:
    with pytest.raises(OmnigentError, match="requires a 'reference:'"):
        parse_mlflow_instructions({"source": "mlflow"})


def test_reference_must_be_prompts_uri() -> None:
    with pytest.raises(OmnigentError, match="prompts:/"):
        parse_mlflow_instructions("mlflow+models:/foo/1")
    with pytest.raises(OmnigentError, match="prompts:/"):
        parse_mlflow_instructions({"source": "mlflow", "reference": "greeting@prod"})


def test_structured_bad_vars_type_raises() -> None:
    with pytest.raises(OmnigentError, match="vars must be a mapping"):
        parse_mlflow_instructions({"source": "mlflow", "reference": "prompts:/g/1", "vars": ["x"]})


# --- resolve_mlflow_prompt (OSS happy path) ----------------------------------


def test_resolve_by_version(greeting_prompt: None) -> None:
    text = resolve_mlflow_prompt("prompts:/greeting/1", vars={"product": "Acme"})
    assert text == "Hello Acme user!"


def test_resolve_by_alias(greeting_prompt: None) -> None:
    text = resolve_mlflow_prompt("prompts:/greeting@production", vars={"product": "Beta"})
    assert text == "Hello Beta user!"


def test_resolve_without_vars_returns_raw_template(greeting_prompt: None) -> None:
    text = resolve_mlflow_prompt("prompts:/greeting/1")
    assert text == "Hello {{product}} user!"


def test_resolve_chat_prompt_rejected(oss_registry: None) -> None:
    register_prompt(
        name="chatp",
        template=[{"role": "user", "content": "hi {{x}}"}],
    )
    with pytest.raises(OmnigentError, match="chat-style prompt"):
        resolve_mlflow_prompt("prompts:/chatp/1")


def test_resolve_missing_alias_raises(greeting_prompt: None) -> None:
    with pytest.raises(OmnigentError, match="failed to load MLflow prompt"):
        resolve_mlflow_prompt("prompts:/greeting@nonexistent")


def test_resolve_missing_prompt_raises(oss_registry: None) -> None:
    with pytest.raises(OmnigentError, match="failed to load MLflow prompt"):
        resolve_mlflow_prompt("prompts:/does-not-exist/1")


def test_resolve_missing_mlflow_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clear install hint fires when mlflow can't be imported."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "mlflow" or name.startswith("mlflow."):
            raise ImportError("no mlflow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(OmnigentError, match="omnigent\\[mlflow\\]"):
        resolve_mlflow_prompt("prompts:/greeting/1")


# --- parser.py wiring --------------------------------------------------------


def test_parse_resolves_mlflow_shorthand(tmp_path: Path, greeting_prompt: None) -> None:
    root = _agent_dir(tmp_path, "mlflow+prompts:/greeting@production")
    spec = parse(root)
    assert spec.instructions == "Hello {{product}} user!"


def test_parse_resolves_mlflow_structured_with_vars(tmp_path: Path, greeting_prompt: None) -> None:
    root = _agent_dir(
        tmp_path,
        {
            "source": "mlflow",
            "reference": "prompts:/greeting/1",
            "vars": {"product": "Acme"},
        },
    )
    spec = parse(root)
    assert spec.instructions == "Hello Acme user!"


def test_parse_guard_no_fetch_when_expand_env_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scaffolding/validation parse must NOT contact the registry."""

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("registry must not be contacted when expand_env=False")

    monkeypatch.setattr("omnigent.spec.parser.resolve_mlflow_prompt", _boom)
    root = _agent_dir(tmp_path, "mlflow+prompts:/greeting@production")
    spec = parse(root, expand_env=False)
    # Left as the literal reference; no network fetch.
    assert spec.instructions == "prompts:/greeting@production"


# --- Databricks-managed backend selection (mocked SDK) -----------------------


def test_databricks_uc_registry_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """registry_uri=databricks-uc + 3-part UC name flow to the SDK unchanged."""
    calls: dict[str, Any] = {}

    def _fake_set_registry_uri(uri: str) -> None:
        calls["registry_uri"] = uri

    def _fake_set_tracking_uri(uri: str) -> None:
        calls["tracking_uri"] = uri

    class _FakePrompt:
        template = "UC prompt {{x}}"
        name = "main.default.greeting"
        version = 3

        def format(self, **kwargs: Any) -> str:
            return f"UC prompt {kwargs['x']}"

    def _fake_load_prompt(reference: str, **kwargs: Any) -> _FakePrompt:
        calls["reference"] = reference
        calls["load_kwargs"] = kwargs
        return _FakePrompt()

    monkeypatch.setattr(mlflow, "set_registry_uri", _fake_set_registry_uri)
    monkeypatch.setattr(mlflow, "set_tracking_uri", _fake_set_tracking_uri)
    monkeypatch.setattr("mlflow.genai.load_prompt", _fake_load_prompt)

    text = resolve_mlflow_prompt(
        "prompts:/main.default.greeting@production",
        registry_uri="databricks-uc",
        vars={"x": "hi"},
    )
    assert text == "UC prompt hi"
    assert calls["registry_uri"] == "databricks-uc"
    assert "tracking_uri" not in calls
    assert calls["reference"] == "prompts:/main.default.greeting@production"
    # link_to_model disabled at load time (no active model during parse).
    assert calls["load_kwargs"]["link_to_model"] is False
