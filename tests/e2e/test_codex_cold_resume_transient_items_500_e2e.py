"""E2E regression tests: Codex cold resume vs a transient /items failure.

Codex cold resume abandons server history on the FIRST transient HTTP failure.
``_fetch_all_session_items_for_codex_resume`` gives each
``GET /v1/sessions/<id>/items?limit=1000`` page exactly one request; a single
recoverable 500 raises ``_CodexResumeHistoryUnavailableError``, after which
``_ensure_local_codex_resume_rollout``:

* with NO local rollout on the machine -> re-raises, so the runner's
  ``_auto_create_codex_terminal`` aborts and the Codex terminal never starts
  (the session gets ``session.status: failed``);
* with an existing local rollout -> silently resumes from it, so one blip
  selects STALE local history even though the very next server read would
  have succeeded.

Both tests drive the REAL user journey end to end: a real ``omnigent server``
subprocess (with a one-shot transient failure injected at the
conversation-store seam, so the real route + exception handler produce the
production ``500 internal_error`` body exactly once per conversation and every
retry of the same read succeeds), a real runner subprocess, a real
codex-native session with committed history and a bound prior Codex thread id
- then the resume is triggered the way the web UI / a daemon relaunch does:
binding the session to the runner, which auto-creates the Codex terminal and
refreshes the local rollout from server history.

Desired behavior (asserted): one transient blip on an idempotent page read
must not lose recoverable server history - the refreshed rollout is written
from the committed items, containing every message exactly once. On the buggy
build test 1 fails because the rollout is never written (the terminal aborts),
and test 2 fails because the stale local rollout is kept.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_codex_cold_resume_transient_items_500_e2e.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every HTTP call targets 127.0.0.1; bypass any CI egress proxy.
_http = httpx.Client(trust_env=False)

# The runner imports ``omnigent_client`` / ``omnigent_ui_sdk``; in a worktree
# they resolve from sdks/, in an installed venv from site-packages.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# Bootstrap for the spawned server: monkeypatch the conversation store so the
# FIRST limit=1000 item page read per conversation raises - the real route and
# exception handler then serve the production ``500 internal_error`` body once
# - while every subsequent identical read succeeds. That is the reported
# transient blip: recoverable on the very next request. Each limit=1000 call
# is appended to $ITEMS_FAULT_LOG so the tests can verify the injected blip
# was consumed by the resume fetch itself.
_SERVER_BOOTSTRAP = """
import json
import os
import threading

import omnigent.stores.conversation_store.sqlalchemy_store as _s

_orig = _s.SqlAlchemyConversationStore.list_items
_failed_once = set()
_lock = threading.Lock()
_log_path = os.environ["ITEMS_FAULT_LOG"]


def _log(conversation_id, limit, after, outcome):
    with _lock:
        with open(_log_path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "limit": limit,
                        "after": after,
                        "outcome": outcome,
                    }
                )
                + "\\n"
            )


def _transient_list_items(self, conversation_id, limit=100, after=None, *args, **kwargs):
    if limit == 1000:
        with _lock:
            first = conversation_id not in _failed_once
            if first:
                _failed_once.add(conversation_id)
        if first:
            _log(conversation_id, limit, after, "injected-500")
            raise RuntimeError(
                "simulated transient store failure reading an item page "
                f"(one-shot, conversation={conversation_id})"
            )
        _log(conversation_id, limit, after, "ok")
    return _orig(self, conversation_id, limit, after, *args, **kwargs)


_s.SqlAlchemyConversationStore.list_items = _transient_list_items

from omnigent.cli import main

main()
"""

# Prior Codex thread ids bound to the conversations (what a previous run's
# capture persisted as ``external_session_id``).
_EXT_SID_NO_ROLLOUT = "019e96aa-0be2-7343-8d3b-6f914d60936b"
_EXT_SID_STALE = "019e96aa-1c3d-7343-8d3b-6f914d60936c"

_STALE_MARKER = "STALE-LOCAL-ONLY rollout line the server no longer has"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0
# Codex terminal auto-create includes catalog probes + bridge prep; generous.
_SETTLE_TIMEOUT_S = 240.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="codex-native terminals run inside tmux; tmux not installed",
)


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports, no proxies, no leaked runner env.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # Keep subprocess logs readable mid-run for failure diagnostics.
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    # Ambient Omnigent vars (present when this suite itself runs inside a
    # server-spawned runner) would push the spawned runner onto the
    # zygote-fork path, where it hangs on control FDs it does not have, and
    # point data/log dirs at another harness's (possibly read-only) tree.
    # Each subprocess gets exactly the vars it needs via *extra*.
    for name in list(env):
        if name.startswith("OMNIGENT") or name == "RUNNER_SERVER_URL":
            env.pop(name)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


@dataclass
class _Rig:
    """One live server + runner pair shared by the tests in this module."""

    base_url: str
    database_uri: str
    runner_id: str
    runner_home: Path
    server_log: Path
    runner_log: Path
    fault_log: Path

    def runner_log_tail(self, limit: int = 4000) -> str:
        return self.runner_log.read_text()[-limit:]

    def fault_entries(self, session_id: str) -> list[dict[str, object]]:
        """Parsed fault-log entries (limit=1000 item reads) for *session_id*."""
        if not self.fault_log.exists():
            return []
        entries = [
            json.loads(line) for line in self.fault_log.read_text().splitlines() if line.strip()
        ]
        return [e for e in entries if e.get("conversation_id") == session_id]


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Rig]:
    """Spawn the injected server and a real runner, once for the module."""
    tmp_path = tmp_path_factory.mktemp("codex-cold-resume")
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    fault_log = tmp_path / "items_fault_log.jsonl"

    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    server_log_path = tmp_path / "server.log"
    runner_log_path = tmp_path / "runner.log"
    server_log = server_log_path.open("w")
    runner_log = runner_log_path.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SERVER_BOOTSTRAP,
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                database_uri,
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
                    # Keep server-side logs/DB out of the ambient HOME, which
                    # sandboxed CI runners mount read-only.
                    "OMNIGENT_DATA_DIR": str(tmp_path / "server-data"),
                    "ITEMS_FAULT_LOG": str(fault_log),
                }
            ),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    # Hermetic HOME: bridge dirs + rollouts live under
                    # ``$HOME/.omnigent/codex-native`` and provider config
                    # resolves from ``$HOME/.omnigent``.
                    "HOME": str(runner_home),
                }
            ),
            stdout=runner_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            try:
                status = _http.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
                if status.status_code == 200 and status.json().get("online") is True:
                    online = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_S)
        assert online, f"runner never came online; log:\n{runner_log_path.read_text()[-3000:]}"

        yield _Rig(
            base_url=base_url,
            database_uri=database_uri,
            runner_id=runner_id,
            runner_home=runner_home,
            server_log=server_log_path,
            runner_log=runner_log_path,
            fault_log=fault_log,
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()


def _create_codex_native_session(base_url: str) -> str:
    """Create a codex-native wrapper session exactly like ``omnigent codex``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the runner's codex-native
    auto-bootstrap recognizes the session.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    import io
    import tarfile
    import tempfile

    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_codex_agent_spec(Path(tmp), model=None).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat
        # translator (the wrapper spec has no ``spec_version``).
        info = tarfile.TarInfo("codex-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "codex-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _seed_history(database_uri: str, session_id: str, texts: list[str]) -> None:
    """Append one message item per canary text straight into the server store.

    Direct store writes are the same seeding pattern the ``tests/e2e_ui``
    suite uses - there is no REST bulk-append.

    :param database_uri: The spawned server's SQLite URI.
    :param session_id: Conversation to append to.
    :param texts: Message texts, alternating user/assistant by index.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    store = SqlAlchemyConversationStore(database_uri)
    items = []
    for i, text in enumerate(texts):
        role = "user" if i % 2 == 0 else "assistant"
        items.append(
            NewConversationItem(
                type="message",
                response_id=f"resp_{i // 2}",
                data=MessageData(
                    role=role,
                    content=[
                        {
                            "type": "input_text" if role == "user" else "output_text",
                            "text": text,
                        }
                    ],
                    agent="codex" if role == "assistant" else None,
                ),
            )
        )
    for start in range(0, len(items), 200):
        store.append(session_id, items[start : start + 200])


def _bind_and_prepare(
    rig: _Rig, session_id: str, external_session_id: str, seeded_count: int
) -> None:
    """Stamp the prior Codex thread id and sanity-check the seeded history.

    The sanity probe uses ``limit=100`` so it cannot consume the one-shot
    ``limit=1000`` fault reserved for the resume fetch.
    """
    _http.patch(
        f"{rig.base_url}/v1/sessions/{session_id}",
        json={"external_session_id": external_session_id},
        timeout=10.0,
    ).raise_for_status()
    ok = _http.get(
        f"{rig.base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=60.0,
    )
    assert ok.status_code == 200 and len(ok.json()["data"]) == min(seeded_count, 100), (
        "seeded history must be readable at small page sizes"
    )


def _trigger_resume(rig: _Rig, session_id: str) -> int:
    """Bind the session to the runner - what the web UI / a daemon relaunch
    does - so the runner auto-creates the Codex terminal (the cold resume).

    :returns: The bind PATCH status code (not raised: on the buggy build the
        launch failure may surface here, and the assertions that follow give
        the bug-specific message).
    """
    resp = _http.patch(
        f"{rig.base_url}/v1/sessions/{session_id}",
        json={"runner_id": rig.runner_id},
        timeout=_SETTLE_TIMEOUT_S,
    )
    return resp.status_code


def _last_task_error(rig: _Rig, session_id: str) -> dict[str, object] | None:
    """The session snapshot's ``last_task_error`` (the reload-visible failure
    a user sees when the native terminal fails to start), or ``None``."""
    resp = _http.get(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)
    if resp.status_code != 200:
        return None
    error = resp.json().get("last_task_error")
    return error if isinstance(error, dict) else None


def _terminal_count(rig: _Rig, session_id: str) -> int:
    """Number of terminal resources the session exposes."""
    resp = _http.get(
        f"{rig.base_url}/v1/sessions/{session_id}/resources/terminals",
        timeout=10.0,
    )
    if resp.status_code != 200:
        return 0
    payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else payload
    return len(data) if isinstance(data, list) else 0


def _rollout_paths(rig: _Rig, external_session_id: str) -> list[Path]:
    """Rollout files for *external_session_id* under the runner's HOME."""
    root = rig.runner_home / ".omnigent" / "codex-native"
    if not root.is_dir():
        return []
    return sorted(root.glob(f"*/codex-home/sessions/**/rollout-*-{external_session_id}.jsonl"))


def _rollout_response_item_text(path: Path) -> str:
    """Concatenated ``response_item`` payloads of a rollout JSONL file.

    Scoping to ``response_item`` records keeps canary counting exact: a
    correct rollout also mirrors each message as an ``event_msg`` record.
    """
    chunks: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if isinstance(record, dict) and record.get("type") == "response_item":
            chunks.append(json.dumps(record))
    return "\n".join(chunks)


def _assert_single_injected_blip(rig: _Rig, session_id: str) -> None:
    """The transient signature must be live: exactly one injected 500 was
    served to this conversation's ``limit=1000`` resume reads."""
    entries = rig.fault_entries(session_id)
    injected = [e for e in entries if e.get("outcome") == "injected-500"]
    assert len(injected) == 1, (
        f"expected exactly one injected transient 500 for {session_id!r}; "
        f"fault log entries: {entries}"
    )


@pytest.mark.timeout(600)
def test_cold_resume_writes_rollout_despite_transient_items_500(rig: _Rig) -> None:
    """
    Cold resume with NO local rollout must survive one transient /items 500.

    Journey (the reporter's): a codex-native conversation with two pages of
    committed server messages and a bound prior Codex thread id is resumed on
    a machine with no local rollout; the server serves one transient 500 on a
    history page, then the same read succeeds.

    Expected: the history is recoverable (the next request serves it), so the
    refreshed rollout must be written from server history with every committed
    message exactly once, and the Codex terminal launch must proceed. Buggy
    behavior: the single-request fetch aborts resume without ever making a
    second request - no rollout is written and the terminal never starts.
    """
    session_id = _create_codex_native_session(rig.base_url)
    # Two pages at the client's limit=1000.
    canaries = [f"cold-resume canary {i:04d} of the committed transcript" for i in range(1200)]
    _seed_history(rig.database_uri, session_id, canaries)
    _bind_and_prepare(rig, session_id, _EXT_SID_NO_ROLLOUT, len(canaries))

    bind_status = _trigger_resume(rig, session_id)

    # Settle: either the rollout refresh happened (desired) or the launch
    # aborted and surfaced the reload-visible failure the user sees.
    task_error: dict[str, object] | None = None
    deadline = time.monotonic() + _SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        if _rollout_paths(rig, _EXT_SID_NO_ROLLOUT):
            break
        task_error = _last_task_error(rig, session_id)
        if task_error is not None:
            # The launch attempt settled as an abort; nothing will write the
            # rollout after this. Re-check once and stop waiting.
            time.sleep(3)
            break
        time.sleep(_POLL_S)

    _assert_single_injected_blip(rig, session_id)
    rollouts = _rollout_paths(rig, _EXT_SID_NO_ROLLOUT)
    assert rollouts, (
        "cold resume ABORTED on a single transient /items 500 instead of "
        "retrying the same idempotent page read: no Codex rollout was "
        f"refreshed from server history and the terminal never started "
        f"(bind status={bind_status}, last_task_error={task_error}). "
        f"fault log: {rig.fault_entries(session_id)}; runner log tail:\n"
        f"{rig.runner_log_tail()}"
    )
    text = _rollout_response_item_text(rollouts[-1])
    missing = [c for c in canaries if text.count(c) == 0]
    duplicated = [c for c in canaries if text.count(c) > 1]
    assert not missing and not duplicated, (
        "refreshed rollout must retain every committed message exactly once; "
        f"missing={len(missing)} (first: {missing[:3]}) "
        f"duplicated={len(duplicated)} (first: {duplicated[:3]})"
    )


@pytest.mark.timeout(600)
def test_cold_resume_transient_items_500_must_not_keep_stale_local_rollout(
    rig: _Rig,
) -> None:
    """
    One transient /items 500 must not make resume keep a stale local rollout.

    Journey: same as above, but the machine still has a valid local rollout
    from an earlier run that is missing the newer committed messages. The
    server blips once on the history read, then serves it fine.

    Expected: the recoverable server history wins - the rollout is refreshed
    with the committed messages. Buggy behavior: the single failed request
    triggers the local-rollout fallback, so the resumed Codex thread keeps
    stale history that silently drops the newer messages.
    """
    session_id = _create_codex_native_session(rig.base_url)
    canaries = [f"server-side history canary {i:02d} the stale rollout lacks" for i in range(40)]
    _seed_history(rig.database_uri, session_id, canaries)

    # A prior run's rollout for this thread, valid but stale. Path mirrors
    # bridge_dir_for_bridge_id (sha256 of the bridge id, which the
    # runner-owned bridge keys by session id) under the runner's HOME.
    bridge_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    sessions_dir = (
        rig.runner_home
        / ".omnigent"
        / "codex-native"
        / bridge_hash
        / "codex-home"
        / "sessions"
        / "2025"
        / "01"
        / "01"
    )
    sessions_dir.mkdir(parents=True)
    stale_rollout = sessions_dir / f"rollout-2025-01-01T00-00-00-{_EXT_SID_STALE}.jsonl"
    timestamp = "2025-01-01T00:00:00.000Z"
    stale_records = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": _EXT_SID_STALE,
                "timestamp": timestamp,
                "cwd": str(rig.runner_home),
                "originator": "omnigent",
                "cli_version": "0.0.0",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": _STALE_MARKER}],
            },
        },
    ]
    stale_rollout.write_text(
        "".join(json.dumps(record) + "\n" for record in stale_records),
        encoding="utf-8",
    )

    _bind_and_prepare(rig, session_id, _EXT_SID_STALE, len(canaries))
    bind_status = _trigger_resume(rig, session_id)

    # Settle: the rollout refresh happened (desired), or the launch finished
    # on the stale fallback (a terminal appears), or it failed outright.
    task_error: dict[str, object] | None = None
    deadline = time.monotonic() + _SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        rollouts = _rollout_paths(rig, _EXT_SID_STALE)
        if rollouts and canaries[0] in rollouts[-1].read_text(encoding="utf-8"):
            break
        task_error = _last_task_error(rig, session_id)
        if task_error is not None or _terminal_count(rig, session_id) > 0:
            # The launch attempt settled without refreshing the rollout;
            # nothing will rewrite the file after this.
            time.sleep(3)
            break
        time.sleep(_POLL_S)

    _assert_single_injected_blip(rig, session_id)
    rollouts = _rollout_paths(rig, _EXT_SID_STALE)
    assert rollouts, f"the pre-seeded stale rollout disappeared (bind status={bind_status})"
    text = _rollout_response_item_text(rollouts[-1])
    missing = [c for c in canaries if text.count(c) == 0]
    duplicated = [c for c in canaries if text.count(c) > 1]
    assert not missing, (
        "cold resume KEPT the stale local rollout after a single transient "
        "/items 500, even though the same read succeeds on the next request: "
        f"{len(missing)}/{len(canaries)} committed messages are absent from "
        f"the resumed history (bind status={bind_status}, "
        f"last_task_error={task_error}). fault log: "
        f"{rig.fault_entries(session_id)}; runner log tail:\n"
        f"{rig.runner_log_tail()}"
    )
    assert not duplicated, (
        "refreshed rollout must retain every committed message exactly once; "
        f"duplicated={len(duplicated)} (first: {duplicated[:3]})"
    )
