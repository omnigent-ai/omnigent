"""E2E: replacing a drifted Claude session by id refreshes its snapshot.

Runs a real host daemon with a seeded Claude transcript against the real Settings > Import UI."""

from __future__ import annotations

import json
import os
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

pytestmark = pytest.mark.min_server_version("0.16.0")

_REPO_ROOT = Path(__file__).resolve().parents[3]

_FIRST_USER = "set up the repo scaffolding"
_DRIFT_USER = "continue: wire up the follow-up turn from a month later"

# Pause after each asserted state so a recording of the journey stays readable.
_HOLD_MS = 1500

_HOST_ENV_STRIP_PREFIXES = ("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")
_HOST_ENV_STRIP = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "QWEN_HOME",
    "PI_CODING_AGENT_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_DATA_DIR",
    "RUNNER_SERVER_URL",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
)


def _turn_records(session_id: str, turns: list[tuple[str, str]]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for index, (role, text) in enumerate(turns):
        if role == "user":
            records.append(
                {
                    "type": "user",
                    "uuid": f"{session_id}-user-{index}",
                    "cwd": "/repo",
                    "message": {"role": "user", "content": text},
                }
            )
        else:
            records.append(
                {
                    "type": "assistant",
                    "uuid": f"{session_id}-assistant-{index}",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                }
            )
    return records


def _write_transcript(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record))
            handle.write("\n")


@dataclass
class _ImportHost:
    host_id: str
    host_name: str
    proc: subprocess.Popen[bytes]
    daemon_log: Path
    session_id: str
    transcript: Path


def _wait_for_host_online(live_server: str, host_id: str, timeout: float = 60.0) -> None:
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
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(f"host {host_id} never came online; last /v1/hosts: {last!r}")


@pytest.fixture
def import_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_ImportHost]:
    """Spawn a real host daemon owning one small Claude session as $HOME."""
    home = tmp_path_factory.mktemp("drift_import_host_home")
    session_id = str(uuid.uuid4())
    transcript = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    _write_transcript(
        transcript,
        _turn_records(session_id, [("user", _FIRST_USER), ("assistant", "On it.")]),
    )

    host_id = uuid.uuid4().hex
    host_name = f"drift-import-host-{uuid.uuid4().hex[:8]}"
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

    yield _ImportHost(
        host_id=host_id,
        host_name=host_name,
        proc=proc,
        daemon_log=daemon_log,
        session_id=session_id,
        transcript=transcript,
    )

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _import_by_id(
    page: Page, live_server: str, host: _ImportHost, *, replace: bool = False
) -> None:
    page.goto(f"{live_server}/settings/import")
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-host-select").click()
    page.get_by_role("option").filter(has_text=host.host_name).click()
    page.get_by_test_id("import-mode-select").click()
    page.get_by_role("option", name="Session by ID").click()
    page.get_by_test_id("import-source-select").click()
    page.get_by_role("option", name="Claude Code").click()
    page.get_by_test_id("import-session-id").fill(host.session_id)
    if replace:
        page.get_by_test_id("import-replace-toggle").click()
    page.get_by_test_id("import-submit").click()
    if replace:
        expect(page.get_by_role("dialog")).to_contain_text("Omnigent-only changes")
        page.wait_for_timeout(_HOLD_MS)
        page.get_by_test_id("import-replace-confirm").click()


@pytest.mark.timeout(240)
def test_import_by_id_refreshes_drifted_session(
    request: pytest.FixtureRequest,
    live_server: str,
    import_host: _ImportHost,
) -> None:
    """Import by id, drift on the host, re-import (skipped), replace, resume."""
    # Non-browser setup (host online) is done; start filming the user journey.
    page = request.getfixturevalue("page")

    _import_by_id(page, live_server, import_host)
    result = page.get_by_test_id("import-result")
    expect(result).to_be_visible(timeout=120_000)
    expect(result).to_contain_text("Imported 1")
    session_link = page.get_by_test_id("import-result-sessions").get_by_role("link")
    session_url = session_link.get_attribute("href")
    assert session_url, "imported session was not linked in the result list"
    page.wait_for_timeout(_HOLD_MS)

    # The user keeps chatting with the session on the host: its transcript drifts.
    _write_transcript(
        import_host.transcript,
        _turn_records(
            import_host.session_id,
            [
                ("user", _FIRST_USER),
                ("assistant", "On it."),
                ("user", _DRIFT_USER),
                ("assistant", "Wired up."),
            ],
        ),
    )

    # A plain re-import is deduplicated; the stale snapshot is kept untouched.
    _import_by_id(page, live_server, import_host)
    result = page.get_by_test_id("import-result")
    expect(result).to_be_visible(timeout=120_000)
    expect(result).to_contain_text("Imported 0, 1 already imported")
    page.wait_for_timeout(_HOLD_MS)

    # Replacing the snapshot pulls the latest transcript into the same session.
    _import_by_id(page, live_server, import_host, replace=True)
    result = page.get_by_test_id("import-result")
    expect(result).to_be_visible(timeout=120_000)
    expect(result).to_contain_text("Imported 1")
    replaced_link = page.get_by_test_id("import-result-sessions").get_by_role("link")
    assert replaced_link.get_attribute("href") == session_url
    page.wait_for_timeout(_HOLD_MS)

    page.goto(f"{live_server}{session_url}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    transcript = page.get_by_role("log")
    expect(transcript.get_by_text(_FIRST_USER)).to_be_visible(timeout=30_000)
    expect(transcript.get_by_text(_DRIFT_USER)).to_be_visible(timeout=10_000)
    page.wait_for_timeout(_HOLD_MS)
