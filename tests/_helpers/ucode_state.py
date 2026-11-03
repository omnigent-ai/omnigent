"""ucode ``state.json`` payloads for a host that serves several workspaces."""

from __future__ import annotations

import json
from pathlib import Path


def shared_host_ucode_state(host: str, workspace_id: str) -> dict[str, object]:
    """Return the state ucode writes after configuring ``<host>?w=<workspace_id>``.

    The block is keyed ``<host>?w=<id>`` and copied to the bare host with
    ``alias_of``; every agent's base URL stays on *host* and carries the
    workspace selector under ``headers``.

    :param host: Shared host origin, e.g. ``"https://shared.example.com"``.
    :param workspace_id: Selected workspace, e.g. ``"1965859176160743"``.
    :returns: A state payload in ucode's shape.
    """
    selector = {"X-Databricks-Org-Id": workspace_id, "X-Databricks-Workspace-Id": workspace_id}
    auth_command = f"ug auth-token --host {host}"
    block: dict[str, object] = {
        "workspace": host,
        "workspace_id": workspace_id,
        "claude_models": {"opus": "system.ai.claude-opus-test"},
        "codex_models": ["system.ai.gpt-test"],
        "agents": {
            "claude": {
                "model": "system.ai.claude-opus-test",
                "base_url": f"{host}/ai-gateway/anthropic",
                "auth_command": auth_command,
                "auth_refresh_interval_ms": 900000,
                "env": {
                    "ANTHROPIC_BASE_URL": f"{host}/ai-gateway/anthropic",
                    "ANTHROPIC_CUSTOM_HEADERS": "\n".join(
                        f"{name}: {value}" for name, value in selector.items()
                    ),
                },
                "headers": selector,
            },
            "codex": {
                "model": "system.ai.gpt-test",
                "base_url": f"{host}/ai-gateway/codex/v1",
                "auth_command": auth_command,
                "headers": selector,
            },
            "pi": {
                "model": "system.ai.gpt-test",
                "base_urls": {
                    "claude": f"{host}/ai-gateway/anthropic",
                    "openai": f"{host}/ai-gateway/codex/v1",
                },
                "auth_command": auth_command,
                "headers": selector,
            },
        },
    }
    key = f"{host}?w={workspace_id}"
    return {
        "state_version": 3,
        "current_workspace": host,
        "current_workspace_key": key,
        "workspaces": {key: block, host: {**block, "alias_of": key}},
    }


def write_ucode_state(path: Path, payload: dict[str, object]) -> Path:
    """Write *payload* as ``state.json`` at *path* and return the path."""
    path.write_text(json.dumps(payload))
    return path
