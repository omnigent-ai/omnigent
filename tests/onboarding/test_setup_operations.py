from pathlib import Path

import pytest

from omnigent import cli_config
from omnigent.config import load_global_config, save_global_config
from omnigent.onboarding import interactive
from omnigent.onboarding.acp_auth import acp_agents
from omnigent.onboarding.setup_operations import acp_entries_settings, provider_add_settings


def test_replacing_openrouter_endpoint_clears_its_chat_protocol() -> None:
    old = {
        "kind": "key",
        "future_setting": {"keep": True},
        "openai": {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_ref": "keychain:openai-" + "a" * 32,
            "wire_api": "chat",
            "context_window": 200000,
            "models": {"default": "old-model", "fast": "fast-model"},
        },
    }
    config = {"providers": {"openai": old}}
    replacement = {
        "kind": "key",
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "keychain:openai-" + "b" * 32,
            "models": {"default": "gpt-5.5"},
        },
    }

    settings, _ = provider_add_settings(config, "openai", replacement, preserve_advanced=True)

    updated = settings["providers"]["openai"]
    assert updated["openai"]["base_url"] == "https://api.openai.com/v1"
    assert "wire_api" not in updated["openai"]
    assert updated["openai"]["context_window"] == 200000
    assert updated["openai"]["models"] == {
        "default": "gpt-5.5",
        "fast": "fast-model",
    }
    assert updated["future_setting"] == {"keep": True}
    assert config["providers"]["openai"] == old


@pytest.mark.parametrize("command", ["fixture --acp", "another-fixture --acp"])
def test_cli_adds_same_name_acp_occurrences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    existing = {
        "name": "Fixture",
        "command": "fixture --acp",
        "future": {"retained": True},
    }
    save_global_config({"acp": {"future": True, "agents": [existing]}})
    answers = iter(["Fixture", command, "fixture-model"])
    monkeypatch.setattr(interactive, "prompt_text", lambda *a, **kw: next(answers))
    monkeypatch.setattr(cli_config, "_print_acp_examples", lambda: None)

    cli_config._add_acp_agent()

    config = load_global_config()
    assert config["acp"] == {
        "future": True,
        "agents": [
            existing,
            {"name": "Fixture", "command": command, "model": "fixture-model"},
        ],
    }
    entries = acp_agents(config)
    assert [entry.slug for entry in entries] == ["fixture", "fixture-2"]
    save_global_config(acp_entries_settings(config, entries[:1]))
    assert load_global_config()["acp"] == {"future": True, "agents": [existing]}
