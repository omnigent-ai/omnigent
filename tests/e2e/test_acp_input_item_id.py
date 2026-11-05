"""Persisted turn identity crosses server, runner, adapter and ACP stdio."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import yaml

from tests._helpers.acp_agent import FAKE_ACP_AGENT_SCRIPT
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import (
    bind_session_runner,
    bundle_files,
    post_session_bundle,
    send_user_text,
)


def test_acp_prompt_names_readable_input_item(tmp_path: Path) -> None:
    """A cold helper, queued replies and a skill all name already-readable items."""
    probe = tmp_path / "probe"
    probe.mkdir()
    agent_script = probe / "agent.py"
    agent_script.write_text(
        f"#!{sys.executable}\n"
        + FAKE_ACP_AGENT_SCRIPT.replace(
            '        sid = msg["params"]["sessionId"]',
            """        import time, urllib.request
        from pathlib import Path
        root = Path(__file__).parent
        chat = (root / "chat").read_text()
        url = (root / "url").read_text() + "/v1/sessions/" + chat + "/items"
        with urllib.request.urlopen(url) as response:
            items = json.load(response)["data"]
        item_id = msg["params"]["_meta"]["ai.omnigent/inputItemId"]
        assert item_id is not None
        item = next(item for item in items if item["id"] == item_id)
        with (root / "prompts.jsonl").open("a") as log:
            log.write(json.dumps({"prompt": msg, "item": item}) + "\\n")
        deadline = time.monotonic() + 30
        while (root / "hold").exists():
            assert time.monotonic() < deadline, "test did not release prompt"
            time.sleep(0.01)
        sid = msg["params"]["sessionId"]""",
        ).replace(
            '    elif method == "session/new":',
            """    elif method == "session/new":
        import time
        from pathlib import Path
        root = Path(__file__).parent
        (root / "new_seen").touch()
        deadline = time.monotonic() + 30
        while (root / "hold_new").exists():
            assert time.monotonic() < deadline, "test did not release session/new"
            time.sleep(0.01)""",
        )
    )
    agent_script.chmod(0o755)
    config = tmp_path / "runner-config"
    config.mkdir()
    (config / "config.yaml").write_text(
        yaml.safe_dump({"harness": {"grok": {"command": str(agent_script)}}})
    )
    bundle = bundle_files(
        {
            "config.yaml": yaml.safe_dump(
                {
                    "spec_version": 1,
                    "name": "input-id",
                    "executor": {"type": "omnigent", "config": {"harness": "grok"}},
                }
            ).encode(),
            "skills/catch-up/SKILL.md": (
                b"---\nname: catch-up\ndescription: Catch up.\n---\nSummarize the current task.\n"
            ),
        }
    )
    repo = Path(__file__).resolve().parents[2]
    env = {
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_AUTH_ENABLED": "0",
        "OPENAI_API_KEY": "unused-test-key",
    }
    with server_runner(
        tmp_path / "stack", server_env=env, server_cwd=repo, poll_interval=0.1
    ) as stack:
        (probe / "url").write_text(stack.base_url)
        stack.start_runner(
            cwd=repo,
            env={
                **env,
                "OMNIGENT_CONFIG_HOME": str(config),
            },
        )
        with httpx.Client(base_url=stack.base_url, timeout=30, trust_env=False) as client:
            created = post_session_bundle(client.post, "/v1/sessions", bundle)
            assert created.status_code == 201, created.text
            chat = created.json()["session_id"]
            (probe / "chat").write_text(chat)

            def external_message(role: str, text: str) -> None:
                response = client.post(
                    f"/v1/sessions/{chat}/events",
                    json={
                        "type": "external_conversation_item",
                        "data": {
                            "item_type": "message",
                            "item_data": {
                                "role": role,
                                **({"agent": "external-agent"} if role == "assistant" else {}),
                                "content": [
                                    {
                                        "type": "input_text" if role == "user" else "output_text",
                                        "text": text,
                                    }
                                ],
                            },
                        },
                    },
                )
                assert response.is_success, response.text

            external_message("user", "previous external context")
            external_message("assistant", "previous reply")
            bind_session_runner(client.patch, stack.base_url, chat, stack.runner_id)

            def records() -> list[dict]:
                path = probe / "prompts.jsonl"
                return (
                    [json.loads(line) for line in path.read_text().splitlines()]
                    if path.exists()
                    else []
                )

            def wait_idle() -> None:
                poll_until(
                    lambda: client.get(f"/v1/sessions/{chat}").json()["status"] == "idle",
                    timeout_s=30,
                )

            (probe / "hold").touch()
            (probe / "hold_new").touch()
            try:
                send_user_text(client, session_id=chat, text="first input")
                poll_until(lambda: (probe / "new_seen").exists(), timeout_s=30)
                items = client.get(f"/v1/sessions/{chat}/items").json()["data"]
                input_id = items[-1]["id"]
                # An external client appends an identical item before the first prompt.
                external_message("user", "first input")
                (probe / "hold_new").unlink()
                poll_until(lambda: len(records()) == 1, timeout_s=30)
                # Both replies arrive while ACP is busy and refuses live injection.
                send_user_text(client, session_id=chat, text="queued input one")
                send_user_text(client, session_id=chat, text="queued input two")
            finally:
                (probe / "hold").unlink()
                (probe / "hold_new").unlink(missing_ok=True)
            poll_until(lambda: len(records()) == 2, timeout_s=30)
            wait_idle()
            rows = records()
            assert rows[0]["item"]["id"] == input_id
            assert "previous reply" in rows[0]["prompt"]["params"]["prompt"][0]["text"]
            assert rows[0]["item"]["content"][0]["text"] == "first input"
            assert rows[1]["item"]["content"][0]["text"] == "queued input two"
            assert rows[0]["item"]["id"] != rows[1]["item"]["id"]
            assert rows[1]["prompt"]["params"]["prompt"][0]["text"].endswith("queued input two")

            skill = client.post(
                f"/v1/sessions/{chat}/events",
                json={"type": "slash_command", "data": {"kind": "skill", "name": "catch-up"}},
            )
            assert skill.status_code == 202, skill.text
            poll_until(lambda: len(records()) == 3, timeout_s=30)
            wait_idle()
            meta = records()[2]["item"]
            assert meta["type"] == "message" and meta["is_meta"] is True
            assert "Summarize the current task." in meta["content"][0]["text"]
