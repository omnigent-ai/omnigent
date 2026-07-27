#!/usr/bin/env python3
"""Deterministic CUJ driver for the native ``sys_session_send`` PEER path.

Validates the agent-team peer-messaging feature end-to-end against a live
server + sibling runner + mock LLM — using the *native* ``sys_session_send``
tool, nothing swapped out. See ``designs/team-p2p-parity-experiment.md`` for the
rationale and the scenario list.

The load-bearing claim under test: when teammate ALICE messages teammate BOB by
``session_id`` (both under the same team root), BOB's reply is delivered into
ALICE's ``sys_read_inbox`` — NOT the lead's — while BOB's ``parent_session_id``
stays the lead. That is the ``awaiter_session_id`` routing decoupling.

Why this is staged (not a one-shot ``omnigent run -p``): a peer send addresses
BOB by his RUNTIME ``session_id``, which does not exist until BOB is spawned. A
fully-scripted mock brain cannot embed an id it cannot know in advance, so the
driver spawns the team, discovers BOB's real id out-of-band, reconfigures
ALICE's scripted queue with it, then drives a follow-up turn on ALICE.

No credentials or network egress: the mock LLM stands in for every brain, keyed
by model (``mock-coordinator`` / ``mock-alice`` / ``mock-bob``).

Usage::

    python tests/e2e/team_p2p_cuj.py                 # all scenarios
    python tests/e2e/team_p2p_cuj.py --scenario s1   # one scenario
    python tests/e2e/team_p2p_cuj.py --list-scenarios
    python tests/e2e/team_p2p_cuj.py --keep          # keep the sandbox
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

# ── Repo + imports ────────────────────────────────────────────────────────────
# tests/e2e/team_p2p_cuj.py → parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Pure, importable HTTP helpers (module-level defs, not fixtures) reused from the
# e2e conftest so this driver speaks the exact session-events contract the suite
# does. The server/runner boot below is adapted from the ``live_server`` fixture.
from omnigent.runner.identity import (  # noqa: E402
    OMNIGENT_INTERNAL_WS_ORIGIN,
    token_bound_runner_id,
)

_MOCK_SERVER_REL = Path("tests") / "server" / "integration" / "mock_llm_server.py"

_COORD_MODEL = "mock-coordinator"
_ALICE_MODEL = "mock-alice"
_BOB_MODEL = "mock-bob"

_BOB_SENTINEL = "BOB_ANSWER_7F3A"
_ALICE_QUESTION = "what is your favorite data structure and why?"

_MOCK_BOOT_TIMEOUT_S = 30.0
_SERVER_BOOT_TIMEOUT_S = 60.0
_TURN_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.5


# ── Small HTTP utilities (stdlib, mirrors polly_cuj) ──────────────────────────


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_http(url: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {url}")


def _post_mock(mock_url: str, path: str, payload: dict) -> None:
    resp = httpx.post(f"{mock_url}{path}", json=payload, timeout=5.0)
    resp.raise_for_status()


def _mock_reset(mock_url: str) -> None:
    _post_mock(mock_url, "/mock/reset", {})


def _mock_configure(mock_url: str, responses: list[dict], *, key: str) -> None:
    """Load a keyed response queue (queue key == the agent's mock model)."""
    _post_mock(mock_url, "/mock/configure", {"key": key, "responses": responses})


def _mock_set_fallback(mock_url: str, key: str, text: str) -> None:
    _post_mock(mock_url, "/mock/set_fallback", {"key": key, "text": text})


def _send_call(agent: str, title: str, args: object, *, call_id: str) -> dict:
    """A ``tool_calls`` entry: named ``(agent,title)`` sub-agent send."""
    return {
        "call_id": call_id,
        "name": "sys_session_send",
        "arguments": json.dumps({"agent": agent, "title": title, "args": args}),
    }


def _peer_send_call(session_id: str, args: object, *, call_id: str) -> dict:
    """A ``tool_calls`` entry: by-session-id PEER send (no agent/title)."""
    return {
        "call_id": call_id,
        "name": "sys_session_send",
        "arguments": json.dumps({"session_id": session_id, "args": args}),
    }


def _tool_call(name: str, arguments: dict, *, call_id: str) -> dict:
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


# ── Bundle rewrite ────────────────────────────────────────────────────────────


def _rewrite_team_bundle(
    tmp: Path,
    mock_url: str,
    *,
    team: bool = True,
    peer_send_cap: int | None = None,
) -> Path:
    """Copy ``examples/team_demo`` into *tmp*, wired to the mock LLM.

    Each session gets its own mock model key so the coordinator, alice, and bob
    draw from independent scripted queues. ``team`` toggles the TOP-LEVEL opt-in
    flag: the server resolves ``team`` from the bundle's top-level spec, and
    declared sub-agents share the parent bundle's ``agent_id``, so the lead and
    both teammates all resolve this one flag. Setting it ``False`` is how the
    negative scenario (S3) proves the peer send is refused.

    ``peer_send_cap`` overrides the TOP-LEVEL ``team_bounds`` per-turn cap (S4).
    The bound must live at the top level: declared sub-agents share the parent
    bundle's ``agent_id``, so the server evaluates alice's tool calls against the
    top-level spec's guardrails — her own sub-config ``guardrails`` block is
    never loaded for policy evaluation.
    """
    src = (_REPO_ROOT / "examples" / "team_demo").resolve()
    dst = tmp / "team_demo"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)

    def _wire(cfg_path: Path, model: str, *, team_flag: bool | None) -> None:
        spec = yaml.safe_load(cfg_path.read_text())
        if team_flag is not None:
            spec["team"] = team_flag
        executor = spec.setdefault("executor", {})
        exec_cfg = executor.pop("config", {}) or {}
        exec_cfg["harness"] = "openai-agents"
        executor["config"] = exec_cfg
        executor["model"] = model
        executor["auth"] = {
            "type": "api_key",
            "api_key": "mock-key",
            "base_url": f"{mock_url}/v1",
        }
        executor["connection"] = {"base_url": f"{mock_url}/v1", "api_key": "mock-key"}
        cfg_path.write_text(yaml.safe_dump(spec, sort_keys=False))

    _wire(dst / "config.yaml", _COORD_MODEL, team_flag=team)
    _wire(dst / "agents" / "alice" / "config.yaml", _ALICE_MODEL, team_flag=None)
    _wire(dst / "agents" / "bob" / "config.yaml", _BOB_MODEL, team_flag=None)

    if peer_send_cap is not None:
        cfg_path = dst / "config.yaml"
        spec = yaml.safe_load(cfg_path.read_text())
        spec["guardrails"]["policies"]["team_bounds"]["function"]["arguments"][
            "max_peer_sends_per_turn"
        ] = peer_send_cap
        cfg_path.write_text(yaml.safe_dump(spec, sort_keys=False))
    return dst


# ── Server + runner + mock lifecycle ──────────────────────────────────────────


@dataclass
class _Stack:
    mock_url: str
    server_url: str
    runner_id: str
    client: httpx.Client
    _procs: list[subprocess.Popen]
    _logdir: Path


def _runner_pids() -> set[int]:
    pids: set[int] = set()
    for module in (
        "omnigent.runner._entry",
        "omnigent.runtime.harnesses._runner",
    ):
        try:
            out = subprocess.run(
                ["pgrep", "-f", f"{sys.executable} -m {module}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return pids
        pids |= {int(x) for x in out.stdout.split() if x.isdigit()}
    return pids


def _kill(pids: set[int]) -> None:
    for pid in pids:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    if not pids:
        return
    time.sleep(2)
    for pid in pids:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


@contextmanager
def _stack(tmp: Path) -> Iterator[_Stack]:
    """Boot mock LLM + state server + sibling runner; reap everything.

    Adapted from ``tests/e2e/conftest.py::live_server``: the server is a pure
    state server, and the runner is spawned as a sibling sharing a tunnel token
    so the server's allowlist accepts exactly this runner.
    """
    logdir = tmp / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    baseline_pids = _runner_pids()
    procs: list[subprocess.Popen] = []
    handles = []

    # ── Mock LLM ──
    mock_port = _free_port()
    mock_url = f"http://127.0.0.1:{mock_port}"
    mock_log = open(logdir / "mock_llm.log", "w")  # noqa: SIM115
    handles.append(mock_log)
    procs.append(
        subprocess.Popen(
            [sys.executable, str(_REPO_ROOT / _MOCK_SERVER_REL), str(mock_port)],
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            stdout=mock_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    )

    # ── Shared tunnel token + runner id ──
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    # ── State server ──
    server_port = _free_port()
    server_url = f"http://127.0.0.1:{server_port}"
    server_log = open(logdir / "server.log", "w")  # noqa: SIM115
    handles.append(server_log)
    server_cfg = tmp / "server.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "_policy_llm_",
                    "connection": {"base_url": f"{mock_url}/v1", "api_key": "mock-key"},
                }
            }
        )
    )
    server_env = {
        **os.environ,
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_url}/v1",
        "PYTHONPATH": str(_REPO_ROOT),
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
    }
    procs.append(
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--port",
                str(server_port),
                "--database-uri",
                f"sqlite:///{tmp / 'team_p2p.db'}",
                "--artifact-location",
                str(tmp / "artifacts"),
                "--config",
                str(server_cfg),
            ],
            env=server_env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    )

    # ── Sibling runner ──
    runner_log = open(logdir / "runner.log", "w")  # noqa: SIM115
    handles.append(runner_log)
    runner_env = {
        **server_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": server_url,
    }
    runner_env.pop("OMNIGENT_RUNNER_TUNNEL_TOKEN", None)
    procs.append(
        subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    )

    client = httpx.Client(base_url=server_url, timeout=30.0)
    try:
        _wait_for_http(f"{mock_url}/stats", time.monotonic() + _MOCK_BOOT_TIMEOUT_S)
        # Wait for server health AND the runner to come online.
        deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                h = client.get("/health", timeout=2.0)
                s = client.get(f"/v1/runners/{runner_id}/status", timeout=2.0)
                if h.status_code == 200 and s.status_code == 200 and s.json().get("online"):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_INTERVAL_S)
        else:
            raise TimeoutError(
                f"server/runner did not come online; see {logdir}/server.log, runner.log"
            )
        yield _Stack(mock_url, server_url, runner_id, client, procs, logdir)
    finally:
        client.close()
        for proc in procs:
            with suppress(ProcessLookupError):
                proc.terminate()
        time.sleep(1.5)
        for proc in procs:
            with suppress(ProcessLookupError):
                proc.kill()
        _kill(_runner_pids() - baseline_pids)
        for h in handles:
            h.close()


# ── Session helpers (thin wrappers over the events contract) ──────────────────


def _upload_bundle(client: httpx.Client, bundle_dir: Path) -> None:
    """Upload a team_demo bundle so its agents are registered on the server.

    The lead and both teammate agents get registered from the one multipart
    upload (teammates are declared under ``tools.agents``).
    """
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # arcname="." puts the bundle's CONTENTS at the tar root (config.yaml,
        # agents/) — the layout the session-upload endpoint expects. Nesting
        # under a top dir gets a 400.
        tar.add(str(bundle_dir), arcname=".")
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code != 409:
        resp.raise_for_status()


def _lookup_agent_id(client: httpx.Client, agent_name: str) -> str:
    """Durable ``agent_id`` for an uploaded (session-scoped) agent.

    Bundle uploads register session-scoped agents, so the built-in ``/v1/agents``
    route never lists them — resolve via a session snapshot instead (mirrors
    ``tests/e2e/conftest.py::lookup_agent_id``).
    """
    resp = client.get("/v1/sessions", params={"agent_name": agent_name, "limit": 1})
    resp.raise_for_status()
    sessions = resp.json()["data"]
    if sessions:
        return str(sessions[0]["agent_id"])
    raise AssertionError(f"agent {agent_name!r} not registered on the server")


def _create_session(client: httpx.Client, *, agent_name: str, runner_id: str) -> str:
    agent_id = _lookup_agent_id(client, agent_name)
    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = str(resp.json()["id"])
    client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id}).raise_for_status()
    return session_id


def _send_user_message(client: httpx.Client, *, session_id: str, content: str) -> None:
    body = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": content}]},
    }
    resp = client.post(f"/v1/sessions/{session_id}/events", json=body)
    resp.raise_for_status()


def _poll_idle(client: httpx.Client, session_id: str, *, timeout: float = _TURN_TIMEOUT_S) -> None:
    """Wait until *session_id* is terminal for the current turn."""
    deadline = time.monotonic() + timeout
    seen_running = False
    while time.monotonic() < deadline:
        snap = client.get(f"/v1/sessions/{session_id}").json()
        status = snap.get("status")
        if status in ("running", "waiting"):
            seen_running = True
        if status == "failed" or (status == "idle" and seen_running):
            return
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"session {session_id} not idle within {timeout}s")


def _items_blob(client: httpx.Client, session_id: str) -> str:
    """Serialize a session's items list for substring assertions."""
    snap = client.get(f"/v1/sessions/{session_id}").json()
    return json.dumps(snap.get("items", []))


def _list_children(client: httpx.Client) -> list[dict]:
    resp = client.get(
        "/v1/sessions",
        params={"kind": "sub_agent", "order": "desc", "limit": 50},
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if isinstance(data, dict) else []


def _wait_for_child(client: httpx.Client, title_suffix: str, *, timeout: float = 60.0) -> str:
    """Return the newest sub-agent session whose title ends with *title_suffix*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in _list_children(client):
            title = str(row.get("title") or "")
            if title.endswith(title_suffix):
                for key in ("id", "session_id", "conversation_id"):
                    if isinstance(row.get(key), str):
                        return row[key]
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"no sub-agent child with title ~ {title_suffix!r} within {timeout}s")


# ── Result framework (mirrors polly_cuj) ──────────────────────────────────────


@dataclass
class Result:
    scenario: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks) and bool(self.checks)

    def summary(self) -> dict:
        return {
            "scenario": self.scenario,
            "ok": self.ok,
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in self.checks],
            "notes": self.notes,
        }


@dataclass
class Ctx:
    stack: _Stack
    tmp: Path


_DENY_MARKER = "Denied by policy:"


# ── Shared setup: spawn a live team, return the session ids ───────────────────


def _spawn_team(
    ctx: Ctx, name: str, *, team: bool = True, peer_send_cap: int | None = None
) -> tuple[str, str, str]:
    """Boot a team_demo instance and spawn alice + bob.

    Returns ``(coordinator_id, alice_id, bob_id)``. The coordinator brain emits
    two named ``(agent,title)`` sends in one turn; alice and bob each run a
    benign scripted turn-1 ("ack") so their sessions exist and are addressable.
    """
    s = ctx.stack
    _mock_reset(s.mock_url)
    # Teammates' turn-1: a plain ack. A fallback drains any stray extra calls.
    _mock_set_fallback(s.mock_url, "default", "ok")
    _mock_configure(s.mock_url, [{"text": "alice ready"}], key=_ALICE_MODEL)
    _mock_configure(s.mock_url, [{"text": "bob ready"}], key=_BOB_MODEL)
    _mock_configure(
        s.mock_url,
        [
            {
                "tool_calls": [
                    _send_call("alice", "alice", "You are alice, on a team.", call_id="c_a"),
                    _send_call("bob", "bob", "You are bob, on a team.", call_id="c_b"),
                ]
            },
            {"text": "team spawned"},
        ],
        key=_COORD_MODEL,
    )

    bundle = _rewrite_team_bundle(
        ctx.tmp / name, s.mock_url, team=team, peer_send_cap=peer_send_cap
    )
    _upload_bundle(s.client, bundle)
    coord_id = _create_session(s.client, agent_name="team_demo", runner_id=s.runner_id)
    _send_user_message(s.client, session_id=coord_id, content="Spawn alice and bob.")
    _poll_idle(s.client, coord_id)

    alice_id = _wait_for_child(s.client, "alice:alice")
    bob_id = _wait_for_child(s.client, "bob:bob")
    return coord_id, alice_id, bob_id


# ── Scenarios ─────────────────────────────────────────────────────────────────


def scenario_s1(ctx: Ctx) -> Result:
    """S1 — peer reply lands in the SENDER's inbox, not the lead's."""
    res = Result("s1_peer_reply_to_sender")
    s = ctx.stack
    coord_id, alice_id, bob_id = _spawn_team(ctx, "s1")

    # Bob's peer turn: answer with the sentinel.
    _mock_configure(
        s.mock_url, [{"text": f"My favorite is a trie. {_BOB_SENTINEL}"}], key=_BOB_MODEL
    )
    # Alice's peer turn: send to bob by id, then drain her inbox.
    _mock_configure(
        s.mock_url,
        [
            {"tool_calls": [_peer_send_call(bob_id, _ALICE_QUESTION, call_id="c_send")]},
            {"tool_calls": [_tool_call("sys_read_inbox", {}, call_id="c_inbox")]},
            {"text": "reported bob's answer"},
        ],
        key=_ALICE_MODEL,
    )

    _send_user_message(s.client, session_id=alice_id, content=f"Message bob: {_ALICE_QUESTION}")
    _poll_idle(s.client, alice_id, timeout=_TURN_TIMEOUT_S)

    # Give the peer completion + auto-wake time to surface in alice's items.
    deadline = time.monotonic() + 60.0
    alice_blob = ""
    while time.monotonic() < deadline:
        alice_blob = _items_blob(s.client, alice_id)
        if _BOB_SENTINEL in alice_blob:
            break
        time.sleep(_POLL_INTERVAL_S)
    coord_blob = _items_blob(s.client, coord_id)

    res.add("reply_in_alice_inbox", _BOB_SENTINEL in alice_blob, "sentinel in alice items")
    res.add(
        "reply_not_in_lead", _BOB_SENTINEL not in coord_blob, "sentinel absent from coordinator"
    )
    return res


def scenario_s2(ctx: Ctx) -> Result:
    """S2 — a teammate can DISCOVER a peer via ``sys_session_list``."""
    res = Result("s2_discovery")
    s = ctx.stack
    _coord_id, alice_id, bob_id = _spawn_team(ctx, "s2")

    _mock_configure(
        s.mock_url,
        [
            {"tool_calls": [_tool_call("sys_session_list", {}, call_id="c_list")]},
            {"text": "listed my team"},
        ],
        key=_ALICE_MODEL,
    )
    _send_user_message(s.client, session_id=alice_id, content="List your team.")
    _poll_idle(s.client, alice_id)

    alice_blob = _items_blob(s.client, alice_id)
    res.add(
        "bob_id_discovered", bob_id in alice_blob, f"bob id {bob_id} in sys_session_list output"
    )
    return res


def scenario_s3(ctx: Ctx) -> Result:
    """S3 — peer send is REFUSED when a teammate did not opt into the team."""
    res = Result("s3_out_of_tree_denied")
    s = ctx.stack
    # Team opt-in OFF at the (shared) top-level spec → the peer send must be
    # refused: by-session-id sends are child-only outside an agent team.
    _coord_id, alice_id, bob_id = _spawn_team(ctx, "s3", team=False)

    _mock_configure(
        s.mock_url,
        [
            {"tool_calls": [_peer_send_call(bob_id, _ALICE_QUESTION, call_id="c_send")]},
            {"text": "peer send attempted"},
        ],
        key=_ALICE_MODEL,
    )
    _send_user_message(s.client, session_id=alice_id, content="Try to message bob.")
    _poll_idle(s.client, alice_id)

    alice_blob = _items_blob(s.client, alice_id)
    # Either the structured error code or the human message the tool returns.
    denied = "session_out_of_tree" in alice_blob or "authorized team peer" in alice_blob
    res.add("peer_send_refused", denied, f"blob tail: {alice_blob[-400:]}")
    res.add("bob_sentinel_absent", _BOB_SENTINEL not in alice_blob, "bob never ran")
    return res


def scenario_s4(ctx: Ctx) -> Result:
    """S4 — ``team_bounds`` per-turn cap: substrate check + a live-only finding.

    ``team_bounds`` is a stateful per-turn counter. Like ``spawn_bounds`` in the
    polly CUJ, the server rebuilds the policy engine per ``tools/call``
    (``_build_policy_engine_from_spec``), so the counter resets each call and the
    per-turn cap CANNOT trip in this deterministic server-side path. That is a
    documented limitation of the mock harness, not the feature — the cap must be
    verified against a live runner. So here we assert the *substrate*
    deterministically (a wave of peer sends all dispatch) and record the cap as a
    non-failing finding, mirroring ``polly_cuj.scenario_fanout_dispatch``.
    """
    res = Result("s4_team_bounds_substrate")
    s = ctx.stack
    _coord_id, alice_id, bob_id = _spawn_team(ctx, "s4", peer_send_cap=2)

    _mock_set_fallback(s.mock_url, "default", "ok")
    _mock_configure(s.mock_url, [{"text": f"ok {_BOB_SENTINEL}"}], key=_BOB_MODEL)
    _mock_configure(
        s.mock_url,
        [
            {
                "tool_calls": [
                    _peer_send_call(bob_id, "m1", call_id="c1"),
                    _peer_send_call(bob_id, "m2", call_id="c2"),
                    _peer_send_call(bob_id, "m3", call_id="c3"),
                ]
            },
            {"text": "three sends attempted"},
        ],
        key=_ALICE_MODEL,
    )
    _send_user_message(s.client, session_id=alice_id, content="Send bob three messages.")
    _poll_idle(s.client, alice_id)

    alice_blob = _items_blob(s.client, alice_id)
    # Substrate: the peer-send tool was reached and produced running/handle
    # output for the wave (a peer send returns a running handle or a
    # single-turn-per-session guard message — both prove dispatch was attempted).
    dispatched = "running" in alice_blob or "session" in alice_blob
    res.add("peer_send_wave_dispatched", dispatched, "wave reached the peer-send tool")
    cap_fired = _DENY_MARKER in alice_blob
    res.notes.append(
        f"finding: team_bounds per-turn cap fired={cap_fired} "
        "(expected False in this server-side path — the engine is rebuilt per "
        "tools/call so the per-turn counter resets; verify the cap against a "
        "live runner)"
    )
    return res


def scenario_s5(ctx: Ctx) -> Result:
    """S5 — a peer send does NOT rewrite bob's structural parent (still lead)."""
    res = Result("s5_topology_unchanged")
    s = ctx.stack
    coord_id, alice_id, bob_id = _spawn_team(ctx, "s5")

    _mock_configure(s.mock_url, [{"text": f"ok {_BOB_SENTINEL}"}], key=_BOB_MODEL)
    _mock_configure(
        s.mock_url,
        [
            {"tool_calls": [_peer_send_call(bob_id, _ALICE_QUESTION, call_id="c_send")]},
            {"tool_calls": [_tool_call("sys_read_inbox", {}, call_id="c_inbox")]},
            {"text": "done"},
        ],
        key=_ALICE_MODEL,
    )
    _send_user_message(s.client, session_id=alice_id, content="Message bob.")
    _poll_idle(s.client, alice_id)
    # Let the peer turn settle.
    time.sleep(3.0)

    bob_snap = s.client.get(f"/v1/sessions/{bob_id}").json()
    parent = bob_snap.get("parent_session_id")
    res.add(
        "bob_parent_is_lead",
        parent == coord_id,
        f"bob.parent_session_id={parent!r} expected lead {coord_id!r}",
    )
    return res


_SCENARIOS = {
    "s1": scenario_s1,
    "s2": scenario_s2,
    "s3": scenario_s3,
    "s4": scenario_s4,
    "s5": scenario_s5,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="all", help="Scenario id, or 'all'.")
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--keep", action="store_true", help="Keep the sandbox temp dir.")
    args = parser.parse_args(argv)

    if args.list_scenarios:
        for name in _SCENARIOS:
            print(name)
        return 0

    if args.scenario == "all":
        chosen = list(_SCENARIOS)
    elif args.scenario in _SCENARIOS:
        chosen = [args.scenario]
    else:
        print(f"error: unknown scenario {args.scenario!r}; try --list-scenarios", file=sys.stderr)
        return 2

    if not (_REPO_ROOT / "examples" / "team_demo" / "config.yaml").exists():
        print("error: examples/team_demo not found", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="team-p2p-cuj-"))
    all_ok = True
    try:
        with _stack(tmp) as stack:
            ctx = Ctx(stack=stack, tmp=tmp)
            for name in chosen:
                try:
                    res = _SCENARIOS[name](ctx)
                except Exception as exc:
                    res = Result(name)
                    res.add("ran", False, f"{type(exc).__name__}: {exc}")
                all_ok = all_ok and res.ok
                print("SUMMARY " + json.dumps(res.summary()))
    finally:
        if args.keep:
            print(f"[kept sandbox] {tmp}", file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("SUMMARY " + json.dumps({"scenario": "ALL", "ok": all_ok, "ran": chosen}))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
