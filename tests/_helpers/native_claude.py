"""Native Claude observations and an owned loopback-only routing stack."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    CONTEXT_RAW_FILE,
    bridge_dir_for_bridge_id,
    read_claude_session_id,
    read_transcript_path,
)
from omnigent.harnesses.claude_native.model_switch import supports_session_only_model_switch
from omnigent.runner.background_titles.service import BACKGROUND_TITLE_INSTRUCTIONS
from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_local_server
from tests._helpers.polling import poll_until
from tests._helpers.session import (
    message_item_text as _item_text,
)
from tests._helpers.session import (
    ordered_message_items as _ordered_message_items,
)
from tests.e2e.conftest import get_mock_message_requests, reset_mock_llm
from tests.harnesses.claude_native._model_status_oracle import (
    native_claude_wire_model as _native_claude_wire_model,
)
from tests.helpers.ui_configuration import validate_native_claude_mock_routing

_PINNED_CLAUDE_MODELS = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "system.ai.claude-opus-4-8[1m]",
}


def _live_claude_snapshot(base_url: str, session_id: str) -> dict:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json()


def _require_session_only_claude(base_url: str, session_id: str) -> None:
    version: object = None

    def version_reported() -> bool:
        nonlocal version
        snapshot = _live_claude_snapshot(base_url, session_id)
        bridge_id = snapshot["labels"].get(BRIDGE_ID_LABEL_KEY) or session_id
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        native_session = read_claude_session_id(bridge_dir)
        try:
            raw = json.loads((bridge_dir / CONTEXT_RAW_FILE).read_text())
        except (OSError, ValueError):
            return False
        if (
            not isinstance(raw, dict)
            or not native_session
            or raw.get("session_id") != native_session
        ):
            return False
        version = raw.get("version")
        return isinstance(version, str) and bool(version)

    poll_until(version_reported, timeout_s=30, interval_s=0.1)
    if not supports_session_only_model_switch(version):
        pytest.skip(
            "session-only model selection requires stable Claude Code 2.1.323+; "
            f"the running session reports {version!r}"
        )


def _claude_text_blocks(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
    return []


def _assert_mock_claude_turn(
    mock_url: str, marker: str, model: str, *, native_session: str, user_text: str
) -> dict:
    """Correlate one conversation request, accounting for explicit title calls."""
    turn_requests = []
    observed = []
    for request in get_mock_message_requests(mock_url):
        body = request["body"]
        users = [message for message in body.get("messages", []) if message.get("role") == "user"]
        if not (body.get("stream") and users and marker in json.dumps(users[-1].get("content"))):
            continue
        text_blocks = _claude_text_blocks(users[-1].get("content"))
        system_blocks = _claude_text_blocks(body.get("system"))
        metadata = body.get("metadata")
        try:
            identity = (
                json.loads(metadata.get("user_id", "{}")) if isinstance(metadata, dict) else {}
            )
        except (TypeError, ValueError):
            identity = {}
        request_session = identity.get("session_id") if isinstance(identity, dict) else None
        exact_blocks = text_blocks.count(user_text)
        details = {
            "model": body.get("model"),
            "native_session": request_session,
            "exact_user_blocks": exact_blocks,
        }
        if exact_blocks:
            assert exact_blocks == 1 and request_session == native_session, details
            details["purpose"] = "conversation"
            turn_requests.append(request)
        elif not body.get("tools") and (
            request_session == native_session
            and len(text_blocks) == 1
            and text_blocks[0].startswith(
                # Native titles can trim trailing LF; conversation text stays exact.
                tuple(
                    f"<session>\n{title_text}\n</session>\n\n"
                    "Write the title in the predominant language of the session"
                    for title_text in (user_text, user_text.rstrip("\n"))
                )
            )
            and any(
                text.startswith(
                    "You are naming a coding session so the user can pick it out "
                    "of a long list of sessions."
                )
                for text in system_blocks
            )
        ):
            details["purpose"] = "native title"
        elif (
            not body.get("tools")
            and isinstance(request_session, str)
            and request_session != native_session
            and text_blocks == [f"<user_message>\n{user_text}\n</user_message>"]
            and BACKGROUND_TITLE_INSTRUCTIONS in system_blocks
        ):
            details["purpose"] = "background title"
        else:
            raise AssertionError(
                {
                    "unclassified_request": details,
                    "marker": marker,
                    "has_tools": bool(body.get("tools")),
                    "user_text_blocks": text_blocks,
                    "system_prefixes": [text[:256] for text in system_blocks],
                }
            )
        observed.append(details)
    assert len(turn_requests) == 1, {"marker": marker, "requests": observed}
    assert turn_requests[0]["body"]["model"] == _native_claude_wire_model(model)
    return turn_requests[0]["body"]


def _assert_native_claude_turn(
    base_url: str, session_id: str, marker: str, reply: str, model: str
) -> tuple[str, str]:
    """Correlate one committed turn with Claude's transcript and fresh raw statusLine."""
    items = _ordered_message_items(base_url, session_id)
    for role, token in (("user", marker), ("assistant", reply)):
        matching = [item for item in items if item["role"] == role and token in _item_text(item)]
        assert len(matching) == 1, {
            "role": role,
            "token": token,
            "item_ids": [item.get("id") for item in matching],
        }
    snapshot = _live_claude_snapshot(base_url, session_id)
    bridge_id = snapshot["labels"].get(BRIDGE_ID_LABEL_KEY) or session_id
    bridge_dir = bridge_dir_for_bridge_id(bridge_id)
    native_session = read_claude_session_id(bridge_dir)
    assert native_session, "The native Claude process reported no session ID"
    transcript = read_transcript_path(bridge_dir)
    assert transcript is not None, "The native Claude process reported no transcript"
    records = [json.loads(line) for line in transcript.read_text().splitlines() if line.strip()]
    turns = []
    for role, token in (("user", marker), ("assistant", reply)):
        matching = [
            record
            for record in records
            if record.get("type") == role
            and token in json.dumps(record.get("message", {}).get("content"))
        ]
        assert len(matching) == 1, {
            "role": role,
            "token": token,
            "record_ids": [record.get("uuid") for record in matching],
        }
        assert matching[0]["sessionId"] == native_session
        turns.append(matching[0])
    user, assistant = turns
    assert isinstance(user.get("promptId"), str) and user["promptId"]
    parents = {
        record["uuid"]: record.get("parentUuid") for record in records if record.get("uuid")
    }
    parent, seen = assistant.get("parentUuid"), set()
    while parent and parent != user["uuid"] and parent not in seen:
        seen.add(parent)
        parent = parents.get(parent)
    assert parent == user["uuid"], "The native reply does not descend from this turn's user record"
    wire_model = _native_claude_wire_model(model)
    assert assistant["requestedModel"] == wire_model
    assert assistant["message"]["model"] == wire_model
    assert _item_text(assistant["message"]) == reply

    observed: dict = {}

    def fresh_context_matches() -> bool:
        nonlocal observed
        try:
            raw = json.loads((bridge_dir / CONTEXT_RAW_FILE).read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(raw, dict):
            return False
        status_model, window = raw.get("model"), raw.get("context_window")
        observed = {
            "version": raw.get("version"),
            "session_id": raw.get("session_id"),
            "prompt_id": raw.get("prompt_id"),
            "transcript_path": raw.get("transcript_path"),
            "model": status_model.get("id") if isinstance(status_model, dict) else None,
            "context_window_size": (
                window.get("context_window_size") if isinstance(window, dict) else None
            ),
        }
        return (
            isinstance(observed["version"], str)
            and bool(observed["version"])
            and observed["session_id"] == native_session
            and observed["prompt_id"] == user["promptId"]
            and observed["transcript_path"] == str(transcript)
            and observed["model"] == wire_model
            and (
                model != _PINNED_CLAUDE_MODELS["opus"]
                or observed["context_window_size"] == 1_000_000
            )
        )

    try:
        poll_until(fresh_context_matches, timeout_s=30, interval_s=0.1)
    except AssertionError as exc:
        raise AssertionError(
            f"No fresh native context for prompt {user['promptId']!r}, "
            f"session {native_session!r}, model {wire_model!r}: {observed!r}"
        ) from exc
    user_blocks = _claude_text_blocks(user["message"].get("content"))
    assert len(user_blocks) == 1 and user_blocks[0], (
        "The native user record must have one complete text block to correlate"
    )
    return native_session, user_blocks[0]


@contextmanager
def isolated_claude_mock_routing_stack(
    root: Path,
    mock_url: str,
    *,
    models: dict[str, str],
    judge_model: str,
    web_ui_dist: Path | None = None,
) -> Iterator[tuple[str, httpx.Client, str, Path, Path]]:
    """Own server and host; callers provide model pins, judge and optional SPA."""
    root.mkdir(parents=True, exist_ok=True)
    validate_native_claude_mock_routing(mock_url)
    reset_mock_llm(mock_url)
    workspace = root / "workspace"
    workspace.mkdir()
    claude_config = root / "claude-config"
    claude_config.mkdir()
    settings_path = claude_config / "settings.json"
    settings_path.write_text(json.dumps({"model": models["sonnet"]}))
    config = root / "server.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": judge_model,
                    "connection": {
                        "base_url": f"{mock_url}/v1",
                        "api_key": "mock-key",
                    },
                }
            }
        )
    )
    providers = {
        "pinned-claude": {
            "kind": "key",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": mock_url,
                "api_key": "mock-key",
                "models": {"default": models["sonnet"], **models},
            },
        }
    }
    mock_env = {
        "OPENAI_BASE_URL": f"{mock_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_BASE_URL": mock_url,
        "ANTHROPIC_AUTH_TOKEN": None,
        "CLAUDE_CODE_OAUTH_TOKEN": None,
        "CLAUDECODE": None,
    }
    with (
        isolated_local_server(
            root / "server",
            extra_args=["--config", str(config)],
            env={
                **mock_env,
                "OMNIGENT_WEB_UI_DIST": str(web_ui_dist) if web_ui_dist is not None else None,
            },
        ) as base_url,
        httpx.Client(base_url=base_url, timeout=30, trust_env=False) as client,
        isolated_host(
            root / "host",
            base_url,
            config={"providers": providers},
            env={
                **mock_env,
                "CLAUDE_CONFIG_DIR": str(claude_config),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            cwd=workspace,
        ) as host,
    ):
        host.wait_online(client, timeout=120, poll_interval=0.5)
        yield base_url, client, host.host_id, workspace, settings_path
