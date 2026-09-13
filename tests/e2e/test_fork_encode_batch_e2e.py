"""E2E: forking must not pay a per-item encode round-trip for every item.

Reported journey ("make forking in the server faster"): fork a long session
on a deployed server -> the fork request takes minutes.

``SqlAlchemyConversationStore`` documents the deployment contract on its
encode hooks: a store whose payload encode carries a per-call cost (e.g. one
encrypt RPC per call) overrides ``_encode_item_data_batch`` so a whole page
is transformed in ONE call -- and ``append`` honors that by batch-encoding
every turn's items up front, outside the write transaction, precisely so a
turn never pays N round-trips or holds the row lock across them.

``fork_conversation`` breaks that contract: it re-encodes the copied items
by calling the per-item ``_encode_item_data`` once per source item, INSIDE
the open copy transaction. On such a deployed store a 600-item fork pays 600
sequential encode round-trips while the user's fork dialog blocks -- at tens
of milliseconds per call, that alone is minutes for a real long session.

This test spawns the real server with a deployment-shaped store (both encode
hooks cost one simulated 20ms round-trip per CALL, matching the documented
contract), seeds a 600-item session, and drives the real
``POST /v1/sessions/{id}/fork``. It FAILS while the bug is live (600
per-item encode calls -> a 12s+ fork) and passes once the fork's copy stops
fanning out per-item encodes (batch encode, or a copy that never re-encodes).

Usage::

    pytest tests/e2e/test_fork_encode_batch_e2e.py -v
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every HTTP call targets 127.0.0.1; bypass any CI egress proxy.
_http = httpx.Client(trust_env=False)

_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

_HEALTH_TIMEOUT_S = 120.0
_N_ITEMS = 600
# Simulated per-call encode round-trip. Small enough to keep the failing run
# bounded (~12s), large enough that per-item fan-out cannot pass the budget.
_ENCODE_RPC_S = 0.02
# A fixed fork (one batch encode call) finishes well under a second here;
# the buggy fork pays _N_ITEMS * _ENCODE_RPC_S = 12s in encode calls alone.
_FORK_BUDGET_S = 8.0

# Bootstrap for the spawned server: shape the store like a deployed one whose
# encode is a per-call round-trip -- exactly the store the batch hooks are
# documented for. Each call (batch or per-item) costs one simulated RPC and
# appends one line to a counts file so the test can see which hook the fork
# used and how many round-trips the user's blocked request paid.
_SERVER_BOOTSTRAP = """
import os, time
import omnigent.stores.conversation_store.sqlalchemy_store as _s

_COUNTS = os.environ["FORK_E2E_ENCODE_COUNTS_FILE"]
_RPC_S = float(os.environ["FORK_E2E_ENCODE_RPC_SECONDS"])

def _record(kind):
    with open(_COUNTS, "a") as fh:
        fh.write(kind + "\\n")

def _per_item_encode(self, data_json):
    _record("item")
    time.sleep(_RPC_S)
    return data_json

def _batch_encode(self, data_jsons):
    _record("batch")
    time.sleep(_RPC_S)
    return list(data_jsons)

_s.SqlAlchemyConversationStore._encode_item_data = _per_item_encode
_s.SqlAlchemyConversationStore._encode_item_data_batch = _batch_encode

from omnigent.cli import main

main()
"""


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
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra)
    return env


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
        time.sleep(1.0)
    raise AssertionError(f"{url} never became healthy: {last}")


def _make_inline_bundle(name: str) -> bytes:
    """Build a minimal single-file agent bundle (no LLM turn ever runs).

    :param name: Agent name for the spec.
    :returns: A gzipped tarball with ``<name>.yaml``.
    """
    config = {
        "name": name,
        "prompt": "you are a test agent",
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o",
            "profile": "DEFAULT",
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": "http://127.0.0.1:9/v1",
            },
        },
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml.dump(config).encode()
        info = tarfile.TarInfo(f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _seed_history(database_uri: str, session_id: str, n_items: int) -> None:
    """Append *n_items* small message items straight into the server's store.

    Runs in the TEST process (unpatched store), so seeding pays none of the
    spawned server's simulated encode cost. Direct store writes are the same
    seeding pattern the ``tests/e2e_ui`` suite uses -- there is no REST
    bulk-append.

    :param database_uri: The spawned server's SQLite URI.
    :param session_id: Conversation to append to.
    :param n_items: Number of alternating user/assistant messages.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    store = SqlAlchemyConversationStore(database_uri)
    items = []
    for i in range(n_items):
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
                            "text": f"turn {i}: content that a fork must copy",
                        }
                    ],
                    agent="bench" if role == "assistant" else None,
                ),
            )
        )
    for start in range(0, len(items), 100):
        store.append(session_id, items[start : start + 100])


def test_fork_does_not_pay_per_item_encode_round_trips(tmp_path: Path) -> None:
    """Fork a 600-item session -- the blocked request must not pay 600 RPCs.

    Journey: a user with a long session clicks fork; the fork dialog blocks
    on ``POST /v1/sessions/{id}/fork``. On a deployed store whose encode is
    a per-call round-trip, the fork's per-item encode fan-out multiplies the
    user's wait by the item count -- the reported minutes-long fork.

    Failure modes this catches:

    - The fork re-encodes copied items one call per item (600 "item" lines
      in the counts file and a 12s+ request at 20ms per call), instead of
      one batch call like ``append`` -- the documented store contract.

    :param tmp_path: Per-test temp dir (server DB, artifacts, counts file).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "e2e.db"
    database_uri = f"sqlite:///{db_path}"
    counts_file = tmp_path / "encode_calls.txt"
    counts_file.write_text("")

    server_log = (tmp_path / "server.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
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
                    "FORK_E2E_ENCODE_COUNTS_FILE": str(counts_file),
                    "FORK_E2E_ENCODE_RPC_SECONDS": str(_ENCODE_RPC_S),
                }
            ),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        # A long-lived source session: agent-bound (fork requires it), with
        # a 600-item committed history.
        create = _http.post(
            f"{base_url}/v1/sessions",
            data={"metadata": json.dumps({})},
            files={
                "bundle": (
                    "agent.tar.gz",
                    _make_inline_bundle("fork-encode-bench"),
                    "application/gzip",
                )
            },
            timeout=30.0,
        )
        assert create.status_code in (200, 201), create.text[:500]
        session_id = str(create.json()["session_id"])
        _seed_history(database_uri, session_id, _N_ITEMS)

        # Only the fork's own encode traffic counts.
        counts_file.write_text("")

        started = time.monotonic()
        fork = _http.post(
            f"{base_url}/v1/sessions/{session_id}/fork",
            json={},
            timeout=300.0,
        )
        elapsed = time.monotonic() - started
        assert fork.status_code == 201, f"fork failed: {fork.status_code} {fork.text[:500]}"

        calls = counts_file.read_text().splitlines()
        per_item_calls = sum(1 for line in calls if line == "item")
        batch_calls = sum(1 for line in calls if line == "batch")
        print(
            f"fork of {_N_ITEMS}-item session took {elapsed:.2f}s; encode "
            f"calls during fork: {per_item_calls} per-item, {batch_calls} batch"
        )

        # Regression pins. Per-item encode fan-out is the deployment
        # multiplier that turns a fork into minutes: N round-trips inside
        # the user's blocked request (append pays exactly one batch call
        # for the same items). A fixed fork encodes in O(1) calls -- or
        # not at all, if the copy reuses the already-encoded payloads.
        assert per_item_calls <= 2, (
            f"fork paid {per_item_calls} per-item encode round-trips for a "
            f"{_N_ITEMS}-item source (one call per copied item, inside the "
            f"copy transaction); on a deployed store each call is a real "
            f"round-trip, so fork time scales linearly into minutes"
        )
        assert elapsed < _FORK_BUDGET_S, (
            f"fork of a {_N_ITEMS}-item session took {elapsed:.1f}s with a "
            f"{_ENCODE_RPC_S * 1000:.0f}ms/call encode -- the user-blocked "
            f"request is paying per-item round-trips "
            f"(expected < {_FORK_BUDGET_S:.0f}s)"
        )
    finally:
        if server_proc is not None and server_proc.poll() is None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
        server_log.close()
