"""Real Pi request serialization through SDK startup and native provider launch."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from omnigent.harnesses.pi_native.credentials import (
    pi_native_provider_launch,
    resolve_pi_native_provider,
)
from omnigent.inference_config import inference_config_scope
from tests._helpers.live_server import isolated_process_env
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import configure_mock_llm, get_mock_requests


@pytest.mark.parametrize("transport", ["sdk", "native"])
@pytest.mark.parametrize(
    ("model", "effort", "thinking"),
    [
        ("claude-opus-5-5", "none", "adaptive"),
        ("system.ai.claude-opus-5-5[1m]", "none", "adaptive"),
        ("claude-sonnet-4-6", "high", "adaptive"),
        ("claude-haiku-4-5", "none", "disabled"),
        ("claude-haiku-4-5", "high", "enabled"),
    ],
)
def test_pi_launch_sends_supported_thinking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_llm_server_url: str,
    omnigent_python: Path,
    transport: str,
    model: str,
    effort: str,
    thinking: str,
) -> None:
    skip_if_harness_cli_missing("pi")
    config = {
        "providers": {
            "local": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": mock_llm_server_url,
                    "api_key": "synthetic",
                    "models": {"default": model},
                },
            }
        }
    }
    if model.endswith("[1m]"):
        config["inference"] = {
            "harnesses": {
                harness: {"provider": "local", "default_model": model, "model_allowlist": [model]}
                for harness in ("pi", "pi-native")
            }
        }
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(yaml.safe_dump(config))
    env = isolated_process_env(
        tmp_path / "home",
        {"OMNIGENT_CONFIG_HOME": str(config_home), "PI_OFFLINE": "1"},
        {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}},
    )
    if transport == "native":
        monkeypatch.setattr(
            "omnigent.inner.pi_settings.DEFAULT_PI_AGENT_DIR", tmp_path / "user-pi"
        )
        with inference_config_scope(config):
            provider = resolve_pi_native_provider(model=model, config_loader=lambda: config)
        assert provider is not None
        launch = pi_native_provider_launch(tmp_path / "managed-pi", provider, effort)
        env.update(launch.env)
        command = [shutil.which("pi"), *launch.args, "--no-session", "--print", "Hello"]
    else:
        agent = tmp_path / "agent"
        agent.mkdir()
        (agent / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "spec_version": 1,
                    "name": "pi-thinking",
                    "instructions": "Reply briefly.",
                    "executor": {
                        "type": "omnigent",
                        "model": model,
                        "reasoning_effort": effort,
                        "config": {"harness": "pi"},
                    },
                }
            )
        )
        command = [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(agent),
            "-p",
            "Hello",
            "--no-log",
            "--no-session",
        ]
    configure_mock_llm(mock_llm_server_url, [{"text": "THINKING_OK"}], key=model)
    before = len(get_mock_requests(mock_llm_server_url, key=model))
    result = subprocess.run(
        command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=90
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "THINKING_OK" in result.stdout
    requests = get_mock_requests(mock_llm_server_url, key=model)[before:]
    assert len(requests) == 1
    assert requests[0]["model"] == model
    assert requests[0]["thinking"]["type"] == thinking
    if thinking == "adaptive":
        assert requests[0]["output_config"]["effort"] == ("high" if effort == "high" else "low")
    elif thinking == "enabled":
        assert requests[0]["thinking"]["budget_tokens"] > 0
        assert "output_config" not in requests[0]
