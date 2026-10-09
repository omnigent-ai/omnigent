"""Record six real browser conversations during one local Kubernetes rollout.

Setup creates external hosts and sessions through the API. Every chat message
is then typed and sent in Chromium. Browser requests, routing, and reconnects
are left to the application. Only model responses are mocked.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import subprocess
import sys
import time
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect
from verify import KEY_HEADER, ROOT, agent_bundle, command, eventually
from verify_multi import mock_ports, ready

OBSERVE_UI = """() => {
  const events = [];
  window.__rolloutUI = events;
  let previous = '';
  const selectors = [
    ['error', '[data-testid="error-pill"]'],
    ['error', '[role="alert"]'],
    ['error', '[data-sonner-toast][data-type="error"]'],
    ['error', '[data-testid$="-error"]'],
    ['notice', '[data-testid="stream-interruption-notice"]'],
    ['reconnecting', '[data-testid="error-reconnecting"]'],
  ];
  const scan = () => {
    const visible = [];
    for (const [kind, selector] of selectors) {
      for (const element of document.querySelectorAll(selector)) {
        const box = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        if (!box.width || !box.height || style.visibility === 'hidden' ||
            style.display === 'none' || Number(style.opacity) === 0) continue;
        visible.push({kind, selector, text: (element.innerText || '').trim()});
      }
    }
    const replyCounts = new Map();
    for (const element of document.querySelectorAll(
      '[data-testid="message-bubble"][data-role="assistant"]'
    )) {
      for (const token of element.innerText.match(/HOST[0-9]+-TURN[0-9]+ confirmed/g) || []) {
        replyCounts.set(token, (replyCounts.get(token) || 0) + 1);
      }
    }
    for (const [text, count] of replyCounts) {
      if (count > 1) visible.push({kind: 'duplicate', text, count});
    }
    const signature = JSON.stringify(visible);
    if (signature !== previous) {
      events.push({at_epoch_ms: Date.now(), visible});
      previous = signature;
    }
  };
  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; scan(); });
  }).observe(document.documentElement, {
    subtree: true, childList: true, attributes: true, characterData: true,
  });
  setInterval(scan, 100);
  scan();
}"""


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


class BrowserHost:
    def __init__(self, args, index, host_id, upstream, pod, port, started):
        self.args = args
        self.index = index
        self.host_id = host_id
        self.output = args.output / f"host-{index}"
        self.output.mkdir()
        self.container = f"omnigent-browser-client-{host_id[:8]}"
        self.name = f"nginx-browser-host-{index}-{host_id[:8]}"
        self.mock_url = f"http://127.0.0.1:{port}"
        self.port = port
        self.started = started
        self.mock = None
        self.mock_log = None
        self.context = None
        self.page = None
        self.session_id = None
        self.monitor_task = None
        self.response_tasks = []
        self.streams = {}
        self.client = httpx.AsyncClient(
            base_url=args.url,
            headers={"Origin": "omnigent://internal", KEY_HEADER: host_id},
            timeout=5,
            trust_env=False,
        )
        self.llm = httpx.AsyncClient(base_url=self.mock_url, timeout=5, trust_env=False)
        self.report = {
            "host": index,
            "host_id": host_id,
            "host_name": self.name,
            "initial_upstream": upstream,
            "initial_pod": pod,
            "turns": [],
            "responses": [],
            "message_requests": [],
            "request_failures": [],
            "console": [],
            "page_errors": [],
            "ui_observations": [],
            "cleanup_errors": [],
            "passed": False,
            "visible_issue_counts": {},
            "http_error_counts": {},
        }

    def at(self):
        return round(time.monotonic() - self.started, 3)

    async def start(self, browser):
        self.mock_log = (self.output / "mock.log").open("w")
        self.mock = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/server/integration/mock_llm_server.py"),
                str(self.port),
            ],
            stdout=self.mock_log,
            stderr=subprocess.STDOUT,
        )

        async def mock_ready():
            if self.mock.poll() is not None:
                raise RuntimeError(f"Mock server exited; see {self.output / 'mock.log'}")
            return (await self.llm.get("/stats")).is_success

        await eventually(mock_ready, timeout=15)
        await command(
            "docker",
            "run",
            "-d",
            "--name",
            self.container,
            "--network",
            "host",
            "-e",
            f"OMNIGENT_HOST_ID={self.host_id}",
            "-e",
            f"OMNIGENT_HOST_NAME={self.name}",
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
            self.args.url,
            "--non-interactive",
        )
        marker = f"browser-host-{self.index}-{self.host_id}"
        await command(
            "docker",
            "exec",
            self.container,
            "python",
            "-c",
            f"from pathlib import Path; Path('/workspace/marker.txt').write_text('{marker}')",
        )

        async def host_ready():
            response = await self.client.get(f"/v1/hosts/{self.host_id}/filesystem/workspace")
            return response.is_success and "marker.txt" in response.text

        await eventually(host_ready)
        response = await self.client.post(
            "/v1/sessions",
            data={"metadata": "{}"},
            files={
                "bundle": (
                    "agent.tar.gz",
                    agent_bundle(self.name, self.mock_url),
                    "application/gzip",
                )
            },
        )
        response.raise_for_status()
        listing = await self.client.get(
            "/v1/sessions", params={"agent_name": self.name, "limit": 1, "visibility": "all"}
        )
        listing.raise_for_status()
        agent_id = listing.json()["data"][0]["agent_id"]
        response = await self.client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": self.host_id,
                "host_type": "external",
                "workspace": "/workspace",
                "title": f"Rolling update — Host {self.index}",
            },
        )
        response.raise_for_status()
        self.session_id = response.json()["id"]
        self.report["session_id"] = self.session_id

        async def runner_ready():
            response = await self.client.get(
                f"/v1/sessions/{self.session_id}/resources/environments/default/filesystem/marker.txt"
            )
            if response.is_success and marker in response.text:
                self.report["confirmed_initial_upstream"] = response.headers.get(
                    "x-omnigent-upstream"
                )
                return True
            return False

        await eventually(runner_ready)
        if not (self.report["confirmed_initial_upstream"] == self.report["initial_upstream"]):
            raise RuntimeError("Host routing changed before the rollout began")
        self.context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(self.output / "raw-video"),
            record_video_size={"width": 1440, "height": 900},
            color_scheme="light",
        )
        if not self.args.capture_streams:
            await self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.report["page_created_at"] = self.at()
        self.report["page_created_epoch_ms"] = time.time() * 1000
        self.page = await self.context.new_page()
        if self.args.capture_streams:
            self.cdp = await self.context.new_cdp_session(self.page)
            await self.cdp.send("Network.enable")
            self.cdp.on("Network.responseReceived", self.observe_stream)
            self.cdp.on("Network.dataReceived", self.observe_stream_chunk)
        self.page.set_default_timeout(15_000)
        self.page.on("request", self.observe_request)
        self.page.on("response", self.observe_response)
        self.page.on(
            "requestfailed",
            lambda request: self.report["request_failures"].append(
                {
                    "at": self.at(),
                    "method": request.method,
                    "url": request.url,
                    "failure": request.failure,
                }
            ),
        )
        self.page.on(
            "pageerror",
            lambda error: self.report["page_errors"].append(
                {
                    "at": self.at(),
                    "error": str(error),
                }
            ),
        )
        self.page.on(
            "console",
            lambda message: (
                self.report["console"].append(
                    {
                        "at": self.at(),
                        "type": message.type,
                        "text": message.text,
                        "location": message.location,
                    }
                )
                if message.type in {"error", "warning"}
                else None
            ),
        )
        await self.page.goto(f"{self.args.url}/c/{self.session_id}", wait_until="domcontentloaded")
        await self.page.get_by_label("Message the agent").wait_for(state="visible")
        await self.page.evaluate(OBSERVE_UI)
        self.monitor_task = asyncio.create_task(self.observe_ui())
        # Collapse only the navigation sidebar, using the app's own control.
        sidebar = self.page.get_by_role("button", name="Collapse sidebar", exact=True)
        if await sidebar.count():
            await sidebar.click()
        await self.send_turn(1, "before_rollout")
        if not (self.report["turns"][-1].get("reply_visible")):
            raise RuntimeError(self.report["turns"][-1])
        await self.page.screenshot(path=str(self.output / "before-rollout.png"))
        self.report["ready_at"] = self.at()
        print(
            f"Host {self.index}: browser warm-up turn completed on {self.report['initial_pod']}",
            flush=True,
        )

    async def observe_stream(self, event):
        response = event["response"]
        if not urlsplit(response["url"]).path.endswith("/stream"):
            return
        if response["status"] != 200:
            return
        request_id = event["requestId"]
        stream = {"at": self.at(), "url": response["url"], "chunks": []}
        self.streams[request_id] = stream
        try:
            buffered = await self.cdp.send(
                "Network.streamResourceContent", {"requestId": request_id}
            )
            stream["chunks"].insert(0, {"at": self.at(), "data": buffered["bufferedData"]})
        except PlaywrightError as exc:
            stream["capture_error"] = str(exc)

    def observe_stream_chunk(self, event):
        stream = self.streams.get(event["requestId"])
        if stream is not None and event.get("data"):
            stream["chunks"].append({"at": self.at(), "data": event["data"]})

    def observe_request(self, request):
        if request.method == "POST" and urlsplit(request.url).path.endswith("/events"):
            with contextlib.suppress(Exception):
                event = request.post_data_json
                if event.get("type") == "message":
                    self.report["message_requests"].append(
                        {
                            "at": self.at(),
                            "event": event,
                            "host_key": request.headers.get(KEY_HEADER.lower()),
                        }
                    )

    def observe_response(self, response):
        self.response_tasks.append(asyncio.create_task(self.record_response(response)))

    async def record_response(self, response):
        request = response.request
        path = urlsplit(response.url).path
        entry = {
            "at": self.at(),
            "path": path,
            "method": request.method,
            "status": response.status,
            "host_key": request.headers.get(KEY_HEADER.lower()),
            "upstream": response.headers.get("x-omnigent-upstream"),
        }
        if request.method == "POST" and path.endswith("/events"):
            with contextlib.suppress(Exception):
                entry["event"] = request.post_data_json
        if response.status >= 400:
            with contextlib.suppress(Exception):
                entry["body"] = (await asyncio.wait_for(response.text(), 3))[:3000]
        self.report["responses"].append(entry)

    async def observe_ui(self):
        captured = False
        while True:
            events = await self.page.evaluate("window.__rolloutUI || []")
            self.report["ui_observations"] = events
            if not captured and any(event["visible"] for event in events):
                await self.page.screenshot(path=str(self.output / "first-visible-issue.png"))
                captured = True
                print(
                    f"Host {self.index}: browser displayed a notice; evidence captured", flush=True
                )
            await asyncio.sleep(0.2)

    async def send_turn(self, number, phase):
        token = f"HOST{self.index}-TURN{number:03d}"
        prompt = f"{token}: Confirm this conversation is responding."
        reply = (
            f"{token} confirmed. Reply received on host {self.index}. "
            "The conversation is responding."
        )
        entry = {
            "number": number,
            "phase": phase,
            "prompt": prompt,
            "reply": reply,
            "started_at": self.at(),
        }
        self.report["turns"].append(entry)
        response = await self.llm.post(
            "/mock/configure",
            json={
                "key": token,
                "match": token,
                "required_tools": [],
                "responses": [{"text": reply, "stream": True, "chunk_delay": 0.05, "delay": 0.2}],
            },
        )
        response.raise_for_status()
        try:
            composer = self.page.get_by_label("Message the agent")
            await expect(composer).to_be_editable(timeout=20_000)
            await composer.fill(prompt)
            entry["clicked_at"] = self.at()
            await self.page.get_by_role("button", name="Send", exact=True).click(timeout=20_000)
            entry["click_completed_at"] = self.at()
            assistant = self.page.get_by_test_id("assistant-text-section").filter(has_text=reply)
            await expect(assistant).to_be_visible(timeout=35_000)
            entry["reply_visible"] = True
            entry["reply_visible_at"] = self.at()
            await self.page.get_by_placeholder("Send a message…", exact=True).wait_for(
                state="visible", timeout=20_000
            )
            entry["composer_ready_at"] = self.at()
            print(f"Host {self.index}: turn {number} completed ({phase})", flush=True)
        except (PlaywrightError, AssertionError, TimeoutError) as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            await self.page.screenshot(path=str(self.output / f"turn-{number:03d}-failed.png"))
            print(
                f"Host {self.index}: turn {number} did not complete: {type(exc).__name__}",
                flush=True,
            )
        entry["finished_at"] = self.at()

    async def drive(self, rolled_out, summary):
        number = 2
        post_rollout = 0
        while not rolled_out.is_set() or post_rollout < 2:
            phase = "after_rollout" if rolled_out.is_set() else "during_rollout"
            await self.send_turn(number, phase)
            if phase == "after_rollout":
                post_rollout += 1
            number += 1
            if number > 60 or self.at() - summary["rollout_started_at"] > 200:
                break
            await asyncio.sleep(0.3)
        await asyncio.sleep(3)

    async def finish_evidence(self):
        save(
            self.output / "streams.json",
            [
                {
                    **{key: value for key, value in stream.items() if key != "chunks"},
                    "text": b"".join(
                        base64.b64decode(chunk["data"]) for chunk in stream["chunks"]
                    ).decode("utf-8", errors="replace"),
                }
                for stream in self.streams.values()
            ],
        )
        if self.page is not None:
            with contextlib.suppress(Exception):
                self.report["ui_observations"] = await self.page.evaluate(
                    "window.__rolloutUI || []"
                )
                self.report["final_body"] = await self.page.locator("body").inner_text()
                self.report["final_bubbles"] = await self.page.get_by_test_id(
                    "message-bubble"
                ).evaluate_all(
                    "elements => elements.map(e => ({role: e.dataset.role, text: e.innerText}))"
                )
                await self.page.screenshot(path=str(self.output / "after-rollout.png"))
                # Walk the virtualized transcript: offscreen rows are absent from the DOM.
                bubbles = {}
                scroller = self.page.locator("[data-virtualized-transcript]").first
                await scroller.evaluate("el => el.scrollTo({top: 0, behavior: 'instant'})")
                for _ in range(200):
                    await asyncio.sleep(0.1)
                    mounted = await self.page.get_by_test_id("message-bubble").evaluate_all(
                        """elements => elements.map(e => ({
                            key: e.closest('[data-bubble-key]').dataset.bubbleKey,
                            index: Number(e.closest('[data-index]').dataset.index),
                            role: e.dataset.role, text: e.innerText,
                        }))"""
                    )
                    bubbles.update({bubble["key"]: bubble for bubble in mounted})
                    bottom = await scroller.evaluate(
                        """el => {
                            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 2) return true;
                            el.scrollBy({
                                top: Math.max(200, el.clientHeight * 0.7), behavior: 'instant'
                            });
                            return false;
                        }"""
                    )
                    if bottom:
                        break
                self.report["rendered_transcript"] = sorted(
                    bubbles.values(), key=lambda bubble: bubble["index"]
                )
                for turn in self.report["turns"]:
                    for role, field in [("user", "prompt"), ("assistant", "reply")]:
                        turn[f"rendered_{role}_count"] = sum(
                            bubble["text"].count(turn[field])
                            for bubble in bubbles.values()
                            if bubble["role"] == role
                        )
        if self.session_id:
            for suffix, name in [("", "session-after.json"), ("/items", "items-after.json")]:
                try:
                    response = await self.client.get(f"/v1/sessions/{self.session_id}{suffix}")
                    response.raise_for_status()
                    save(self.output / name, response.json())
                    if suffix == "":
                        self.report["final_session_status"] = response.json().get("status")
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    self.report["api_crosscheck_error"] = str(exc)
            if (self.output / "items-after.json").exists():
                items = json.loads((self.output / "items-after.json").read_text())["data"]
                for turn in self.report["turns"]:
                    for role, field in [("user", "prompt"), ("assistant", "reply")]:
                        count = sum(
                            item.get("type") == "message"
                            and item.get("role") == role
                            and any(
                                turn[field] == block.get("text")
                                for block in item.get("content", [])
                            )
                            for item in items
                        )
                        turn[f"saved_{role}_count"] = count
                self.report["saved_error_items"] = [
                    item for item in items if item.get("type") == "error"
                ]
                expected_text = {
                    role: {turn[field] for turn in self.report["turns"]}
                    for role, field in [("user", "prompt"), ("assistant", "reply")]
                }
                self.report["unexpected_saved_messages"] = [
                    item
                    for item in items
                    if item.get("type") == "message"
                    and item.get("role") in expected_text
                    and "".join(block.get("text", "") for block in item.get("content", []))
                    not in expected_text[item["role"]]
                ]
        if self.mock is not None:
            with contextlib.suppress(Exception):
                response = await self.llm.get("/mock/requests")
                recorded = response.json()
                save(self.output / "model-requests.json", recorded)
                model_counts = Counter()
                for request in recorded["requests"]:
                    inputs = request.get("input", request.get("messages", []))
                    users = [item for item in inputs if item.get("role") == "user"]
                    if users and isinstance(users[-1].get("content"), str):
                        model_counts[users[-1]["content"]] += 1
                for turn in self.report["turns"]:
                    turn["model_request_count"] = model_counts[turn["prompt"]]

    async def cleanup(self):
        if self.monitor_task:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        await asyncio.gather(*self.response_tasks, return_exceptions=True)
        if self.context:
            try:
                if not self.args.capture_streams:
                    await self.context.tracing.stop(path=str(self.output / "trace.zip"))
            except (PlaywrightError, OSError) as exc:
                self.report["cleanup_errors"].append(str(exc))
            try:
                video = self.page.video if self.page else None
                await self.context.close()
                if video:
                    await video.save_as(str(self.output / "browser.webm"))
                    self.report["video"] = str(self.output / "browser.webm")
            except (PlaywrightError, OSError) as exc:
                self.report["cleanup_errors"].append(str(exc))
        for args, destination in [
            (["docker", "logs", self.container], self.output / "host.log"),
            (
                [
                    "docker",
                    "cp",
                    f"{self.container}:/root/.omnigent/logs",
                    str(self.output / "client-logs"),
                ],
                None,
            ),
            (["docker", "rm", "-f", self.container], None),
        ]:
            try:
                output = await command(*args)
                if destination:
                    destination.write_text(output)
            except (RuntimeError, OSError) as exc:
                self.report["cleanup_errors"].append(str(exc))
        if self.mock:
            self.mock.terminate()
            try:
                await asyncio.to_thread(self.mock.wait, timeout=5)
            except subprocess.TimeoutExpired:
                self.mock.kill()
                await asyncio.to_thread(self.mock.wait, timeout=5)
        if self.mock_log:
            self.mock_log.close()
        await self.client.aclose()
        await self.llm.aclose()
        visible = [issue for event in self.report["ui_observations"] for issue in event["visible"]]
        self.report["visible_issue_counts"] = dict(Counter(issue["kind"] for issue in visible))
        self.report["http_error_counts"] = dict(
            Counter(
                str(response["status"])
                for response in self.report["responses"]
                if response["status"] >= 400
            )
        )
        self.report["passed"] = bool(
            self.report["turns"]
            and all(
                turn.get("reply_visible")
                and not turn.get("error")
                and turn.get("saved_user_count") == 1
                and turn.get("saved_assistant_count") == 1
                and turn.get("rendered_user_count") == 1
                and turn.get("rendered_assistant_count") == 1
                and turn.get("model_request_count") == 1
                for turn in self.report["turns"]
            )
            and not any(issue["kind"] in {"error", "duplicate"} for issue in visible)
            and not self.report["page_errors"]
            and not self.report.get("saved_error_items")
            and not self.report.get("unexpected_saved_messages")
            and self.report.get("final_session_status") == "idle"
            and not self.report.get("api_crosscheck_error")
            and not self.report["cleanup_errors"]
            and not self.report.get("driver_error")
            and all(
                request["host_key"] == self.host_id for request in self.report["message_requests"]
            )
        )
        save(self.output / "report.json", self.report)


async def run(args):
    if urlsplit(args.url).hostname != "localhost" or args.replicas != 3 or args.hosts != 6:
        raise ValueError("This local recording uses three replicas and six hosts")
    args.output.mkdir(parents=True, exist_ok=False)
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        "kind-omnigent-nginx-prototype",
        "-n",
        "omnigent-prototype",
    ]
    started = time.monotonic()
    summary = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "started_epoch_ms": time.time() * 1000,
        "replicas": args.replicas,
        "hosts_requested": args.hosts,
        "passed": False,
        "rollouts_triggered": 0,
        "pod_samples": [],
        "feature_maps": [
            "feature-map/composer.md: in-session composer",
            "feature-map/sessions.md: reconnect and message recovery",
        ],
    }
    (args.output / "verify_browser.py").write_text(Path(__file__).read_text())
    (args.output / "source-revision.txt").write_text(await command("git", "rev-parse", "HEAD"))
    (args.output / "source-working-tree.patch").write_text(await command("git", "diff"))
    summary["client_image_id"] = (
        await command(
            "docker", "image", "inspect", "omnigent-prototype-client:local", "--format", "{{.Id}}"
        )
    ).strip()
    deployment = json.loads(await command(*kube, "get", "deployment/omnigent", "-o", "json"))
    save(args.output / "deployment-before.json", deployment)
    if not (deployment["spec"]["replicas"] == args.replicas):
        raise RuntimeError("Expected three replicas; run the scale command in the README")
    await command(*kube, "rollout", "status", "deployment/omnigent", "--timeout=180s")
    initial = json.loads(await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json"))
    if not (len(initial["items"]) == 3 and all(ready(pod) for pod in initial["items"])):
        raise RuntimeError("Expected three ready server pods before the rollout")
    summary["initial_pods"] = [pod["metadata"]["name"] for pod in initial["items"]]
    by_upstream = {
        f"{pod['status']['podIP']}:8000": pod["metadata"]["name"] for pod in initial["items"]
    }
    groups = {upstream: [] for upstream in by_upstream}
    async with httpx.AsyncClient(base_url=args.url, timeout=5, trust_env=False) as probe:
        for _ in range(512):
            host_id = uuid.uuid4().hex
            response = await probe.get("/health", headers={KEY_HEADER: host_id})
            response.raise_for_status()
            upstream = response.headers["x-omnigent-upstream"]
            if upstream in groups and len(groups[upstream]) < 2:
                groups[upstream].append(host_id)
            if all(len(group) == 2 for group in groups.values()):
                break
    if not (all(len(group) == 2 for group in groups.values())):
        raise RuntimeError("Could not find two host IDs for every server pod")
    summary["initial_distribution"] = {
        by_upstream[upstream]: ids for upstream, ids in groups.items()
    }
    ports = mock_ports(6)
    hosts = []
    for upstream, host_ids in sorted(groups.items()):
        for host_id in host_ids:
            index = len(hosts) + 1
            hosts.append(
                BrowserHost(
                    args,
                    index,
                    host_id,
                    upstream,
                    by_upstream[upstream],
                    ports[index - 1],
                    started,
                )
            )
    logs = {}

    async def monitor_pods():
        while True:
            snapshot = json.loads(
                await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json")
            )
            pods = []
            for pod in snapshot["items"]:
                name = pod["metadata"]["name"]
                pods.append(
                    {
                        "name": name,
                        "ready": ready(pod),
                        "terminating": bool(pod["metadata"].get("deletionTimestamp")),
                        "ip": pod["status"].get("podIP"),
                    }
                )
                if ready(pod) and name not in logs:
                    handle = (args.output / f"{name}.log").open("w")
                    process = await asyncio.create_subprocess_exec(
                        *kube,
                        "logs",
                        name,
                        "--timestamps",
                        "--follow",
                        "--since=10s",
                        stdout=handle,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    logs[name] = (process, handle)
            summary["pod_samples"].append(
                {"at": round(time.monotonic() - started, 3), "pods": pods}
            )
            await asyncio.sleep(0.5)

    monitor = asyncio.create_task(monitor_pods())
    drivers = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
        )
        summary["chromium_version"] = browser.version
        try:
            async with asyncio.timeout(150), asyncio.TaskGroup() as group:
                for host in hosts:
                    group.create_task(host.start(browser))
            summary["browsers_ready_at_rollout"] = 6
            summary["rollout_started_at"] = round(time.monotonic() - started, 3)
            summary["rollout_started_epoch_ms"] = time.time() * 1000
            rolled_out = asyncio.Event()
            drivers = [asyncio.create_task(host.drive(rolled_out, summary)) for host in hosts]
            print(
                "All six browsers ready. Starting one rolling update with turns continuing.",
                flush=True,
            )
            await command(*kube, "rollout", "restart", "deployment/omnigent")
            summary["rollouts_triggered"] = 1
            result = await command(
                *kube, "rollout", "status", "deployment/omnigent", "--timeout=180s"
            )
            (args.output / "rollout.log").write_text(result)
            summary["deployment_available_at"] = round(time.monotonic() - started, 3)
            # Deployment availability can precede the last old pod's shutdown.
            async with asyncio.timeout(120):
                while True:
                    current = json.loads(
                        await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json")
                    )["items"]
                    if (
                        len(current) == args.replicas
                        and all(ready(pod) for pod in current)
                        and set(summary["initial_pods"]).isdisjoint(
                            pod["metadata"]["name"] for pod in current
                        )
                    ):
                        break
                    await asyncio.sleep(0.5)
            summary["rollout_complete_at"] = round(time.monotonic() - started, 3)
            summary["rollout_complete_epoch_ms"] = time.time() * 1000
            rolled_out.set()
            print(
                "Kubernetes rollout complete; browsers are finishing follow-up turns.", flush=True
            )
            outcomes = await asyncio.wait_for(
                asyncio.gather(*drivers, return_exceptions=True), timeout=120
            )
            for host, outcome in zip(hosts, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    host.report["driver_error"] = f"{type(outcome).__name__}: {outcome}"
            final = json.loads(
                await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json")
            )
            summary["final_pods"] = [pod["metadata"]["name"] for pod in final["items"]]
            if not (len(final["items"]) == 3 and all(ready(pod) for pod in final["items"])):
                raise RuntimeError("Expected three ready replacement pods")
            if not (set(summary["initial_pods"]).isdisjoint(summary["final_pods"])):
                raise RuntimeError("An original server pod remains after the rollout")
            summary["rollout_verified"] = True
        except Exception:
            summary["error"] = traceback.format_exc()
            raise
        finally:
            for driver in drivers:
                if not driver.done():
                    driver.cancel()
            await asyncio.gather(*drivers, return_exceptions=True)
            evidence = await asyncio.gather(
                *(host.finish_evidence() for host in hosts), return_exceptions=True
            )
            for host, result in zip(hosts, evidence, strict=True):
                if isinstance(result, BaseException):
                    host.report["api_crosscheck_error"] = f"{type(result).__name__}: {result}"
            cleanup = await asyncio.gather(
                *(host.cleanup() for host in hosts), return_exceptions=True
            )
            for host, result in zip(hosts, cleanup, strict=True):
                if isinstance(result, BaseException):
                    host.report["cleanup_errors"].append(f"{type(result).__name__}: {result}")
                    host.report["passed"] = False
            summary["cleanup_errors"] = []
            try:
                await browser.close()
            except PlaywrightError as exc:
                summary["cleanup_errors"].append(str(exc))
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            for process, handle in logs.values():
                try:
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 5)
                        except TimeoutError:
                            with contextlib.suppress(ProcessLookupError):
                                process.kill()
                            await process.wait()
                finally:
                    handle.close()
            with contextlib.suppress(RuntimeError):
                (args.output / "nginx.log").write_text(
                    await command(*kube, "logs", "deployment/nginx", "--timestamps", "--since=10m")
                )
            summary["hosts"] = []
            for host in hosts:
                during = {
                    json.dumps(request["event"], sort_keys=True)
                    for request in host.report["message_requests"]
                    if summary.get("rollout_started_at", float("inf"))
                    <= request["at"]
                    <= summary.get("rollout_complete_at", 0)
                }
                summary["hosts"].append(
                    {
                        "host": host.index,
                        "host_id": host.host_id,
                        "session_id": host.session_id,
                        "passed": host.report["passed"],
                        "turns": len(host.report["turns"]),
                        "turns_sent_during_rollout": len(during),
                        "turns_completed": sum(
                            bool(turn.get("reply_visible")) for turn in host.report["turns"]
                        ),
                        "visible_issue_counts": host.report["visible_issue_counts"],
                        "page_errors": host.report["page_errors"],
                        "http_error_counts": host.report["http_error_counts"],
                        "cleanup_errors": host.report["cleanup_errors"],
                        "video": host.report.get("video"),
                    }
                )
            summary["passed"] = bool(
                summary.get("rollout_verified")
                and not summary["cleanup_errors"]
                and all(
                    host["passed"] and host["turns_sent_during_rollout"] >= 2
                    for host in summary["hosts"]
                )
            )
            summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
            save(args.output / "summary.json", summary)
            print(
                json.dumps(
                    {key: value for key, value in summary.items() if key != "pod_samples"},
                    indent=2,
                ),
                flush=True,
            )
            print(f"Recordings and evidence: {args.output}", flush=True)
    return summary["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--url", default="http://localhost:18081")
    parser.add_argument("--replicas", type=int, default=3)
    parser.add_argument("--hosts", type=int, default=6)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--capture-streams",
        action="store_true",
        help="Capture raw SSE through CDP instead of a Playwright trace (diagnostics only)",
    )
    sys.exit(0 if asyncio.run(run(parser.parse_args())) else 1)
