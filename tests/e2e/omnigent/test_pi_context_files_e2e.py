"""Real CLI → bundle → harness → Pi request coverage with a mock LLM."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from omnigent.runtime.prompt import EMBEDDED_BROWSER_PRIORITY_INSTRUCTION
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent.conftest import configure_mock_llm

_PI_UNAVAILABLE = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(_PI_UNAVAILABLE is not None, reason=str(_PI_UNAVAILABLE))


@pytest.mark.parametrize("directory", [False, True], ids=["yaml", "bundle"])
@pytest.mark.parametrize("enabled", [None, True, False], ids=["default", "enabled", "disabled"])
def test_pi_run_context_files(
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    directory: bool,
    enabled: bool | None,
) -> None:
    """Opting out removes discovered context but preserves authored/runtime instructions."""
    model = f"mock-pi-context-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(mock_llm_server_url, [{"text": "Captured."}], key=model)
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "auth": {"type": "api_key"},
                "providers": {
                    "mock-oai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": f"{mock_llm_server_url}/v1",
                            "api_key": "mock-key",
                            "models": {"default": model},
                        },
                    }
                },
            }
        )
    )
    workspace = tmp_path / "project" / "workspace"
    workspace.mkdir(parents=True)
    (workspace.parent / "AGENTS.md").write_text("ANCESTOR_CONTEXT_73D2")
    (workspace / "CLAUDE.md").write_text("WORKSPACE_CONTEXT_94C1")
    source = tmp_path / "agent"
    source.mkdir()
    executor: dict[str, object] = {"harness": "pi", "model": model}
    if enabled is not None:
        executor["context_files"] = enabled
    config: dict[str, object] = {"name": "pi-context-probe", "skills": "none"}
    if directory:
        config.update(
            spec_version=1,
            executor={"type": "omnigent", "config": executor},
            instructions="AGENTS.md",
        )
        (source / "AGENTS.md").write_text("AUTHORED_PROMPT_42D9")
        (source / "config.yaml").write_text(yaml.safe_dump(config))
        agent_path = source
    else:
        config.update(executor=executor, prompt="AUTHORED_PROMPT_42D9")
        agent_path = source / "agent.yaml"
        agent_path.write_text(yaml.safe_dump(config))

    env = {**mock_credentials_env, "OMNIGENT_CONFIG_HOME": str(config_home)}
    # A stale ambient value must never override the YAML/default policy.
    env["HARNESS_PI_CONTEXT_FILES"] = "true" if enabled is False else "false"
    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(agent_path),
            "--model",
            model,
            "-p",
            "Hello",
            "--no-log",
            "--no-session",
        ],
        env=env,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    requests = get_mock_requests(mock_llm_server_url, key=model)
    assert requests, f"No Pi model requests captured. stdout:\n{result.stdout}"
    for request in requests:
        system = "\n".join(
            json.dumps(message["content"])
            for message in request["messages"]
            if message["role"] in ("system", "developer")
        )
        assert "AUTHORED_PROMPT_42D9" in system
        assert EMBEDDED_BROWSER_PRIORITY_INSTRUCTION in system
        assert ("ANCESTOR_CONTEXT_73D2" in system) is (enabled is not False)
        assert ("WORKSPACE_CONTEXT_94C1" in system) is (enabled is not False)
