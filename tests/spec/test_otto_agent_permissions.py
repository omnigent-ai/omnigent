"""Contract tests for unattended Otto agent permission modes."""

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _executor_config(relative_path: str) -> dict[str, object]:
    config = yaml.safe_load((_ROOT / relative_path).read_text())
    return config["executor"]["config"]


@pytest.mark.parametrize("agent", ["repro-agent", "resolve-agent", "feature-agent"])
def test_otto_agent_fully_bypasses_sdk_permission_prompts(agent: str) -> None:
    assert _executor_config(f"dev/{agent}/config.yaml")["permission_mode"] == ("bypassPermissions")
