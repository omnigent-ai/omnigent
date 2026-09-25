"""E2E: import flows must survive super-large local sessions.

The Settings › Import flow reads harness transcripts on a real connected host
and streams them to the server one WebSocket frame per session
(``host.import_local_session``). If a "super large" session rode in a single
tunnel frame past the server's WebSocket message cap
(``RUNNER_TUNNEL_MAX_MESSAGE_BYTES``), the server would drop the host tunnel,
the import stream would wait out its 60s per-frame timeout, and the whole
batch would die. The user-visible failures this suite guards against:

* the oversized session itself can never be imported, and
* sessions newer than it never import either — the web flow does not
  continue past the single failing session, and
* the user stares at "Importing…" through the full 60s stall before one
  wholesale error replaces the tally.

Unlike ``test_import_sessions.py`` (which stubs the import endpoints), these
tests run the REAL path end to end: they spawn an actual host daemon
(``omnigent.host._daemon_entry``) against the live server, seed real Claude
Code transcripts under the daemon's ``$HOME/.claude``, and drive the real
Settings › Import UI. The assertions encode the DESIRED behavior, so they
fail on a build with the bug and pass once imports survive oversized
sessions.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.runner.transports.ws_tunnel.limits import RUNNER_TUNNEL_MAX_MESSAGE_BYTES

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Claude Code source session ids seeded on the host, oldest → newest. The
# oversized one sits in the middle so a batch import has real sessions on
# both sides of the failure.
_OLD_SESSION_ID = "8c1d0e0a-aaaa-4aaa-8aaa-000000000001"
_GIANT_SESSION_ID = "8c1d0e0a-bbbb-4bbb-8bbb-000000000002"
_NEW_SESSION_ID = "8c1d0e0a-cccc-4ccc-8ccc-000000000003"

# First-user-message texts double as the imported sessions' synthesized
# titles, which the import panel's result list renders.
_OLD_TITLE = "inspect oversized-import OLD.md"
_NEW_TITLE = "inspect oversized-import NEW.md"

# Ambient env that must not leak into the spawned host daemon: harness config
# overrides would redirect the transcript scan away from the seeded $HOME,
# and runner/host identity vars would make omnigent subprocesses take the
# wrong startup paths.
_HOST_ENV_STRIP_PREFIXES = ("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")
_HOST_ENV_STRIP = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "QWEN_HOME",
    "PI_CODING_AGENT_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_DATA_DIR",
    "RUNNER_SERVER_URL",
)


def _write_transcript(path: Path, records: list[dict[str, object]]) -> None:
    """Write one Claude Code JSONL transcript."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record))
            handle.write("\n")


def _small_session_records(session_id: str, title_text: str) -> list[dict[str, object]]:
    """A minimal importable two-message Claude transcript."""
    return [
        {
            "type": "user",
            "uuid": f"{session_id}-user-1",
            "cwd": "/repo",
            "message": {"role": "user", "content": title_text},
        },
        {
            "type": "assistant",
            "uuid": f"{session_id}-assistant-1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]


def _write_giant_transcript(path: Path, session_id: str) -> None:
    """Write a Claude transcript that normalizes past the tunnel message cap.

    Few items (nowhere near the 100k item cap), but their combined payload
    exceeds ``RUNNER_TUNNEL_MAX_MESSAGE_BYTES``, so the single
    ``host.import_local_session`` frame carrying the session is larger than
    the server accepts — the shape of a real months-long Claude session.
    """
    chunk = "x" * (4 * 1024 * 1024)
    n_chunks = RUNNER_TUNNEL_MAX_MESSAGE_BYTES // len(chunk) + 3
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        first: dict[str, object] = {
            "type": "user",
            "uuid": f"{session_id}-user-0",
            "cwd": "/repo",
            "message": {"role": "user", "content": "giant session start"},
        }
        handle.write(json.dumps(first))
        handle.write("\n")
        for index in range(n_chunks):
            record: dict[str, object] = {
                "type": "user",
                "uuid": f"{session_id}-user-{index + 1}",
                "cwd": "/repo",
                "message": {"role": "user", "content": chunk},
            }
            handle.write(json.dumps(record))
            handle.write("\n")


@dataclass
class _ImportHost:
    """A live host daemon whose $HOME carries the seeded Claude transcripts."""

    host_id: str
    host_name: str
    proc: subprocess.Popen[bytes]
    daemon_log: Path


def _seed_claude_home(home: Path) -> None:
    """Seed ``home/.claude`` with old-small, giant, new-small sessions."""
    projects = home / ".claude" / "projects" / "-repo"
    old = projects / f"{_OLD_SESSION_ID}.jsonl"
    giant = projects / f"{_GIANT_SESSION_ID}.jsonl"
    new = projects / f"{_NEW_SESSION_ID}.jsonl"
    _write_transcript(old, _small_session_records(_OLD_SESSION_ID, _OLD_TITLE))
    _write_giant_transcript(giant, _GIANT_SESSION_ID)
    _write_transcript(new, _small_session_records(_NEW_SESSION_ID, _NEW_TITLE))
    # Recency (mtime) drives enumeration order; the host streams oldest
    # first, so the giant session fails between the two small ones.
    now = time.time()
    os.utime(old, (now - 300, now - 300))
    os.utime(giant, (now - 200, now - 200))
    os.utime(new, (now - 100, now - 100))


def _wait_for_host_online(live_server: str, host_id: str, timeout: float = 60.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* reports online."""
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{live_server}/v1/hosts", timeout=5)
            if resp.status_code == 200:
                last = resp.json()
                hosts = last.get("hosts", []) if isinstance(last, dict) else []
                for host in hosts:
                    if host.get("host_id") == host_id and host.get("status") == "online":
                        return
        except httpx.HTTPError as exc:  # server still booting
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(f"host {host_id} never came online; last /v1/hosts: {last!r}")


@pytest.fixture(scope="module")
def import_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_ImportHost]:
    """Spawn a real host daemon with the seeded Claude transcripts as $HOME."""
    home = tmp_path_factory.mktemp("oversized_import_host_home")
    _seed_claude_home(home)

    host_id = uuid.uuid4().hex
    host_name = f"oversized-import-host-{uuid.uuid4().hex[:8]}"
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    (omni_dir / "config.yaml").write_text(
        json.dumps({"host": {"host_id": host_id, "name": host_name}}),
        encoding="utf-8",
    )

    env = {**os.environ}
    for key in list(env):
        if key in _HOST_ENV_STRIP or key.startswith(_HOST_ENV_STRIP_PREFIXES):
            env.pop(key, None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"

    daemon_log = home / "host-daemon.log"
    with daemon_log.open("w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for_host_online(live_server, host_id)
    except RuntimeError as exc:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        raise RuntimeError(f"{exc}; daemon log tail: {daemon_log.read_text()[-2000:]}") from exc

    yield _ImportHost(host_id=host_id, host_name=host_name, proc=proc, daemon_log=daemon_log)

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _open_import_panel(page: Page, live_server: str, host: _ImportHost) -> None:
    """Open Settings › Import and pick the seeded machine + Claude harness."""
    page.goto(f"{live_server}/settings/import")
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    # Pick OUR machine explicitly — the shared test server may know others.
    page.get_by_test_id("import-host-select").click()
    page.get_by_role("option").filter(has_text=host.host_name).click()


@pytest.mark.timeout(360)
def test_recent_import_continues_past_oversized_session(
    page: Page,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """A batch import must survive one oversized session.

    Journey: the machine has three recent Claude sessions — small (old),
    super-large (middle), small (new). The user imports recent sessions from
    Settings. Desired: the two small sessions import and a final tally
    renders. Buggy behavior this guards against: the oversized session's
    tunnel frame kills the host connection, the import stalls out the 60s
    frame timeout, the run ends in one wholesale error, and the newer small
    session is never imported.
    """
    _open_import_panel(page, live_server, import_host)
    page.get_by_test_id("import-source-select").click()
    page.get_by_role("option", name="Claude Code").click()

    started = time.monotonic()
    page.get_by_test_id("import-submit").click()

    # The oldest small session streams in first, proving the host read ran.
    expect(page.get_by_test_id("import-result-sessions")).to_contain_text(
        _OLD_TITLE, timeout=120_000
    )

    # DESIRED: the run completes with a tally; it must not abort wholesale.
    result = page.get_by_test_id("import-result")
    error = page.get_by_test_id("import-error")
    expect(result.or_(error)).to_be_visible(timeout=180_000)
    if error.is_visible():
        elapsed = time.monotonic() - started
        pytest.fail(
            "batch import aborted instead of continuing past the oversized "
            f"session (after {elapsed:.0f}s): {error.inner_text()!r}"
        )

    # DESIRED: the session newer than the oversized one still imported.
    expect(page.get_by_test_id("import-result-sessions")).to_contain_text(_NEW_TITLE)


@pytest.mark.timeout(420)
def test_import_by_id_handles_super_large_session(
    page: Page,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """Importing one super-large session by id must succeed.

    Journey: the user asks Settings › Import for the exact oversized Claude
    session by its id. Desired: the session imports ("Imported 1"; "already
    imported" when the batch test ran first on the shared server — either
    proves the oversized session is importable). Buggy behavior this guards
    against: the single oversized frame drops the tunnel and the user gets
    only a wholesale error after the 60s stall — the session can never be
    imported at all.
    """
    _open_import_panel(page, live_server, import_host)
    page.get_by_test_id("import-mode-select").click()
    page.get_by_role("option", name="Session by ID").click()
    page.get_by_test_id("import-session-id").fill(_GIANT_SESSION_ID)

    started = time.monotonic()
    page.get_by_test_id("import-submit").click()

    result = page.get_by_test_id("import-result")
    error = page.get_by_test_id("import-error")
    expect(result.or_(error)).to_be_visible(timeout=300_000)
    if error.is_visible():
        elapsed = time.monotonic() - started
        pytest.fail(
            "super-large session failed to import by id "
            f"(after {elapsed:.0f}s): {error.inner_text()!r}"
        )

    expect(result).to_contain_text(re.compile(r"Imported 1|already imported"))
