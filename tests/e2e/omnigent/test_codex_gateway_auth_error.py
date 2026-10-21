"""E2E coverage for Codex gateway authentication failures."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path


def test_codex_gateway_auth_failure_exits_with_actionable_error(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
) -> None:
    """A terminal gateway 401 fails promptly and interrupts the Codex turn."""
    request_log = tmp_path / "codex-requests.jsonl"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        textwrap.dedent(
            f"""\
            #!{omnigent_python}
            import json
            import sys

            if "--version" in sys.argv:
                print("codex-cli 0.136.0")
                raise SystemExit(0)

            if len(sys.argv) < 2 or sys.argv[1] != "app-server":
                raise SystemExit(2)

            request_log = {str(request_log)!r}
            for line in sys.stdin:
                request = json.loads(line)
                with open(request_log, "a", encoding="utf-8") as log:
                    log.write(json.dumps(request) + "\\n")

                method = request["method"]
                if method == "thread/start":
                    result = {{"thread": {{"id": "thread-1"}}}}
                elif method == "turn/start":
                    result = {{"turn": {{"id": "turn-1"}}}}
                else:
                    result = {{}}

                print(json.dumps({{"id": request["id"], "result": result}}), flush=True)
                if method == "turn/start":
                    print("ERROR: Reconnecting... 5/5", file=sys.stderr, flush=True)
                    print(
                        "ERROR: unexpected status 401 Unauthorized: {{}}, "
                        "url: https://example.test/ai-gateway/codex/v1/responses",
                        file=sys.stderr,
                        flush=True,
                    )
            """
        ),
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    env = {
        "CODEX_HOME": str(codex_home),
        "FAKE_CODEX_PATH": str(fake_codex),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(omnigent_repo_root),
        "TEST_REPO_ROOT": str(omnigent_repo_root),
    }

    driver = textwrap.dedent(
        """
        import asyncio
        import json
        import os

        from omnigent.inner.codex_executor import CodexExecutor
        from omnigent.inner.executor import ExecutorError


        async def main():
            executor = CodexExecutor(
                cwd=os.environ["TEST_REPO_ROOT"],
                model="databricks-gpt-5",
                codex_path=os.environ["FAKE_CODEX_PATH"],
                enable_web_search=False,
            )
            try:
                events = [
                    event
                    async for event in executor.run_turn(
                        [{"role": "user", "content": "hello", "session_id": "e2e"}],
                        [],
                        "Be helpful.",
                    )
                ]
            finally:
                await executor.close()

            errors = [event for event in events if isinstance(event, ExecutorError)]
            print(json.dumps([event.message for event in errors]))
            return 1 if errors else 0


        raise SystemExit(asyncio.run(main()))
        """
    )
    result = subprocess.run(
        [str(omnigent_python), "-c", driver],
        env=env,
        cwd=omnigent_repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    assert "gateway returned 401 Unauthorized" in output
    assert "databricks-gpt-5" in output
    assert "https://example.test/ai-gateway/codex/v1/responses" in output
    assert "auth likely expired/misconfigured" in output
    assert "wedged LLM" not in output
    assert "harness idle watchdog" not in output

    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert any(
        request.get("method") == "turn/interrupt"
        and request.get("params") == {"threadId": "thread-1", "turnId": "turn-1"}
        for request in requests
    )


def test_codex_launcher_certificate_failure_fails_fast_with_actionable_error(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
) -> None:
    """An expired certificate named by the launcher fails the turn as soon as Codex reconnects.

    The scripted ``codex`` prints the launcher's certificate line to stderr at
    start, as the Databricks wrapper does, then answers ``turn/start`` with the
    ``error``/``willRetry`` reconnect notification codex-cli 0.154 emits for a
    failed TLS handshake and keeps running without a terminal turn event.
    """
    request_log = tmp_path / "codex-requests.jsonl"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        textwrap.dedent(
            f"""\
            #!{omnigent_python}
            import json
            import sys

            print(
                "Failed to fetch safe flags from proxy: [SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] "
                "ssl/tls alert certificate expired (_ssl.c:2580)",
                file=sys.stderr,
                flush=True,
            )
            if "--version" in sys.argv:
                print("codex-cli 0.154.0")
                raise SystemExit(0)

            if len(sys.argv) < 2 or sys.argv[1] != "app-server":
                raise SystemExit(2)

            request_log = {str(request_log)!r}
            for line in sys.stdin:
                request = json.loads(line)
                with open(request_log, "a", encoding="utf-8") as log:
                    log.write(json.dumps(request) + "\\n")

                if "id" not in request:
                    continue
                method = request.get("method")
                if method == "thread/start":
                    result = {{"thread": {{"id": "thread-1"}}}}
                elif method == "turn/start":
                    result = {{"turn": {{"id": "turn-1"}}}}
                else:
                    result = {{}}

                print(json.dumps({{"id": request["id"], "result": result}}), flush=True)
                if method == "turn/start":
                    reconnect = {{
                        "method": "error",
                        "params": {{
                            "error": {{
                                "message": "Reconnecting... waiting for network",
                                "codexErrorInfo": {{
                                    "responseStreamDisconnected": {{"httpStatusCode": None}}
                                }},
                                "additionalDetails": "Connection failed: error sending request",
                            }},
                            "willRetry": True,
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                        }},
                    }}
                    print(json.dumps(reconnect), flush=True)
            """
        ),
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    env = {
        "CODEX_HOME": str(codex_home),
        "FAKE_CODEX_PATH": str(fake_codex),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(omnigent_repo_root),
        "TEST_REPO_ROOT": str(omnigent_repo_root),
    }

    driver = textwrap.dedent(
        """
        import asyncio
        import json
        import os

        from omnigent.inner.codex_executor import CodexExecutor
        from omnigent.inner.executor import ExecutorError


        async def main():
            executor = CodexExecutor(
                cwd=os.environ["TEST_REPO_ROOT"],
                model="databricks-gpt-5",
                codex_path=os.environ["FAKE_CODEX_PATH"],
                enable_web_search=False,
            )
            try:
                events = [
                    event
                    async for event in executor.run_turn(
                        [{"role": "user", "content": "hello", "session_id": "e2e"}],
                        [],
                        "Be helpful.",
                    )
                ]
            finally:
                await executor.close()

            errors = [event for event in events if isinstance(event, ExecutorError)]
            print(
                json.dumps(
                    [
                        {
                            "message": event.message,
                            "code": event.code,
                            "remediation": event.remediation,
                            "retryable": event.retryable,
                        }
                        for event in errors
                    ]
                )
            )
            return 1 if errors else 0


        raise SystemExit(asyncio.run(main()))
        """
    )
    # The timeout bounds the fast-fail path; without it the turn hangs until the idle watchdog.
    result = subprocess.run(
        [str(omnigent_python), "-c", driver],
        env=env,
        cwd=omnigent_repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, output
    stdout_lines = result.stdout.strip().splitlines()
    assert stdout_lines, output
    errors = json.loads(stdout_lines[-1])
    assert len(errors) == 1, output
    error = errors[0]
    assert "could not connect to its model endpoint for databricks-gpt-5" in error["message"]
    assert "the TLS certificate has expired" in error["message"]
    assert "SSLV3_ALERT_CERTIFICATE_EXPIRED" in error["message"]
    assert error["code"] == "model_endpoint_certificate_rejected"
    assert "run dbcert" in error["remediation"]
    assert error["retryable"] is False
    assert "harness idle watchdog" not in output
    assert "wedged LLM" not in output

    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert any(
        request.get("method") == "turn/interrupt"
        and request.get("params") == {"threadId": "thread-1", "turnId": "turn-1"}
        for request in requests
    )
