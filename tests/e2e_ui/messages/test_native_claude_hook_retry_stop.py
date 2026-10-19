"""A real Claude hook refusal stops Enter retries without claiming web delivery."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge
from tests.e2e_ui import conftest as ui_fixtures

from .test_native_claude_shell_input import (  # noqa: F401 — autouse CLI availability guard
    _requires_mockable_claude,
)


@pytest.mark.nightly
@pytest.mark.timeout(180)
def test_hook_refusal_sends_one_enter_and_keeps_delivery_unconfirmed(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    claude_managed_settings: None,
) -> None:
    workspaces = ui_fixtures._REPO_ROOT / ".omnigent" / "e2e-hook-workspaces"
    workspaces.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="retry-stop-", dir=workspaces) as directory:
        workspace = Path(directory)
        settings = workspace / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            "python -c 'import json,sys; json.load(sys.stdin); "
                                            'print(json.dumps({"decision":"block",'
                                            '"reason":"Synthetic retry refusal."}))\''
                                        ),
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
        )
        original_create = ui_fixtures.create_native_session

        def create_in_workspace(*args: Any, **kwargs: Any) -> dict[str, Any]:
            kwargs["metadata"] = {**kwargs.get("metadata", {}), "workspace": str(workspace)}
            return original_create(*args, **kwargs)

        monkeypatch.setattr(ui_fixtures, "create_native_session", create_in_workspace)
        with ui_fixtures.native_claude_mock_session_context(
            live_server, mock_llm_server_url, tmp_path_factory
        ) as (base_url, session_id):
            snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15).json()
            bridge_id = snapshot["labels"].get(bridge.BRIDGE_ID_LABEL_KEY) or session_id
            native = bridge.bridge_dir_for_bridge_id(bridge_id)
            original_tmux = bridge._run_tmux
            submit_enters = 0

            def refusals() -> list[dict[str, Any]]:
                transcript = bridge.read_transcript_path(native)
                if transcript is None:
                    return []
                try:
                    text = transcript.read_text()
                except FileNotFoundError:
                    return []
                records = [
                    json.loads(line)
                    for line in text.splitlines(keepends=True)
                    if line.endswith("\n")
                ]
                return [
                    record
                    for record in records
                    if record.get("type") == "system"
                    and record.get("sessionId") == bridge.read_claude_session_id(native)
                    and record.get("content")
                    == "UserPromptSubmit operation blocked by hook:\n"
                    "Synthetic retry refusal.\n\nOriginal prompt: BLOCKME retry probe"
                ]

            def count_enters(socket_path: str, *args: str) -> str:
                nonlocal submit_enters
                result = original_tmux(socket_path, *args)
                if args[-1] == "Enter":
                    submit_enters += 1
                    if submit_enters == 1:
                        # Exercise the retry guard after Claude records the refusal,
                        # rather than during its transiently empty composer state.
                        deadline = time.monotonic() + 20
                        while not refusals():
                            assert time.monotonic() < deadline, "Claude did not record its refusal"
                            time.sleep(0.05)
                return result

            assert refusals() == []
            monkeypatch.setattr(bridge, "_run_tmux", count_enters)
            with pytest.raises(RuntimeError) as caught:
                bridge.inject_user_message(native, content="BLOCKME retry probe", timeout_s=60)
            assert submit_enters == 1
            assert isinstance(caught.value, bridge.ClaudeSubmitBlockedUnconfirmed)
            blocks = refusals()
            assert len(blocks) == 1
            ending = blocks[0].get("promptSubmitEnding")
            if ending is not None:
                assert ending.get("ending", {}).get("kind") == "blocked_by_hook"
            hooks = [
                json.loads(line)["payload"]
                for line in (native / "hooks.jsonl").read_text().splitlines()
            ]
            submits = [hook for hook in hooks if hook.get("hook_event_name") == "UserPromptSubmit"]
            assert len(submits) == 1
            output = Path(request.config.getoption("--output")) / request.node.name
            output.mkdir(parents=True, exist_ok=True)
            (output / "retry-stop-evidence.json").write_text(
                json.dumps(
                    {
                        "submit_enters": submit_enters,
                        "hook_prompt_id": submits[0].get("prompt_id"),
                        "blocked_record_uuid": blocks[0].get("uuid"),
                        "exception_type": type(caught.value).__name__,
                        "ownership": "unconfirmed",
                    },
                    indent=2,
                )
            )
