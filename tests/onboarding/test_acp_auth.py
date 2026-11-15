from __future__ import annotations

import pytest

from omnigent.onboarding.acp_auth import (
    acp_agents,
    acp_agents_settings,
    shadowed_builtin_acp_rows,
)


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


def test_shadowed_builtin_acp_rows_matches_row_ids_only() -> None:
    """Only a slug equal to a row id shadows it — an alias-shaped name does not.

    "Grok" slugifies onto the ``grok`` row id, so the two name the same harness
    and the row is dropped in favor of the user's command. "Grok Build" slugifies
    to ``grok-build``, which is a *different* harness id from the ``grok`` row
    (the ``acp:`` prefix survives canonicalization), so both stay addressable.

    "Devin" is the negative case that used to be the positive one: Devin's ACP
    row is keyed ``devin-acp`` now (the bare spelling belongs to the native
    ``devin-native`` wrap), so a configured agent named "Devin" shadows nothing.
    """
    entries = acp_agents(
        {
            "acp": {
                "agents": [
                    {"name": "Grok", "command": "grok agent stdio"},
                    {"name": "Grok Build", "command": "grok agent stdio"},
                    {"name": "Devin", "command": "devin acp --model swe-2-medium"},
                    {"name": "Gemini CLI", "command": "gemini --experimental-acp"},
                ]
            }
        }
    )
    assert [e.slug for e in entries] == ["grok", "grok-build", "devin", "gemini-cli"]
    assert shadowed_builtin_acp_rows(entries) == {"grok"}


def test_shadowed_builtin_acp_rows_empty_without_config() -> None:
    """No configured agents → no rows suppressed (the common case)."""
    assert shadowed_builtin_acp_rows([]) == frozenset()


def test_resolve_acp_harness_entry_honors_path_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Builtin ACP CLI entries use the shared env -> config -> binary lookup."""
    from omnigent.onboarding.acp_auth import resolve_acp_harness_entry

    monkeypatch.delenv("OMNIGENT_GROK_PATH", raising=False)
    cfg = {"harness": {"grok": {"command": "/opt/cfg/grok"}}}
    cfg_entry = resolve_acp_harness_entry("grok", config=cfg)
    assert cfg_entry is not None
    assert cfg_entry.command.startswith("/opt/cfg/grok ")

    monkeypatch.setenv("OMNIGENT_GROK_PATH", "/opt/env/grok")
    env_entry = resolve_acp_harness_entry("grok", config=cfg)
    assert env_entry is not None
    assert env_entry.command.startswith("/opt/env/grok ")


def test_acp_agents_workspace_file_mentions_parsing_and_map() -> None:
    """``workspace_file_mentions`` defaults to False, validates booleans, and serializes."""
    from omnigent.onboarding.acp_auth import acp_agents, acp_file_mentions_map

    cfg = {
        "acp": {
            "agents": [
                {"name": "Coder", "command": "coder acp", "workspace_file_mentions": True},
                {"name": "Chat", "command": "chat acp"},
            ]
        }
    }
    entries = acp_agents(cfg)
    assert [e.workspace_file_mentions for e in entries] == [True, False]
    assert acp_file_mentions_map(cfg) == {"acp:coder": True, "acp:chat": False}
    assert acp_agents_settings(entries) == {
        "acp": {
            "agents": [
                {"name": "Coder", "command": "coder acp", "workspace_file_mentions": True},
                {"name": "Chat", "command": "chat acp"},
            ]
        }
    }

    with pytest.raises(ValueError, match="workspace_file_mentions"):
        acp_agents(
            {
                "acp": {
                    "agents": [
                        {"name": "Bad", "command": "bad acp", "workspace_file_mentions": "yes"}
                    ]
                }
            }
        )
