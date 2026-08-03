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
