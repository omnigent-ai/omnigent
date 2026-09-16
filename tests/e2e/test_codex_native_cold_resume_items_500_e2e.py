"""E2E regression test: Codex cold resume aborts when a large /items page 500s.

Reproduces the reported bug: cold-resuming a **large** codex-native
conversation on a host with no local Codex rollout aborts the native terminal
launch when the server's ``GET /v1/sessions/<id>/items?limit=1000`` fails on a
large page while the same history paginates fine at smaller page sizes. The
client chain:

* ``_fetch_all_session_items_for_codex_resume`` hardcodes ``limit=1000`` and
  raises ``_CodexResumeHistoryUnavailableError`` on the first 5xx/dropped
  response — no retry at a smaller page size (the claude-native resume path
  already halves the page limit and recovers);
* ``_ensure_local_codex_resume_rollout`` falls back to an existing local
  rollout — on a new host there is none, so the raise propagates;
* the runner's ``_auto_create_codex_terminal`` never writes the resume
  rollout and the native Codex terminal never starts.

This test drives the REAL user journey end to end — a real ``omnigent
server`` subprocess (with the failure injected at the conversation-store seam
so the real route + exception handler produce the production 500 body), a
real runner subprocess, a real codex-native session with 600 seeded history
items and a bound prior Codex thread id — and then triggers the resume the
way the web UI / a daemon relaunch does: binding the session to the runner,
which auto-creates the Codex terminal on a host with no local rollout.

Desired behavior (asserted): the history IS recoverable — every page at
``limit<=400`` serves fine (600 items = three pages at limit 250) — so the
resume must rebuild the local rollout from server history, carrying all 600
messages exactly once in chronological order, and then launch the terminal.
On the buggy build the first ``limit=1000`` page 500s, the fetch aborts, the
rollout is never written, and the terminal never starts, so this test FAILS.

The Codex CLI itself is replaced with a tiny stub that answers ``--version``
and parks, so the test needs no Codex login and asserts on the rollout
rebuild — the exact seam where the aborted resume kills the terminal launch.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_codex_native_cold_resume_items_500_e2e.py -v
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy in the environment; every HTTP call in
# this test targets 127.0.0.1, so bypass proxy autodetection entirely.
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

# Bootstrap for the spawned server: monkeypatch the conversation store so
# item pages ABOVE the deployed failure threshold raise — the REAL route and
# the REAL app-level exception handler then produce the production
# ``500 {"error":{"code":"internal_error",...}}`` body — while smaller pages
# keep succeeding. Same signature as the claude-native counterpart test:
# 200 at <=400, 500 at >=500 (and at the client's hardcoded 1000).
_SERVER_BOOTSTRAP = """
import omnigent.stores.conversation_store.sqlalchemy_store as _s

_orig = _s.SqlAlchemyConversationStore.list_items

def _failing_list_items(self, conversation_id, limit=100, *args, **kwargs):
    if limit > 400:
        raise RuntimeError(
            "simulated deployed-DB failure reading a large item page "
            f"(limit={limit})"
        )
    return _orig(self, conversation_id, limit, *args, **kwargs)

_s.SqlAlchemyConversationStore.list_items = _failing_list_items

from omnigent.cli import main

main()
"""

# The prior Codex thread id bound to the conversation (what a previous run
# persisted as ``external_session_id``). UUIDv7-shaped like real thread ids.
_EXTERNAL_SID = "01920000-0000-7000-8000-000000000001"

_SEEDED_ITEMS = 600

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0
# Terminal auto-create includes launch-catalog probes against the parked
# stub before the resume rollout is ensured; generous for CI.
_ROLLOUT_TIMEOUT_S = 240.0

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
    """Subprocess env with worktree imports and no proxy in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        # CI shells often carry an egress proxy; localhost must bypass it.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
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


def _runner_log_tail(runner_home: Path, fallback: Path) -> str:
    """Tail of the runner's real log for assertion failure messages.

    The runner writes its log under ``$HOME/.omnigent/logs/runner``, not to
    stdout; *fallback* is the (usually empty) captured-stdout file.

    :param runner_home: The runner subprocess's hermetic ``$HOME``.
    :param fallback: The captured runner stdout/stderr file.
    :returns: The last ~4000 characters of the newest available log.
    """
    logs_dir = runner_home / ".omnigent" / "logs" / "runner"
    candidates = sorted(logs_dir.glob("*.log")) if logs_dir.is_dir() else []
    source = candidates[-1] if candidates else fallback
    try:
        return f"[{source}]\n{source.read_text(errors='replace')[-4000:]}"
    except OSError as exc:
        return f"[no runner log readable: {exc}]"


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


def _seed_large_history(database_uri: str, session_id: str) -> None:
    """Append ~9MB / 600 message items straight into the server's store.

    Multi-MB transcript, hundreds of items — three pages at the reduced
    ``limit=250`` a recovering client would use. Direct store writes are the
    same seeding pattern the ``tests/e2e_ui`` suite uses — there is no REST
    bulk-append.

    :param database_uri: The spawned server's SQLite URI.
    :param session_id: Conversation to append to.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    store = SqlAlchemyConversationStore(database_uri)
    chunk = "x" * 15000
    items = []
    for i in range(_SEEDED_ITEMS):
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
                            "text": f"turn {i}: {chunk}",
                        }
                    ],
                    agent="codex" if role == "assistant" else None,
                ),
            )
        )
    for start in range(0, len(items), 50):
        store.append(session_id, items[start : start + 50])


def _find_resume_rollout(runner_home: Path, session_id: str) -> Path | None:
    """Locate the cold-resume rollout the runner rebuilds for *session_id*.

    The runner-owned bridge keys by session id (no bridge-id label), so the
    per-session ``CODEX_HOME`` lives at a digest path under the runner's
    ``~/.omnigent/codex-native``.

    :param runner_home: The runner subprocess's hermetic ``$HOME``.
    :param session_id: Omnigent conversation id.
    :returns: The rollout JSONL path, or ``None`` while absent.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    sessions_dir = runner_home / ".omnigent" / "codex-native" / digest / "codex-home" / "sessions"
    if not sessions_dir.is_dir():
        return None
    for path in sorted(sessions_dir.rglob(f"rollout-*-{_EXTERNAL_SID}.jsonl")):
        return path
    return None


def _seeded_turns_in_rollout(rollout: Path) -> list[int]:
    """Extract the seeded turn numbers carried by the rollout's history.

    Reads the canonical ``response_item`` message records (each message also
    gets an ``event_msg`` mirror, which is not history) and returns the
    ``turn <n>:`` markers in file order.

    :param rollout: The rebuilt rollout JSONL.
    :returns: Turn numbers in the order the rollout carries them.
    """
    turns: list[int] = []
    marker = re.compile(r"^turn (\d+): ")
    for line in rollout.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            match = marker.match(str(block.get("text") or ""))
            if match is not None:
                turns.append(int(match.group(1)))
    return turns


def test_cold_resume_rebuilds_rollout_when_large_item_page_500s(
    tmp_path: Path,
) -> None:
    """
    Cold resume must rebuild the rollout when /items fails only at large pages.

    Journey (the reporter's): a large codex-native conversation exists on
    the server with a bound prior Codex thread id; this host has no local
    Codex rollout for that thread; the server 500s on ``/items`` pages above
    the deployed size threshold while smaller pages succeed; the user
    resumes the session (relaunch -> the runner auto-creates the Codex
    terminal).

    Expected: the history is recoverable (small pages work), so the resume
    must retry the failed cursor at bounded smaller pages, rebuild the local
    rollout with all 600 messages exactly once in chronological order, and
    proceed to launch the terminal. Buggy behavior: the hardcoded
    ``limit=1000`` fetch 500s, ``_CodexResumeHistoryUnavailableError``
    aborts the resume with no local-rollout fallback available, the rollout
    is never written, and the native terminal never starts.

    :param tmp_path: Per-test temp dir (server DB, stub codex, runner HOME).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()

    # Stub Codex CLI: answers the version probe (the resume rollout stamps
    # ``cli_version``) and parks, so the launch needs no Codex login. The
    # rollout rebuild under test happens before any Codex process must
    # respond, so the parked stub never blocks the asserted seam.
    argv_file = tmp_path / "codex_argv.txt"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "codex"
    stub.write_text(
        "#!/bin/sh\n"
        f"{{ printf '%s\\037' \"$@\"; printf '\\n'; }} >> \"{argv_file}\"\n"
        'if [ "$1" = "--version" ]; then\n'
        '  echo "codex-cli 0.136.0"\n'
        "  exit 0\n"
        "fi\n"
        "exec sleep 600\n"
    )
    stub.chmod(0o755)

    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    server_log = (tmp_path / "server.log").open("w")
    runner_log = (tmp_path / "runner.log").open("w")
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
            env=_localhost_env({"OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}),
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
                    # Hermetic HOME: the bridge CODEX_HOME lives under
                    # ``$HOME/.omnigent/codex-native`` and provider config
                    # resolves from ``$HOME/.omnigent`` — keep both off the
                    # real HOME, and guarantee no local rollout pre-exists
                    # (the new-host cold-resume precondition).
                    "HOME": str(runner_home),
                    # tests/conftest.py points OMNIGENT_DATA_DIR (where logs
                    # land) at a shared temp; pin it per-test under HOME so
                    # the runner traceback is co-located and findable.
                    "OMNIGENT_DATA_DIR": str(runner_home / ".omnigent"),
                    # The stub shadows any real codex on PATH.
                    "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
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
                # The server/runner is still booting; transient connection
                # errors are expected while polling and simply retried.
                pass
            time.sleep(_POLL_S)
        assert online, (
            f"runner never came online; log:\n"
            f"{_runner_log_tail(runner_home, tmp_path / 'runner.log')}"
        )

        # A prior large codex-native conversation with the Codex thread id
        # captured — the state a user cold-resumes into on a new host.
        session_id = _create_codex_native_session(base_url)
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"external_session_id": _EXTERNAL_SID},
            timeout=10.0,
        ).raise_for_status()
        _seed_large_history(database_uri, session_id)

        # Sanity: the deployed failure signature is live — small pages serve,
        # the client's hardcoded limit=1000 page 500s.
        ok = _http.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "asc"},
            timeout=60.0,
        )
        assert ok.status_code == 200, "small item pages must keep working"
        assert len(ok.json()["data"]) == 100
        big = _http.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 1000, "order": "asc"},
            # Uvicorn closes this connection after logging the injected error.
            # Keep the subsequent resume off a socket still being torn down.
            headers={"Connection": "close"},
            timeout=60.0,
        )
        assert big.status_code == 500, "large-page failure signature must be live"
        assert big.json()["error"]["code"] == "internal_error"

        # THE RESUME: bind the session to the runner (what the web UI / a
        # daemon relaunch does) -> the runner auto-creates the Codex
        # terminal, which must rebuild the local rollout from server history
        # before launch. The bind can block on (or fail with) the runner-side
        # bring-up, so don't raise here — the rollout assertion below is the
        # verdict either way.
        with contextlib.suppress(httpx.HTTPError):
            _http.patch(
                f"{base_url}/v1/sessions/{session_id}",
                json={"runner_id": runner_id},
                timeout=_ROLLOUT_TIMEOUT_S,
            )

        deadline = time.monotonic() + _ROLLOUT_TIMEOUT_S
        rollout: Path | None = None
        while time.monotonic() < deadline:
            rollout = _find_resume_rollout(runner_home, session_id)
            if rollout is not None:
                break
            time.sleep(_POLL_S)

        # The bug: with the item history fully recoverable at smaller page
        # sizes, the resume must rebuild the rollout and start the terminal.
        # The buggy build aborts on the first limit=1000 page 500 and, with
        # no local rollout on this host, never writes it — the native Codex
        # terminal never starts.
        assert rollout is not None, (
            "codex cold resume ABORTED after the limit=1000 /items page "
            "500'd, even though the same history serves fine at limit<=400 "
            "— no resume rollout was rebuilt on this rollout-less host, so "
            "the native Codex terminal can never start. runner log:\n"
            f"{_runner_log_tail(runner_home, tmp_path / 'runner.log')}"
        )
        turns = _seeded_turns_in_rollout(rollout)
        assert turns == list(range(_SEEDED_ITEMS)), (
            "rebuilt rollout must carry the full server history exactly "
            "once in chronological order; got "
            f"{len(turns)} markers (first 20: {turns[:20]})"
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()
