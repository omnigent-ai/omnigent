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
    assert "600s harness idle watchdog" not in output

    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert any(
        request.get("method") == "turn/interrupt"
        and request.get("params") == {"threadId": "thread-1", "turnId": "turn-1"}
        for request in requests
    )
