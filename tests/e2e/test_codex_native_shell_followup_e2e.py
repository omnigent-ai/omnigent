"""Native Codex shell input preserves prompt ordering and owner authorization."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native.bridge import (
    mark_shell_command_pending,
    read_bridge_state,
)
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import send_user_text
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL = "gpt-5.4-mini"


@contextmanager
def _codex_native_session(
    tmp_path: Path, mock_llm_server_url: str
) -> Iterator[tuple[ServerRunner, httpx.Client, str, Path]]:
    """Run a real native session with the configured current or compatibility server."""
    if shutil.which("codex") is None or shutil.which("tmux") is None:
        pytest.skip("requires real Codex and tmux binaries")
    config = tmp_path / "config"
    source = tmp_path / "codex-source"
    config.mkdir()
    source.mkdir()
    (source / "config.toml").write_text(
        f'model = "{_MODEL}"\n'
        'model_provider = "shell-test"\n'
        "features.plugins = false\n"
        "check_for_update_on_startup = false\n"
        "[model_providers.shell-test]\n"
        'name = "Shell test"\n'
        f'base_url = "{mock_llm_server_url}/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
        "supports_websockets = false\n"
    )
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "shell-test": {
                        "kind": "cli-config",
                        "cli": "codex",
                        "model_provider": "shell-test",
                        "default": True,
                    }
                },
            }
        )
    )
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    native_env = {"OMNIGENT_CONFIG_HOME": str(config), "CODEX_HOME": str(source)}
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
            timeout=30,
            trust_env=False,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        stack.start_runner(cwd=_REPO_ROOT, env=native_env)
        created = create_native_session(
            client,
            stack.base_url,
            harness="codex",
            model=_MODEL,
            metadata={"workspace": str(stack.workspace)},
        )
        session_id = created["session_id"]
        bridge_id = hashlib.sha256(session_id.encode()).hexdigest()[:32]
        bridge = stack.runner_home / ".omnigent" / "codex-native" / bridge_id
        try:
            client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": stack.runner_id}
            ).raise_for_status()
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
                timeout=90,
            ).raise_for_status()
            poll_until(lambda: read_bridge_state(bridge), timeout_s=60, interval_s=0.1)
            yield stack, client, session_id, bridge
        finally:
            client.delete(f"/v1/sessions/{session_id}")


@pytest.mark.posix_only
@pytest.mark.timeout(240)
@pytest.mark.min_server_version("0.21.0")
@pytest.mark.min_runner_version("0.21.0")
def test_web_prompt_during_standalone_shell_gets_model_reply(
    tmp_path: Path, mock_llm_server_url: str
) -> None:
    """The shell output and follow-up answer both survive the native round trip."""
    with _codex_native_session(tmp_path, mock_llm_server_url) as (
        stack,
        client,
        session_id,
        bridge,
    ):
        release = stack.workspace / "release-shell"

        def items() -> list[dict[str, Any]]:
            response = client.get(
                f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
            )
            response.raise_for_status()
            return response.json()["data"]

        def assistant_has(marker: str) -> bool:
            return any(
                item.get("role") == "assistant" and marker in json.dumps(item.get("content"))
                for item in items()
            )

        def configure_reply(marker: str) -> None:
            configure_mock_llm(mock_llm_server_url, [], match=marker)
            set_fallback_mock_llm(mock_llm_server_url, key=marker, text=marker)

        try:
            warmup = f"WARMUP_{uuid.uuid4().hex}"
            configure_reply(warmup)
            send_user_text(client, session_id=session_id, text=f"Reply with {warmup}")
            poll_until(lambda: assistant_has(warmup), timeout_s=60, interval_s=0.1)
            poll_until(
                lambda: (
                    (state := read_bridge_state(bridge)) is not None
                    and state.active_turn_id is None
                ),
                timeout_s=60,
                interval_s=0.1,
            )

            output = f"SHELL_OUTPUT_{uuid.uuid4().hex}"
            reply = f"FOLLOWUP_REPLY_{uuid.uuid4().hex}"
            configure_reply(reply)
            # Hold the shell until the ordinary prompt is accepted by the server.
            command = (
                f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.1; done; "
                f"sleep 2; printf '%s\\n' {output}"
            )
            send_user_text(client, session_id=session_id, text=f"!{command}")
            poll_until(
                lambda: any(
                    item.get("type") == "terminal_command"
                    and item.get("kind") == "input"
                    and item.get("input") == command
                    for item in items()
                ),
                timeout_s=60,
                interval_s=0.1,
            )
            state = read_bridge_state(bridge)
            assert state is not None and state.active_turn_id is not None
            shell_turn_id = state.active_turn_id
            with ThreadPoolExecutor(max_workers=1) as sender:
                prompt = sender.submit(
                    send_user_text, client, session_id=session_id, text=f"Reply with {reply}"
                )
                try:
                    poll_until(
                        lambda: (
                            reply
                            in json.dumps(
                                client.get(f"/v1/sessions/{session_id}").json()["pending_inputs"]
                            )
                        ),
                        timeout_s=60,
                        interval_s=0.1,
                    )
                    state = read_bridge_state(bridge)
                    assert state is not None and state.active_turn_id == shell_turn_id
                finally:
                    release.touch()
                prompt.result(timeout=30)

            poll_until(lambda: assistant_has(reply), timeout_s=60, interval_s=0.1)
            poll_until(
                lambda: client.get(f"/v1/sessions/{session_id}").json()["pending_inputs"] == [],
                timeout_s=30,
                interval_s=0.1,
            )
            transcript = items()
            replies = [
                item
                for item in transcript
                if item.get("role") == "assistant" and reply in json.dumps(item.get("content"))
            ]
            assert len(replies) == 1
            shell_outputs = [
                item
                for item in transcript
                if item.get("type") == "terminal_command" and item.get("kind") == "output"
            ]
            assert len(shell_outputs) == 1
            assert shell_outputs[0]["stdout"] == f"{output}\n"
            requests = [
                request
                for request in get_mock_requests(mock_llm_server_url)
                if request.get("prompt_cache_key") == state.thread_id
                and reply in json.dumps(request.get("input"))
            ]
            assert len(requests) == 1, requests
            assert output in json.dumps(requests[0]["input"])
            assert not [item for item in transcript if item.get("type") == "error"]

            poll_until(
                lambda: (
                    (state := read_bridge_state(bridge)) is not None
                    and state.active_turn_id is None
                    and state.pending_shell_command_id is None
                ),
                timeout_s=30,
                interval_s=0.1,
            )
            state = read_bridge_state(bridge)
            assert state is not None
            # Simulate a disconnected submission that never reached the native server.
            assert mark_shell_command_pending(bridge, state, uuid.uuid4().hex)
            recovered = f"RECOVERED_REPLY_{uuid.uuid4().hex}"
            configure_reply(recovered)
            send_user_text(client, session_id=session_id, text=f"Reply with {recovered}")
            poll_until(lambda: assistant_has(recovered), timeout_s=60, interval_s=0.1)
            poll_until(
                lambda: client.get(f"/v1/sessions/{session_id}").json()["pending_inputs"] == [],
                timeout_s=30,
                interval_s=0.1,
            )
            state = read_bridge_state(bridge)
            assert state is not None and state.pending_shell_command_id is None
            transcript = items()
            assert (
                sum(
                    item.get("role") == "assistant"
                    and recovered in json.dumps(item.get("content"))
                    for item in transcript
                )
                == 1
            )
            assert (
                sum(
                    item.get("type") == "terminal_command" and item.get("kind") == "input"
                    for item in transcript
                )
                == 1
            )
            recovered_requests = [
                request
                for request in get_mock_requests(mock_llm_server_url)
                if request.get("prompt_cache_key") == state.thread_id
                and recovered in json.dumps(request.get("input"))
            ]
            assert len(recovered_requests) == 1, recovered_requests
            assert not [item for item in transcript if item.get("type") == "error"]
        finally:
            release.touch()
            (tmp_path / "transcript.json").write_text(json.dumps(items(), indent=2))


@pytest.mark.posix_only
@pytest.mark.timeout(180)
@pytest.mark.compat_smoke
def test_editor_bang_input_cannot_execute_through_an_older_server(
    tmp_path: Path, mock_llm_server_url: str
) -> None:
    """Legacy unmarked shell input is refused without blocking ordinary editor prompts."""
    with _codex_native_session(tmp_path, mock_llm_server_url) as (
        stack,
        client,
        session_id,
        bridge,
    ):
        editor = {"X-Forwarded-Email": "editor@example.com"}
        client.put(
            f"/v1/sessions/{session_id}/permissions",
            json={"user_id": editor["X-Forwarded-Email"], "level": 2},
        ).raise_for_status()
        marker = f"EDITOR_REPLY_{uuid.uuid4().hex}"
        target = stack.workspace / "unauthorized-shell-output"
        command = f"printf {marker} > {shlex.quote(str(target))}"
        configure_mock_llm(mock_llm_server_url, [], match=marker)
        set_fallback_mock_llm(mock_llm_server_url, key=marker, text=marker)

        def items() -> list[dict[str, Any]]:
            response = client.get(
                f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
            )
            response.raise_for_status()
            return response.json()["data"]

        def answered() -> bool:
            return any(
                item.get("role") == "assistant" and marker in json.dumps(item.get("content"))
                for item in items()
            )

        def authorization_error() -> dict[str, Any] | None:
            return next(
                (
                    item
                    for item in items()
                    if item.get("type") == "error"
                    and item.get("code") == "codex_shell_authorization_required"
                ),
                None,
            )

        response = client.post(
            f"/v1/sessions/{session_id}/events",
            headers=editor,
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"!{command}"}],
                },
            },
        )
        # Current servers reject editors; older servers forward ordinary unmarked input.
        assert response.status_code in {202, 403}, response.text
        if response.status_code == 202:
            poll_until(
                lambda: target.exists() or answered() or authorization_error(),
                timeout_s=60,
                interval_s=0.1,
            )
            assert not target.exists(), "The legacy message executed as a direct shell command"
            error = authorization_error()
            assert error is not None, items()
            assert error.get("remediation")
            session = client.get(f"/v1/sessions/{session_id}")
            session.raise_for_status()
            assert command in json.dumps(session.json()["pending_inputs"])
            state = read_bridge_state(bridge)
            assert state is not None
            assert not [
                request
                for request in get_mock_requests(mock_llm_server_url)
                if request.get("prompt_cache_key") == state.thread_id
            ]
        response = client.post(
            f"/v1/sessions/{session_id}/events",
            headers=editor,
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Reply with {marker}"}],
                },
            },
        )
        assert response.status_code == 202, response.text
        poll_until(answered, timeout_s=60, interval_s=0.1)
        transcript = items()
        (tmp_path / "transcript.json").write_text(json.dumps(transcript, indent=2))
        assert not target.exists()
        assert not any(item.get("type") == "terminal_command" for item in transcript)
        state = read_bridge_state(bridge)
        assert state is not None
        requests = [
            request
            for request in get_mock_requests(mock_llm_server_url)
            if request.get("prompt_cache_key") == state.thread_id
            and marker in json.dumps(request.get("input"))
        ]
        assert len(requests) == 1, requests
