"""Real replicas and a transport gate for the failures found during an NGINX rollout.

The proxy forwards application responses unchanged. Faults close connections or
hold real bytes; they never manufacture wrong_replica, receipts, or model output.
Only the external model is scripted. Both client-API and UI journeys use this lab.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml
from starlette.types import Receive, Scope, Send
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tests._helpers.live_server import find_free_port, local_server_env, terminate_process
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.resilience.lab.proxy import LoopThread

ROOT = Path(__file__).resolve().parents[2]
KEY_HEADER = "X-Databricks-Omnigent-Slice-Key"
_HOP_HEADERS = {b"connection", b"transfer-encoding", b"keep-alive", b"upgrade"}


def eventually(check: Callable[[], Any], what: str, timeout: float = 30) -> Any:
    """Wait for observable state; propagate setup and assertion failures."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {what}")


def sse_events(body: str) -> list[dict[str, Any]]:
    """Decode complete JSON events without changing their contents."""
    result = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            with contextlib.suppress(ValueError):
                result.append(json.loads(line[6:]))
    return result


class HandoffProxy:
    """HTTP/WS relay with explicit gates at the two sides of a server handoff."""

    def __init__(self, upstream: str, evidence: Path) -> None:
        self.target = upstream
        self.host_routes: dict[str, str] = {}
        self.evidence = evidence
        self.port = find_free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.loop = LoopThread("replica-handoff")
        self.records: list[dict[str, Any]] = []
        self.tunnels: dict[str, tuple[Send, Any]] = {}
        self.streams: set[asyncio.Task[Any]] = set()
        self.probes: dict[str, tuple[asyncio.Future[dict[str, Any]], bytearray]] = {}
        self.drop_forward = False
        self.drop_status: str | None = None
        self.refuse_tunnels = False
        self.loop.start()
        self.loop.run(self._start())

    async def _start(self) -> None:
        self.client = httpx.AsyncClient(trust_env=False, timeout=60)
        self.gates = {
            name: asyncio.Event() for name in ("history", "runner_stream", "browser", "updates")
        }
        for gate in self.gates.values():
            gate.set()
        self.server = uvicorn.Server(
            uvicorn.Config(
                self, host="127.0.0.1", port=self.port, lifespan="off", log_level="error"
            )
        )
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.task.done():
                await self.task
            await asyncio.sleep(0.01)

    def note(self, kind: str, **fields: Any) -> None:
        self.records.append({"at": time.monotonic(), "kind": kind, **fields})

    def gate(self, name: str, *, hold: bool) -> None:
        self.loop.loop.call_soon_threadsafe(
            self.gates[name].clear if hold else self.gates[name].set
        )

    def seen(self, kind: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record["kind"] == kind]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:
            await self._http(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        path = scope["path"]
        query = scope["query_string"].decode()
        headers = [(k, v) for k, v in scope["headers"] if k not in _HOP_HEADERS]
        host_key = dict(headers).get(KEY_HEADER.lower().encode(), b"").decode()
        target = self.host_routes.get(host_key, self.target)
        request = self.client.build_request(
            scope["method"], f"{target}{path}?{query}", headers=headers, content=bytes(body)
        )
        response = await self.client.send(request, stream=True)
        streaming = "text/event-stream" in response.headers.get("content-type", "")
        task = asyncio.current_task()
        if streaming and task is not None:
            self.streams.add(task)
        response_headers = [
            (k, v) for k, v in response.headers.raw if k.lower() not in _HOP_HEADERS
        ]
        try:
            if streaming:
                await send(
                    {
                        "type": "http.response.start",
                        "status": response.status_code,
                        "headers": response_headers,
                    }
                )
                self.note("browser_stream", target=target, path=path)
                async for chunk in response.aiter_raw():
                    await self.gates["browser"].wait()
                    self.note(
                        "browser_events",
                        target=target,
                        path=path,
                        events=sse_events(chunk.decode(errors="replace")),
                    )
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b""})
                return
            data = await response.aread()
            if path.endswith("/items"):
                self.note("history_read", body=json.loads(data), target=target)
                await self.gates["history"].wait()
            is_message = (
                scope["method"] == "POST"
                and path.endswith("/events")
                and (json.loads(body).get("type") == "message")
            )
            if is_message:
                self.note(
                    "message_response",
                    status=response.status_code,
                    body=json.loads(data),
                    request=json.loads(body),
                    target=target,
                    host_key=request.headers.get(KEY_HEADER),
                )
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": response_headers,
                }
            )
            await send({"type": "http.response.body", "body": data})
            if path.endswith("/items"):
                self.note("history_delivered", target=target)
        except asyncio.CancelledError:
            if streaming:
                # Finish the real SSE response so the downstream proxy forwards
                # EOF immediately instead of leaving a half-open browser socket.
                await send({"type": "http.response.body", "body": b""})
            else:
                raise
        finally:
            await response.aclose()
            if task is not None:
                self.streams.discard(task)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        path = scope["path"]
        is_tunnel = path.endswith("/tunnel")
        if is_tunnel and self.refuse_tunnels:
            await send({"type": "websocket.close", "code": 1013})
            self.note("refused_tunnel", path=path)
            return
        target = self.target
        headers = [
            (k.decode(), v.decode())
            for k, v in scope["headers"]
            if k not in _HOP_HEADERS and k != b"host" and not k.startswith(b"sec-websocket-")
        ]
        url = f"{target.replace('http:', 'ws:')}{path}?{scope['query_string'].decode()}"
        async with connect(url, additional_headers=headers, max_size=None) as upstream:
            await send({"type": "websocket.accept"})
            if is_tunnel:
                self.tunnels[path] = (send, upstream)
                self.note("tunnel", path=path, target=target)
            requests: dict[str, dict[str, Any]] = {}

            async def to_runner() -> None:
                async for raw in upstream:
                    if path.endswith("/updates"):
                        await self.gates["updates"].wait()
                    if isinstance(raw, str) and "/runners/" in path:
                        frame = json.loads(raw)
                        if frame.get("kind") == "request":
                            requests[frame["id"]] = frame
                            self.note(
                                "runner_request",
                                target=target,
                                path=frame["path"],
                                method=frame["method"],
                            )
                            if frame["path"].endswith("/stream"):
                                await self.gates["runner_stream"].wait()
                            if (
                                self.drop_forward
                                and frame["path"].endswith("/events")
                                and (
                                    json.loads(frame.get("body") or "{}").get("type") == "message"
                                )
                            ):
                                self.drop_forward = False
                                self.refuse_tunnels = True
                                self.note("lost_forward", body=json.loads(frame["body"]))
                                await send({"type": "websocket.close", "code": 1012})
                                return
                    await send(
                        {"type": "websocket.send", "text": raw}
                        if isinstance(raw, str)
                        else {"type": "websocket.send", "bytes": raw}
                    )

            async def to_server() -> None:
                while True:
                    message = await receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    raw = message.get("text") or message.get("bytes")
                    if isinstance(raw, str) and "/runners/" in path:
                        frame = json.loads(raw)
                        probe = self.probes.get(frame.get("id"))
                        if probe is not None:
                            future, probe_body = probe
                            if frame["kind"] == "response.body":
                                probe_body.extend(frame["body"].encode())
                            elif frame["kind"] == "response.end":
                                future.set_result(json.loads(probe_body))
                            continue
                        if frame.get("kind") == "response.body":
                            events = sse_events(frame["body"])
                            if events:
                                self.note("runner_events", target=target, events=events)
                            if any(
                                e.get("type") == "session.status"
                                and e.get("status") == self.drop_status
                                for e in events
                            ):
                                self.drop_status = None
                                self.refuse_tunnels = True
                                self.note("lost_status", events=events)
                                await send({"type": "websocket.close", "code": 1012})
                                return
                        if frame.get("kind") == "response.end":
                            request = requests.pop(frame["id"], None)
                            if request is not None:
                                self.note(
                                    "runner_response",
                                    target=target,
                                    path=request["path"],
                                    method=request["method"],
                                )
                    await upstream.send(raw)

            tasks = [asyncio.create_task(to_runner()), asyncio.create_task(to_server())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    with contextlib.suppress(ConnectionClosed):
                        finished.result()
            finally:
                for child in tasks:
                    child.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if is_tunnel and self.tunnels.get(path, (None,))[0] is send:
                    self.tunnels.pop(path, None)
                with contextlib.suppress(RuntimeError, OSError):
                    await send({"type": "websocket.close", "code": 1012})

    async def _cut(self, *, tunnels: bool, browser: bool) -> None:
        if tunnels:
            for send, upstream in list(self.tunnels.values()):
                with contextlib.suppress(RuntimeError, OSError):
                    await send({"type": "websocket.close", "code": 1012})
                await upstream.close()
        if browser:
            for task in list(self.streams):
                task.cancel()

    def cut(self, *, tunnels: bool = True, browser: bool = True) -> None:
        self.loop.run(self._cut(tunnels=tunnels, browser=browser))

    async def _probe(self, session_id: str) -> dict[str, Any]:
        send = next(send for path, (send, _) in self.tunnels.items() if "/runners/" in path)
        key = f"probe-{uuid.uuid4().hex}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.probes[key] = (future, bytearray())
        try:
            await send(
                {
                    "type": "websocket.send",
                    "text": json.dumps(
                        {
                            "kind": "request",
                            "id": key,
                            "method": "GET",
                            "path": f"/v1/sessions/{session_id}",
                        }
                    ),
                }
            )
            return await asyncio.wait_for(future, 10)
        finally:
            self.probes.pop(key, None)

    def runner_snapshot(self, session_id: str) -> dict[str, Any]:
        """Read the runner's real session endpoint through its existing tunnel."""
        return self.loop.run(self._probe(session_id))

    async def _close(self) -> None:
        for gate in self.gates.values():
            gate.set()
        self.refuse_tunnels = True
        await self._cut(tunnels=True, browser=True)
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, 10)
        finally:
            await self.client.aclose()

    def close(self) -> None:
        try:
            self.loop.run(self._close())
        finally:
            self.loop.stop()
            self.evidence.write_text(json.dumps(self.records, indent=2) + "\n")


class HandoffLab:
    """Two real servers, a host, one SDK runner, and the checkout's web client."""

    def __init__(self, root: Path, model_url: str, resources: ExitStack) -> None:
        self.root = root
        self.model_url = model_url
        httpx.post(f"{model_url}/mock/reset", timeout=5, trust_env=False).raise_for_status()
        self.model = f"handoff-{uuid.uuid4().hex[:10]}"
        self.host_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        self.host_env = env = {
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": f"{model_url}/v1",
            "OMNIGENT_AUTH_ENABLED": "0",
            "OMNIGENT_SKIP_WEB_UI": "true",
        }
        self.a = resources.enter_context(
            server_runner(
                root / "a",
                server_env=env,
                binding_token=token,
                database_uri=f"sqlite:///{root / 'shared.db'}",
                artifact_location=root / "artifacts",
                server_cwd=ROOT,
            )
        )
        self.b = resources.enter_context(
            server_runner(
                root / "b",
                server_env=env,
                binding_token=token,
                database_uri=self.a.database_uri,
                artifact_location=self.a.artifact_location,
                server_cwd=ROOT,
            )
        )
        self.proxy = HandoffProxy(self.a.base_url, root / "network.json")
        resources.callback(self.proxy.close)
        self.client = resources.enter_context(
            httpx.Client(
                base_url=self.proxy.url,
                headers={"Origin": "omnigent://internal", KEY_HEADER: self.host_id},
                trust_env=False,
                timeout=30,
            )
        )
        self.a.start_host(
            server_url=self.proxy.url,
            cwd=ROOT,
            env={
                **env,
                "OMNIGENT_HOST_ID": self.host_id,
                "OMNIGENT_HOST_SLICE_KEY_ENABLED": "1",
                "OMNIGENT_HOST_NAME": "rollout-test-host",
            },
        )
        eventually(
            lambda: any(self.host_id in path for path in self.proxy.tunnels), "host tunnel", 60
        )
        eventually(
            lambda: self.client.get(f"/v1/hosts/{self.host_id}").status_code == 200,
            "host registration in shared storage",
            30,
        )
        config = {
            "name": self.model,
            "prompt": "Reply to the user's message.",
            "executor": {
                "harness": "openai-agents",
                "model": self.model,
                "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{model_url}/v1"},
            },
        }
        response = post_session_bundle(
            self.client.post,
            "/v1/sessions",
            bundle_files({"agent.yaml": yaml.safe_dump(config).encode()}),
            metadata={
                "host_id": self.host_id,
                "workspace": str(self.a.workspace),
                "title": "Rolling update regression",
            },
        )
        assert response.is_success, response.text
        self.session_id = response.json()["session_id"]
        self.agent_id = self.snapshot()["agent_id"]
        self.configure([{"text": "Ready for the rolling update."}])
        self.post("Confirm this session is ready.").raise_for_status()
        eventually(lambda: self.messages("assistant"), "warm-up reply", 60)
        eventually(lambda: self.snapshot()["status"] == "idle", "warm-up idle", 30)
        self.baseline_calls = len(self.model_requests())
        self.baseline_items = len(self.items())
        self.ui_url = self._frontend(resources)

    def move_to_new_host(self) -> str:
        """Move the session through the same bind/launch API used by another client."""
        host_id = uuid.uuid4().hex
        self.b.start_host(
            cwd=ROOT,
            env={
                **self.host_env,
                "OMNIGENT_HOST_ID": host_id,
                "OMNIGENT_HOST_SLICE_KEY_ENABLED": "1",
                "OMNIGENT_HOST_NAME": "replacement-test-host",
            },
        )
        eventually(
            lambda: self.client.get(f"{self.b.base_url}/v1/hosts/{host_id}").status_code == 200,
            "replacement host registration",
            60,
        )
        self.client.patch(
            f"/v1/sessions/{self.session_id}", json={"runner_id": ""}
        ).raise_for_status()
        response = self.client.post(
            f"{self.b.base_url}/v1/hosts/{host_id}/runners",
            json={"session_id": self.session_id, "workspace": str(self.b.workspace)},
        )
        assert response.is_success, response.text
        self.proxy.host_routes[host_id] = self.b.base_url
        assert self.snapshot()["host_id"] == host_id
        self.proxy.note("moved_host", before=self.host_id, after=host_id)
        return host_id

    def _frontend(self, resources: ExitStack) -> str:
        port = find_free_port()
        env = local_server_env(
            {"OMNIGENT_URL": self.proxy.url, "VITE_OMNIGENT_HOST_ROUTING": "true"}
        )
        for name in ("VITE_DATABRICKS_WORKSPACE", "OMNIGENT_AUTH_TOKEN"):
            env.pop(name, None)
        log = resources.enter_context((self.root / "web.log").open("wb"))
        proc = subprocess.Popen(
            [
                "pnpm",
                "--dir",
                "web",
                "exec",
                "vite",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--strictPort",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        resources.callback(terminate_process, proc)
        url = f"http://127.0.0.1:{port}"

        def ready() -> bool:
            assert proc.poll() is None, (self.root / "web.log").read_text()
            try:
                return httpx.get(url, timeout=1, trust_env=False).status_code == 200
            except httpx.HTTPError:
                return False

        eventually(ready, "isolated Vite server", 30)
        return url

    def configure(self, replies: list[dict[str, Any]]) -> None:
        httpx.post(
            f"{self.model_url}/mock/configure",
            json={"key": self.model, "responses": replies},
            timeout=5,
            trust_env=False,
        ).raise_for_status()

    def model_requests(self) -> list[dict[str, Any]]:
        return httpx.get(
            f"{self.model_url}/mock/requests",
            params={"key": self.model},
            timeout=5,
            trust_env=False,
        ).json()["requests"]

    def model_paused(self) -> bool:
        return httpx.get(f"{self.model_url}/gate/pending", timeout=5, trust_env=False).json()[
            "pending"
        ]

    def release_model(self) -> None:
        response = httpx.post(f"{self.model_url}/gate/release", timeout=5, trust_env=False)
        response.raise_for_status()
        assert response.json()["released"], "model did not reach its gate"

    def post(self, text: str) -> httpx.Response:
        return self.client.post(
            f"/v1/sessions/{self.session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
        )

    def items(self) -> list[dict[str, Any]]:
        response = self.client.get(
            f"{self.proxy.target}/v1/sessions/{self.session_id}/items",
            params={"limit": 100, "order": "asc"},
        )
        response.raise_for_status()
        return response.json()["data"]

    def messages(self, role: str) -> list[str]:
        return [
            block.get("text", "")
            for item in self.items()
            if item.get("role") == role
            for block in item.get("content", [])
            if block.get("type") in ("input_text", "output_text")
        ]

    def snapshot(self) -> dict[str, Any]:
        response = self.client.get(f"{self.proxy.target}/v1/sessions/{self.session_id}")
        response.raise_for_status()
        return response.json()

    def handoff(self) -> None:
        self.proxy.target = self.b.base_url
        self.proxy.cut()
        self.proxy.refuse_tunnels = False
        eventually(
            lambda: any(
                record["target"] == self.b.base_url and "/runners/" in record["path"]
                for record in self.proxy.seen("tunnel")
            ),
            "runner on replacement",
            30,
        )

    def save(self) -> None:
        for name, value in (
            ("session", self.snapshot()),
            ("items", self.items()),
            ("model-requests", self.model_requests()),
        ):
            (self.root / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")


@contextmanager
def handoff_lab(root: Path, model_url: str) -> Iterator[HandoffLab]:
    with ExitStack() as resources:
        lab = HandoffLab(root, model_url, resources)
        try:
            yield lab
        finally:
            for name in ("history", "runner_stream", "browser", "updates"):
                lab.proxy.gate(name, hold=False)
            lab.proxy.refuse_tunnels = False
            with contextlib.suppress(httpx.HTTPError):
                lab.save()
