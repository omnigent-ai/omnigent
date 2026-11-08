"""Single-file ``omnigent.yaml`` bundles honor ``expand_env`` for executor auth."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from omnigent.inner.datamodel import ExecutorSpec
from omnigent.inner.loader import load_agent_def
from omnigent.inner.policies import PromptPolicy
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.bundles import validate_agent_bundle
from omnigent.spec import load as load_spec
from omnigent.spec.types import AgentSpec, ApiKeyAuth
from omnigent.stores.artifact_store.local import LocalArtifactStore

_KEY_REF = "${OMNIGENT_TEST_EXECUTOR_KEY}"
_URL_REF = "${OMNIGENT_TEST_EXECUTOR_URL}"
_KEY_VALUE = "synthetic-key"
_URL_VALUE = "https://example.invalid/v1"


@pytest.fixture(autouse=True)
def _executor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the variables the executor auth references point at."""
    monkeypatch.setenv("OMNIGENT_TEST_EXECUTOR_KEY", _KEY_VALUE)
    monkeypatch.setenv("OMNIGENT_TEST_EXECUTOR_URL", _URL_VALUE)


def _executor() -> dict[str, object]:
    """Return an executor block whose auth references the test variables."""
    return {
        "harness": "openai-agents",
        "model": "gpt-4o",
        "auth": {"type": "api_key", "api_key": _KEY_REF, "base_url": _URL_REF},
    }


def _agent_config() -> dict[str, object]:
    """Return a single-file agent with executor auth at the root and two nested levels."""
    return {
        "name": "executor_auth_env_test",
        "prompt": "Test agent",
        "executor": _executor(),
        "tools": {
            "worker": {
                "type": "agent",
                "prompt": "Test worker",
                "executor": _executor(),
                "tools": {
                    "nested_worker": {
                        "type": "agent",
                        "prompt": "Test nested worker",
                        "executor": _executor(),
                    }
                },
            }
        },
    }


def _bundle(config: dict[str, object]) -> bytes:
    """Pack *config* as the ``omnigent.yaml`` of a tar.gz bundle."""
    content = json.dumps(config).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("omnigent.yaml")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _assert_auth(spec: AgentSpec, *, expanded: bool) -> None:
    """Check executor auth at every level is expanded, or kept literal."""
    expected_key = _KEY_VALUE if expanded else _KEY_REF
    expected_url = _URL_VALUE if expanded else _URL_REF
    for agent in (spec, spec.sub_agents[0], spec.sub_agents[0].sub_agents[0]):
        assert agent.executor is not None
        auth = agent.executor.auth
        assert isinstance(auth, ApiKeyAuth)
        assert auth.api_key == expected_key
        assert auth.base_url == expected_url


@pytest.mark.parametrize("source_form", ["yaml", "archive"])
@pytest.mark.parametrize("expand_env", [False, True, None])
def test_single_file_bundle_honors_expand_env(
    tmp_path: Path, source_form: str, expand_env: bool | None
) -> None:
    """``load(expand_env=False)`` keeps executor auth references literal.

    Covers the bare ``omnigent.yaml`` path and the extracted-archive path, at
    the root agent and in nested agent tools. The default (``None`` here)
    still expands, as before.
    """
    config = _agent_config()
    if source_form == "yaml":
        source: Path | bytes = tmp_path / "omnigent.yaml"
        source.write_text(json.dumps(config))
    else:
        source = _bundle(config)
    if expand_env is None:
        spec = load_spec(source, dest=tmp_path / "extracted")
    else:
        spec = load_spec(source, dest=tmp_path / "extracted", expand_env=expand_env)
    _assert_auth(spec, expanded=expand_env is not False)


def test_uploaded_bundle_and_agent_cache_keep_auth_references(tmp_path: Path) -> None:
    """Bundle validation and the agent cache's default load do not expand executor auth."""
    bundle = _bundle(_agent_config())
    _assert_auth(validate_agent_bundle(bundle), expanded=False)

    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    artifacts.put("agent/bundle", bundle)
    loaded = AgentCache(artifacts, tmp_path / "cache").load("test_agent", "agent/bundle")
    _assert_auth(loaded.spec, expanded=False)


@pytest.mark.parametrize("expand_env", [False, True])
def test_loader_applies_expand_env_to_prompt_policy_executor(expand_env: bool) -> None:
    """A prompt policy's executor auth follows the same ``expand_env`` setting."""
    config = _agent_config()
    config["policies"] = {
        "review": {"type": "prompt", "prompt": "Review", "executor": _executor()}
    }
    agent = load_agent_def(config, expand_env=expand_env)
    policy = agent.policies["review"]
    assert isinstance(policy, PromptPolicy)
    assert isinstance(policy.executor, ExecutorSpec)
    auth = policy.executor.auth
    assert isinstance(auth, ApiKeyAuth)
    assert auth.api_key == (_KEY_VALUE if expand_env else _KEY_REF)
    assert auth.base_url == (_URL_VALUE if expand_env else _URL_REF)
