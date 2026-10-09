"""Exercise a real external host and runner through a local Kubernetes rollout.

Only the model is mocked. The host, runner, filesystem RPCs, terminal, NGINX,
Postgres, and Kubernetes Deployment are real. Run ``run.sh up`` followed by
``run.sh verify``. Requires Linux host networking.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import re
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

ROOT = Path(__file__).resolve().parents[3]
KEY_HEADER = "X-Databricks-Omnigent-Slice-Key"


def start_mock_server(port, log):
    """Pass a bound listener to the mock so another process cannot claim its port."""
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.server.integration.mock_llm_server:app",
                "--fd",
                str(listener.fileno()),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        return process, listener.getsockname()[1]


async def command(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    output, _ = await process.communicate()
    text = output.decode()
    if process.returncode:
        raise RuntimeError(f"{args[0]} exited {process.returncode}: {text}")
    return text


async def eventually(check, *, timeout: float = 90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = await check()
            if result:
                return result
        except (httpx.HTTPError, OSError, WebSocketException) as exc:
            last = exc
        await asyncio.sleep(0.25)
    raise TimeoutError(f"Condition did not become true in {timeout}s; last error: {last}")


def agent_bundle(name: str, mock_url: str) -> bytes:
    connection = {"api_key": "mock-key", "base_url": f"{mock_url}/v1"}
    os_env = {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}
    spec = {
        "spec_version": 1,
        "name": name,
        "prompt": "You are a helpful assistant.",
        "executor": {
            "type": "omnigent",
            "model": "mock-prototype",
            "config": {"harness": "openai-agents"},
            "auth": {"type": "api_key", **connection},
            "connection": connection,
        },
        "os_env": os_env,
        "terminals": {"shell": {"command": "bash", "os_env": os_env}},
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = yaml.safe_dump(spec).encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


async def terminal_reply(ws, pattern: str) -> re.Match:
    output = ""
    async with asyncio.timeout(10):
        while True:
            frame = await ws.recv()
            output += frame.decode(errors="replace") if isinstance(frame, bytes) else frame
            match = re.search(pattern, output)
            if match:
                return match


async def verify(
    args,
    *,
    coordinated_rollout: Callable[[], Awaitable[float]] | None = None,
    all_hosts_checked: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    if urlsplit(args.url).hostname != "localhost":
        raise ValueError("This fixture only connects to the local prototype at localhost")
    args.output.mkdir(parents=True, exist_ok=True)
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        "kind-omnigent-nginx-prototype",
        "-n",
        "omnigent-prototype",
    ]
    host_id = args.host_id or uuid.uuid4().hex
    host_name = f"nginx-prototype-{host_id[:8]}"
    replicas = args.replicas
    container = f"omnigent-prototype-client-{host_id[:8]}"
    marker = uuid.uuid4().hex
    marker_file = f"marker-{host_id}.txt"
    report = {
        "host_id": host_id,
        "host_name": host_name,
        "container": container,
        "marker": marker,
        "replicas": replicas,
        "samples": [],
        "sse_connections": 0,
    }
    sse_text = []
    backfills = []
    tasks = []
    started = time.monotonic()
    mock_log = None
    mock = None
    client = None
    llm = None
    ws = None
    try:
        mock_log = (args.output / "mock.log").open("w")
        mock, mock_port = start_mock_server(args.mock_port, mock_log)
        mock_url = f"http://127.0.0.1:{mock_port}"
        client = httpx.AsyncClient(
            base_url=args.url,
            headers={"Origin": "omnigent://internal", KEY_HEADER: host_id},
            timeout=5,
            trust_env=False,
        )
        llm = httpx.AsyncClient(base_url=mock_url, timeout=5, trust_env=False)

        async def mock_ready():
            if mock.poll() is not None:
                raise RuntimeError(f"Mock server exited; see {args.output / 'mock.log'}")
            response = await llm.get("/stats")
            return response.is_success

        await eventually(mock_ready, timeout=15)

        async def ingress_ready():
            response = await client.get("/health")
            response.raise_for_status()
            return response

        health = await eventually(ingress_ready, timeout=30)
        report["initial_upstream"] = health.headers.get("x-omnigent-upstream")
        initial_pods = json.loads(
            await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json")
        )
        old_names = {pod["metadata"]["name"] for pod in initial_pods["items"]}
        report["initial_pods"] = sorted(old_names)
        if not (len(old_names) == replicas):
            raise RuntimeError(f"Expected {replicas} initial server pods")
        backends = set()
        for _ in range(128):
            response = await client.get("/health", headers={KEY_HEADER: uuid.uuid4().hex})
            response.raise_for_status()
            backends.add(response.headers["x-omnigent-upstream"])
            if len(backends) == replicas:
                break
        if not (len(backends) == replicas):
            raise RuntimeError("Host-key hashing did not reach every server pod")
        report["initial_backends"] = sorted(backends)
        await command(
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--network",
            "host",
            "-e",
            f"OMNIGENT_HOST_ID={host_id}",
            "-e",
            f"OMNIGENT_HOST_NAME={host_name}",
            "-e",
            "OMNIGENT_HOST_SLICE_KEY_ENABLED=1",
            "-e",
            "OMNIGENT_AUTH_ENABLED=0",
            "omnigent-prototype-client:local",
            "python",
            "-m",
            "omnigent",
            "host",
            "--server",
            args.url,
            "--non-interactive",
        )
        await command(
            "docker",
            "exec",
            container,
            "python",
            "-c",
            f"from pathlib import Path; Path('/workspace/{marker_file}').write_text('{marker}')",
        )
        host_path = f"/v1/hosts/{host_id}/filesystem/workspace"

        async def host_ready():
            response = await client.get(host_path)
            ready = response.is_success and marker_file in response.text
            if ready:
                report["initial_host_upstream"] = response.headers.get("x-omnigent-upstream")
            return ready

        await eventually(host_ready)
        response = await client.post(
            "/v1/sessions",
            data={"metadata": "{}"},
            files={
                "bundle": ("agent.tar.gz", agent_bundle(host_name, mock_url), "application/gzip")
            },
        )
        response.raise_for_status()
        listing = await client.get(
            "/v1/sessions", params={"agent_name": host_name, "limit": 1, "visibility": "all"}
        )
        listing.raise_for_status()
        agent_id = listing.json()["data"][0]["agent_id"]

        async def launch_session():
            response = await client.post(
                "/v1/sessions",
                json={
                    "agent_id": agent_id,
                    "host_id": host_id,
                    "host_type": "external",
                    "workspace": "/workspace",
                    "title": "NGINX Kubernetes rollout",
                },
            )
            response.raise_for_status()
            return response.json()["id"]

        session_id = await launch_session()
        report["session_id"] = session_id
        file_path = (
            f"/v1/sessions/{session_id}/resources/environments/default/filesystem/{marker_file}"
        )

        async def runner_ready():
            response = await client.get(file_path)
            ready = response.is_success and marker in response.text
            if ready:
                report["initial_runner_upstream"] = response.headers.get("x-omnigent-upstream")
            return ready

        await eventually(runner_ready)
        if not (
            report["initial_upstream"]
            == report["initial_host_upstream"]
            == report["initial_runner_upstream"]
        ):
            raise RuntimeError(
                "Host and runner requests reached different replicas before the rollout"
            )
        response = await client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={"terminal": "shell", "session_key": "rollout"},
        )
        response.raise_for_status()
        terminal_id = response.json()["id"]
        ws_url = (
            args.url.replace("http://", "ws://")
            + f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach"
            + f"?omnigent_slice_key={host_id}"
        )
        ws = await connect(ws_url, origin=args.url)
        await ws.send(
            (
                f"export PROTOTYPE_MARKER={marker}; "
                'printf \'before:%s:%s\\n\' "$$" "$PROTOTYPE_MARKER"\n'
            ).encode()
        )
        before = await terminal_reply(ws, rf"before:(\d+):{marker}")
        report["terminal_pid_before"] = before.group(1)
        print(
            f"Host and runner connected; shell PID {before.group(1)}. Starting gated turn.",
            flush=True,
        )

        stream_ready = asyncio.Event()

        async def stream():
            while True:
                try:
                    async with client.stream(
                        "GET", f"/v1/sessions/{session_id}/stream", timeout=None
                    ) as response:
                        response.raise_for_status()
                        report["sse_connections"] += 1
                        first_line = True
                        async for line in response.aiter_lines():
                            sse_text.append(line)
                            if first_line and line:
                                first_line = False
                                # The stream is live-only. Like the browser, fetch
                                # persisted items after subscribing to cover the gap.
                                snapshot = await client.get(f"/v1/sessions/{session_id}/items")
                                snapshot.raise_for_status()
                                backfills.append(snapshot.text)
                                stream_ready.set()
                except httpx.HTTPError:
                    pass
                finally:
                    stream_ready.clear()
                await asyncio.sleep(0.2)

        tasks.append(asyncio.create_task(stream()))
        await asyncio.wait_for(stream_ready.wait(), 10)
        expected = f"turn-survived-{marker}"
        response = await llm.post(
            "/mock/configure",
            json={
                "key": "mock-prototype",
                "responses": [{"text": expected, "block": True, "stream": True}],
            },
        )
        response.raise_for_status()
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Wait through the rollout."}],
                },
            },
        )
        response.raise_for_status()

        async def gated():
            return (await llm.get("/gate/pending")).json()["pending"]

        await eventually(gated, timeout=30)

        async def sample(kind, path):
            while True:
                at = round(time.monotonic() - started, 3)
                try:
                    response = await client.get(path, timeout=2)
                    status = response.status_code
                    upstream = response.headers.get("x-omnigent-upstream")
                    expected_content = marker_file if kind == "host" else marker
                    if status == 200 and expected_content not in response.text:
                        status = "wrong_content"
                except httpx.HTTPError:
                    status, upstream = "connection_error", None
                report["samples"].append(
                    {
                        "at": at,
                        "kind": kind,
                        "status": status,
                        "upstream": upstream,
                        "latency_seconds": round(time.monotonic() - started - at, 3),
                    }
                )
                await asyncio.sleep(0.25)

        tasks.extend(
            [
                asyncio.create_task(sample("host", host_path)),
                asyncio.create_task(sample("runner", file_path)),
            ]
        )
        report["ready_for_rollout_at"] = round(time.monotonic() - started, 3)
        if coordinated_rollout is None:
            rollout_started = time.monotonic()
            print(f"Replacing {replicas} server pods with work active...", flush=True)
            await command(*kube, "rollout", "restart", "deployment/omnigent")
            print(
                await command(*kube, "rollout", "status", "deployment/omnigent", "--timeout=180s"),
                flush=True,
            )
        else:
            rollout_started = await coordinated_rollout()
        report["rollout_started_at"] = round(rollout_started - started, 3)

        async def replaced():
            pods = json.loads(
                await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json")
            )
            names = {pod["metadata"]["name"] for pod in pods["items"]}
            if old_names.isdisjoint(names) and len(names) == replicas:
                report["final_pods"] = sorted(names)
                return True
            return False

        await eventually(replaced, timeout=40)
        settled_after = time.monotonic() - started

        async def stable():
            for kind in ("host", "runner"):
                samples = [
                    sample
                    for sample in report["samples"]
                    if sample["kind"] == kind and sample["at"] > settled_after
                ]
                if len(samples) < 20 or any(s["status"] != 200 for s in samples[-20:]):
                    return False
            return True

        # Endpoint updates and reconnects can outlast `rollout status`.
        await eventually(stable, timeout=60)
        report["recovered_at"] = round(time.monotonic() - started, 3)

        async def old_terminal_closed():
            return ws.close_code is not None

        await eventually(old_terminal_closed, timeout=15)
        report["old_terminal_connection_closed"] = True

        async def reattach_terminal():
            nonlocal ws
            await ws.close()
            ws = await connect(ws_url, origin=args.url)
            report["final_upstream"] = ws.response.headers.get("x-omnigent-upstream")
            await ws.send(b'printf \'after:%s:%s\\n\' "$$" "$PROTOTYPE_MARKER"\n')
            return await terminal_reply(ws, rf"after:(\d+):{marker}")

        after = await eventually(reattach_terminal, timeout=30)
        report["terminal_pid_after"] = after.group(1)
        if not (before.group(1) == after.group(1)):
            raise RuntimeError("Shell process was replaced")
        response = await llm.post("/gate/release")
        if not (response.json()["released"]):
            raise RuntimeError("Agent's blocked model request did not survive")

        async def turn_completed():
            snapshot = await client.get(f"/v1/sessions/{session_id}")
            items = await client.get(f"/v1/sessions/{session_id}/items")
            return snapshot.json().get("status") == "idle" and expected in items.text

        await eventually(turn_completed, timeout=45)
        report["active_turn_completed"] = True

        async def turn_recovered():
            return any(expected in body for body in [*sse_text, *backfills])

        await eventually(turn_recovered, timeout=10)
        report["active_turn_recovery"] = (
            "live_stream" if any(expected in line for line in sse_text) else "history_backfill"
        )
        new_session = await launch_session()

        async def new_runner_ready():
            response = await client.get(file_path.replace(session_id, new_session))
            return response.is_success and marker in response.text

        await eventually(new_runner_ready)
        report["new_session_after_rollout"] = new_session
        settled_after = time.monotonic() - started
        await eventually(stable, timeout=30)
        # Also prove that the recovered stream delivers a new turn live.
        await asyncio.wait_for(stream_ready.wait(), 10)
        followup = f"post-rollout-{marker}"
        response = await llm.post(
            "/mock/configure",
            json={
                "key": "mock-prototype",
                "responses": [{"text": followup, "stream": True}],
            },
        )
        response.raise_for_status()
        followup_started = time.monotonic()
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "Continue."}]},
            },
            timeout=30,
        )
        report["followup_post_seconds"] = round(time.monotonic() - followup_started, 3)
        response.raise_for_status()

        async def followup_streamed():
            return any(followup in line for line in sse_text)

        report["followup_turn_streamed"] = await eventually(followup_streamed, timeout=30)

        async def followup_completed():
            snapshot = await client.get(f"/v1/sessions/{session_id}")
            items = await client.get(f"/v1/sessions/{session_id}/items")
            return snapshot.json().get("status") == "idle" and followup in items.text

        report["followup_turn_completed"] = await eventually(followup_completed, timeout=30)
        if any(s["status"] == "wrong_content" for s in report["samples"]):
            raise RuntimeError("A successful file request returned another host's content")
        if all_hosts_checked is not None:
            await all_hosts_checked()
        report["passed"] = True
        print(
            "PASS: host/runner RPCs recovered, the same shell survived, "
            "the active turn completed, and a new runner launched.",
            flush=True,
        )
        return report
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if ws is not None:
            await ws.close()
        if client is not None:
            await client.aclose()
        if llm is not None:
            await llm.aclose()
        if mock is not None:
            mock.terminate()
            try:
                await asyncio.to_thread(mock.wait, timeout=5)
            except subprocess.TimeoutExpired:
                mock.kill()
                await asyncio.to_thread(mock.wait, timeout=5)
        if mock_log is not None:
            mock_log.close()
        for kind in ("host", "runner"):
            samples = [s for s in report["samples"] if s["kind"] == kind]
            report[f"{kind}_statuses"] = dict(Counter(str(s["status"]) for s in samples))
            longest = 0
            failed_at = None
            for sample in samples:
                if sample["status"] != 200 and failed_at is None:
                    failed_at = sample["at"]
                elif sample["status"] == 200 and failed_at is not None:
                    longest = max(longest, sample["at"] - failed_at)
                    failed_at = None
            report[f"{kind}_longest_observed_outage_seconds"] = round(longest, 3)
            report[f"{kind}_still_failing_at_end"] = failed_at is not None
        for name, content in (
            ("report.json", json.dumps(report, indent=2) + "\n"),
            ("sse.log", "\n".join(sse_text)),
            ("backfills.json", json.dumps(backfills, indent=2) + "\n"),
        ):
            try:
                (args.output / name).write_text(content)
            except OSError as exc:
                print(f"Could not save {name}: {exc}", file=sys.stderr, flush=True)
        with contextlib.suppress(RuntimeError, OSError):
            (args.output / "host.log").write_text(await command("docker", "logs", container))
        with contextlib.suppress(RuntimeError, OSError):
            await command(
                "docker",
                "cp",
                f"{container}:/root/.omnigent/logs",
                str(args.output / "client-logs"),
            )
        with contextlib.suppress(RuntimeError, OSError):
            await command("docker", "rm", "-f", container)
        with contextlib.suppress(RuntimeError, OSError):
            (args.output / "nginx.log").write_text(
                await command(*kube, "logs", "deployment/nginx", "--since=15m")
            )
        print(f"Evidence: {args.output / 'report.json'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--url", default="http://localhost:18081")
    parser.add_argument("--mock-port", type=int, default=18082)
    parser.add_argument("--replicas", type=int, default=2)
    parser.add_argument("--host-id")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(verify(parser.parse_args()))
