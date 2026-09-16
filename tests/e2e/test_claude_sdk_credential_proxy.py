"""Exercise credential renewal through a real SDK harness and OS sandbox."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Exercise the Linux bubblewrap path used by repro/resolve agents",
)

_REFRESH_INTERVAL_SECONDS = 0.1


@pytest.fixture
def credential_source(tmp_path: Path) -> tuple[Path, tuple[str, str]]:
    secrets = (
        f"initial-test-secret-{uuid.uuid4().hex}",
        f"replacement-test-secret-{uuid.uuid4().hex}",
    )
    source = tmp_path / "credential.txt"
    source.write_text(secrets[0])
    return source, secrets


@pytest.fixture
def credential_upstream(
    credential_source: tuple[Path, tuple[str, str]],
) -> Iterator[tuple[str, list[str | None]]]:
    """Rotate the parent-side secret before allowing the second sandbox request."""
    captured: list[str | None] = []
    source, secrets = credential_source

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            captured.append(self.headers.get("Authorization"))
            if len(captured) == 1:
                source.write_text(secrets[1])
                time.sleep(_REFRESH_INTERVAL_SECONDS)
            body = b"authenticated-probe-reached-upstream"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://localhost:{server.server_port}/probe", captured
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_claude_sdk_credential_proxy_survives_transport_and_renews(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    credential_upstream: tuple[str, list[str | None]],
    credential_source: tuple[Path, tuple[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh credentials within one real SDK shell call without exposing the source."""
    assert shutil.which("claude"), "Install the Claude CLI to exercise the real SDK harness"
    assert shutil.which("curl"), "Install curl to exercise the sandbox's outbound request"
    assert shutil.which("bwrap"), "Install bubblewrap to exercise the Linux sandbox"
    monkeypatch.setitem(http_client.headers, "x-omnigent-background-session-titles", "off")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source, secrets = credential_source
    upstream_url, captured = credential_upstream
    model = f"mock-credential-proxy-{uuid.uuid4().hex}"
    prompt = f"Run the credential probe {model}."
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": '{"title":"Credential proxy E2E"}'}],
        key=f"{model}-title",
        match=f"<session>\n{prompt}\n</session>",
    )
    agent_name = register_inline_agent(
        http_client,
        name=model,
        harness="claude-sdk",
        model=model,
        profile="",
        prompt="Execute the requested shell probe, then report completion.",
        mock_llm_base_url=mock_llm_server_url,
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": str(workspace),
                "sandbox": {
                    "type": "linux_bwrap",
                    "read_paths": [str(Path(__file__).resolve().parents[2]), sys.prefix],
                    "write_paths": ["."],
                    "egress_rules": ["* localhost/**", "* 127.0.0.1/**"],
                    "egress_allow_private_destinations": True,
                    "credential_proxy": [
                        {
                            "type": "https_bearer",
                            "target": "localhost",
                            "env": "CREDENTIAL_TEST_TOKEN",
                            "source": {
                                "file": str(source),
                                "refresh_interval_seconds": _REFRESH_INTERVAL_SECONDS,
                            },
                        }
                    ],
                },
            }
        },
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    placeholder_file = workspace / "placeholder.txt"
    result_files = [workspace / f"result-{index}.txt" for index in range(2)]
    commands = [
        "set -eu",
        f"if cat {shlex.quote(str(source))} >/dev/null 2>&1; then exit 41; fi",
        f'printf "%s" "$CREDENTIAL_TEST_TOKEN" > {shlex.quote(str(placeholder_file))}',
    ]
    commands.extend(
        'curl --fail --silent --show-error --max-time 15 --noproxy "" '
        '--proxy "$HTTP_PROXY" --header "Authorization: Bearer $CREDENTIAL_TEST_TOKEN" '
        f"{shlex.quote(upstream_url)} > {shlex.quote(str(result_file))}"
        for result_file in result_files
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex}",
                        "name": "mcp__omnigent__sys_os_shell",
                        "arguments": json.dumps({"command": "\n".join(commands)}),
                    }
                ]
            },
            {"text": "Credential probe completed."},
        ],
        key=model,
    )
    response_id = send_user_message_to_session(http_client, session_id=session_id, content=prompt)
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=120
    )
    assert body["status"] == "completed", body
    for result_file in result_files:
        assert result_file.is_file(), body
        assert result_file.read_text() == "authenticated-probe-reached-upstream", body
    assert captured == [f"Bearer {secret}" for secret in secrets]
    assert placeholder_file.read_text().startswith("oa_cred_")
    requests = httpx.get(f"{mock_llm_server_url}/mock/requests", params={"key": model})
    requests.raise_for_status()
    for secret in secrets:
        assert secret not in json.dumps(body)
        assert secret not in requests.text
