"""Tests for effective model/provider resolution (issue #7134, part 2).

Covers :func:`omnigent.host.provider_ops.effective_list` — the host-side
op behind ``GET /v1/hosts/{id}/agent-specs/effective``. Resolution order:
agent spec pin > host default > unresolved. All filesystem effects land in
``tmp_path``.
"""

from __future__ import annotations

import yaml

from omnigent.host import provider_ops


def _write_config(tmp_path) -> str:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "gw": {
                        "kind": "gateway",
                        "default": True,
                        "openai": {
                            "base_url": "https://gw.example/v1",
                            "api_key_ref": "env:GW_KEY",
                            "wire_api": "chat",
                            "models": {"default": "gpt-x"},
                        },
                    }
                }
            },
            sort_keys=False,
        )
    )
    return str(config)


def _write_agent(agents_dir, name: str, executor: dict) -> None:
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / f"{name}.yaml").write_text(
        yaml.safe_dump({"executor": executor}, sort_keys=False)
    )


class TestEffectiveList:
    def test_spec_pin_wins_over_host_default(self, tmp_path) -> None:
        config = _write_config(tmp_path)
        agents = tmp_path / "agents"
        _write_agent(
            agents,
            "pinned",
            {
                "harness": "codex",
                "model": "gpt-pinned",
                "auth": {"type": "provider", "name": "gw"},
                "reasoning_effort": "high",
            },
        )
        result = provider_ops.effective_list(config_path=config, agents_dir=str(agents))
        (row,) = result["rows"]
        assert row["agent"] == "pinned"
        assert row["model"] == "gpt-pinned"
        assert row["model_source"] == "spec"
        assert row["provider"] == "gw"
        assert row["provider_source"] == "spec"
        assert row["reasoning_effort"] == "high"
        assert row["effort_source"] == "spec"

    def test_unpinned_agent_falls_back_to_host_default(self, tmp_path) -> None:
        config = _write_config(tmp_path)
        agents = tmp_path / "agents"
        _write_agent(agents, "plain", {"harness": "codex"})
        result = provider_ops.effective_list(config_path=config, agents_dir=str(agents))
        (row,) = result["rows"]
        assert row["model"] == "gpt-x"
        assert row["model_source"] == "host-default"
        assert row["provider"] == "gw"
        assert row["provider_source"] == "host-default"
        assert row["reasoning_effort"] is None
        assert row["effort_source"] == "unresolved"

    def test_unknown_harness_uses_pi_fallback_for_provider(self, tmp_path) -> None:
        # An unmapped harness consumes both families (same pi fallback the
        # runtime applies): the provider resolves, and the model resolves
        # through the provider's served families with pi preference.
        config = _write_config(tmp_path)
        agents = tmp_path / "agents"
        _write_agent(agents, "weird", {"harness": "not-a-harness"})
        result = provider_ops.effective_list(config_path=config, agents_dir=str(agents))
        (row,) = result["rows"]
        assert row["model"] == "gpt-x"
        assert row["model_source"] == "host-default"
        assert row["provider"] == "gw"
        assert row["provider_source"] == "host-default"

    def test_opencode_harness_resolves_openai_default(self, tmp_path) -> None:
        config = _write_config(tmp_path)
        agents = tmp_path / "agents"
        _write_agent(agents, "coder", {"harness": "opencode"})
        result = provider_ops.effective_list(config_path=config, agents_dir=str(agents))
        (row,) = result["rows"]
        assert row["model"] == "gpt-x"
        assert row["model_source"] == "host-default"
        assert row["provider"] == "gw"

    def test_broken_spec_becomes_error_row(self, tmp_path) -> None:
        config = _write_config(tmp_path)
        agents = tmp_path / "agents"
        agents.mkdir(exist_ok=True)
        (agents / "broken.yaml").write_text("just a string\n")
        _write_agent(agents, "plain", {"harness": "codex"})
        result = provider_ops.effective_list(config_path=config, agents_dir=str(agents))
        by_name = {row["agent"]: row for row in result["rows"]}
        assert "error" in by_name["broken"]
        assert by_name["plain"]["model"] == "gpt-x"

    def test_missing_everything_lists_empty(self, tmp_path) -> None:
        result = provider_ops.effective_list(
            config_path=str(tmp_path / "none.yaml"),
            agents_dir=str(tmp_path / "no-agents"),
        )
        assert result["rows"] == []

    def test_op_dispatch(self, tmp_path, monkeypatch) -> None:
        import os

        home = tmp_path / "home"
        (home / ".omnigent" / "agents").mkdir(parents=True)
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(home / ".omnigent"))
        (home / ".omnigent" / "config.yaml").write_text(
            yaml.safe_dump({"providers": {}}, sort_keys=False)
        )
        (home / ".omnigent" / "agents" / "a.yaml").write_text(
            yaml.safe_dump({"executor": {"harness": "codex"}}, sort_keys=False)
        )
        result = provider_ops.run_provider_op("effective_list", {})
        (row,) = result["rows"]
        assert row["agent"] == "a"
        assert row["model_source"] == "unresolved"
        assert os.environ["OMNIGENT_CONFIG_HOME"]
