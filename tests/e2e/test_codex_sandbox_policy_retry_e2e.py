"""Real server, runner and Codex CLI recovery using a mock HTTP model provider."""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import (
    bind_session_runner,
    bundle_files,
    post_session_bundle,
    send_user_text,
)
from tests.e2e._harness_probes import bwrap_namespace_unavailable_reason, cli_unavailable_reason
from tests.e2e.conftest import configure_mock_llm, get_mock_requests

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL = "gpt-5.4"
_MARKER = "SANDBOX_POLICY_RETRY_COMPLETE"

# Observe the real runner's spawn boundary without replacing its process manager or worker.
_RUNNER_BOOTSTRAP = """
import json, os, sys
from pathlib import Path

def observe_spawn(event, arguments):
    if event != "subprocess.Popen":
        return
    executable, argv, cwd, env = arguments
    if "omnigent.inner.codex_harness" in argv:
        assert env["HARNESS_CODEX_GATEWAY_AUTH_COMMAND"] == "printf %s mock-key"
        with Path(os.environ["SANDBOX_RETRY_SPAWNS"]).open("a") as output:
            output.write(json.dumps(json.loads(env["HARNESS_CODEX_OS_ENV"])) + "\\n")

sys.addaudithook(observe_spawn)
from omnigent.runner._entry import main
main()
"""


@pytest.mark.posix_only
@pytest.mark.timeout(240)
def test_codex_rejects_fork_then_retries_with_sandbox_in_same_session(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(reason)
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec") is None:
            pytest.skip("requires the real macOS sandbox-exec backend")
        backend = "darwin_seatbelt"
    elif sys.platform.startswith("linux"):
        reason = bwrap_namespace_unavailable_reason()
        if reason is not None:
            pytest.skip(reason)
        backend = "linux_bwrap"
    else:
        pytest.skip("requires a filesystem sandbox on macOS or Linux")

    mock_url = isolated_mock_llm_server_url
    spawn_ledger = tmp_path / "codex-spawns.jsonl"
    base_env = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    runtime_env = {
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT"),
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    config_dir = Path(runtime_env["OMNIGENT_CONFIG_HOME"])
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "sandbox-test": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _MODEL},
                        },
                    }
                }
            }
        )
    )
    spec = {
        "spec_version": 1,
        "name": "sandbox-policy-retry",
        "instructions": "Check the sandbox using the requested command, then reply briefly.",
        "skills_filter": "none",
        "executor": {
            "type": "omnigent",
            "config": {"harness": "codex"},
            "model": _MODEL,
            "auth": {"type": "provider", "name": "sandbox-test"},
        },
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "fork": True,
            "sandbox": {
                "type": backend,
                "allow_network": True,
                "read_paths": ["."],
                "write_paths": [],
            },
        },
    }
    print(
        f"stack=real-server+real-runner+real-Codex provider=mock-HTTP sandbox={backend}",
        flush=True,
    )
    with (
        server_runner(
            tmp_path / "stack", base_env=base_env, server_env=runtime_env, server_cwd=_REPO_ROOT
        ) as stack,
        httpx.Client(
            base_url=stack.base_url,
            timeout=30,
            trust_env=False,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        allowed = stack.workspace / "allowed.txt"
        allowed.write_text("ALLOWED_WORKSPACE_READ\n")
        private = tmp_path / "private.txt"
        private.write_text("private canary")
        forbidden = stack.workspace / "forbidden.txt"
        command = (
            f"cat {shlex.quote(str(allowed))}; "
            f"if cat {shlex.quote(str(private))} >/dev/null 2>&1; "
            "then echo OUTSIDE_READ_ALLOWED; else echo OUTSIDE_READ_DENIED; fi; "
            f"if (echo forbidden > {shlex.quote(str(forbidden))}) 2>/dev/null; "
            "then echo WORKSPACE_WRITE_ALLOWED; else echo WORKSPACE_WRITE_DENIED; fi"
        )
        configure_mock_llm(
            mock_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "sandbox_probe",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": command, "max_output_tokens": 1000}),
                        }
                    ]
                },
                {"text": _MARKER, "stream": True},
            ],
            key=_MODEL,
        )
        stack.start_runner(
            bootstrap=_RUNNER_BOOTSTRAP,
            env={**runtime_env, "SANDBOX_RETRY_SPAWNS": str(spawn_ledger)},
            cwd=_REPO_ROOT,
        )
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"config.yaml": yaml.safe_dump(spec).encode()}),
            metadata={"workspace": str(stack.workspace)},
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id)
        agent_url = f"/v1/sessions/{session_id}/agent"
        original_agent = client.get(agent_url)
        original_agent.raise_for_status()
        original_agent = original_agent.json()

        send_user_text(client, session_id=session_id, text="Try the declared sandbox policy.")

        def rejected() -> bool:
            response = client.get(f"/v1/sessions/{session_id}")
            response.raise_for_status()
            snapshot = response.json()
            return snapshot["status"] in {"failed", "idle"} and "os_env.fork" in json.dumps(
                snapshot.get("last_task_error")
            )

        poll_until(rejected, timeout_s=60, interval_s=0.1)
        assert not spawn_ledger.exists(), (
            "unsupported fork reached the Codex harness spawn boundary"
        )
        assert not get_mock_requests(mock_url), "rejected policy reached the mock model provider"
        print("fork=true rejected; Codex harness launches=0; model requests=0", flush=True)

        corrected_spec = copy.deepcopy(spec)
        corrected_spec["os_env"]["fork"] = False
        corrected_bundle = bundle_files({"config.yaml": yaml.safe_dump(corrected_spec).encode()})
        updated = client.put(
            agent_url,
            files={"bundle": ("agent.tar.gz", corrected_bundle, "application/gzip")},
        )
        updated.raise_for_status()
        assert updated.json()["id"] == original_agent["id"]
        assert updated.json()["version"] > original_agent["version"]
        downloaded = client.get(f"{agent_url}/contents")
        downloaded.raise_for_status()
        assert downloaded.content == corrected_bundle
        print(
            "PUT agent revision accepted; same session; sandbox unchanged; fork=false", flush=True
        )

        send_user_text(client, session_id=session_id, text="Complete the corrected policy retry.")

        def completed() -> bool:
            response = client.get(f"/v1/sessions/{session_id}")
            response.raise_for_status()
            snapshot = response.json()
            if snapshot["status"] == "failed" and "os_env.fork" not in json.dumps(
                snapshot.get("last_task_error")
            ):
                pytest.fail(f"Corrected turn failed: {snapshot.get('last_task_error')}")
            answered = any(
                item["type"] == "message"
                and item["data"].get("role") == "assistant"
                and _MARKER in json.dumps(item["data"].get("content", []))
                for item in snapshot["items"]
            )
            return (
                snapshot["status"] == "idle" and answered and not snapshot.get("last_task_error")
            )

        poll_until(completed, timeout_s=120, interval_s=0.1)
        launches = [json.loads(line) for line in spawn_ledger.read_text().splitlines()]
        assert launches, "recovery must launch the real Codex harness"
        for launch in launches:
            assert launch["fork"] is False
            sandbox = launch["sandbox"]
            assert sandbox["type"] == backend
            assert sandbox["allow_network"] is True
            assert sandbox["read_paths"] == ["."]
            assert sandbox["write_paths"] == []
        requests = get_mock_requests(mock_url, key=_MODEL)
        assert requests, "real Codex never reached the mock Responses API"
        assert any(
            "Complete the corrected policy retry." in json.dumps(r.get("input")) for r in requests
        )
        outputs = [
            item["output"]
            for request in requests
            for item in request.get("input", [])
            if item.get("type") == "function_call_output"
            and item.get("call_id") == "sandbox_probe"
        ]
        assert outputs, "real Codex never returned its native command output"
        output = json.dumps(outputs)
        assert "ALLOWED_WORKSPACE_READ" in output
        assert "OUTSIDE_READ_DENIED" in output
        assert "WORKSPACE_WRITE_DENIED" in output
        assert "OUTSIDE_READ_ALLOWED" not in output
        assert "WORKSPACE_WRITE_ALLOWED" not in output
        assert not forbidden.exists()
        assert private.read_text() == "private canary"
        print(
            "native command: workspace read=allowed; outside read=denied; workspace write=denied",
            flush=True,
        )
        print(
            f"same-session retry=idle; sandbox retained; mock model requests={len(requests)}; "
            f"reply={_MARKER}",
            flush=True,
        )
