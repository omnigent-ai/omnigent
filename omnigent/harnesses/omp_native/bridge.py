"""OMP's isolated file-inbox bridge, sharing Pi's wire protocol."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent.harnesses.pi_native import bridge as shared
from omnigent.native import native_bridge_common
from omnigent.util.json_types import JsonObject

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance

OMP_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_OMP_NATIVE_BRIDGE_DIR"
OMP_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_OMP_NATIVE_REQUEST_SESSION_ID"
OMP_NATIVE_CONFIG_ENV_VAR = "OMNIGENT_OMP_NATIVE_CONFIG"
OMP_NATIVE_ENV_UNSET_ENV_VAR = "OMNIGENT_OMP_ENV_UNSET"
_EXTENSION_FILE = "omnigent_omp_native_extension.mjs"

# The protocol, atomic writers and credentials refresh are intentionally identical.
clear_inbox = shared.clear_inbox
config_path = shared.config_path
enqueue_user_message = shared.enqueue_user_message
enqueue_interrupt = shared.enqueue_interrupt
enqueue_compact = shared.enqueue_compact
enqueue_model_change = shared.enqueue_model_change
enqueue_thinking_level_change = shared.enqueue_thinking_level_change
refresh_config_auth_headers = shared.refresh_config_auth_headers
inject_relay_into_config = shared.inject_relay_into_config
write_agent_instructions = shared.write_agent_instructions


def bridge_root() -> Path:
    return native_bridge_common.native_bridge_data_dir() / "omp-native"


def bridge_dir_for_session_id(session_id: str) -> Path:
    return shared.bridge_dir_for_session_id(session_id, root=bridge_root())


def prepare_bridge_dir(session_id: str) -> Path:
    return shared.prepare_bridge_dir(session_id, root=bridge_root())


def prune_orphaned_bridge_dirs() -> int:
    return native_bridge_common.prune_orphaned_dirs(
        bridge_root(), should_prune=shared._pi_orphan_retention_expired
    )


def omp_session_dir(bridge_dir: Path) -> Path:
    return shared.pi_session_dir(bridge_dir)


def extension_path(bridge_dir: Path) -> Path:
    return bridge_dir / _EXTENSION_FILE


def omp_native_env_unset(environ: Mapping[str, str]) -> list[str]:
    return sorted(
        {
            name.strip()
            for name in environ.get(OMP_NATIVE_ENV_UNSET_ENV_VAR, "").split(",")
            if name.strip()
        }
    )


def build_omp_native_spawn_env(conversation_id: str) -> dict[str, str]:
    return {
        OMP_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_session_id(conversation_id)),
        OMP_NATIVE_REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }


def native_input_ready(session_id: str, instance: TerminalInstance) -> bool:
    del session_id
    config = instance.env.get(OMP_NATIVE_CONFIG_ENV_VAR)
    return bool(config) and (Path(config).parent / "input_ready").is_file()


def write_extension_files(
    bridge_dir: Path,
    *,
    session_id: str,
    server_url: str,
    conversation_url: str,
    auth_headers: dict[str, str] | None = None,
    tools: list[JsonObject] | None = None,
    usage_baseline: dict[str, int | float] | None = None,
    previous_external_session_id: str | None = None,
    fresh: bool = False,
) -> tuple[Path, Path]:
    _, config = shared.write_extension_files(
        bridge_dir,
        session_id=session_id,
        server_url=server_url,
        conversation_url=conversation_url,
        auth_headers=auth_headers,
        tools=tools,
        usage_baseline=usage_baseline,
        harness="omp-native",
        display_name="OMP",
        extension_filename="omnigent_pi_native_extension.cjs",
    )
    if fresh:
        import json

        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["previousExternalSessionId"] = previous_external_session_id
        shared._atomic_json(config, payload)
    shared._atomic_text(
        bridge_dir / "omp-settings.yml",
        "extensionHandlers:\n  toolCallTimeoutMs: 86410000\n",
    )
    shared._atomic_text(
        extension_path(bridge_dir),
        files("omnigent.resources.omp_native")
        .joinpath(_EXTENSION_FILE)
        .read_text(encoding="utf-8"),
    )
    return extension_path(bridge_dir), config
