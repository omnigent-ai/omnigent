"""Real SDK/native Pi dialogs through the runner, server and approval API."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent._wrapper_labels import (
    PI_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.pi_native.main import _materialize_pi_agent_spec
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import (
    bind_session_runner,
    bundle_files,
    post_session_bundle,
    send_user_text,
)
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import configure_mock_llm, get_mock_requests

_REPO = Path(__file__).resolve().parents[2]
_MODEL = "claude-sonnet-4-5"


@pytest.mark.posix_only
@pytest.mark.timeout(180)
@pytest.mark.parametrize("transport", ["sdk", "native"])
def test_real_pi_dialog_answers_and_continues(
    tmp_path: Path, mock_llm_server_url: str, transport: str
) -> None:
    """Answers reach real Pi; rejection and native abort leave a usable session."""
    skip_if_harness_cli_missing("pi")
    if transport == "native" and not shutil.which("tmux"):
        pytest.skip("native Pi requires tmux")
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "local": {
                        "kind": "key",
                        "default": ["anthropic", "pi"],
                        "anthropic": {
                            "base_url": mock_llm_server_url,
                            "api_key": "synthetic",
                            "models": {"default": _MODEL},
                        },
                    }
                },
            }
        )
    )
    env = {"OMNIGENT_CONFIG_HOME": str(config_root), "PI_OFFLINE": "1", "TERM": "xterm-256color"}
    base_env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
    with (
        server_runner(
            tmp_path / "stack",
            base_env=base_env,
            server_env=env,
            server_cwd=_REPO,
            poll_interval=0.1,
        ) as stack,
        httpx.Client(
            base_url=stack.base_url,
            timeout=30,
            trust_env=False,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        result = tmp_path / "answers.json"
        cancelled = tmp_path / "cancelled.json"
        extension = stack.workspace / ".pi" / "extensions" / "dialogs.js"
        extension.parent.mkdir(parents=True)
        extension.write_text(
            """const fs = require("node:fs");
module.exports = pi => {
  pi.on("input", async (event, ctx) => {
    if (event.text.includes("PI_DIALOG_START")) {
      const values = [
        await ctx.ui.confirm("Confirm choice", "Continue?"),
        await ctx.ui.select("Select color", ["Blue", "Red"]),
        await ctx.ui.input("Enter name", "Name"),
        await ctx.ui.editor("Edit note", "original note"),
      ];
      fs.writeFileSync(RESULT_PATH, JSON.stringify(values));
    }
    if (event.text.includes("PI_DIALOG_ABORT")) {
      const controller = new AbortController();
      const started = Date.now();
      const timer = setTimeout(() => controller.abort(), 300);
      const value = await ctx.ui.confirm("Abort dialog", "Will cancel", {
        signal: controller.signal, timeout: 5000,
      });
      clearTimeout(timer);
      fs.writeFileSync(CANCELLED_PATH, JSON.stringify({value, elapsedMs: Date.now() - started}));
    }
    return {action: "continue"};
  });
};
""".replace("RESULT_PATH", json.dumps(str(result))).replace(
                "CANCELLED_PATH", json.dumps(str(cancelled))
            )
        )
        stack.start_runner(env=env, cwd=_REPO)
        if transport == "native":
            spec = yaml.safe_load(_materialize_pi_agent_spec(tmp_path).read_text())
            labels = {
                WRAPPER_LABEL_KEY: PI_NATIVE_WRAPPER_VALUE,
                UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            }
        else:
            spec = {
                "name": "pi-dialogs",
                "prompt": "Answer briefly.",
                "spawn": True,
                "executor": {"harness": "pi"},
                "os_env": {"type": "caller_process", "sandbox": {"type": "none"}},
            }
            labels = {}
        spec["executor"]["model"] = _MODEL
        spec["executor"]["auth"] = {"type": "provider", "name": "local"}
        response = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()}),
            metadata={"workspace": str(stack.workspace), "labels": labels},
        )
        response.raise_for_status()
        session_id = response.json()["session_id"]
        bind_session_runner(client.patch, "", session_id, stack.runner_id)

        def pending():
            response = client.get(f"/v1/sessions/{session_id}")
            response.raise_for_status()
            return response.json().get("pending_elicitations") or []

        def replied(marker):
            response = client.get(
                f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
            )
            response.raise_for_status()
            return any(
                item.get("role") == "assistant" and marker in json.dumps(item)
                for item in response.json()["data"]
            )

        configure_mock_llm(
            mock_llm_server_url,
            [
                {"text": "DIALOGS_ANSWERED", "stream": True},
                {"text": "FOLLOWUP_WORKED", "stream": True},
                {"text": "ABORT_CONTINUED", "stream": True},
            ],
            key=_MODEL,
        )
        send_user_text(client, session_id=session_id, text="PI_DIALOG_START")
        previous_id = None
        for title, action, content in [
            ("Confirm choice", "decline", None),
            ("Select color", "accept", {"0": "Blue"}),
            ("Enter name", "accept", {"0": "Ada"}),
            ("Edit note", "accept", {"0": "two\nlines"}),
        ]:
            poll_until(
                lambda previous_id=previous_id: any(
                    c.get("elicitation_id") != previous_id for c in pending()
                ),
                timeout_s=60,
                interval_s=0.1,
            )
            card = next(c for c in pending() if c.get("elicitation_id") != previous_id)
            assert title in json.dumps(card), card
            previous_id = card["elicitation_id"]
            client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "approval",
                    "data": {
                        "elicitation_id": previous_id,
                        "action": action,
                        "content": content,
                    },
                },
            ).raise_for_status()
        poll_until(result.exists, timeout_s=60, interval_s=0.1)
        assert json.loads(result.read_text()) == [False, "Blue", "Ada", "two\nlines"]
        poll_until(lambda: replied("DIALOGS_ANSWERED"), timeout_s=60, interval_s=0.1)
        poll_until(lambda: not pending(), timeout_s=60, interval_s=0.1)
        send_user_text(client, session_id=session_id, text="PI_DIALOG_FOLLOWUP")
        poll_until(lambda: replied("FOLLOWUP_WORKED"), timeout_s=60, interval_s=0.1)
        requests = get_mock_requests(mock_llm_server_url)
        last = next(
            r for r in reversed(requests) if "PI_DIALOG_FOLLOWUP" in json.dumps(r.get("messages"))
        )
        assert "DIALOGS_ANSWERED" in json.dumps(last["messages"])
        if transport == "native":
            send_user_text(client, session_id=session_id, text="PI_DIALOG_ABORT")
            poll_until(cancelled.exists, timeout_s=60, interval_s=0.1)
            outcome = json.loads(cancelled.read_text())
            assert outcome["value"] is False and outcome["elapsedMs"] < 1500, outcome
            poll_until(lambda: replied("ABORT_CONTINUED"), timeout_s=60, interval_s=0.1)
            poll_until(lambda: not pending(), timeout_s=60, interval_s=0.1)
