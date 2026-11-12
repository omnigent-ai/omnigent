"""Owned native-peer metadata for executor scenarios with explicit injection callbacks."""

from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native.framework_context import (
    consume_prompt_framework_context,
    prompt_framework_context_capability_ready,
)
from omnigent.harnesses.claude_native.hook import record_hook_payload
from omnigent.inner import claude_native_executor
from omnigent.inner.terminal_lifecycle import TERMINAL_INSTANCE_ID_ENV, TERMINAL_LAUNCH_ID_ENV


def prepare_native_peer(
    bridge_dir: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    submit_hook: bool = True,
    native_ready: bool = True,
) -> None:
    """Seed missing peer metadata and emit source hooks around successful fake injections."""
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", root)
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", root.parent)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    records = {
        "bridge.json": {
            "active_session_id": claude_native_executor._request_session_id_from_env()
            or "native-test"
        },
        "state.json": {"claude_session_id": "native-session"} if native_ready else {},
        "tmux.json": {
            "socket_path": str(root / "native.sock"),
            "tmux_target": "main",
            "launch_id": "c" * 32,
            "instance_id": "d" * 32,
        },
    }
    for name, payload in records.items():
        if not (bridge_dir / name).exists():
            claude_bridge._write_json_file(bridge_dir / name, payload)
    owner = claude_bridge.read_delivery_owner(bridge_dir)
    if owner.launch_id is not None:
        monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, owner.launch_id)
    if owner.instance_id is not None:
        monkeypatch.setenv(TERMINAL_INSTANCE_ID_ENV, owner.instance_id)
    if (
        owner.session_id
        and owner.native_session_id
        and owner.launch_id
        and owner.instance_id
        and not prompt_framework_context_capability_ready(bridge_dir)
    ):
        record_hook_payload(
            bridge_dir,
            {
                "hook_event_name": "SessionStart",
                "session_id": owner.native_session_id,
                "source": "startup",
            },
            env={
                TERMINAL_LAUNCH_ID_ENV: owner.launch_id,
                TERMINAL_INSTANCE_ID_ENV: owner.instance_id,
            },
            prompt_framework_context=True,
        )
    injector = claude_native_executor.inject_user_message
    if (
        submit_hook
        and injector is not claude_bridge.inject_user_message
        and not getattr(injector, "_emits_owned_submit_hook", False)
    ):

        def inject_with_hook(directory: Path, *, content: str, **kwargs: Any) -> None:
            injector(directory, content=content, **kwargs)
            consume_prompt_framework_context(
                directory,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": claude_bridge.read_delivery_owner(directory).native_session_id,
                    "prompt": content,
                },
            )

        inject_with_hook._emits_owned_submit_hook = True
        monkeypatch.setattr(claude_native_executor, "inject_user_message", inject_with_hook)
