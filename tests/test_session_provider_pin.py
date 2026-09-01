"""Explicit Codex-native provider pins never fall back to another account."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.codex_native_app_server as app_server
from omnigent.errors import OmnigentError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _config(**providers: object) -> dict[str, object]:
    return {"providers": dict(providers)}


def _gateway(env_var: str) -> dict[str, object]:
    return {
        "kind": "gateway",
        "openai": {
            "base_url": "https://gateway.example/v1",
            "api_key_ref": f"env:{env_var}",
        },
    }


def test_unknown_pin_fails_instead_of_using_a_valid_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIN_GOOD_KEY", "good")
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: _config(good={**_gateway("PIN_GOOD_KEY"), "default": "openai"}),
    )

    with pytest.raises(OmnigentError, match="not configured"):
        app_server.resolve_native_codex_launch(model=None, provider_name="stale account")


def test_exact_pin_ignores_an_unrelated_malformed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_name = "Work account / 東京 --model"
    monkeypatch.setenv("PIN_WORK_KEY", "work")
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: {
            "providers": {
                provider_name: _gateway("PIN_WORK_KEY"),
                "broken unrelated provider": 5,
            }
        },
    )

    launch = app_server.resolve_native_codex_launch(model=None, provider_name=provider_name)

    assert f"pinned provider {provider_name!r}" in launch.summary


def test_empty_pin_fails_even_if_the_yaml_contains_an_empty_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIN_EMPTY_KEY", "value")
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: _config(**{"": _gateway("PIN_EMPTY_KEY")}),
    )

    with pytest.raises(OmnigentError, match="must not be empty"):
        app_server.resolve_native_codex_launch(model=None, provider_name="")


def test_incompatible_pin_fails_instead_of_using_a_valid_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIN_GOOD_KEY", "good")
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: _config(
            claude={"kind": "subscription", "cli": "claude"},
            good={**_gateway("PIN_GOOD_KEY"), "default": "openai"},
        ),
    )

    with pytest.raises(OmnigentError, match="cannot serve codex-native"):
        app_server.resolve_native_codex_launch(model=None, provider_name="claude")


def test_unavailable_pin_fails_instead_of_using_another_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIN_MISSING_KEY", raising=False)
    monkeypatch.setenv("PIN_GOOD_KEY", "good")
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: _config(
            missing=_gateway("PIN_MISSING_KEY"),
            good={**_gateway("PIN_GOOD_KEY"), "default": "openai"},
        ),
    )

    with pytest.raises(OmnigentError, match="unavailable"):
        app_server.resolve_native_codex_launch(model=None, provider_name="missing")


def test_logged_out_subscription_pin_preserves_login_required_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_home = tmp_path / "codex-work"
    monkeypatch.setenv("PIN_GOOD_KEY", "good")
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: _config(
            work={"kind": "subscription", "cli": "codex", "cli_home": str(work_home)},
            good={**_gateway("PIN_GOOD_KEY"), "default": "openai"},
        ),
    )
    monkeypatch.setattr("omnigent.onboarding.ambient.codex_auth_has_credential", lambda _p: False)

    launch = app_server.resolve_native_codex_launch(model=None, provider_name="work")

    assert launch.login_required is True
    assert launch.cli_home == work_home
    assert launch.config_overrides == ['model_provider="openai"']
    assert "pinned provider 'work'" in launch.summary


def test_provider_pin_round_trips_and_switch_clears_only_when_incompatible(
    db_uri: str,
) -> None:
    conversation_store = SqlAlchemyConversationStore(db_uri)
    conversation = conversation_store.create_conversation()
    pinned = conversation_store.update_conversation(
        conversation.id,
        provider_override="Work account / 東京 --model",
    )
    assert pinned is not None
    assert pinned.provider_override == "Work account / 東京 --model"

    cleared = conversation_store.switch_conversation_agent(
        conversation.id,
        new_agent_id="a" * 32,
        new_agent_name="claude switch",
        new_agent_bundle_location="bundle/claude",
        new_agent_description=None,
        copy_model_settings=False,
        carry_history_into_native=False,
        presentation_labels={},
        previous_builtin_id=None,
    )
    assert cleared.provider_override is None

    repinned = conversation_store.update_conversation(
        conversation.id,
        provider_override="Work account / 東京 --model",
    )
    assert repinned is not None
    preserved = conversation_store.switch_conversation_agent(
        conversation.id,
        new_agent_id="b" * 32,
        new_agent_name="codex switch",
        new_agent_bundle_location="bundle/codex",
        new_agent_description=None,
        copy_model_settings=True,
        carry_history_into_native=True,
        presentation_labels={},
        previous_builtin_id=None,
        preserve_provider_override=True,
    )
    assert preserved.provider_override == "Work account / 東京 --model"
