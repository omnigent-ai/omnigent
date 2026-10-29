"""Registry validation, route and catalog checks, and launch ownership.

Synthetic identities only; no host processes or credentials are used.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from omnigent.server import host_accounts
from omnigent.server.host_accounts import (
    HOST_ACCOUNTS_FILE_ENV,
    HostAccountRegistry,
    load_account_registry,
    require_no_account_transition,
    validate_target_workspace,
    validate_transition_launch,
)


def registry_data() -> dict:
    return {
        "version": 1,
        "accounts": [
            {
                "id": "openai-personal",
                "label": "OpenAI personal",
                "provider": "openai",
                "harness": "codex-native",
                "placements": [
                    {
                        "host_id": "machine-a",
                        "machine_id": "machine-a",
                        "machine_label": "Machine A",
                    },
                    {
                        "host_id": "machine-b",
                        "machine_id": "machine-b",
                        "machine_label": "Machine B",
                    },
                ],
            },
            {
                "id": "claude-personal",
                "label": "Claude personal",
                "provider": "anthropic",
                "harness": "claude-native",
                "placements": [
                    {
                        "host_id": "machine-a",
                        "machine_id": "machine-a",
                        "machine_label": "Machine A",
                    }
                ],
            },
        ],
    }


def test_one_account_on_two_machines_and_two_accounts_on_one_host() -> None:
    registry = HostAccountRegistry.model_validate(registry_data())
    assert registry.resolve("machine-a", "codex-native").id == "openai-personal"
    assert registry.resolve("machine-b", "codex-native").id == "openai-personal"
    assert registry.resolve("machine-a", "claude-native").id == "claude-personal"


@pytest.mark.parametrize("account_id", [None, "openai-personal"])
def test_unknown_host_cannot_bypass_registry_by_omitting_account(
    account_id: str | None,
) -> None:
    registry = HostAccountRegistry.model_validate(registry_data())
    with pytest.raises(HTTPException, match="not registered"):
        registry.resolve("unregistered", "codex-native", account_id)


def test_dual_capability_does_not_grant_undeclared_route() -> None:
    registry = HostAccountRegistry.model_validate(registry_data())
    with pytest.raises(HTTPException, match="not registered"):
        registry.resolve("machine-b", "claude-native")


def test_explicit_account_must_match_route() -> None:
    registry = HostAccountRegistry.model_validate(registry_data())
    with pytest.raises(HTTPException, match="does not match"):
        registry.resolve("machine-a", "codex-native", "claude-personal")


@pytest.mark.parametrize("harness", ["hermes-native", "pi-native", "custom-harness", None])
def test_unrelated_harnesses_are_not_routed(harness: str | None) -> None:
    registry = HostAccountRegistry.model_validate(registry_data())
    assert registry.resolve("machine-b", harness) is None


@pytest.mark.parametrize(
    "kind", ["id", "route", "provider", "secret", "placement", "empty", "version"]
)
def test_malformed_config_rejected(kind: str) -> None:
    data = registry_data()
    account = data["accounts"][0]
    if kind == "id":
        data["accounts"][1]["id"] = account["id"]
    elif kind == "route":
        account["placements"].append(account["placements"][0])
    elif kind == "provider":
        account["provider"] = "anthropic"
    elif kind == "secret":
        account["credential_path"] = "/not-a-supported-setting"
    elif kind == "placement":
        account["placements"][0]["launch_config"] = "forbidden"
    elif kind == "empty":
        account["placements"] = []
    else:
        data["version"] = 2
    with pytest.raises(ValidationError):
        HostAccountRegistry.model_validate(data)


def test_missing_registry_is_none_but_explicit_bad_file_fails_closed(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv(HOST_ACCOUNTS_FILE_ENV, raising=False)
    assert load_account_registry() is None
    path = tmp_path / "accounts.json"
    monkeypatch.setenv(HOST_ACCOUNTS_FILE_ENV, str(path))
    with pytest.raises(RuntimeError, match="Invalid explicit"):
        load_account_registry()
    path.write_text("{")
    with pytest.raises(RuntimeError, match="Invalid explicit"):
        load_account_registry()
    path.write_text(json.dumps(registry_data()))
    assert load_account_registry().resolve("machine-a", "codex-native").id == "openai-personal"
    monkeypatch.setenv(HOST_ACCOUNTS_FILE_ENV, "")
    with pytest.raises(RuntimeError, match="Invalid explicit"):
        load_account_registry()


def request_for(registry: HostAccountRegistry | None) -> SimpleNamespace:
    """A request whose app state carries the startup registry snapshot."""
    state = SimpleNamespace(account_registry=registry)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def claude_catalog() -> dict:
    return {
        "status": "ok",
        "models": [
            {
                "id": "opus",
                "model": "claude-sample-model",
                "displayName": "Opus",
                "isDefault": True,
            }
        ],
        "routable_models": ["opus", "claude-sample-model"],
    }


@pytest.mark.asyncio
async def test_claude_catalog_without_effort_list_uses_harness_efforts(monkeypatch) -> None:
    async def catalog(**kwargs):
        return claude_catalog()

    monkeypatch.setattr(host_accounts, "request_host_model_options", catalog)
    request = request_for(HostAccountRegistry.model_validate(registry_data()))
    host_registry = SimpleNamespace(get=lambda host_id: object())
    for model in ["opus", "claude-sample-model"]:
        selected = await host_accounts.validate_account_selection(
            request,
            host_id="machine-a",
            harness="claude-native",
            model=model,
            effort="high",
            host_registry=host_registry,
        )
        assert selected.id == "claude-personal"
    with pytest.raises(HTTPException, match="Effort"):
        await host_accounts.validate_account_selection(
            request,
            host_id="machine-a",
            harness="claude-native",
            model="opus",
            effort="minimal",
            host_registry=host_registry,
        )


@pytest.mark.asyncio
async def test_selection_rejects_unknown_model_offline_host_and_unconfigured_account() -> None:
    request = request_for(HostAccountRegistry.model_validate(registry_data()))
    with pytest.raises(HTTPException, match="offline"):
        await host_accounts.validate_account_selection(
            request,
            host_id="machine-a",
            harness="claude-native",
            model=None,
            effort=None,
            host_registry=SimpleNamespace(get=lambda host_id: None),
        )
    with pytest.raises(HTTPException, match="not configured"):
        await host_accounts.validate_account_selection(
            request_for(None),
            host_id="machine-a",
            harness="claude-native",
            model=None,
            effort=None,
            host_registry=None,
            account_id="claude-personal",
        )
    assert (
        await host_accounts.validate_account_selection(
            request_for(None),
            host_id="anything",
            harness="claude-native",
            model="anything",
            effort=None,
            host_registry=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_unknown_model_is_rejected_by_the_host_catalog(monkeypatch) -> None:
    async def catalog(**kwargs):
        return claude_catalog()

    monkeypatch.setattr(host_accounts, "request_host_model_options", catalog)
    with pytest.raises(HTTPException, match="Model is not offered"):
        await host_accounts.validate_account_selection(
            request_for(HostAccountRegistry.model_validate(registry_data())),
            host_id="machine-a",
            harness="claude-native",
            model="not-in-catalog",
            effort=None,
            host_registry=SimpleNamespace(get=lambda host_id: object()),
        )


def conversation(marker: dict | None, runner_id: str | None = None, **fields) -> SimpleNamespace:
    values = {"account_transition": marker, "runner_id": runner_id}
    values.update({"model_override": "opus", "reasoning_effort": "high", **fields})
    return SimpleNamespace(**values)


MARKER = {
    "id": "move-1",
    "account_id": "claude-personal",
    "host_id": "machine-a",
    "harness": "claude-native",
    "model": "opus",
    "effort": "high",
    "workspace": "/work/project",
    "launch_runner_id": "attempt-1",
}


def launch(conv, **overrides):
    values = {
        "host_id": "machine-a",
        "harness": "claude-native",
        "account_id": "claude-personal",
        "transition_id": "move-1",
        "workspace": "/work/project",
    }
    return validate_transition_launch(conv, **{**values, **overrides})


def test_launch_must_match_the_recorded_destination() -> None:
    conv = conversation(MARKER)
    assert launch(conv) is MARKER
    for override in [
        {"host_id": "machine-b"},
        {"account_id": "other"},
        {"transition_id": "stale"},
        {"transition_id": None},
        {"workspace": "/elsewhere"},
        {"harness": "codex-native"},
    ]:
        with pytest.raises(HTTPException) as caught:
            launch(conv, **override)
        assert caught.value.status_code == 409
    with pytest.raises(HTTPException, match="stale"):
        launch(conversation(None))
    assert launch(conversation(None), transition_id=None) is None
    assert launch(conversation({**MARKER, "workspace": None}), workspace="/anywhere")


def test_pending_move_lets_through_only_its_own_launch_runner() -> None:
    require_no_account_transition(conversation(None))
    require_no_account_transition(conversation(None, runner_id="any"), runner_id="any")
    # User events and detached or stray runners are blocked.
    for conv, runner in [
        (conversation(MARKER), None),
        (conversation(MARKER), "attempt-1"),
        (conversation(MARKER, runner_id="attempt-1"), None),
        (conversation(MARKER, runner_id="stray"), "stray"),
        (conversation(MARKER, runner_id="attempt-1"), "stray"),
    ]:
        with pytest.raises(Exception, match="pending"):
            require_no_account_transition(conv, runner_id=runner)
    require_no_account_transition(
        conversation(MARKER, runner_id="attempt-1"), runner_id="attempt-1"
    )


@pytest.mark.parametrize("path", [None, "/home/dev/project", "C:\\work", "D:/work"])
def test_target_workspace_accepts_absolute_paths(path: str | None) -> None:
    validate_target_workspace(path)


@pytest.mark.parametrize("path", ["", "relative/dir", "~/project", "/a\x00b", "/" + "x" * 1024])
def test_target_workspace_rejects_other_values(path: str) -> None:
    with pytest.raises(HTTPException) as caught:
        validate_target_workspace(path)
    assert caught.value.status_code == 400
