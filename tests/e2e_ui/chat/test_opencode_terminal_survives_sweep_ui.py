"""Web-lane recording driver — the ``opencode:main`` terminal survives a sweep.

Recording driver (an ``after`` clip on the ``terminal`` surface) for the fix
whose durable regression guard is
``tests/e2e/test_opencode_terminal_survives_orphan_sweep_e2e.py``. It stands up
a real runner-direct ``opencode-native-ui`` session so the runner auto-creates
the live ``opencode:main`` terminal, shows that terminal in the SPA's Terminal
view, then drives the condition that used to take it down: a *foreign* runner's
startup orphan sweep reads the terminal's owner marker as a pid it cannot place
(the dir is stamped with a pid that is dead in this namespace — what a marker
written in another pid namespace or boot reads as locally) and runs
``reap_orphaned_terminals``. The namespace-blind sweep killed the live tmux
server here, the idle watcher logged ``tmux unavailable after 3 consecutive
probes for terminal opencode:main`` and the pane vanished from the session; the
pid-domain-aware sweep leaves it alone. The Playwright video captures the
OpenCode terminal pane present, the sweep running, and the pane still alive
afterwards.

Run under the e2e_ui recorder (spawns its own local server + runner; this test
uses the pytest-playwright ``page`` fixture, so ``--video on`` records it)::

    OMNIGENT_E2E_OPENCODE_NATIVE=1 \\
    pytest tests/e2e_ui/chat/test_opencode_terminal_survives_sweep_ui.py \\
        --ui-skip-build -p no:randomly --video on --output recordings/opencode-sweep
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.inner.terminal import (
    _IDLE_EXIT_FAILURE_THRESHOLD,
    _IDLE_POLL_INTERVAL_SECONDS,
    _OWNER_PID_FILENAME,
)
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_OPENCODE_NATIVE") != "1" or shutil.which("opencode") is None,
    reason=(
        "opencode-native sweep-survival recording needs a pinned `opencode` binary; "
        "set OMNIGENT_E2E_OPENCODE_NATIVE=1 to run"
    ),
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TERMINAL_APPEAR_TIMEOUT_S = 90.0
# Past the idle watcher's whole probe budget (3 failed probes, one per poll
# interval) plus margin: a killed tmux server MUST have tripped it in here.
_SURVIVE_OBSERVE_MS = int(
    max(8.0, _IDLE_EXIT_FAILURE_THRESHOLD * _IDLE_POLL_INTERVAL_SECONDS * 3) * 1000
)


def _create_native_opencode_session(base_url: str, runner_id: str) -> str:
    """Register the ``opencode-native`` wrapper agent and bind its session.

    Mirrors :func:`tests.e2e_ui.conftest._create_native_goose_session` for
    opencode: reuses the terminal-first spec ``omnigent opencode`` ships and
    stamps the wrapper / terminal-first labels. Binding triggers the runner's
    opencode-native auto-bootstrap (``_auto_create_opencode_terminal``), which
    launches ``opencode serve`` + ``opencode attach`` in the session terminal.
    """
    from omnigent._wrapper_labels import (
        OPENCODE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )

    try:  # current layout
        from omnigent.harnesses.opencode_native.main import _materialize_opencode_agent_spec
    except ImportError:  # pre-harnesses-package layout
        from omnigent.opencode_native import _materialize_opencode_agent_spec

    with tempfile.TemporaryDirectory() as _tmp:
        spec_path = _materialize_opencode_agent_spec(Path(_tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("opencode-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: OPENCODE_NATIVE_WRAPPER_VALUE,
    }
    metadata = {"labels": labels, "workspace": str(_REPO_ROOT)}
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("opencode-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    # Bind with a generous timeout: unlike goose/codex, opencode-native's bind
    # boots ``opencode serve`` synchronously, which can exceed the shared
    # helper's 10s budget.
    patch = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=90.0,
    )
    patch.raise_for_status()
    return session_id


def _terminal_ids(base_url: str, session_id: str) -> list[str]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    if resp.status_code != 200:
        return []
    return [r.get("id") for r in resp.json().get("data", []) if r.get("type") == "terminal"]


def _wait_for_terminal_socket(base_url: str, session_id: str, resource_id: str) -> str:
    deadline = time.monotonic() + _TERMINAL_APPEAR_TIMEOUT_S
    last: list[str] = []
    while time.monotonic() < deadline:
        last = _terminal_ids(base_url, session_id)
        if resource_id in last:
            detail = httpx.get(
                f"{base_url}/v1/sessions/{session_id}/resources/terminals/{resource_id}",
                timeout=10.0,
            )
            detail.raise_for_status()
            socket = detail.json().get("metadata", {}).get("tmux_socket")
            if socket:
                return str(socket)
        time.sleep(1.0)
    raise AssertionError(
        f"opencode terminal {resource_id!r} never registered a tmux socket within "
        f"{_TERMINAL_APPEAR_TIMEOUT_S}s; saw {last!r}"
    )


def _run_foreign_startup_sweep(instance_dir: Path) -> str:
    """Run ``reap_orphaned_terminals`` the way a starting runner would.

    A separate interpreter (the "foreign runner") sweeps the temp root the
    instance dir lives in. Returns the subprocess's combined output for
    diagnostics.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(_REPO_ROOT), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    # The sweep scans tempfile.gettempdir(); point the foreign runner's temp
    # root at the dir the live instance actually lives in.
    env["TMPDIR"] = str(instance_dir.parent)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.inner.terminal import reap_orphaned_terminals; "
            "print('reaped', reap_orphaned_terminals())",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_opencode_terminal_persists_in_ui_through_foreign_runner_sweep(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Film the OpenCode terminal pane staying alive through a startup sweep."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    session_id: str | None = None
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_native_opencode_session(live_server, runner_id)
        terminal_id = terminal_resource_id("opencode", "main")
        tmux_socket = _wait_for_terminal_socket(live_server, session_id, terminal_id)
        instance_dir = Path(tmux_socket).parent

        # Show the live OpenCode terminal in the SPA's Terminal view.
        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
        # Switch Chat -> Terminal view via the header toggle.
        page.get_by_role("button", name="Terminal view").click()
        term = page.get_by_test_id("main-terminal-view")
        expect(term).to_have_attribute("data-visible", "true", timeout=15_000)
        # Let the OpenCode TUI paint into the pane, on camera.
        page.wait_for_timeout(4_000)

        # Stamp the owner marker with a pid that is dead in this namespace —
        # what a namespace-blind sweep reads for a marker written by a runner
        # in another pid namespace or boot, while the terminal is fully alive.
        dead = subprocess.Popen(["sh", "-c", "exit 0"])
        dead.wait()
        (instance_dir / _OWNER_PID_FILENAME).write_text(str(dead.pid), encoding="utf-8")

        # A foreign runner starts up and sweeps for leaked terminals. This is
        # the condition that used to kill the live tmux server, after which
        # the watcher logged 'tmux unavailable after 3 consecutive probes for
        # terminal opencode:main' and the pane vanished from the session.
        sweep_diag = _run_foreign_startup_sweep(instance_dir)

        # Keep filming past the watcher's whole probe budget: were the server
        # gone, the pane would be torn down in this window.
        page.wait_for_timeout(_SURVIVE_OBSERVE_MS)

        assert terminal_id in _terminal_ids(live_server, session_id), (
            "opencode terminal was deleted after the foreign runner's startup "
            f"sweep — the live pane did not survive. sweep: {sweep_diag}"
        )
        expect(term).to_have_attribute("data-visible", "true")
        # Hold the surviving pane on camera for a beat.
        page.wait_for_timeout(3_000)
    finally:
        with __import__("contextlib").suppress(Exception):
            if session_id is not None:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)
