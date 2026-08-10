from __future__ import annotations

import pytest

from omnigent.onboarding.acp_auth import acp_agents, acp_agents_settings


def test_omnigent_mcp_round_trips_only_when_disabled() -> None:
    entries = acp_agents(
        {
            "acp": {
                "agents": [
                    {
                        "name": "OpenClaw",
                        "command": "openclaw acp --url https://gateway --token token",
                        "omnigent_mcp": False,
                    },
                    {
                        "name": "Goose",
                        "command": "goose acp",
                        "model": "gpt-5.3",
                        "session_id_mode": "client",
                    },
                ]
            }
        }
    )

    assert [entry.omnigent_mcp for entry in entries] == [False, True]
    assert acp_agents_settings(entries) == {
        "acp": {
            "agents": [
                {
                    "name": "OpenClaw",
                    "command": "openclaw acp --url https://gateway --token token",
                    "omnigent_mcp": False,
                },
                {
                    "name": "Goose",
                    "command": "goose acp",
                    "model": "gpt-5.3",
                    "session_id_mode": "client",
                },
            ]
        }
    }


@pytest.mark.parametrize("value", ["false", None, 0, 1])
def test_omnigent_mcp_requires_boolean(value: object) -> None:
    with pytest.raises(ValueError, match="omnigent_mcp must be a boolean"):
        acp_agents(
            {
                "acp": {
                    "agents": [
                        {
                            "name": "OpenClaw",
                            "command": "openclaw acp",
                            "omnigent_mcp": value,
                        }
                    ]
                }
            }
        )


def test_env_passthrough_parses_and_round_trips() -> None:
    """Declared names survive parse → persist, so the config is stable."""
    entries = acp_agents(
        {
            "acp": {
                "agents": [
                    {
                        "name": "Grok Build",
                        "command": "grok agent stdio",
                        "env_passthrough": ["XAI_API_KEY", "XAI_API_KEY", " GROK_TOKEN "],
                    },
                    {"name": "Goose", "command": "goose acp"},
                ]
            }
        }
    )

    # De-duplicated, trimmed, order preserved; absent → empty.
    assert entries[0].env_passthrough == ("XAI_API_KEY", "GROK_TOKEN")
    assert entries[1].env_passthrough == ()
    assert acp_agents_settings(entries) == {
        "acp": {
            "agents": [
                {
                    "name": "Grok Build",
                    "command": "grok agent stdio",
                    "env_passthrough": ["XAI_API_KEY", "GROK_TOKEN"],
                },
                {"name": "Goose", "command": "goose acp"},
            ]
        }
    }


def test_env_passthrough_accepts_a_bare_string() -> None:
    entries = acp_agents(
        {"acp": {"agents": [{"name": "A", "command": "a", "env_passthrough": "XAI_API_KEY"}]}}
    )
    assert entries[0].env_passthrough == ("XAI_API_KEY",)


def test_env_passthrough_rejects_a_value_instead_of_a_name() -> None:
    """``NAME=secret`` would put a credential in plaintext and not reach the agent."""
    with pytest.raises(ValueError, match="NAMES, not values"):
        acp_agents(
            {
                "acp": {
                    "agents": [
                        {"name": "A", "command": "a", "env_passthrough": ["XAI_API_KEY=sk-secret"]}
                    ]
                }
            }
        )


@pytest.mark.parametrize("value", [5, {"a": 1}, [""], [None]])
def test_env_passthrough_rejects_non_names(value: object) -> None:
    with pytest.raises(ValueError, match="env_passthrough"):
        acp_agents({"acp": {"agents": [{"name": "A", "command": "a", "env_passthrough": value}]}})
