"""Execute both Hermes policy wrappers against a real local policy endpoint."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.harnesses.hermes_native import bridge


@pytest.mark.posix_only
@pytest.mark.parametrize("use_relay", [False, True], ids=["server", "relay"])
@pytest.mark.parametrize("action", ["ALLOW", "DENY"])
def test_policy_wrapper_executes_hook_and_enforces_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_relay: bool, action: str
) -> None:
    # Keep real user credentials out of the generated home and wrapper.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda **_kw: None)
    monkeypatch.setattr("omnigent.cli_auth.databricks_request_headers", lambda *_a, **_kw: {})
    requests: list[tuple[str, object, str | None]] = []

    class PolicyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body["event"], self.headers.get("Authorization")))
            response = json.dumps(
                {"result": f"POLICY_ACTION_{action}", "reason": "test policy"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), PolicyHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            bridge_dir = tmp_path / "bridge with spaces"
            hermes_home = bridge.write_policy_hook_config(bridge_dir, url, "wrapper-test")
            if use_relay:
                assert bridge.inject_relay_into_policy_hook(
                    bridge_dir, url, "test-relay-token", "http://unused.invalid", "wrapper-test"
                )

            env = {k: v for k, v in os.environ.items() if not k.startswith("_OMNIGENT_")}
            tool_input = {"command": "pwd"}
            result = subprocess.run(
                [str(hermes_home / "omnigent-policy-hook.sh")],
                input=json.dumps({"tool_name": "terminal", "tool_input": tool_input}),
                capture_output=True,
                text=True,
                env=env,
                timeout=20,
                check=True,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert result.stderr == ""
    assert requests == [
        (
            "/policies/evaluate" if use_relay else "/v1/sessions/wrapper-test/policies/evaluate",
            {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {"name": "terminal", "arguments": tool_input},
                "context": {},
            },
            "Bearer test-relay-token" if use_relay else None,
        )
    ]
    decision = json.loads(result.stdout)
    if action == "ALLOW":
        assert decision == {}
    else:
        assert decision == {
            "decision": "block",
            "reason": "Tool 'terminal' denied by Omnigent policy: test policy",
        }
