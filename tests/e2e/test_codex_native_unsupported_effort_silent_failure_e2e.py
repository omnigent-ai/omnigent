"""End-to-end: a codex-rejected reasoning effort must not fail the turn silently.

Reported journey: set
``reasoning_effort: minimal`` on a ``codex-native`` session pinned to
``gpt-6-astra`` (a model whose advertised ladder has no ``minimal``): the
session PATCH succeeds and a GET reports the effort back, but the first turn
then ends ``status: failed`` with no output, no error item, and zero token
usage. Nothing tells the user the model/effort pairing was rejected. The same
brief at ``low`` works.

Omnigent accepts the effort because ``CODEX_NATIVE_EFFORTS`` deliberately
carries codex's full ladder (codex is the per-model authority,
``omnigent/util/reasoning_effort.py``), and nothing re-checks the pairing
against the model's ``model/list`` ``supportedReasoningEfforts``. When codex
then ends the turn with a bare failed status and no ``TurnError`` payload,
``_terminal_turn_status_edge`` (omnigent/harnesses/codex_native/forwarder.py)
derives a ``failed`` edge with ``error=None`` and ``_post_turn_status_edge``
publishes it with ``output=None`` -- a silent failed turn.

This drives the real stack -- server subprocess, real ``omnigent.runner._entry``
runner, the real codex-native bridge/executor/forwarder -- against a fake
``codex`` CLI (an app-server speaking the same WebSocket JSON-RPC the real one
does). The fake advertises ``gpt-6-astra`` without ``minimal`` in
``supportedReasoningEfforts`` and, when a turn runs with an effort outside the
model's advertised ladder, ends it with ``turn/failed`` carrying no error
payload -- matching the report's "no provider error at all". The real codex CLI
requires a live ChatGPT login and the real ``gpt-6-astra`` model, so this fake
is a stand-in for the codex side; the omnigent side is fully real.

While the bug is live, the ``minimal`` turn ends failed with no surfaced error
anywhere (no error item, no failure output) and the test FAILS on that
silence. It passes once an unsupported model/effort pairing surfaces a reason
(a turn error naming the rejection) or is gated up front by the model's
advertised levels -- either fix direction from the report.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_codex_native_unsupported_effort_silent_failure_e2e.py -v
"""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="boots POSIX server/runner subprocesses with a fake codex CLI"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HEALTH_TIMEOUT_S = 90.0
#: Terminal auto-create (app-server spawn + trust) plus one fake turn.
_TURN_OUTCOME_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 2.0

#: The model the report pins -- advertised by the fake WITHOUT ``minimal``.
_ASTRA_MODEL = "gpt-6-astra"

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# Shared fixtures/helpers use ambient ``httpx`` calls that DO trust env, so
# also exclude loopback from any forced proxy at import time.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


# The codex-native agent shape from the report, reduced to the fields that
# pick the launch path under test. Sandbox none: the rig itself may already
# run inside a container/bwrap where nested sandboxes cannot start.
_CODEX_NATIVE_AGENT_YAML = """\
spec_version: 1
name: codex-effort-repro
description: codex-native session shape for the unsupported-effort repro.

executor:
  type: omnigent
  config:
    harness: codex-native
    yolo: true

prompt: |
  You are a codex-native session used to reproduce a reasoning-effort bug.

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

# A fake ``codex`` CLI: implements the app-server WebSocket JSON-RPC surface
# the codex-native harness drives (initialize, model/list, hooks/list,
# thread/resume, thread/settings/update, turn/start) plus ``--version`` and a
# parked TUI mode for the tmux pane the runner opens. When a turn runs with a
# reasoning effort outside the current model's advertised
# ``supportedReasoningEfforts``, the turn ends with ``turn/failed`` and NO
# error payload -- the report's observed silence ("no provider error at all").
_FAKE_CODEX_TEMPLATE = """#!{python}
'''Fake codex CLI (app-server) for the unsupported-effort silent-failure e2e.'''
import asyncio
import json
import sys
import uuid

MODELS = [
    {{"id": "gpt-6-codex", "model": "gpt-6-codex", "displayName": "GPT-6 Codex",
      "isDefault": True,
      "supportedReasoningEfforts": ["low", "medium", "high", "xhigh"],
      "defaultReasoningEffort": "medium"}},
    {{"id": "gpt-6-astra", "model": "gpt-6-astra", "displayName": "GPT-6 Astra",
      "supportedReasoningEfforts": ["low", "medium", "high", "xhigh"],
      "defaultReasoningEffort": "medium"}},
]

THREAD_ID = "thread_" + uuid.uuid4().hex


def _pinned_model():
    # The harness pins the session model into the private CODEX_HOME
    # config.toml before the app-server boots; honor it like real codex.
    import os
    home = os.environ.get("CODEX_HOME", "")
    if not home:
        return None
    try:
        for line in open(os.path.join(home, "config.toml"), encoding="utf-8"):
            line = line.strip()
            if line.startswith("model =") or line.startswith("model="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


STATE = {{"model": _pinned_model(), "effort": None, "turn_seq": 0}}
CONNECTIONS = set()


def _log(text):
    import os
    path = os.environ.get("FAKE_CODEX_LOG", "")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + chr(10))


async def _broadcast(method, params):
    payload = json.dumps({{"method": method, "params": params}})
    for ws in list(CONNECTIONS):
        try:
            await ws.send(payload)
        except Exception:
            pass


async def _announce_thread(ws):
    await asyncio.sleep(0.5)
    try:
        await ws.send(json.dumps({{
            "method": "thread/started",
            "params": {{"thread": {{"id": THREAD_ID}}}},
        }}))
    except Exception:
        pass


async def _run_turn(turn_id):
    await asyncio.sleep(0.3)
    await _broadcast("turn/started", {{"threadId": THREAD_ID, "turn": {{"id": turn_id}}}})
    await asyncio.sleep(0.5)
    model = STATE["model"] or "gpt-6-codex"
    effort = STATE["effort"]
    row = next((r for r in MODELS if r["id"] == model), None)
    supported = row["supportedReasoningEfforts"] if row else []
    _log("turn " + turn_id + " model=" + str(model) + " effort=" + str(effort))
    if effort and effort not in supported:
        # The model does not offer this effort: codex ends the turn failed
        # with no TurnError payload (the reported "no provider error at all").
        await _broadcast("turn/failed", {{
            "threadId": THREAD_ID,
            "turn": {{"id": turn_id, "status": "failed", "items": []}},
        }})
        return
    item = {{"id": "item_" + turn_id, "type": "agentMessage",
             "text": "FAKE-CODEX-REPLY model=" + model + " effort=" + str(effort)}}
    await _broadcast("item/completed",
                     {{"threadId": THREAD_ID, "turnId": turn_id, "item": item}})
    await _broadcast("turn/completed", {{
        "threadId": THREAD_ID,
        "turn": {{"id": turn_id, "status": "completed", "items": [item]}},
    }})


async def _handler(ws):
    CONNECTIONS.add(ws)
    try:
        async for raw in ws:
            msg = json.loads(raw)
            if "id" not in msg:
                continue  # client notification (e.g. "initialized")
            method = msg.get("method")
            params = msg.get("params") or {{}}
            _log("request " + str(method) + " " + json.dumps(params)[:300])
            if method == "initialize":
                result = {{"serverInfo": {{"name": "fake-codex", "version": "0.153.4"}}}}
            elif method == "model/list":
                result = {{"data": MODELS, "nextCursor": None}}
            elif method == "hooks/list":
                cwds = params.get("cwds") or [""]
                result = {{"data": [
                    {{"cwd": cwd, "hooks": [{{
                        "id": "omnigent-policy-hook",
                        "command": "python -m omnigent.harnesses.codex_native.hook run",
                        "trustStatus": "trusted",
                        "currentHash": "fake-hash",
                    }}]}} for cwd in cwds
                ]}}
            elif method in ("thread/start", "thread/resume"):
                result = {{"thread": {{"id": THREAD_ID, "turns": []}}}}
            elif method == "thread/settings/update":
                for key in ("model", "effort"):
                    if key in params:
                        STATE[key] = params[key]
                result = {{}}
                asyncio.ensure_future(_broadcast("thread/settings/updated", {{
                    "threadId": THREAD_ID,
                    "threadSettings": {{"model": STATE["model"], "effort": STATE["effort"]}},
                }}))
            elif method == "turn/start":
                STATE["turn_seq"] += 1
                turn_id = "turn_" + str(STATE["turn_seq"])
                result = {{"turn": {{"id": turn_id}}}}
                asyncio.ensure_future(_run_turn(turn_id))
            else:
                result = {{}}
            await ws.send(json.dumps({{"id": msg["id"], "result": result}}))
            if method == "initialize":
                asyncio.ensure_future(_announce_thread(ws))
    finally:
        CONNECTIONS.discard(ws)


async def _serve(listen_url):
    import websockets

    host, _, port = listen_url.removeprefix("ws://").partition(":")
    async with websockets.serve(_handler, host, int(port)):
        await asyncio.Future()


args = sys.argv[1:]
if "--version" in args:
    print("codex-cli 0.153.4")
elif args and args[0] == "app-server" and "--listen" in args:
    asyncio.run(_serve(args[args.index("--listen") + 1]))
elif args[:2] == ["debug", "models"]:
    sys.exit(2)  # tolerated: the catalog probe treats failure as "no catalog"
else:
    # The runner opens a codex TUI pane (``--remote``); park it -- the
    # app-server above drives the whole session.
    print("fake codex TUI (e2e stand-in); the app-server drives this session")
    import time as _time
    while True:
        _time.sleep(3600)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback excluded from any forced HTTP(S) proxy.

    Also drops any omnigent session/runner env leaked by the invoking
    environment (data dir, runner identity, server URL): the rig must boot
    its own isolated server and runner, not inherit a live session's.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OMNIGENT") and key != "RUNNER_SERVER_URL"
    }
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


def _spec_bundle() -> bytes:
    """Gzip the codex-native agent spec as a session bundle."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _CODEX_NATIVE_AGENT_YAML.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@dataclass
class _Rig:
    """A booted omnigent server + runner wired to the fake codex CLI."""

    base_url: str
    runner_id: str
    server_log: Path
    runner_log: Path
    workspace: Path

    def log_tails(self) -> str:
        return (
            f"server log tail:\n{self.server_log.read_text()[-2000:]}\n"
            f"runner log tail:\n{self.runner_log.read_text()[-4000:]}"
        )


@pytest.fixture(scope="module")
def fake_codex_rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Rig]:
    """Server + runner whose only codex CLI is the fake app-server above.

    Isolated ``HOME`` / ``OMNIGENT_CONFIG_HOME`` / ``CODEX_HOME`` so the rig
    sees no ambient providers; ``CODEX_HOME/auth.json`` carries a fake API key
    so the codex launch router treats codex as logged in (``login_required``
    false) instead of parking the session on the sign-in screen.
    """
    from omnigent.runner.identity import token_bound_runner_id

    work = tmp_path_factory.mktemp("codex_effort_silent_failure")
    bin_dir = work / "bin"
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    workspace = work / "workspace"
    for path in (bin_dir, config_home, codex_home, home_dir, state_dir, artifacts, workspace):
        path.mkdir(parents=True, exist_ok=True)

    fake_codex = bin_dir / "codex"
    fake_codex.write_text(_FAKE_CODEX_TEMPLATE.format(python=sys.executable))
    fake_codex.chmod(0o755)
    # A stored codex login so resolve_native_codex_launch does not mark the
    # launch login_required (which fails every chat turn fast by design).
    (codex_home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-fake-e2e"}))

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "OMNIGENT_CODEX_PATH": str(fake_codex),
        "CODEX_HOME": str(codex_home),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "fake-codex rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )
        yield _Rig(
            base_url=base_url,
            runner_id=runner_id,
            server_log=server_log,
            runner_log=runner_log,
            workspace=workspace,
        )
    finally:
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


def _create_pinned_session(rig: _Rig, *, reasoning_effort: str) -> str:
    """Create a live codex-native session pinned to gpt-6-astra, then set the effort.

    Mirrors the reported journey exactly: the session runs ``gpt-6-astra``
    (explicit ``model_override``), and once the native thread is live the
    ``reasoning_effort`` PATCH succeeds and a GET reports it back -- all
    before the first turn.
    """
    create = _client.post(
        f"{rig.base_url}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(rig.workspace)})},
        files={"bundle": ("codex.tar.gz", _spec_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])

    pin = _client.patch(
        f"{rig.base_url}/v1/sessions/{session_id}",
        json={"model_override": _ASTRA_MODEL},
        timeout=30.0,
    )
    assert pin.status_code < 400, f"model PATCH rejected: {pin.status_code} {pin.text}"

    bind = _client.patch(
        f"{rig.base_url}/v1/sessions/{session_id}",
        json={"runner_id": rig.runner_id},
        timeout=60.0,
    )
    bind.raise_for_status()

    # Wait for the runner to adopt the native codex thread (terminal +
    # app-server up), so the effort PATCH lands on a live session exactly as
    # in the report.
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    thread_live = False
    while time.monotonic() < deadline:
        snapshot = _client.get(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)
        if snapshot.status_code == 200 and snapshot.json().get("external_session_id"):
            thread_live = True
            break
        time.sleep(1.0)
    assert thread_live, f"codex-native thread never came live for {session_id}\n{rig.log_tails()}"

    # The report's PATCH: accepted by omnigent (CODEX_NATIVE_EFFORTS carries
    # the full ladder) ...
    effort_patch = _client.patch(
        f"{rig.base_url}/v1/sessions/{session_id}",
        json={"reasoning_effort": reasoning_effort},
        timeout=30.0,
    )
    assert effort_patch.status_code < 400, (
        f"reasoning_effort PATCH rejected: {effort_patch.status_code} {effort_patch.text}"
    )

    # ... and a GET reports it back.
    snapshot = _client.get(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)
    snapshot.raise_for_status()
    body = snapshot.json()
    assert body.get("model_override") == _ASTRA_MODEL, body.get("model_override")
    assert body.get("reasoning_effort") == reasoning_effort, body.get("reasoning_effort")
    return session_id


def _send_user_message(rig: _Rig, session_id: str, text: str) -> None:
    send = _client.post(
        f"{rig.base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    )
    assert send.status_code == 202, f"send rejected: {send.status_code} {send.text}"


@dataclass
class _TurnOutcome:
    """Observed terminal state of one codex-native turn."""

    session_status: str
    assistant_texts: list[str]
    error_messages: list[str]
    error_label: str
    items: list[dict]

    def surfaced_errors(self) -> list[str]:
        """Every user-visible failure explanation this turn produced."""
        return [msg for msg in [*self.error_messages, self.error_label] if msg.strip()]


def _wait_for_turn_outcome(rig: _Rig, session_id: str) -> _TurnOutcome:
    """Poll until the turn reaches a terminal, user-visible outcome."""
    deadline = time.monotonic() + _TURN_OUTCOME_TIMEOUT_S
    last: _TurnOutcome | None = None
    while time.monotonic() < deadline:
        session = _client.get(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)
        session_body = session.json() if session.status_code == 200 else {}
        status = str(session_body.get("status", ""))
        labels = session_body.get("labels") or {}
        items_resp = _client.get(
            f"{rig.base_url}/v1/sessions/{session_id}/items?limit=100", timeout=10.0
        )
        items = list(items_resp.json().get("data", [])) if items_resp.status_code == 200 else []
        assistant_texts = [
            json.dumps(item.get("content", item))
            for item in items
            if item.get("type") == "message" and item.get("role") == "assistant"
        ]
        error_messages = [
            str(item.get("message", "")) for item in items if item.get("type") == "error"
        ]
        last = _TurnOutcome(
            session_status=status,
            assistant_texts=assistant_texts,
            error_messages=error_messages,
            error_label=str(labels.get("omnigent.last_task_error_message", "") or ""),
            items=items,
        )
        if status == "failed" or error_messages or assistant_texts:
            return last
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"turn reached no terminal outcome within {_TURN_OUTCOME_TIMEOUT_S:.0f}s; "
        f"last={last}\n{rig.log_tails()}"
    )


@pytest.mark.timeout(600)
def test_supported_effort_low_turn_completes(fake_codex_rig: _Rig) -> None:
    """Control: the same brief at ``low`` works (the report's working run).

    Proves the rig itself is sound so the repro test below fails only on the
    bug's silence, never on a broken fixture.
    """
    session_id = _create_pinned_session(fake_codex_rig, reasoning_effort="low")
    _send_user_message(fake_codex_rig, session_id, "Say hello.")
    outcome = _wait_for_turn_outcome(fake_codex_rig, session_id)
    assert outcome.assistant_texts, (
        f"low-effort control turn produced no assistant reply: {outcome}\n"
        f"{fake_codex_rig.log_tails()}"
    )
    # The reply text proves the pinned model AND the patched effort reached
    # codex through the real settings-update path -- rig fidelity, not luck.
    reply = " ".join(outcome.assistant_texts)
    assert "FAKE-CODEX-REPLY" in reply and "effort=low" in reply, reply
    assert not outcome.surfaced_errors(), (
        f"low-effort control turn errored: {outcome.surfaced_errors()}\n"
        f"{fake_codex_rig.log_tails()}"
    )


@pytest.mark.timeout(600)
def test_unsupported_effort_minimal_failure_is_surfaced(fake_codex_rig: _Rig) -> None:
    """An effort the model doesn't offer must not kill the turn silently.

    Journey (the report's): pin ``gpt-6-astra`` + ``reasoning_effort:
    minimal`` on a codex-native session (PATCH succeeds, GET reports it
    back), send the first message, wait for the turn to end.

    While the bug is live the turn ends ``status: failed`` with no output and
    no error item -- pure silence -- and this test FAILS on the missing
    explanation. After a fix the turn must either surface an error naming the
    rejected model/effort pairing, or complete because the effort was gated
    to the model's advertised levels.
    """
    session_id = _create_pinned_session(fake_codex_rig, reasoning_effort="minimal")
    _send_user_message(fake_codex_rig, session_id, "Say hello.")
    outcome = _wait_for_turn_outcome(fake_codex_rig, session_id)

    if outcome.assistant_texts and outcome.session_status != "failed":
        # Fix direction 2: the effort was gated/clamped to the model's
        # advertised ladder and the turn completed -- not silent, acceptable.
        return

    # The turn failed: the failure must carry a user-visible reason -- an
    # error item, or a failure output persisted as the session's
    # last_task_error (what a failed edge with output produces).
    assert outcome.surfaced_errors(), (
        "codex-native turn with an effort the model doesn't offer "
        f"(model={_ASTRA_MODEL!r}, effort='minimal') ended "
        f"status={outcome.session_status!r} with NO error item, no failure "
        "output, and no last_task_error -- the silent failure from the "
        "report. Items seen: "
        f"{json.dumps(outcome.items)[:1500]}\n"
        f"{fake_codex_rig.log_tails()}"
    )
