"""Real Claude draft preservation against an owned loopback provider, without a browser."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge, model_switch
from omnigent.process_logging import redact_log_text
from tests._helpers.mock_llm import advertised_tool_names
from tests._helpers.native_claude import (
    _PINNED_CLAUDE_MODELS,
    _assert_mock_claude_turn,
    _assert_native_claude_turn,
    _claude_text_blocks,
    _live_claude_snapshot,
    _require_session_only_claude,
    isolated_claude_mock_routing_stack,
)
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.session import message_item_text, ordered_message_items, send_user_text
from tests.e2e.conftest import configure_mock_llm, get_mock_message_requests, set_fallback_mock_llm
from tests.e2e.routing._helpers import claude_settings_snapshot
from tests.harnesses.claude_native._model_status_oracle import native_claude_wire_model

pytestmark = [
    pytest.mark.mock_only,
    pytest.mark.skipif(
        shutil.which("claude") is None or shutil.which("tmux") is None,
        reason="requires Claude Code and tmux",
    ),
    pytest.mark.timeout(300),
]


def test_claude_model_switch_preserves_application_wrapped_drafts(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    claude_managed_settings: None,
) -> None:
    mock_url = isolated_mock_llm_server_url
    with isolated_claude_mock_routing_stack(
        tmp_path,
        mock_url,
        models=_PINNED_CLAUDE_MODELS,
        judge_model="mock-claude-draft-judge",
    ) as (base_url, client, host_id, workspace, settings_path):
        saved_settings = claude_settings_snapshot(settings_path)
        agents = client.get("/v1/agents")
        agents.raise_for_status()
        agent_id = next(
            row["id"] for row in agents.json()["data"] if row["name"] == "claude-native-ui"
        )
        created = client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
                "cost_control_mode_override": "off",
            },
            timeout=120,
        )
        created.raise_for_status()
        session_id = str(created.json()["id"])
        try:
            _require_session_only_claude(base_url, session_id)
            selected = client.patch(
                f"/v1/sessions/{session_id}", json={"model_override": "sonnet"}
            )
            assert selected.status_code == 200, selected.text
            model = _PINNED_CLAUDE_MODELS["sonnet"]
            poll_until(
                lambda: (
                    _live_claude_snapshot(base_url, session_id)["llm_model"]
                    == native_claude_wire_model(model)
                ),
                timeout_s=30,
            )
            assert claude_settings_snapshot(settings_path) == saved_settings
            snapshot = _live_claude_snapshot(base_url, session_id)
            bridge_id = snapshot["labels"].get(bridge.BRIDGE_ID_LABEL_KEY) or session_id
            bridge_dir = bridge.bridge_dir_for_bridge_id(bridge_id)
            advert = bridge.read_tmux_target(bridge_dir)
            assert advert is not None, "The owned Claude terminal advertised no tmux pane"
            tmux = ["tmux", "-S", advert["socket_path"]]
            target = advert["tmux_target"]
            columns = int(
                subprocess.check_output(
                    [*tmux, "display-message", "-p", "-t", target, "#{pane_width}"],
                    text=True,
                    timeout=5,
                )
            )
            evidence = Path(os.environ.get("OMNIGENT_E2E_RECORD_DIR", str(tmp_path)))
            evidence.mkdir(parents=True, exist_ok=True)
            for kind in ("soft-wrap", "hard-newline"):
                nonce = uuid.uuid4().hex[:8]
                marker, reply = f"draft-{kind}-{nonce}", f"preserved-draft-{nonce}"
                draft = marker + " " + "0123456789 " * (columns // 8 + 1) + "draft-end"
                if kind == "hard-newline":
                    split_at = draft.index(" ", columns // 2)
                    draft = draft[:split_at] + "\n" + draft[split_at:]
                set_fallback_mock_llm(mock_url, "default", reply)
                set_fallback_mock_llm(mock_url, native_claude_wire_model(model), reply)
                buffer_name = f"owned-renderer-{uuid.uuid4().hex}"
                subprocess.run(
                    [*tmux, "load-buffer", "-b", buffer_name, "-"],
                    input=draft,
                    text=True,
                    check=True,
                    timeout=5,
                )
                subprocess.run(
                    [*tmux, "paste-buffer", "-d", "-p", "-b", buffer_name, "-t", target],
                    check=True,
                    timeout=5,
                )
                raw = ""

                def draft_rendered(expected_marker: str = marker) -> bool:
                    nonlocal raw
                    raw = bridge._capture_pane(advert["socket_path"], target)
                    box = bridge._composer_box(raw)
                    return bool(
                        box is not None
                        and box.framed
                        and len(box.rows) > 1
                        and expected_marker in box.rows[0]
                        and "draft-end" in "\n".join(box.rows)
                    )

                poll_until(draft_rendered, timeout_s=10)
                joined = bridge._capture_pane(advert["socket_path"], target, join_wrapped=True)
                (evidence / f"{kind}-renderer.json").write_text(
                    json.dumps(
                        {"columns": columns, "input": draft, "raw": raw, "joined": joined},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                box = bridge._composer_box(raw)
                assert box is not None and box.framed and len(box.rows) > 1
                refused = client.patch(
                    f"/v1/sessions/{session_id}", json={"model_override": "opus"}
                )
                assert refused.status_code == 503, refused.text
                preserved = bridge._composer_box(
                    bridge._capture_pane(advert["socket_path"], target)
                )
                assert preserved is not None and preserved.framed
                assert preserved.rows == box.rows
                assert claude_settings_snapshot(settings_path) == saved_settings
                assert all(
                    marker not in json.dumps(request)
                    for request in get_mock_message_requests(mock_url)
                )
                assert _live_claude_snapshot(base_url, session_id)["llm_model"] == (
                    native_claude_wire_model(model)
                )

                # Submit explicitly to observe logical newlines independently of wrapping.
                subprocess.run([*tmux, "send-keys", "-t", target, "Enter"], check=True, timeout=5)
                poll_until(
                    lambda reply=reply: any(
                        item.get("role") == "assistant" and message_item_text(item) == reply
                        for item in ordered_message_items(base_url, session_id)
                    ),
                    timeout_s=30,
                )
                native_session, user_text = _assert_native_claude_turn(
                    base_url, session_id, marker, reply, model
                )
                assert user_text == draft, {"expected": draft, "native_user_text": user_text}
                _assert_mock_claude_turn(
                    mock_url,
                    marker,
                    model,
                    native_session=native_session,
                    user_text=draft,
                )
                (evidence / f"{kind}-delivered.json").write_text(
                    json.dumps(
                        {"native_session": native_session, "user_text": user_text, "reply": reply},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                poll_until(
                    lambda: (
                        model_switch._draft(bridge._capture_pane(advert["socket_path"], target))
                        == ""
                    ),
                    timeout_s=10,
                )
                assert claude_settings_snapshot(settings_path) == saved_settings
        finally:
            client.delete(f"/v1/sessions/{session_id}").raise_for_status()


def test_claude_model_control_during_an_active_turn_preserves_delivery(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    claude_managed_settings: None,
) -> None:
    mock_url = isolated_mock_llm_server_url
    with isolated_claude_mock_routing_stack(
        tmp_path,
        mock_url,
        models=_PINNED_CLAUDE_MODELS,
        judge_model="mock-claude-active-turn-judge",
    ) as (base_url, client, host_id, workspace, settings_path):
        created = create_native_session(
            client,
            base_url,
            harness="claude",
            metadata={
                "host_id": host_id,
                "workspace": str(workspace),
            },
        )
        session_id = str(created["session_id"])
        try:
            _require_session_only_claude(base_url, session_id)
            saved_settings = claude_settings_snapshot(settings_path)
            selected = client.patch(
                f"/v1/sessions/{session_id}",
                json={"model_override": "sonnet", "cost_control_mode_override": "off"},
            )
            assert selected.status_code == 200, selected.text
            initial_model = _PINNED_CLAUDE_MODELS["sonnet"]
            nonce = uuid.uuid4().hex[:8]
            marker, reply = f"warm-user-{nonce}", f"warm-reply-{nonce}"
            set_fallback_mock_llm(mock_url, "default", reply)
            set_fallback_mock_llm(mock_url, native_claude_wire_model(initial_model), reply)
            send_user_text(client, session_id=session_id, text=f"Reply with {reply}. {marker}")
            poll_until(
                lambda: any(
                    item.get("role") == "assistant" and message_item_text(item) == reply
                    for item in ordered_message_items(base_url, session_id)
                ),
                timeout_s=30,
            )
            native_session, user_text = _assert_native_claude_turn(
                base_url, session_id, marker, reply, initial_model
            )
            prior_request = _assert_mock_claude_turn(
                mock_url, marker, initial_model, native_session=native_session, user_text=user_text
            )
            tool_names = sorted(advertised_tool_names([prior_request]))
            assert tool_names, (
                "A conversation tool must distinguish the held turn from title calls"
            )
            snapshot = _live_claude_snapshot(base_url, session_id)
            bridge_id = snapshot["labels"].get(bridge.BRIDGE_ID_LABEL_KEY) or session_id
            bridge_dir = bridge.bridge_dir_for_bridge_id(bridge_id)
            advert = bridge.read_tmux_target(bridge_dir)
            assert advert is not None, "The owned Claude terminal advertised no tmux pane"
            target = advert["tmux_target"]
            evidence = Path(os.environ.get("OMNIGENT_E2E_RECORD_DIR", str(tmp_path)))
            evidence.mkdir(parents=True, exist_ok=True)
            busy_marker, busy_reply = f"busy-user-{nonce}", f"busy-reply-{nonce}"
            busy_prompt = f"Reply with exactly {busy_reply}. Context marker: {busy_marker}."
            configure_mock_llm(
                mock_url,
                [{"text": busy_reply, "block": True}],
                match=busy_marker,
                required_tools=tool_names[:1],
            )
            with httpx.Client(base_url=mock_url, timeout=10) as model_client:
                try:
                    send_user_text(client, session_id=session_id, text=busy_prompt)
                    poll_until(
                        lambda: bool(model_client.get("/gate/pending").json()["pending"]),
                        timeout_s=30,
                    )
                    poll_until(
                        lambda: (
                            _live_claude_snapshot(base_url, session_id).get("status") == "running"
                        ),
                        timeout_s=5,
                    )
                    (evidence / "busy-before-controls.txt").write_text(
                        bridge._capture_pane(advert["socket_path"], target)
                    )
                    response = client.patch(
                        f"/v1/sessions/{session_id}",
                        json={"model_override": "opus"},
                        timeout=40,
                    )
                    pending = model_client.get("/gate/pending").json()["pending"]
                    (evidence / "busy-model-control.json").write_text(
                        json.dumps(
                            {
                                "version": json.loads(
                                    (bridge_dir / bridge.CONTEXT_RAW_FILE).read_text()
                                ).get("version"),
                                "status": response.status_code,
                                "provider_still_held": pending,
                                "saved_settings_unchanged": (
                                    claude_settings_snapshot(settings_path) == saved_settings
                                ),
                                "terminal": bridge._capture_pane(advert["socket_path"], target),
                            },
                            indent=2,
                        )
                    )
                    assert response.status_code == 200, {
                        "response": response.text,
                        "model_control_failure": [
                            redact_log_text(line, include_whitespace_credentials=True)[:2048]
                            for directory in (
                                tmp_path
                                / "server"
                                / "server-home"
                                / ".omnigent"
                                / "logs"
                                / "server",
                                tmp_path / "host" / "home" / ".omnigent" / "logs" / "runner",
                            )
                            for path in directory.glob("*.log")
                            for line in path.read_text(errors="replace").splitlines()
                            if any(
                                marker in line
                                for marker in (
                                    "Model change not applied to the terminal",
                                    "claude-native model change failed:",
                                    "ClaudeModelSwitchRefused:",
                                    "ClaudeComposerNotCleared:",
                                    "ClaudePromptTimeout:",
                                    "ClaudeInputBoxOccupied:",
                                    "ClaudeTerminalDialog:",
                                    "ClaudeUserPromptPending:",
                                    "RuntimeError:",
                                )
                            )
                        ][-6:],
                    }
                    assert pending, "Model control must finish before the active turn is released"
                    assert claude_settings_snapshot(settings_path) == saved_settings
                    poll_until(
                        lambda: (
                            _live_claude_snapshot(base_url, session_id).get("llm_model")
                            == native_claude_wire_model(_PINNED_CLAUDE_MODELS["opus"])
                        ),
                        timeout_s=10,
                    )
                    poll_until(
                        lambda: (
                            _live_claude_snapshot(base_url, session_id).get("status") == "running"
                        ),
                        timeout_s=5,
                    )
                finally:
                    while model_client.post("/gate/release").json()["released"]:
                        pass
            poll_until(
                lambda: any(
                    item.get("role") == "assistant" and message_item_text(item) == busy_reply
                    for item in ordered_message_items(base_url, session_id)
                ),
                timeout_s=30,
            )
            (evidence / "busy-after-release.txt").write_text(
                bridge._capture_pane(advert["socket_path"], target)
            )
            items = ordered_message_items(base_url, session_id)
            for role, token in (("user", busy_marker), ("assistant", busy_reply)):
                assert (
                    sum(
                        item.get("role") == role and token in message_item_text(item)
                        for item in items
                    )
                    == 1
                )
            transcript = bridge.read_transcript_path(bridge_dir)
            assert transcript is not None
            busy_users = [
                record
                for line in transcript.read_text().splitlines()
                if line.strip()
                and (record := json.loads(line)).get("type") == "user"
                and busy_marker in json.dumps(record.get("message", {}).get("content"))
            ]
            assert len(busy_users) == 1 and busy_users[0]["sessionId"] == native_session
            busy_blocks = _claude_text_blocks(busy_users[0]["message"]["content"])
            assert len(busy_blocks) == 1
            _assert_mock_claude_turn(
                mock_url,
                busy_marker,
                initial_model,
                native_session=native_session,
                user_text=busy_blocks[0],
            )

            marker, reply = f"after-busy-user-{nonce}", f"after-busy-reply-{nonce}"
            model = _PINNED_CLAUDE_MODELS["opus"]
            set_fallback_mock_llm(mock_url, native_claude_wire_model(model), reply)
            configure_mock_llm(
                mock_url,
                [{"text": reply}],
                match=marker,
                required_tools=tool_names[:1],
            )
            send_user_text(client, session_id=session_id, text=f"Reply with {reply}. {marker}")
            poll_until(
                lambda: any(
                    item.get("role") == "assistant" and message_item_text(item) == reply
                    for item in ordered_message_items(base_url, session_id)
                ),
                timeout_s=30,
            )
            observed_session, user_text = _assert_native_claude_turn(
                base_url, session_id, marker, reply, model
            )
            assert observed_session == native_session
            _assert_mock_claude_turn(
                mock_url, marker, model, native_session=native_session, user_text=user_text
            )
            assert claude_settings_snapshot(settings_path) == saved_settings
        finally:
            client.delete(f"/v1/sessions/{session_id}").raise_for_status()
