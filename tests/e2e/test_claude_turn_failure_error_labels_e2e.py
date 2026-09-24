"""E2E regression: a claude-native turn failure must keep its Claude
error category and must not be mislabeled as a Codex error.

Guarded bug
-----------
On a claude-native session Claude reports a failing turn through a
``StopFailure`` hook that carries an ``error`` category (e.g.
``authentication_failed``, ``model_not_found``, ``rate_limit``). Two things go
wrong before the failure reaches the web UI:

* **Facet A -- category lost.** The bridge's ``_hook_record_from_jsonl_record``
  never reads the hook's ``error`` field, and the forwarder's
  ``_forward_available_status_events`` posts ``failed`` with no ``output``. So
  the session ends as a bare ``failed`` with no cause: ``last_task_error`` is
  null (the SPA error card has nothing to show) and the server logs
  ``session turn failed ...: no detail``. The category the user needs to act on
  (re-auth, wrong model, rate limit) is gone.
* **Facet B -- Codex misattribution.** When a failure *does* carry wire output,
  the events route hardcodes the error code to ``codex_turn_error`` for any
  wrapper session, so a Claude session's failure is attributed to Codex.

Both facets are user-observable on the web SPA failure card, which renders the
session snapshot's ``last_task_error`` (``GET /v1/sessions/<id>``).

Environment fidelity
--------------------
This drives the REAL runner->server contract: a real ``omnigent server``
subprocess (so the real ``POST /v1/sessions/<id>/events`` route and status
persistence run), a real claude-native wrapper session, and -- for Facet A --
the real ``forward_claude_transcript_to_session`` loop tailing a seeded native
Claude bridge whose ``StopFailure`` hook carries an ``error`` category the way a
live Claude CLI writes it. The failure is induced at the exact production seam
(the hook the CLI emits on an errored turn; the wire status a forwarder POSTs)
rather than by driving an interactive Claude login, so this reproduces the
reported mechanism on the same product code path.

Desired behavior (asserted):

* Facet A: the failed session carries a cause derived from the Claude error
  category -- ``last_task_error`` is non-null and reflects ``authentication_failed``.
  Buggy behavior: ``last_task_error`` is null (bare ``failed``) -- this FAILS.
* Facet B: a claude-native failure's error code is harness-neutral, not
  ``codex_turn_error``. Buggy behavior: the code is ``codex_turn_error`` on a
  Claude session -- this FAILS.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_turn_failure_error_labels_e2e.py -v

No ``--llm-api-key`` / ``--profile`` needed -- no LLM is invoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
_http = httpx.Client(trust_env=False)

# The spawned server resolves worktree imports from the repo root and the SDKs.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

_SERVER_BOOTSTRAP = "from omnigent.cli import main\n\nmain()\n"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5

# Facet A: the category a StopFailure hook carries; the fix must surface it.
_STOP_FAILURE_CATEGORY = "authentication_failed"

# Facet B: a generic Claude turn failure with no HTTP status / rate-limit
# keywords, so the classifier does not refine the base code away (a rate-limit
# message would become ``rate_limit_exceeded`` and mask the Codex mislabel).
_CLAUDE_FAILURE_OUTPUT = "Claude Code hit an unrecoverable error while completing this turn."


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy/credentials in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # Header auth + single-user keeps the spawned server out of login mode;
        # ambient auth/OIDC vars would otherwise 401 every call.
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    for name in list(env):
        if (
            name.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or name.endswith("_SECRET")
            or name
            in (
                "ANTHROPIC_API_KEY",
                "OMNIGENT_AUTH_ENABLED",
                "OMNIGENT_RUNNER_TUNNEL_TOKEN",
            )
        ):
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


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the created session is a real
    claude-native conversation -- the kind whose failures the runner->server
    contract carries in production.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat translator
        # (the wrapper spec has no ``spec_version``).
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _get_snapshot(base_url: str, session_id: str) -> dict:
    """Fetch the session snapshot the SPA reads (status + last_task_error)."""
    resp = _http.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="module")
def claude_native_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """Spawn one real ``omnigent server`` subprocess for the module.

    :yields: ``(base_url, server_log_path)`` for the running server.
    """
    tmp = tmp_path_factory.mktemp("claude-turn-failure-server")
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    database_uri = f"sqlite:///{tmp / 'chat.db'}"
    server_log_path = tmp / "server.log"
    server_log = server_log_path.open("w")
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
                str(tmp / "artifacts"),
            ],
            env=_localhost_env({}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)
        yield base_url, server_log_path
    finally:
        _terminate(server_proc)
        server_log.close()


async def _drive_forwarder_through_stop_failure(
    base_url: str, session_id: str, bridge_dir: Path
) -> None:
    """Run the real forwarder loop over a seeded StopFailure hook.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation the forwarder mirrors into.
    :param bridge_dir: Seeded native Claude bridge directory.
    """
    import omnigent.harnesses.claude_native.forwarder as fwd

    task = asyncio.create_task(
        fwd.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.02,
        )
    )
    try:
        await asyncio.sleep(5.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _seed_stop_failure_hook(bridge_dir: Path) -> Path:
    """Seed a native Claude bridge with a StopFailure hook carrying a category.

    Writes an empty transcript (Claude persisted no explanation for the errored
    turn) and records a ``SessionStart`` then a ``StopFailure`` hook whose
    ``error`` field carries the Claude error category, exactly as the live CLI's
    hook payload does on an errored turn.

    :param bridge_dir: Native Claude bridge directory.
    :returns: The transcript path.
    """
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session-stopfailure",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "claude-session-stopfailure",
            "error": _STOP_FAILURE_CATEGORY,
            "transcript_path": str(transcript_path),
        },
    )
    return transcript_path


@pytest.mark.timeout(300)
def test_claude_stop_failure_category_reaches_failure_cause(
    claude_native_server: tuple[str, Path], tmp_path: Path
) -> None:
    """Facet A: a Claude StopFailure category must survive to the failure cause.

    Journey: on a claude-native session Claude's turn fails and its CLI emits a
    ``StopFailure`` hook carrying an ``authentication_failed`` category. The
    forwarder mirrors that failure to the server, and the user opens the web
    view expecting the failure card to say *why* it failed.

    Expected: the session ends ``failed`` with a cause derived from the Claude
    error category (``last_task_error`` non-null, referencing authentication).
    Buggy behavior: the bridge drops the hook's ``error`` category and the
    forwarder posts ``failed`` with no output, so ``last_task_error`` is null --
    a bare ``failed`` with ``no detail`` in the server log. This test FAILS.

    :param claude_native_server: Running server ``(base_url, server_log_path)``.
    :param tmp_path: Per-test temp dir (workspace + bridge root).
    """
    base_url, server_log_path = claude_native_server
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir: Path | None = None
    try:
        session_id = _create_claude_native_session(base_url)

        from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

        bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)
        _seed_stop_failure_hook(bridge_dir)

        asyncio.run(_drive_forwarder_through_stop_failure(base_url, session_id, bridge_dir))

        snapshot = _get_snapshot(base_url, session_id)
        server_tail = server_log_path.read_text()[-2000:]

        assert snapshot.get("status") == "failed", (
            "seed invariant: the StopFailure hook must fail the session; "
            f"status={snapshot.get('status')!r}. server log tail:\n{server_tail}"
        )

        last_task_error = snapshot.get("last_task_error")
        assert last_task_error is not None, (
            "Claude reported a StopFailure carrying an "
            f"'{_STOP_FAILURE_CATEGORY}' category, but the session failed with "
            "no cause: last_task_error is null (a bare 'failed'; the server logs "
            "'session turn failed ...: no detail'). The bridge never reads the "
            "hook's 'error' category and the forwarder posts 'failed' with no "
            f"output, so the category is lost. server log tail:\n{server_tail}"
        )

        serialized = json.dumps(last_task_error).lower()
        assert "auth" in serialized, (
            f"the StopFailure category '{_STOP_FAILURE_CATEGORY}' was not "
            f"surfaced in the failure cause: last_task_error={last_task_error!r}"
        )
    finally:
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.timeout(300)
def test_claude_native_failure_not_labeled_as_codex(
    claude_native_server: tuple[str, Path],
) -> None:
    """Facet B: a claude-native failure must not be attributed to Codex.

    Journey: on a claude-native session a turn fails with wire-supplied output,
    the runner->server contract POSTs that ``failed`` status, and the user opens
    the web view; the failure card's error code identifies the harness.

    Expected: the persisted error code is harness-neutral (not
    ``codex_turn_error``) for a Claude session. Buggy behavior: the events route
    hardcodes ``codex_turn_error`` for any wire-supplied failure output, so a
    Claude session's failure is mislabeled as a Codex error. This test FAILS.

    :param claude_native_server: Running server ``(base_url, server_log_path)``.
    """
    base_url, _ = claude_native_server
    session_id = _create_claude_native_session(base_url)

    body = {
        "type": "external_session_status",
        "data": {
            "status": "failed",
            "response_id": "r1",
            "output": _CLAUDE_FAILURE_OUTPUT,
        },
    }
    resp = _http.post(f"{base_url}/v1/sessions/{session_id}/events", json=body, timeout=15.0)
    resp.raise_for_status()
    time.sleep(0.5)

    snapshot = _get_snapshot(base_url, session_id)
    last_task_error = snapshot.get("last_task_error")
    assert last_task_error is not None, (
        "seed invariant: the failed status with output must persist an error "
        f"cause; last_task_error is null. snapshot status={snapshot.get('status')!r}"
    )

    assert last_task_error.get("code") != "codex_turn_error", (
        "a claude-native session's turn failure was mislabeled as a Codex "
        f"error: last_task_error.code={last_task_error.get('code')!r}. The "
        "events route hardcodes 'codex_turn_error' for any wire-supplied "
        "failure output regardless of harness; a Claude failure should carry a "
        "harness-neutral code (e.g. 'native_turn_error')."
    )
