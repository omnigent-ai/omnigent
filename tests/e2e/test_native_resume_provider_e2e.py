"""Claude and Pi runner restarts use the current configured inference provider.

Real server, runner, native CLI and tmux processes exercise web/API delivery and
terminal input against local model endpoints. No vendor login is needed; Claude
installations with machine-wide managed settings need an isolated environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
import yaml

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    PI_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec
from omnigent.harnesses.pi_native.main import _materialize_pi_agent_spec
from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm
from tests.e2e.test_host_codex_native_e2e import _send_user_text

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL = "claude-sonnet-4-5"
_T = TypeVar("_T")


def _wait(check: Callable[[], _T], description: str, timeout: float = 60) -> _T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


@pytest.mark.posix_only
@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    ("harness", "selection"),
    [
        ("claude", "changed-default"),
        ("claude", "unchanged-default"),
        ("claude", "agent-pinned"),
        ("pi", "changed-default"),
        ("pi", "unchanged-default"),
        ("pi", "harness-pinned"),
    ],
)
def test_connected_native_resume_routes_to_configured_provider(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    mock_llm_server_url: str,
    harness: str,
    selection: str,
) -> None:
    """A restart refreshes routing while keeping history, identity and explicit pins."""
    if (reason := cli_unavailable_reason(harness)) is not None:
        pytest.skip(reason)
    if shutil.which("tmux") is None:
        pytest.skip("requires tmux for the real native terminal")
    if harness == "claude" and any(
        path.exists()
        for path in (
            Path("/etc/claude-code/managed-settings.json"),
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
        )
    ):
        pytest.skip("run in an isolated environment without machine-wide Claude managed settings")

    providers = {"legacy": isolated_mock_llm_server_url, "current": mock_llm_server_url}
    config_root = tmp_path / "config"
    config_root.mkdir()

    def select_provider(selected: str) -> str:
        config = {
            "runner": {"idle_timeout_s": 0},
            "providers": {
                name: {
                    "kind": "key",
                    "default": ["anthropic", "pi"] if name == selected else False,
                    "anthropic": {
                        "base_url": url,
                        "api_key": f"local-{name}-test-key",
                        "models": {"default": _MODEL},
                    },
                }
                for name, url in providers.items()
            },
        }
        if selection == "harness-pinned":
            config["inference"] = {
                "harnesses": {"pi-native": {"provider": "legacy", "default_model": _MODEL}},
            }
        contents = json.dumps(config)
        (config_root / "config.yaml").write_text(contents)
        return contents

    select_provider("legacy")
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    native_env = {
        "OMNIGENT_CONFIG_HOME": str(config_root),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "TERM": "xterm-256color",
    }
    with (
        server_runner(
            tmp_path / "stack",
            base_env=base_env,
            server_env=native_env,
            server_cwd=_REPO_ROOT,
            poll_interval=0.1,
        ) as stack,
        httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        if harness == "claude":
            claude_config = Path(native_env["CLAUDE_CONFIG_DIR"])
            claude_config.mkdir()
            (claude_config / ".claude.json").write_text(
                json.dumps(
                    {
                        "hasCompletedOnboarding": True,
                        "theme": "dark",
                        "projects": {str(stack.workspace): {"hasTrustDialogAccepted": True}},
                    }
                )
            )
        stack.start_runner(env=native_env, cwd=_REPO_ROOT)
        materialize = (
            _materialize_claude_agent_spec if harness == "claude" else _materialize_pi_agent_spec
        )
        spec = yaml.safe_load(materialize(tmp_path).read_text())
        spec["executor"]["model"] = _MODEL
        if selection == "agent-pinned":
            spec["executor"]["auth"] = {"type": "provider", "name": "legacy"}
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({f"{harness}-native-ui.yaml": yaml.safe_dump(spec).encode()}),
            metadata={
                "workspace": str(stack.workspace),
                "labels": {
                    WRAPPER_LABEL_KEY: (
                        CLAUDE_NATIVE_WRAPPER_VALUE
                        if harness == "claude"
                        else PI_NATIVE_WRAPPER_VALUE
                    ),
                    UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                },
            },
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        client.patch(
            f"/v1/sessions/{session_id}", json={"runner_id": stack.runner_id}
        ).raise_for_status()

        def ensure_terminal() -> dict[str, Any]:
            response = client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": harness, "session_key": "main", "ensure_native_terminal": True},
            )
            assert response.is_success, f"{response.text}\n{stack.log_tail()}"
            return response.json()

        def snapshot() -> dict[str, Any]:
            response = client.get(f"/v1/sessions/{session_id}")
            response.raise_for_status()
            return response.json()

        history: list[str] = []

        def round_trip(expected: str, terminal: dict[str, Any] | None = None) -> None:
            prompt = f"provider-probe-{uuid.uuid4().hex}"
            replies = {name: f"{name}-reply-{uuid.uuid4().hex}" for name in providers}
            for name, url in providers.items():
                configure_mock_llm(url, [], match=prompt)
                set_fallback_mock_llm(url, prompt, replies[name])

            def captured_requests() -> dict[str, list[dict[str, Any]]] | None:
                captured = {
                    name: [
                        request
                        for request in get_mock_requests(url)
                        if request.get("stream") and prompt in json.dumps(request.get("messages"))
                    ]
                    for name, url in providers.items()
                }
                return captured if any(captured.values()) else None

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
                time.sleep(0.3)
                subprocess.run([*tmux, "Enter"], check=True, timeout=10)
                for _ in range(10):
                    time.sleep(1)
                    if captured_requests():
                        break
                    subprocess.run([*tmux, "Enter"], check=True, timeout=10)

            captured = _wait(captured_requests, f"{harness} inference request")
            assert captured is not None
            (tmp_path / f"{prompt}-requests.json").write_text(json.dumps(captured, indent=2))
            counts = {name: len(requests) for name, requests in captured.items()}
            assert captured[expected], (
                f"{harness} resume ignored configured provider {expected!r}; "
                f"request counts={counts}"
            )
            assert not captured[next(name for name in providers if name != expected)]
            request = captured[expected][-1]
            assert request["model"] == _MODEL
            for previous_text in history:
                assert previous_text in json.dumps(request["messages"]), "resume lost history"

            def mirrored_reply() -> bool:
                response = client.get(
                    f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
                )
                response.raise_for_status()
                return any(
                    item.get("role") == "assistant" and replies[expected] in json.dumps(item)
                    for item in response.json()["data"]
                )

            _wait(mirrored_reply, f"{harness} assistant reply mirrored into Omnigent")
            history.extend((prompt, replies[expected]))
            print(f"{harness} {selection}: expected={expected}, requests={counts}", flush=True)

        terminal = ensure_terminal()
        try:
            round_trip("legacy")
            before = snapshot()
            assert before["external_session_id"]

            previous_runner = stack.runner
            assert previous_runner is not None
            terminate_process(previous_runner)
            assert previous_runner.poll() is not None
            _wait(
                lambda: not client.get(f"/v1/runners/{stack.runner_id}/status").json()["online"],
                "stopped runner",
            )
            source_config = select_provider(
                "legacy" if selection == "unchanged-default" else "current"
            )
            stack.runner = None
            stack.start_runner(env=native_env, cwd=_REPO_ROOT)
            assert stack.runner is not None and stack.runner.pid != previous_runner.pid
            terminal = ensure_terminal()
            expected = "current" if selection == "changed-default" else "legacy"
            round_trip(expected)
            after = snapshot()
            for field in (
                "external_session_id",
                "workspace",
                "model_override",
                "reasoning_effort",
            ):
                assert after.get(field) == before.get(field), f"resume changed {field}"
            round_trip(expected, terminal)
            assert snapshot()["external_session_id"] == before["external_session_id"]
            assert (config_root / "config.yaml").read_text() == source_config
        except Exception:
            print(stack.log_tail(), flush=True)
            metadata = terminal.get("metadata", {})
            if metadata.get("tmux_socket") and metadata.get("tmux_target"):
                pane = subprocess.run(
                    [
                        "tmux",
                        "-S",
                        metadata["tmux_socket"],
                        "capture-pane",
                        "-p",
                        "-t",
                        metadata["tmux_target"],
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                print(pane.stdout, flush=True)
            raise
