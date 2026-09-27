# Pi turn settlement across a retry and a second prompt

*2026-09-27T00:44:14Z by Showboat 0.6.1*
<!-- showboat-id: 38d772ec-3d9b-4cc2-921b-f5dacacc6a19 -->

Run from the proposed Omnigent branch root with its dev environment and Pi >= 0.80.4 installed. This demo starts a local OpenAI-compatible provider, makes its first request return HTTP 503, then observes the real Pi RPC process through PiExecutor. It uses no external model credentials and asserts both turns complete on one Pi process with no unread frames.

```bash
set -e
PYTHONPATH=. .venv/bin/python - <<'PYCODE'
"""Exercise two Omnigent turns on one real Pi RPC process with a local provider."""

import asyncio
import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.inner.pi_executor import PiExecutor, _PiRpcSession, _PiSessionState


PI = shutil.which("pi")
assert PI is not None, "Install a Pi CLI that supports agent_settled"


class Provider(BaseHTTPRequestHandler):
    calls = 0
    responses = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        Provider.calls += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if Provider.calls == 1:
            body = b'{"error":{"message":"temporary failure","type":"server_error"}}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            Provider.responses.append("HTTP 503")
            return

        answer = "RETRY_RECOVERED" if Provider.calls == 2 else "SECOND_TURN_OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in [
            {"id": "local", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": None}]},
            {"id": "local", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        Provider.responses.append(answer)


async def main():
    from omnigent.harnesses.pi_native.main import pi_version

    assert (pi_version(PI) or (0, 0, 0)) >= (0, 80, 4)
    print("Pi supports agent_settled: yes")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    rpc = _PiRpcSession()
    try:
        with tempfile.TemporaryDirectory(prefix="pi-settlement-demo-") as directory:
            path = Path(directory)
            (path / "models.json").write_text(json.dumps({"providers": {"fake": {
                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "local-test",
                "api": "openai-completions", "models": [{"id": "local-test", "name": "Local Test",
                    "contextWindow": 8192, "maxTokens": 512, "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
            (path / "settings.json").write_text(json.dumps({"autoRetryEnabled": True, "maxRetries": 1, "retryDelayMs": 50}))
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": directory,
                   "PI_CODING_AGENT_DIR": directory, "PI_OFFLINE": "1"}
            if "TMPDIR" in os.environ:
                env["TMPDIR"] = os.environ["TMPDIR"]
            await rpc.start(PI, env=env, cwd=directory, model="fake/local-test",
                extra_args=["--no-tools", "--no-extensions", "--no-skills", "--no-context-files", "--approve"])
            assert rpc.process is not None
            pid = rpc.process.pid
            original_read_line = rpc.read_line
            trace = []

            async def traced_read_line(timeout=120):
                line = await original_read_line(timeout=timeout)
                if line:
                    event = json.loads(line)
                    kind = event.get("type")
                    if kind == "message_end" and event.get("message", {}).get("stopReason") == "error":
                        trace.append("message_end(error)")
                    elif kind == "agent_end":
                        trace.append(f"agent_end(willRetry={str(event.get('willRetry', False)).lower()})")
                    elif kind in {"auto_retry_start", "auto_retry_end", "agent_settled"}:
                        suffix = f"(success={str(event.get('success')).lower()})" if kind == "auto_retry_end" else ""
                        trace.append(f"{kind}{suffix}")
                return line

            rpc.read_line = traced_read_line
            executor = PiExecutor(pi_path=PI, model="fake/local-test")
            executor._session_states["__default__"] = _PiSessionState(rpc=rpc)
            executor._ensure_rpc = AsyncMock(return_value=rpc)
            for number, expected in [(1, "RETRY_RECOVERED"), (2, "SECOND_TURN_OK")]:
                before_calls = Provider.calls
                trace.clear()
                events = await asyncio.wait_for(
                    collect(executor.run_turn([{"role": "user", "content": f"Reply {expected}"}], [], "")),
                    timeout=40,
                )
                errors = [e.message for e in events if isinstance(e, ExecutorError)]
                text = "".join(e.text for e in events if isinstance(e, TextChunk))
                completes = [e.response for e in events if isinstance(e, TurnComplete)]
                print(f"\nOmnigent turn {number}")
                print("Provider responses:", " -> ".join(Provider.responses[before_calls:]))
                print("Pi events:", " -> ".join(trace))
                print("Omnigent streamed:", text)
                print("Omnigent completed:", completes)
                assert not errors, errors
                assert text == expected and completes == [expected]
                assert rpc.process.pid == pid and rpc.process.returncode is None
                assert rpc._line_queue.empty(), "Pi left unread RPC frames"
            assert Provider.calls == 3, Provider.calls
            print("\nSame Pi process for both turns; no unread frames; PASS", flush=True)
    finally:
        await rpc.close()
        server.shutdown()
        thread.join(timeout=2)


async def collect(iterator):
    return [event async for event in iterator]


if __name__ == "__main__":
    asyncio.run(main())

PYCODE

```

```output
Pi supports agent_settled: yes

Omnigent turn 1
Provider responses: HTTP 503 -> RETRY_RECOVERED
Pi events: message_end(error) -> agent_end(willRetry=true) -> auto_retry_start -> auto_retry_end(success=true) -> agent_end(willRetry=false) -> agent_settled
Omnigent streamed: RETRY_RECOVERED
Omnigent completed: ['RETRY_RECOVERED']

Omnigent turn 2
Provider responses: SECOND_TURN_OK
Pi events: agent_end(willRetry=false) -> agent_settled
Omnigent streamed: SECOND_TURN_OK
Omnigent completed: ['SECOND_TURN_OK']

Same Pi process for both turns; no unread frames; PASS
```
