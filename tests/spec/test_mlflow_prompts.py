"""Tests for MLflow Prompt Registry resolution of agent ``instructions:``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# mlflow is an optional extra; CI groups that don't install omnigent[mlflow]
# must skip this module cleanly rather than error at collection.
mlflow = pytest.importorskip("mlflow")
from mlflow.genai import register_prompt, set_prompt_alias  # noqa: E402

from omnigent.errors import OmnigentError  # noqa: E402
from omnigent.spec.mlflow_prompts import (  # noqa: E402
    parse_mlflow_instructions,
    resolve_mlflow_prompt,
)
from omnigent.spec.parser import parse  # noqa: E402


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


def _register(db_uri: str, name: str, template: str) -> None:
    """Register a text prompt in an isolated sqlite-backed registry.

    Uses the ``MlflowClient`` per-call URIs so it doesn't touch process globals,
    matching how :func:`resolve_mlflow_prompt` reads the registry back.
    """
    from mlflow.tracking import MlflowClient

    MlflowClient(tracking_uri=db_uri, registry_uri=db_uri).register_prompt(
        name=name, template=template
    )


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


@pytest.mark.parametrize(
    "reference",
    [
        "prompts:/greeting/1?tag=production",  # query string smuggling a tag
        "prompts:/greeting@prod?x=y",  # query string on an alias ref
        "prompts:/greeting/1#frag",  # URL fragment
        "prompts:/greeting",  # neither @alias nor /version
        "prompts:/greeting/prod",  # non-numeric version segment
        "prompts:/greeting@prod@extra",  # trailing junk
    ],
)
def test_reference_rejects_tag_query_and_fragment(reference: str) -> None:
    """Only ``prompts:/name@alias`` or ``prompts:/name/<int>`` are accepted.

    A query string or fragment would be silently ignored by MLflow, resolving a
    different prompt than the reference reads as (e.g. ``?tag=production``).
    """
    with pytest.raises(OmnigentError, match=r"alias .* or version"):
        parse_mlflow_instructions({"source": "mlflow", "reference": reference})


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


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, Any], prompt: Any
) -> None:
    """Replace ``mlflow.tracking.MlflowClient`` with a call-recording fake.

    Records the per-call ``tracking_uri`` / ``registry_uri`` the resolver passes
    to the constructor and the ``load_prompt`` kwargs, and asserts the global
    ``set_*_uri`` mutators are never touched.
    """

    def _boom(uri: str) -> None:  # pragma: no cover - must never run
        raise AssertionError("resolver must not mutate MLflow global URIs")

    monkeypatch.setattr(mlflow, "set_registry_uri", _boom)
    monkeypatch.setattr(mlflow, "set_tracking_uri", _boom)

    class _FakeClient:
        def __init__(
            self,
            tracking_uri: str | None = None,
            registry_uri: str | None = None,
            **_: Any,
        ) -> None:
            calls["tracking_uri"] = tracking_uri
            calls["registry_uri"] = registry_uri

        def load_prompt(self, name_or_uri: str, **kwargs: Any) -> Any:
            calls["reference"] = name_or_uri
            calls["load_kwargs"] = kwargs
            return prompt

    monkeypatch.setattr("mlflow.tracking.MlflowClient", _FakeClient)


def test_databricks_uc_registry_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """registry_uri=databricks-uc + 3-part UC name flow to the SDK unchanged."""
    calls: dict[str, Any] = {}

    class _FakePrompt:
        template = "UC prompt {{x}}"
        name = "main.default.greeting"
        version = 3

        def format(self, **kwargs: Any) -> str:
            return f"UC prompt {kwargs['x']}"

    _install_fake_client(monkeypatch, calls, _FakePrompt())

    text = resolve_mlflow_prompt(
        "prompts:/main.default.greeting@production",
        registry_uri="databricks-uc",
        vars={"x": "hi"},
    )
    assert text == "UC prompt hi"
    # URIs are passed per-call to the client, not set as process globals.
    assert calls["registry_uri"] == "databricks-uc"
    assert calls["tracking_uri"] is None
    assert calls["reference"] == "prompts:/main.default.greeting@production"


def test_resolve_passes_uris_per_call_without_global_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker 2: tracking/registry URIs reach the client, globals untouched."""
    calls: dict[str, Any] = {}

    class _FakePrompt:
        template = "hi"
        name = "greeting"
        version = 1

    _install_fake_client(monkeypatch, calls, _FakePrompt())

    resolve_mlflow_prompt(
        "prompts:/greeting/1",
        tracking_uri="sqlite:///a.db",
        registry_uri="sqlite:///b.db",
    )
    assert calls["tracking_uri"] == "sqlite:///a.db"
    assert calls["registry_uri"] == "sqlite:///b.db"


def test_resolve_disables_cache_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocker 3: without an explicit TTL the URI-keyed cache is bypassed (0)."""
    calls: dict[str, Any] = {}

    class _FakePrompt:
        template = "hi"
        name = "greeting"
        version = 1

    _install_fake_client(monkeypatch, calls, _FakePrompt())

    resolve_mlflow_prompt("prompts:/greeting/1")
    assert calls["load_kwargs"]["cache_ttl_seconds"] == 0


def test_resolve_cache_does_not_bleed_across_registries(tmp_path: Path) -> None:
    """Blocker 3 end-to-end: same URI, two registries, each returns its own text.

    The MLflow prompt cache is process-global and keyed by URI only. Resolving
    ``prompts:/greeting/1`` against registry A then registry B must return B's
    template, not A's cached copy.
    """
    db_a = f"sqlite:///{tmp_path / 'a.db'}"
    db_b = f"sqlite:///{tmp_path / 'b.db'}"
    _register(db_a, "greeting", "From registry A")
    _register(db_b, "greeting", "From registry B")

    text_a = resolve_mlflow_prompt("prompts:/greeting/1", tracking_uri=db_a, registry_uri=db_a)
    text_b = resolve_mlflow_prompt("prompts:/greeting/1", tracking_uri=db_b, registry_uri=db_b)
    assert text_a == "From registry A"
    assert text_b == "From registry B"


def test_resolve_partial_render_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocker 5: a non-string ``format`` result (partial render) raises."""
    calls: dict[str, Any] = {}

    class _PartialResult:
        pass

    class _FakePrompt:
        template = "Hello {{a}} and {{b}}"
        name = "greeting"
        version = 1

        def format(self, **kwargs: Any) -> Any:
            # allow_partial leaves a PromptVersion when a var is missing.
            return _PartialResult()

    _install_fake_client(monkeypatch, calls, _FakePrompt())

    with pytest.raises(OmnigentError, match="did not render to text"):
        resolve_mlflow_prompt("prompts:/greeting/1", vars={"a": "x"})


@pytest.mark.parametrize("key", ["tracking_uri", "registry_uri"])
def test_resolve_rejects_credential_uris(key: str) -> None:
    """Blocker 6: a ``user:pass@host`` URI is rejected before any load."""
    with pytest.raises(OmnigentError, match="must not embed credentials"):
        resolve_mlflow_prompt("prompts:/greeting/1", **{key: "https://user:pass@host/mlflow"})


@pytest.mark.parametrize("key", ["tracking_uri", "registry_uri"])
def test_parse_rejects_credential_uris(key: str) -> None:
    """Blocker 6: credentials in structured config are rejected at parse time."""
    with pytest.raises(OmnigentError, match="must not embed credentials"):
        parse_mlflow_instructions(
            {
                "source": "mlflow",
                "reference": "prompts:/greeting/1",
                key: "https://user:pass@host",
            }
        )


def test_resolve_empty_vars_still_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocker 7: explicit ``vars: {}`` renders (drops escapes) rather than
    returning the raw template — presence, not truthiness, drives formatting."""
    calls: dict[str, Any] = {}

    class _FakePrompt:
        template = "Hello {{name}}"
        name = "greeting"
        version = 1

        def format(self, **kwargs: Any) -> str:
            calls["formatted"] = True
            return "Hello {{name}}"

    _install_fake_client(monkeypatch, calls, _FakePrompt())

    resolve_mlflow_prompt("prompts:/greeting/1", vars={})
    assert calls.get("formatted") is True
