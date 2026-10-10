"""A connected-host Codex restart respects the current configured provider.

Real Omnigent server/runner processes, Codex and tmux exercise fresh launch,
runner restart, and terminal reattachment. Both model endpoints are local.
The same test runs against main without calling any API introduced by the fix.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent._wrapper_labels import (
    CODEX_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.codex_native.app_server import client_for_transport
from omnigent.harnesses.codex_native.bridge import read_bridge_state
from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec
from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm, get_mock_requests
from tests.e2e.test_host_codex_native_e2e import _poll_for_assistant_marker, _send_user_text

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL = "gpt-5.4-mini"


def _wait(check, description: str, timeout: float = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


async def _resume_settings(transport: str, thread_id: str) -> dict:
    """Attach a second client without a provider override, as the TUI does."""
    client = client_for_transport(transport, client_name="provider-resume-test")
    try:
        await client.connect()
        response = await client.request(
            "thread/resume", {"threadId": thread_id, "excludeTurns": True}
        )
        return response["result"]
    finally:
        await client.close()


@pytest.mark.posix_only
@pytest.mark.timeout(240)
@pytest.mark.parametrize("selection", ["changed-default", "unchanged-default", "agent-pinned"])
def test_connected_codex_resume_routes_to_configured_provider(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    mock_llm_server_url: str,
    selection: str,
) -> None:
    """A config change reaches inference without changing history or other settings."""
    if shutil.which("codex") is None or shutil.which("tmux") is None:
        pytest.skip("requires real Codex and tmux binaries")
    providers = {"legacy": isolated_mock_llm_server_url, "current": mock_llm_server_url}
    config_root = tmp_path / "config"
    config_root.mkdir()
    source_home = tmp_path / "codex-config"
    source_home.mkdir()
    source_config = "\n".join(
        [
            'model_provider="legacy"',
            f'model="{_MODEL}"',
            'model_reasoning_effort="high"',
            "analytics.enabled=false",
            "feedback.enabled=false",
            "features.plugins=false",
            'otel.metrics_exporter="none"',
            "check_for_update_on_startup=false",
            *(
                f'[model_providers.{name}]\nname="{name}"\n'
                f'base_url="{url}/v1"\nwire_api="responses"\n'
                "requires_openai_auth=false\n"
                f'[model_providers.{name}.auth]\ncommand="printf"\nargs=["mock-key"]'
                for name, url in providers.items()
            ),
        ]
    )
    (source_home / "config.toml").write_text(source_config)

    def select_provider(selected: str) -> None:
        (config_root / "config.yaml").write_text(
            json.dumps(
                {
                    "runner": {"idle_timeout_s": 0},
                    "providers": {
                        name: {
                            "kind": "cli-config",
                            "cli": "codex",
                            "model_provider": name,
                            "default": name == selected,
                        }
                        for name in providers
                    },
                }
            )
        )

    select_provider("legacy")
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    native_env = {
        "OMNIGENT_CONFIG_HOME": str(config_root),
        "CODEX_HOME": str(source_home),
    }
    with (
        server_runner(
            tmp_path / "stack",
            base_env=base_env,
            server_cwd=_REPO_ROOT,
            server_env=native_env,
            poll_interval=0.1,
        ) as stack,
        httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        state_root = stack.runner_home / ".omnigent" / "codex-native"
        stack.start_runner(env=native_env, cwd=_REPO_ROOT)
        spec = yaml.safe_load(_materialize_codex_agent_spec(tmp_path, model=_MODEL).read_text())
        if selection == "agent-pinned":
            spec["executor"]["auth"] = {"type": "provider", "name": "legacy"}
        create = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"codex-native-ui.yaml": yaml.safe_dump(spec).encode()}),
            metadata={
                "workspace": str(stack.workspace),
                "reasoning_effort": "high",
                "terminal_launch_args": ["-s", "read-only", "-a", "never"],
                "labels": {
                    WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
                    UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                },
            },
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]
        client.patch(
            f"/v1/sessions/{session_id}", json={"runner_id": stack.runner_id}
        ).raise_for_status()
        terminal_url = f"/v1/sessions/{session_id}/resources/terminals"

        def ensure_terminal() -> dict:
            response = client.post(
                terminal_url,
                json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            )
            assert response.is_success, f"{response.text}\n{stack.log_tail()}"
            return response.json()

        def state():
            for path in state_root.glob("*/state.json"):
                value = read_bridge_state(path.parent)
                if value is not None and value.session_id == session_id:
                    return value
            return None

        def round_trip(prompt: str, expected_provider: str, terminal: dict | None = None) -> None:
            def captured_requests():
                captured = {
                    name: [
                        request
                        for request in get_mock_requests(url)
                        if request.get("prompt_cache_key") == original.thread_id
                        and prompt in json.dumps(request.get("input"))
                    ]
                    for name, url in providers.items()
                }
                return captured if any(captured.values()) else None

            replies = {name: f"{name}-reply-{uuid.uuid4().hex}" for name in providers}
            for name, url in providers.items():
                # Background title requests can share the prompt without exhausting its reply.
                configure_mock_llm(url, [], match=prompt)
                httpx.post(
                    f"{url}/mock/set_fallback",
                    json={"key": prompt, "text": replies[name]},
                    timeout=5,
                ).raise_for_status()
            if terminal is None:
                _send_user_text(client, session_id=session_id, text=prompt)
            else:
                tmux = [
                    "tmux",
                    "-S",
                    terminal["metadata"]["tmux_socket"],
                    "send-keys",
                    "-t",
                    terminal["metadata"]["tmux_target"],
                ]
                subprocess.run([*tmux, "-l", prompt], check=True, timeout=10)
                # Let Codex's paste detection settle before submitting the message.
                time.sleep(0.3)
                subprocess.run([*tmux, "Enter"], check=True, timeout=10)
                # A slow TUI can drop Enter while absorbing a paste; retry until it submits.
                for _ in range(10):
                    time.sleep(1)
                    if captured_requests():
                        break
                    subprocess.run([*tmux, "Enter"], check=True, timeout=10)

            requests = _wait(captured_requests, "Codex inference request")
            (tmp_path / f"{prompt}-requests.json").write_text(json.dumps(requests, indent=2))
            counts = {name: len(value) for name, value in requests.items()}
            assert requests[expected_provider], (
                f"resume ignored configured provider {expected_provider!r}; "
                f"request counts={counts}"
            )
            assert not requests[next(name for name in providers if name != expected_provider)]
            assert requests[expected_provider][-1]["model"] == _MODEL
            assert requests[expected_provider][-1]["reasoning"]["effort"] == "high"
            if prompt != first_prompt:
                assert first_prompt in json.dumps(requests[expected_provider][-1]["input"])
            _poll_for_assistant_marker(
                client, session_id=session_id, marker=replies[expected_provider], timeout=30
            )
            _wait(lambda: (s := state()) is not None and s.active_turn_id is None, "idle thread")

        ensure_terminal()
        original = _wait(state, "initial Codex thread")
        first_prompt = f"provider-probe-{uuid.uuid4().hex}"
        round_trip(first_prompt, "legacy")
        before = asyncio.run(_resume_settings(original.socket_path, original.thread_id))
        assert before["modelProvider"] == "legacy"
        assert before["approvalPolicy"] == "never"
        assert before["sandbox"]["type"] == "readOnly"
        assert before["reasoningEffort"] == "high"

        terminate_process(stack.runner)
        _wait(
            lambda: not client.get(f"/v1/runners/{stack.runner_id}/status").json()["online"],
            "stopped runner",
        )
        select_provider("legacy" if selection == "unchanged-default" else "current")
        stack.runner = None
        stack.start_runner(env=native_env, cwd=_REPO_ROOT)
        terminal = ensure_terminal()
        resumed = _wait(state, "resumed Codex thread")
        assert resumed.thread_id == original.thread_id
        expected = "current" if selection == "changed-default" else "legacy"
        round_trip(f"provider-probe-{uuid.uuid4().hex}", expected)
        after = asyncio.run(_resume_settings(resumed.socket_path, resumed.thread_id))
        assert after["modelProvider"] == expected
        for field in (
            "model",
            "reasoningEffort",
            "cwd",
            "approvalPolicy",
            "approvalsReviewer",
            "sandbox",
            "runtimeWorkspaceRoots",
        ):
            assert after[field] == before[field], f"resume changed {field}"
        round_trip(f"provider-probe-{uuid.uuid4().hex}", expected, terminal)
        assert (source_home / "config.toml").read_text() == source_config
