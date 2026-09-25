"""
Browser e2e guard: a managed sandbox whose workspace-prep init container
crash-loops must fail the launch fast, and the failure banner must carry the
init container's log tail (the git clone error).

User journey, from the bug report:

1. An operator configures ``sandbox.provider: kubernetes`` on the server,
   pointing at a cluster whose sandbox namespace cannot reach the clone host
   (a default-deny NetworkPolicy is enough).
2. On the new-chat landing the user picks the New Sandbox host, adds a
   repository URL, and sends a message — the server submits a Job and the
   session shows the launch band.
3. ``git clone`` in the ``workspace-prep`` init container fails immediately
   and the kubelet restarts it: the Pod sits in ``Pending`` with the init
   container in ``CrashLoopBackOff``. It will never come up.
4. Expected: the "Sandbox launch failed" banner appears within seconds,
   names the crash-looping container, and its expanded error carries a tail
   of the container's log (the clone error). Observed (bug): the band sits
   in the starting stage for the whole ``pod_ready_timeout_s`` budget (90s
   by default) and the expanded error shows only Pod events — no log tail.

No cluster is reachable from the test environment, so a stub ``kubernetes``
package on the server subprocess's PYTHONPATH replays what a real apiserver
reports for this state (see ``tests/e2e/_k8s_crashloop_stub_sdk``). The
server process, its config parsing, the managed-session create, the
launcher's start wait, the failure-message builder, and the SPA are real.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.async_api import async_playwright, expect

from tests.e2e._k8s_crashloop_stub_sdk import CLONE_ERROR_LINE as _CLONE_ERROR_LINE
from tests.e2e._k8s_crashloop_stub_sdk import STUB_FILES as _STUB_FILES

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HEALTH_TIMEOUT_S = 180.0
_POLL_INTERVAL_S = 0.5

# The repository workspace the user asks for — the clone the init container
# fails on.
_REPO_URL = "https://github.com/omnigent-ai/omnigent"

# Pod-ready budget: generous enough that a fail-fast well under it is
# unambiguous, small enough that the buggy poll-to-the-deadline path keeps
# the run (and its recording) short. 90s in a default deployment.
_POD_READY_TIMEOUT_S = 25

# The stub parks workspace-prep in CrashLoopBackOff ~3s after the Job is
# submitted; a fail-fast start wait surfaces the banner a few polls later.
# The buggy path cannot fail before the 25s deadline. 15s splits the two.
_FAILFAST_MAX_S = 15.0


def _find_free_port() -> int:
    """Bind port 0 and return the assigned free port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_server_config(tmp_path: Path, port: int) -> Path:
    """Write a server config enabling the kubernetes sandbox provider."""
    config_path = tmp_path / "server-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {
                    "server_url": f"http://127.0.0.1:{port}",
                    "provider": "kubernetes",
                    "kubernetes": {
                        "image": "ghcr.io/omnigent-ai/omnigent-host:e2e",
                        "namespace": "omnigent-sandboxes",
                        "in_cluster": False,
                        "kubeconfig": str(tmp_path / "kubeconfig"),
                        "pod_ready_timeout_s": _POD_READY_TIMEOUT_S,
                    },
                }
            }
        )
    )
    (tmp_path / "kubeconfig").write_text("")
    return config_path


def _spawn_server(tmp_path: Path, port: int) -> tuple[subprocess.Popen[bytes], Path]:
    """Start a real ``omnigent server`` subprocess wired to the stub SDK."""
    stub_root = tmp_path / "k8s_stub"
    for rel, source in _STUB_FILES.items():
        target = stub_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    pythonpath = os.pathsep.join(
        [
            str(stub_root),
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "OPENAI_API_KEY": "unused-no-turn-runs",
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(
            _REPO_ROOT / "tests" / "resources" / "agents" / "sdk-chat-builtin.yaml"
        ),
    }
    config_path = _write_server_config(tmp_path, port)
    log_path = tmp_path / "server.log"
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen's lifetime
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
            "--config",
            str(config_path),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return proc, log_path


def _wait_for_health(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    """Wait for /health, failing with the server log if the process dies."""
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"server exited (code {proc.returncode}) before serving /health:\n"
                f"{log_path.read_text()[-2000:]}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"server did not become healthy:\n{log_path.read_text()[-2000:]}")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread's event loop, re-raising its error.

    The e2e_ui suite runs pytest-playwright sync tests in the same session;
    once one has run, an asyncio loop can't start on the main thread.
    """
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


async def _drive_launch_to_failure(base_url: str, outcome: dict[str, Any]) -> None:
    """Drive new chat → New Sandbox → repo URL → send; watch the launch fail.

    Records into *outcome*: ``elapsed_s`` (submit click → failure banner
    visible) and ``error`` (the expanded failure text shown to the user).
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        try:
            await page.goto(f"{base_url}/")
            prompt = page.get_by_test_id("new-chat-landing-input")
            await prompt.wait_for(state="visible", timeout=60_000)

            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id("new-chat-landing-sandbox-option").click()

            controls = page.get_by_test_id("new-chat-landing-workspace-controls")
            repository = controls.get_by_test_id("new-chat-landing-repo-chip")
            await repository.click()
            repo_input = page.get_by_test_id("new-chat-landing-repo-input")
            await repo_input.fill(_REPO_URL)
            # Enter commits the URL like the Add button, which re-renders with
            # the popover's repo-list state and flakes pointer clicks.
            await repo_input.press("Enter")
            # The repo list re-renders after Enter commits the URL; allow a
            # slow CI box more than expect()'s 5s default before flaking.
            await expect(repository).to_have_attribute(
                "aria-label", "Sandbox repositories: omnigent", timeout=15_000
            )
            await page.keyboard.press("Escape")

            await prompt.fill("Investigate this repository")
            started = time.monotonic()
            await page.get_by_test_id("new-chat-landing-submit").click()
            await page.wait_for_url("**/c/*", timeout=30_000)

            failed = page.get_by_test_id("sandbox-failed-indicator")
            await failed.wait_for(state="visible", timeout=(_POD_READY_TIMEOUT_S + 90) * 1_000)
            outcome["elapsed_s"] = time.monotonic() - started

            await failed.get_by_test_id("error-pill").click()
            content = failed.get_by_test_id("error-message-content")
            await content.wait_for(state="visible", timeout=15_000)
            outcome["error"] = await content.inner_text()
            # Hold the expanded error on screen so the recording ends on it.
            await page.wait_for_timeout(4_000)
        finally:
            await context.close()
            await browser.close()


def test_init_crashloop_launch_fails_fast_with_log_tail(tmp_path: Path) -> None:
    """The launch must fail fast and its banner must carry the clone error.

    The bug: ``_terminal_failure`` never treats a crash-looping init
    container as terminal, so the start wait polls the full
    ``pod_ready_timeout_s`` and the timeout error attaches Pod events but
    never the init container's log tail.
    """
    port = _find_free_port()
    proc, log_path = _spawn_server(tmp_path, port)
    outcome: dict[str, Any] = {}
    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(proc, base_url, log_path)
        _run_in_fresh_loop(_drive_launch_to_failure(base_url, outcome))
    finally:
        proc.kill()
        proc.wait(timeout=30)

    elapsed = outcome["elapsed_s"]
    error = outcome["error"]
    problems: list[str] = []
    if elapsed >= _FAILFAST_MAX_S:
        problems.append(
            f"launch failure took {elapsed:.1f}s — the crash-looping workspace-prep "
            f"init container was not fail-fast detected and the start wait polled to "
            f"its {_POD_READY_TIMEOUT_S}s pod-ready deadline (90s in a default "
            f"deployment)"
        )
    if _CLONE_ERROR_LINE not in error:
        problems.append(
            "the failure banner dropped the workspace-prep log tail — the user "
            "never sees the git clone error the init container printed; error "
            f"shown: {error[:800]}"
        )
    if problems:
        pytest.fail("\n".join(problems))
