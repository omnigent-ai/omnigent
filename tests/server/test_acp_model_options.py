"""Unit tests for the ACP model-picker branch in ``_fetch_model_options``."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from omnigent.entities.conversation import Conversation
from omnigent.errors import OmnigentError
from omnigent.runner.app import _build_spawn_env_from_spec
from omnigent.server.routes._sessions import orchestration as orch
from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth


@pytest.fixture(autouse=True)
def _clear_model_options_caches() -> None:
    """Model-option caches are module-global; isolate one test from the next."""
    orch._model_options_cache.clear()
    orch._model_options_stale.clear()
    orch._model_options_inflight.clear()
    orch._pushed_model_options_cache.clear()


def _conv(**overrides: object) -> Conversation:
    """Minimal conversation for ACP picker-path testing."""
    defaults: dict[str, object] = {
        "id": "conv_acp",
        "created_at": 1700000000,
        "updated_at": 1700000001,
        "root_conversation_id": "conv_acp",
        "harness_override": None,
        "agent_id": "agent_1",
        "sub_agent_name": None,
    }
    defaults.update(overrides)
    return Conversation(**defaults)  # type: ignore[arg-type]


def test_resolve_harness_is_acp_detects_canonical_acp() -> None:
    """An explicit ``harness_override: acp`` matches.

    Other ``acp:<slug>`` harnesses also canonicalize to ``acp``, but the
    override path exercises the detection without an agent store.
    """
    conv = _conv(harness_override="acp")
    assert orch._resolve_harness_impl_is_acp(conv, None) is True


def test_resolve_harness_is_acp_rejects_other_harnesses() -> None:
    conv = _conv(harness_override="claude-native")
    assert orch._resolve_harness_impl_is_acp(conv, None) is False


def test_resolve_harness_is_acp_false_without_override_or_spec() -> None:
    """No override + no agent_id → harness unknown → not acp."""
    conv = _conv(harness_override=None, agent_id=None)
    assert orch._resolve_harness_impl_is_acp(conv, None) is False


def test_resolve_harness_is_acp_for_nested_subagent() -> None:
    """A nested ACP worker is detected even when its bundle root uses another harness."""
    worker = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": "acp:synthetic"}),
    )
    middle = AgentSpec(spec_version=1, name="middle", sub_agents=[worker])
    root = AgentSpec(
        spec_version=1,
        name="root",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[middle],
    )
    cache = MagicMock()
    cache.load.return_value.spec = root
    with patch("omnigent.runtime.get_agent_cache", return_value=cache):
        assert orch._resolve_harness_impl_is_acp(_conv(sub_agent_name="worker"), MagicMock())


@pytest.mark.asyncio
async def test_load_acp_model_options_returns_empty_without_agent_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No agent store and no runtime global → no spec → empty options."""
    # Another test in the same shard may have left a runtime-installed store
    # behind; pin the global to None so this branch is exercised, not the
    # ambient DB-backed store (whose agents id column rejects "agent_1").
    monkeypatch.setattr("omnigent.runtime._globals._agent_store", None)
    conv = _conv()
    result = await orch._load_acp_model_options("conv_acp", conv, None)
    assert result == []


@pytest.mark.asyncio
async def test_load_acp_model_options_caches_and_serves() -> None:
    """A resolved curated shortlist fills the cache and returns on hit."""
    conv = _conv()
    curated = ("gpt-5.4", "claude-fable-5")

    with (
        patch.object(orch, "_load_agent_spec_for_session", return_value=MagicMock()) as mock_load,
        patch.object(orch, "_publish_model_options", return_value=None) as mock_publish,
        patch("omnigent.models.model_catalog.acp_curated_models", return_value=curated),
        patch("omnigent.models.model_catalog._acp_launch_model", return_value=curated[0]),
    ):
        options = await orch._load_acp_model_options("conv_acp", conv, MagicMock())
        assert options == [
            {"id": "gpt-5.4", "displayName": "gpt-5.4", "isDefault": True},
            {"id": "claude-fable-5", "displayName": "claude-fable-5", "isDefault": False},
        ]
        mock_publish.assert_called_once_with("conv_acp")

        # Second call: cache hit - no re-resolution.
        mock_load.reset_mock()
        cached = await orch._load_acp_model_options("conv_acp", conv, MagicMock())
        assert cached == options
        mock_load.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("curated", [(), ("only-model",)])
async def test_load_acp_model_options_caches_empty_catalog(curated: tuple[str, ...]) -> None:
    """An uncurated session does not reread configuration on every snapshot."""
    conv = _conv()

    with (
        patch.object(orch, "_load_agent_spec_for_session", return_value=MagicMock()) as mock_load,
        patch("omnigent.models.model_catalog.acp_curated_models", return_value=curated) as resolve,
        patch.object(orch, "_publish_model_options"),
    ):
        assert await orch._load_acp_model_options("conv_acp", conv, MagicMock()) == []
        assert await orch._load_acp_model_options("conv_acp", conv, MagicMock()) == []
        assert orch._model_options_cache["conv_acp"] == []
        mock_load.assert_called_once()
        resolve.assert_called_once()


@pytest.mark.asyncio
async def test_load_acp_model_options_resolves_provider_off_event_loop() -> None:
    """Provider configuration I/O must not block unrelated session requests."""
    event_loop_thread = threading.get_ident()
    resolver_threads: list[int] = []

    def resolve(_spec: object) -> tuple[str, ...]:
        resolver_threads.append(threading.get_ident())
        return ("gpt-5.4", "claude-fable-5")

    def default_model(_spec: object) -> str:
        resolver_threads.append(threading.get_ident())
        return "gpt-5.4"

    with (
        patch.object(orch, "_load_agent_spec_for_session", return_value=MagicMock()),
        patch("omnigent.models.model_catalog.acp_curated_models", side_effect=resolve),
        patch("omnigent.models.model_catalog._acp_launch_model", side_effect=default_model),
        patch.object(orch, "_publish_model_options"),
    ):
        await orch._load_acp_model_options("conv_acp", _conv(), MagicMock())

    assert len(resolver_threads) == 2
    assert all(thread != event_loop_thread for thread in resolver_threads)


@pytest.mark.asyncio
@pytest.mark.parametrize("default_source", ["spec", "agent"])
async def test_acp_picker_default_matches_runtime_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    default_source: str,
) -> None:
    """The provider's first row need not be the model restored on reset."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "gateway": {
                        "kind": "gateway",
                        "openai": {
                            "base_url": "https://gateway.example/v1",
                            "api_key": "synthetic",
                            "models": {"default": "model-a", "small": "model-b"},
                        },
                    }
                }
            }
        )
    )
    agent = {"name": "Test", "command": "fake-cli"}
    if default_source == "agent":
        agent["model"] = "model-b"
    spec = AgentSpec(
        spec_version=1,
        name="test-acp",
        executor=ExecutorSpec(
            type="omnigent",
            model="model-b" if default_source == "spec" else None,
            auth=ProviderAuth(name="gateway"),
            config={"harness": "acp", "acp_agent": agent},
        ),
    )
    conv = _conv(harness_override="acp", model_override="model-a")
    with (
        patch.object(orch, "_load_agent_spec_for_session", return_value=spec),
        patch.object(orch, "_publish_model_options"),
    ):
        options = await orch._load_acp_model_options(conv.id, conv, MagicMock())
    spawn_env = _build_spawn_env_from_spec(spec, "acp", model_override="model-a")

    assert options == [
        {"id": "model-a", "displayName": "model-a", "isDefault": False},
        {"id": "model-b", "displayName": "model-b", "isDefault": True},
    ]
    assert spawn_env is not None
    assert spawn_env["HARNESS_ACP_DEFAULT_MODEL"] == "model-b"


@pytest.mark.parametrize(
    ("override", "wrapper"),
    [
        ("claude-sdk", None),
        ("claude-native", None),
        ("auto", None),
        (None, "claude-code-native-ui"),
    ],
)
def test_model_validation_preserves_known_non_acp_sessions(
    override: str | None, wrapper: str | None
) -> None:
    """Trusted harness metadata keeps non-ACP model changes independent of bundle availability."""
    conv = _conv(
        harness_override=override, labels={"omnigent.wrapper": wrapper} if wrapper else {}
    )
    with patch.object(
        orch, "_load_agent_spec_for_session", side_effect=OSError("bundle unavailable")
    ) as load:
        orch._validate_session_model_selection(conv, "model-a", MagicMock())
    load.assert_not_called()


def test_acp_override_requires_spec_even_with_native_wrapper_label() -> None:
    """The persisted harness override wins over a stale native presentation label."""
    conv = _conv(
        harness_override="acp:synthetic", labels={"omnigent.wrapper": "claude-code-native-ui"}
    )
    with (
        patch.object(orch, "_load_agent_spec_for_session", side_effect=OSError("unavailable")),
        pytest.raises(OmnigentError, match="Cannot load the session's agent spec"),
    ):
        orch._validate_session_model_selection(conv, "model-a", MagicMock())


def test_model_validation_rejects_unknown_harness() -> None:
    """An unknown wrapper and unavailable spec cannot opt out of model policy checks."""
    with (
        patch.object(orch, "_load_agent_spec_for_session", return_value=None),
        pytest.raises(OmnigentError, match="Cannot resolve the session's agent spec"),
    ):
        orch._validate_session_model_selection(
            _conv(labels={"omnigent.wrapper": "unknown-wrapper"}), "model-a", MagicMock()
        )


def test_model_validation_applies_inherited_acp_harness_to_nested_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A nested worker without its own harness still applies its provider's ACP catalog."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "worker": {
                        "kind": "gateway",
                        "openai": {
                            "base_url": "https://gateway.example.invalid/v1",
                            "api_key": "synthetic",
                            "models": {"default": "model-a", "large": "model-b"},
                        },
                    }
                }
            }
        )
    )
    worker = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(type="omnigent", model="model-a", auth=ProviderAuth(name="worker")),
    )
    middle = AgentSpec(spec_version=1, name="middle", sub_agents=[worker])
    root = AgentSpec(
        spec_version=1,
        name="root",
        executor=ExecutorSpec(type="omnigent", config={"harness": "acp:synthetic"}),
        sub_agents=[middle],
    )
    with patch.object(orch, "_load_agent_spec_for_session", return_value=root):
        with pytest.raises(OmnigentError, match="configured model list"):
            orch._validate_session_model_selection(
                _conv(sub_agent_name="worker"), "model-c", MagicMock()
            )
        orch._validate_session_model_selection(
            _conv(sub_agent_name="worker"), "model-b", MagicMock()
        )
