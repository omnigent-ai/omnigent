"""Shared journey helpers for the opencode-native host e2e suites.

The live-provider suite (``test_host_opencode_native_e2e.py``) and the
scripted-provider suite (``test_host_opencode_native_mock_e2e.py``) both create
host-bound sessions, send turns through the events API, wait for the settled
transcript, record the web SSE stream and read the runner's bridge state.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.opencode_native.bridge import (
    OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
    read_bridge_state,
)
from omnigent.native.native_coding_agents import OPENCODE_NATIVE_AGENT_NAME
from tests.e2e.helpers import POLL_INTERVAL_S

TURN_TIMEOUT_S = 240.0
TERMINAL_TIMEOUT_S = 90.0
ASK_ON_OS_TOOLS = "omnigent.policies.builtins.safety.ask_on_os_tools"


def poll_for_terminal(
    client: httpx.Client, *, session_id: str, resource_id: str, timeout: float
) -> None:
    """Poll resources until the runner registers the opencode terminal."""
    deadline = time.monotonic() + timeout
    last: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/resources")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            last = [r.get("id") for r in data]
            if any(r.get("id") == resource_id and r.get("type") == "terminal" for r in data):
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Terminal {resource_id!r} never appeared for {session_id} within {timeout}s; saw {last!r}"
    )


def opencode_agent_id(client: httpx.Client) -> str:
    """Return the id of the seeded ``opencode-native-ui`` agent."""
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json()["data"] if a["name"] == OPENCODE_NATIVE_AGENT_NAME), None
    )
    assert agent_id is not None, "opencode-native-ui agent not seeded"
    return str(agent_id)


def session_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return every persisted item of *session_id* in position order."""
    resp = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 200, "order": "asc"})
    resp.raise_for_status()
    return list(resp.json().get("data", []))


def item_text(item: dict[str, Any]) -> str:
    """Concatenate an item's text blocks."""
    return "".join(
        blk["text"]
        for blk in item.get("content") or []
        if isinstance(blk, dict) and isinstance(blk.get("text"), str)
    )


def assistant_texts(items: list[dict[str, Any]]) -> list[str]:
    """Texts of the assistant message items, in order."""
    return [
        item_text(it)
        for it in items
        if it.get("type") == "message" and it.get("role") == "assistant"
    ]


def transcript(items: list[dict[str, Any]]) -> str:
    """Render items compactly for assertion messages."""
    lines = []
    for it in items:
        body = item_text(it) or str(it.get("output") or it.get("name") or "")
        lines.append(f"{it.get('type')}/{it.get('role') or '-'}: {body[:200]!r}")
    return "\n".join(lines)


def session(client: httpx.Client, session_id: str) -> dict[str, Any]:
    """Return the ``GET /v1/sessions/{id}`` snapshot."""
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return dict(resp.json())


def send(client: httpx.Client, session_id: str, text: str) -> None:
    """Post a user message event, as the web composer does."""
    client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    ).raise_for_status()


def wait_for_idle_after(
    client: httpx.Client,
    session_id: str,
    *,
    min_assistant: int,
    timeout: float = TURN_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Wait until *min_assistant* assistant items exist and the session stays idle."""
    deadline = time.monotonic() + timeout
    idle_since: float | None = None
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        items = session_items(client, session_id)
        snap = session(client, session_id)
        done = (
            len(assistant_texts(items)) >= min_assistant
            and snap.get("status") == "idle"
            and not snap.get("pending_elicitations")
        )
        if not done:
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= 1.5:
            return items
        time.sleep(0.5)
    raise AssertionError(
        f"turn for {session_id} did not finish within {timeout}s "
        f"(wanted {min_assistant} assistant items):\n{transcript(items)}"
    )


def turn(client: httpx.Client, session_id: str, text: str) -> list[dict[str, Any]]:
    """Send *text* and return the items once its assistant reply has settled."""
    before = len(assistant_texts(session_items(client, session_id)))
    send(client, session_id, text)
    return wait_for_idle_after(client, session_id, min_assistant=before + 1)


def bridge_dir(client: httpx.Client, session_id: str) -> Path:
    """Resolve a session's opencode bridge dir the way the runner does."""
    labels = session(client, session_id).get("labels") or {}
    return bridge_dir_for_bridge_id(labels.get(OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id)


def opencode_session_id(client: httpx.Client, session_id: str) -> str:
    """Return the OpenCode session id recorded in the bridge state."""
    state = read_bridge_state(bridge_dir(client, session_id))
    assert state is not None and state.opencode_session_id, f"no bridge state for {session_id}"
    return state.opencode_session_id


class SseRecorder:
    """Record ``(event, data, monotonic_ts)`` from a session's live SSE stream."""

    def __init__(self, live_server: str, session_id: str) -> None:
        self.events: list[tuple[str, dict[str, Any], float]] = []
        self._live_server = live_server
        self._session_id = session_id
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> SseRecorder:
        self._thread.start()
        # The stream's first frame is a ready-ack, so later events are not missed.
        assert self._connected.wait(timeout=30.0), "SSE stream never connected"
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        timeout = httpx.Timeout(10.0, read=2.0)
        with httpx.Client(base_url=self._live_server, timeout=timeout) as client:
            while not self._stop.is_set():
                try:
                    with client.stream("GET", f"/v1/sessions/{self._session_id}/stream") as resp:
                        self._consume(resp)
                except httpx.HTTPError:
                    continue

    def _consume(self, resp: httpx.Response) -> None:
        event_type = ""
        for line in resp.iter_lines():
            if self._stop.is_set():
                return
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
                self._connected.set()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[len("data:") :].strip())
                except ValueError:
                    data = {}
                self.events.append(
                    (event_type, data if isinstance(data, dict) else {}, time.monotonic())
                )
                self._connected.set()

    def of_type(self, event_type: str) -> list[tuple[str, dict[str, Any], float]]:
        """Recorded events of *event_type*."""
        return [e for e in list(self.events) if e[0] == event_type]

    def wait_for(self, event_type: str, *, count: int = 1, timeout: float) -> None:
        """Block until *count* events of *event_type* have been recorded."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.of_type(event_type)) >= count:
                return
            time.sleep(POLL_INTERVAL_S)
        seen = sorted({e[0] for e in list(self.events)})
        raise AssertionError(f"never saw {count}x {event_type!r} within {timeout}s; saw {seen}")


@dataclass
class OpenCodeHost:
    """A live host daemon plus the sessions a test created on it."""

    client: httpx.Client
    live_server: str
    host_id: str
    agent_id: str
    root: Path
    model: str
    created: list[str] = field(default_factory=list)

    def workspace(self, name: str) -> Path:
        """Create a fresh workspace dir with a known marker file."""
        path = self.root / f"{name}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True)
        (path / "omnigent_marker.txt").write_text("marker\n")
        return path

    def create_session(self, workspace: Path, *, model: str | None = None) -> str:
        """Create a host-bound session and wait for its OpenCode terminal.

        :param model: Model override; defaults to the host's :attr:`model`.
        """
        create = self.client.post(
            "/v1/sessions",
            json={
                "agent_id": self.agent_id,
                "host_id": self.host_id,
                "workspace": str(workspace),
                "model_override": model or self.model,
            },
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = str(create.json()["id"])
        self.created.append(session_id)
        poll_for_terminal(
            self.client,
            session_id=session_id,
            resource_id=terminal_resource_id("opencode", "main"),
            timeout=TERMINAL_TIMEOUT_S,
        )
        return session_id

    def cleanup(self) -> None:
        """Delete every tracked session while the runner can still reap its bridge dir."""
        for session_id in reversed(self.created):
            bridge = bridge_dir(self.client, session_id) if self._exists(session_id) else None
            self.client.delete(f"/v1/sessions/{session_id}", timeout=60.0)
            if bridge is not None:
                wait_gone(bridge, timeout=20.0)
        self.created.clear()

    def _exists(self, session_id: str) -> bool:
        return self.client.get(f"/v1/sessions/{session_id}").status_code == 200


def wait_gone(path: Path, *, timeout: float) -> None:
    """Wait for the runner to remove *path*; remove it ourselves as a fallback."""
    deadline = time.monotonic() + timeout
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    shutil.rmtree(path, ignore_errors=True)


def attach_ask_on_os_tools(client: httpx.Client, session_id: str) -> None:
    """Attach the built-in policy that ASKs before every file/shell tool call."""
    resp = client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "ask_on_os_tools", "type": "python", "handler": ASK_ON_OS_TOOLS},
    )
    assert resp.status_code == 200, f"policy attach failed: {resp.status_code} {resp.text[:300]}"


def outputs(items: list[dict[str, Any]]) -> list[str]:
    """Outputs of every function_call_output item."""
    return [
        str(it.get("output") or "") for it in items if it.get("type") == "function_call_output"
    ]
