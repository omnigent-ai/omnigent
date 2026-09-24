"""Exercise ACP model policy through persisted sessions and runner launch wiring."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.app import _build_spawn_env_from_spec
from omnigent.runtime import get_agent_cache, get_agent_store, get_conversation_store
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.helpers import _load_agent_spec_for_session
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio

_DEFAULT = "catalog-model-default"
_ALTERNATE = "catalog-model-alternate"
_CHILD_DEFAULT = "worker-model-default"
_CHILD_ALTERNATE = "worker-model-alternate"
_UNLISTED = "another-model"


@pytest.fixture(autouse=True)
def _isolated_provider_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Use synthetic provider configuration without ambient credentials or network discovery."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    providers: dict[str, Any] = {}
    for name, default, alternate in (
        ("curated", _DEFAULT, _ALTERNATE),
        ("worker", _CHILD_DEFAULT, _CHILD_ALTERNATE),
        ("default-only", _DEFAULT, None),
    ):
        models = {"default": default}
        if alternate is not None:
            models["alternate"] = alternate
        providers[name] = {
            "kind": "gateway",
            "default": name == "curated",
            "openai": {
                "base_url": "https://gateway.example.invalid/v1",
                "api_key": "fake-test-key",
                "models": models,
            },
        }
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": providers,
                "acp": {"agents": [{"name": "Synthetic", "command": "synthetic-acp"}]},
            }
        )
    )


def _executor(provider: str | None = "curated") -> dict[str, Any]:
    """Build an ACP executor whose command is never started by these metadata-only tests."""
    executor: dict[str, Any] = {
        "type": "omnigent",
        "config": {"harness": "acp:synthetic"},
    }
    if provider is not None:
        executor["auth"] = {"type": "provider", "name": provider}
    return executor


async def _session(client: httpx.AsyncClient, *, provider: str | None = "curated") -> str:
    """Create a real stored ACP bundle and return its owning session id."""
    agent = await create_test_agent(client, executor=_executor(provider), include_llm=False)
    return str(agent["_session_id"])


def _launch_env(session_id: str) -> dict[str, str]:
    """Build the runner environment from the same stored spec and override the API uses."""
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None
    spec = _load_agent_spec_for_session(conv, get_agent_store())
    assert spec is not None
    env = _build_spawn_env_from_spec(spec, "acp:synthetic", model_override=conv.model_override)
    assert env is not None
    return env


def _break_provider_resolution(
    failure: str, monkeypatch: pytest.MonkeyPatch, config_home: Path
) -> int:
    """Make a previously configured provider unavailable and return its expected HTTP error."""
    if failure == "missing-provider":
        path = config_home / "config.yaml"
        config = yaml.safe_load(path.read_text())
        del config["providers"]["curated"]
        path.write_text(yaml.safe_dump(config))
        return 400

    def fail_resolution(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("provider configuration unavailable")

    monkeypatch.setattr("omnigent.runtime.workflow._resolve_provider_for_build", fail_resolution)
    return 500


async def test_approved_model_patch_reaches_runner_without_changing_catalog(
    client: httpx.AsyncClient,
) -> None:
    """A picker selection persists and changes launch model without replacing the default."""
    session_id = await _session(client)
    before = await client.get(f"/v1/sessions/{session_id}")
    assert before.status_code == 200, before.text
    options = before.json()["model_options"]
    assert [option["id"] for option in options] == [_DEFAULT, _ALTERNATE]
    assert options[0]["isDefault"] is True

    response = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert response.status_code == 200, response.text
    orchestration._model_options_cache.pop(session_id, None)
    after = await client.get(f"/v1/sessions/{session_id}")
    assert after.status_code == 200, after.text
    assert after.json()["model_override"] == _ALTERNATE
    assert after.json()["model_options"] == options

    env = _launch_env(session_id)
    assert env["HARNESS_ACP_MODEL"] == _ALTERNATE
    assert env["HARNESS_ACP_DEFAULT_MODEL"] == _DEFAULT
    assert env["HARNESS_ACP_MODEL_LIST"].split(",") == [_DEFAULT, _ALTERNATE]


async def test_unlisted_model_patch_does_not_mutate_session_metadata(
    client: httpx.AsyncClient,
) -> None:
    """An ordinary but unconfigured model id is rejected before any PATCH fields are persisted."""
    session_id = await _session(client)
    selected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert selected.status_code == 200, selected.text
    before = (await client.get(f"/v1/sessions/{session_id}")).json()

    rejected = await client.patch(
        f"/v1/sessions/{session_id}",
        json={
            "model_override": _UNLISTED,
            "title": "This title should not be stored",
            "labels": {"test.acp-rejected": "true"},
            "silent": True,
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert "configured model list" in rejected.text
    after = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert after["model_override"] == _ALTERNATE
    assert after["title"] == before["title"]
    assert after["labels"] == before["labels"]
    assert after["model_options"] == before["model_options"]


async def test_model_reset_restores_provider_default(client: httpx.AsyncClient) -> None:
    """Reset clears persistence and subsequent launches use the original provider default."""
    session_id = await _session(client)
    selected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert selected.status_code == 200, selected.text
    reset = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": "default", "silent": True}
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["model_override"] is None
    env = _launch_env(session_id)
    assert env["HARNESS_ACP_MODEL"] == _DEFAULT
    assert env["HARNESS_ACP_DEFAULT_MODEL"] == _DEFAULT
    assert env["HARNESS_ACP_MODEL_LIST"].split(",") == [_DEFAULT, _ALTERNATE]


async def test_child_session_uses_its_own_provider_catalog(client: httpx.AsyncClient) -> None:
    """A bundled worker advertises and validates its own provider rather than its parent's."""
    agent = await create_test_agent(
        client,
        executor=_executor(),
        include_llm=False,
        sub_agents=[
            {"name": "worker", "executor": {**_executor("worker"), "model": _CHILD_DEFAULT}}
        ],
    )
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": agent["_session_id"],
            "sub_agent_name": "worker",
            "initial_items": [],
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert [option["id"] for option in snapshot["model_options"]] == [
        _CHILD_DEFAULT,
        _CHILD_ALTERNATE,
    ]

    rejected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert rejected.status_code == 400, rejected.text
    accepted = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"model_override": _CHILD_ALTERNATE, "silent": True},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["model_override"] == _CHILD_ALTERNATE


@pytest.mark.parametrize("root_harness", ["acp:synthetic", "claude-sdk"])
async def test_nested_child_uses_its_own_harness_and_provider(
    client: httpx.AsyncClient, root_harness: str
) -> None:
    """A grandchild's picker and PATCH policy match the recursively resolved launch spec."""
    root_executor = {**_executor(), "config": {"harness": root_harness}}
    files = {
        "config.yaml": {
            "spec_version": 1,
            "name": "root",
            "executor": root_executor,
            "tools": {"agents": ["middle"]},
        },
        "agents/middle/config.yaml": {
            "spec_version": 1,
            "name": "middle",
            "executor": root_executor,
            "tools": {"agents": ["worker"]},
        },
        "agents/middle/agents/worker/config.yaml": {
            "spec_version": 1,
            "name": "worker",
            "executor": {**_executor("worker"), "model": _CHILD_DEFAULT},
        },
    }
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        for path, config in files.items():
            content = yaml.safe_dump(config).encode()
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    uploaded = await client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={"bundle": ("nested.tar.gz", bundle.getvalue(), "application/gzip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    root_id = uploaded.json()["session_id"]
    agent = (await client.get(f"/v1/sessions/{root_id}/agent")).json()
    middle = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": root_id,
            "sub_agent_name": "middle",
            "initial_items": [],
        },
    )
    assert middle.status_code == 201, middle.text
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": middle.json()["id"],
            "sub_agent_name": "worker",
            "model_override": _CHILD_ALTERNATE,
            "initial_items": [],
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert snapshot["harness"] == "acp"
    assert [option["id"] for option in snapshot["model_options"]] == [
        _CHILD_DEFAULT,
        _CHILD_ALTERNATE,
    ]

    rejected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert rejected.status_code == 400, rejected.text
    accepted = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"model_override": _CHILD_DEFAULT, "silent": True},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["model_override"] == _CHILD_DEFAULT


async def _agent_with_removed_default(
    client: httpx.AsyncClient, config_home: Path
) -> dict[str, Any]:
    """Create a valid agent, then remove its pinned default from the provider's catalog."""
    path = config_home / "config.yaml"
    original = path.read_text()
    config = yaml.safe_load(original)
    config["providers"]["curated"]["openai"]["models"]["temporary"] = _UNLISTED
    path.write_text(yaml.safe_dump(config))
    try:
        return await create_test_agent(
            client, executor={**_executor(), "model": _UNLISTED}, include_llm=False
        )
    finally:
        path.write_text(original)


@pytest.mark.parametrize("model", [None, _ALTERNATE])
async def test_invalid_default_rejects_create_before_persistence(
    client: httpx.AsyncClient, tmp_path: Path, model: str | None
) -> None:
    """Creation rejects an invalid default with or without a listed override."""
    agent = await _agent_with_removed_default(client, tmp_path)
    store = get_conversation_store()
    before_ids = {conv.id for conv in store.list_conversations().data}
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "model_override": model, "initial_items": []},
    )
    assert response.status_code == 400, response.text
    assert "configured model list" in response.text
    assert {conv.id for conv in store.list_conversations().data} == before_ids


async def test_invalid_default_rejects_multipart_create_before_persistence(
    client: httpx.AsyncClient,
) -> None:
    """Uploading an ACP bundle cannot persist a session with an invalid default."""
    bundle = build_agent_bundle(
        "invalid-acp", executor={**_executor(), "model": _UNLISTED}, include_llm=False
    )
    store = get_conversation_store()
    before_ids = {conv.id for conv in store.list_conversations().data}
    response = await client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert response.status_code == 400, response.text
    assert "configured model list" in response.text
    assert {conv.id for conv in store.list_conversations().data} == before_ids


@pytest.mark.parametrize("model", [_ALTERNATE, "default"])
async def test_invalid_default_rejects_patch_without_mutating_metadata(
    client: httpx.AsyncClient, tmp_path: Path, model: str
) -> None:
    """Both selecting and resetting require a default inside the curated catalog."""
    agent = await _agent_with_removed_default(client, tmp_path)
    session_id = agent["_session_id"]
    before = (await client.get(f"/v1/sessions/{session_id}")).json()
    response = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"model_override": model, "title": "Must not change", "silent": True},
    )
    assert response.status_code == 400, response.text
    after = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert after["model_override"] == before["model_override"]
    assert after["title"] == before["title"]


@pytest.mark.parametrize("provider", [None, "default-only"])
async def test_unrelated_or_default_only_config_keeps_model_selection_unrestricted(
    client: httpx.AsyncClient, provider: str | None
) -> None:
    """Existing vendor-owned agents and default-only providers retain arbitrary model selection."""
    session_id = await _session(client, provider=provider)
    response = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _UNLISTED, "silent": True}
    )
    assert response.status_code == 200, response.text
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert snapshot["model_override"] == _UNLISTED
    assert snapshot["model_options"] == []
    env = _launch_env(session_id)
    assert env["HARNESS_ACP_MODEL"] == _UNLISTED
    assert "HARNESS_ACP_MODEL_LIST" not in env


@pytest.mark.parametrize("model, expected_status", [(_ALTERNATE, 201), (_UNLISTED, 400)])
async def test_create_session_validates_model_override(
    client: httpx.AsyncClient, model: str, expected_status: int
) -> None:
    """The create endpoint applies the same curated selection policy before storing an override."""
    agent = await create_test_agent(client, executor=_executor(), include_llm=False)
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "model_override": model, "initial_items": []},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 201:
        session_id = response.json()["id"]
        snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
        assert snapshot["model_override"] == _ALTERNATE
        env = _launch_env(session_id)
        assert env["HARNESS_ACP_MODEL"] == _ALTERNATE
        assert env["HARNESS_ACP_MODEL_LIST"].split(",") == [_DEFAULT, _ALTERNATE]


@pytest.mark.parametrize("failure", ["missing-provider", "resolution-error"])
@pytest.mark.parametrize("model", [None, _ALTERNATE])
async def test_provider_resolution_failure_rejects_create_before_persistence(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    model: str | None,
) -> None:
    """A failed catalog read rejects creation before storing a row, even without an override."""
    agent = await create_test_agent(client, executor=_executor(), include_llm=False)
    store = get_conversation_store()
    before_ids = {conv.id for conv in store.list_conversations().data}
    expected_status = _break_provider_resolution(failure, monkeypatch, tmp_path)
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "model_override": model, "initial_items": []},
    )
    assert response.status_code == expected_status, response.text
    assert {conv.id for conv in store.list_conversations().data} == before_ids


@pytest.mark.parametrize("failure", ["missing-provider", "resolution-error"])
async def test_provider_resolution_failure_rejects_multipart_before_persistence(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """Multipart creation requires resolving an ACP bundle's explicit provider."""
    bundle = build_agent_bundle("invalid-acp", executor=_executor(), include_llm=False)
    store = get_conversation_store()
    before_ids = {conv.id for conv in store.list_conversations().data}
    expected_status = _break_provider_resolution(failure, monkeypatch, tmp_path)
    response = await client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert response.status_code == expected_status, response.text
    assert {conv.id for conv in store.list_conversations().data} == before_ids


@pytest.mark.parametrize("failure", ["missing-provider", "resolution-error"])
@pytest.mark.parametrize("model", [_DEFAULT, "default"])
async def test_provider_resolution_failure_rejects_patch_without_mutation(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    model: str,
) -> None:
    """Cached display options cannot authorize selections after provider resolution fails."""
    session_id = await _session(client)
    selected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert selected.status_code == 200, selected.text
    store = get_conversation_store()
    before = store.get_conversation(session_id)
    assert before is not None
    cached = orchestration._model_options_cache[session_id]
    expected_status = _break_provider_resolution(failure, monkeypatch, tmp_path)

    response = await client.patch(
        f"/v1/sessions/{session_id}",
        json={
            "model_override": model,
            "title": "Must not be persisted",
            "labels": {"test.acp-provider-error": "true"},
            "silent": True,
        },
    )
    assert response.status_code == expected_status, response.text
    after = store.get_conversation(session_id)
    assert after is not None
    assert after.model_override == _ALTERNATE
    assert after.title == before.title
    assert after.labels == before.labels
    assert orchestration._model_options_cache[session_id] == cached


@pytest.mark.parametrize(
    ("removed", "model"),
    [("selected", _ALTERNATE), ("default", _ALTERNATE), ("default", "default")],
)
async def test_cached_catalog_cannot_authorize_removed_model_or_default(
    client: httpx.AsyncClient, tmp_path: Path, removed: str, model: str
) -> None:
    """PATCH checks current policy for both the requested model and the original reset default."""
    agent = await create_test_agent(
        client, executor={**_executor(), "model": _DEFAULT}, include_llm=False
    )
    session_id = agent["_session_id"]
    original_pick = _DEFAULT if removed == "selected" else _ALTERNATE
    selected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": original_pick, "silent": True}
    )
    assert selected.status_code == 200, selected.text
    cached = orchestration._model_options_cache[session_id]
    assert {option["id"] for option in cached} == {_DEFAULT, _ALTERNATE}
    store = get_conversation_store()
    before = store.get_conversation(session_id)
    assert before is not None
    path = tmp_path / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["providers"]["curated"]["openai"]["models"] = (
        {"default": _DEFAULT, "alternate": "catalog-model-replacement"}
        if removed == "selected"
        else {"default": "catalog-model-replacement", "alternate": _ALTERNATE}
    )
    path.write_text(yaml.safe_dump(config))

    response = await client.patch(
        f"/v1/sessions/{session_id}",
        json={
            "model_override": model,
            "title": "Must not be persisted",
            "labels": {"test.acp-removed-model": "true"},
            "silent": True,
        },
    )
    assert response.status_code == 400, response.text
    assert "configured model list" in response.text
    after = store.get_conversation(session_id)
    assert after is not None
    assert after.model_override == original_pick
    assert after.title == before.title
    assert after.labels == before.labels
    assert orchestration._model_options_cache[session_id] == cached


@pytest.mark.parametrize("model", [_DEFAULT, "default"])
async def test_unloadable_bundle_rejects_model_patch_without_mutation(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """An unavailable bundle must not turn an ACP session into an unrestricted harness."""
    session_id = await _session(client)
    selected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert selected.status_code == 200, selected.text
    cached = orchestration._model_options_cache[session_id]
    store = get_conversation_store()
    before = store.get_conversation(session_id)
    assert before is not None

    def fail_load(*_args: object, **_kwargs: object) -> None:
        raise OSError("agent bundle unavailable")

    monkeypatch.setattr(get_agent_cache(), "load", fail_load)
    response = await client.patch(
        f"/v1/sessions/{session_id}",
        json={
            "model_override": model,
            "title": "Must not be persisted",
            "labels": {"test.acp-unavailable-spec": "true"},
            "silent": True,
        },
    )
    assert response.status_code == 400, response.text
    assert "Cannot load the session's agent spec" in response.text
    after = store.get_conversation(session_id)
    assert after is not None
    assert after.model_override == _ALTERNATE
    assert after.title == before.title
    assert after.labels == before.labels
    assert orchestration._model_options_cache[session_id] == cached
