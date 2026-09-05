"""Contract tests for unattended Otto agent permission modes."""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _executor_config(relative_path: str) -> dict[str, object]:
    config = yaml.safe_load((_ROOT / relative_path).read_text())
    return config["executor"]["config"]


def test_repro_agent_fully_bypasses_sdk_permission_prompts() -> None:
    assert _executor_config("dev/repro-agent/config.yaml")["permission_mode"] == (
        "bypassPermissions"
    )


def test_resolve_agent_fully_bypasses_sdk_permission_prompts() -> None:
    assert _executor_config("dev/resolve-agent/config.yaml")["permission_mode"] == (
        "bypassPermissions"
    )
