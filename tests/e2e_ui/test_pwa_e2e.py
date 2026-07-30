"""Browser-level e2e coverage of the installable-PWA behavior.

Companion to the build-output guard in ``conftest._assert_pwa_build`` and
``test_pwa_build.py`` (which inspect the emitted files): this drives the SPA in a
real browser and asserts the runtime PWA contract end to end —

  1. the service worker registers and reaches an active state,
  2. the app is installable (manifest linked, served with the manifest MIME,
     with the required name / display / icons),
  3. the worker caches ONLY ``version.json`` — never the app shell, and
  4. navigations always hit the network (the worker does not intercept them,
     even once it controls the page).

(3) and (4) are the load-bearing invariants for a cloud app: a cached/served app
shell would white-screen users behind a stale deploy. Asserting them in a real
browser is the only place that catches a service worker that *looks* fine in the
source guard but actually serves the shell at runtime.

Unlike the rest of this suite, the PWA contract is independent of the agent /
LLM, so this spawns a plain ``omnigent server`` (no ``--agent``, no runner, no
Databricks credentials) — keeping the test fast and runnable anywhere. It still
serves the production static mount + cache/MIME headers from
``omnigent/server/app.py``, which is exactly what the browser checks rely on.

Part of the gated e2e_ui suite (excluded from the default ``pytest`` run via
``--ignore=tests/e2e_ui``); see this package's ``conftest`` module docstring.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTH_TIMEOUT_S = 30.0
_HEALTH_POLL_INTERVAL_S = 0.5


def _free_port() -> int:
    """Return a free localhost TCP port (bind-to-0, then release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    """Poll ``/health`` until 200, or raise with the server log on failure."""
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"omnigent server exited early (code {proc.returncode}).\n{log[:2000]}"
            )
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2)
            if resp.status_code == 200:
                return
            last_error = f"health HTTP {resp.status_code}"
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    log = log_path.read_text() if log_path.exists() else ""
    raise RuntimeError(
        f"omnigent server not healthy within {_HEALTH_TIMEOUT_S:.0f}s on "
        f"{base_url} (last_error={last_error}).\n{log[:2000]}"
    )


@pytest.fixture(scope="session")
def pwa_server(
    built_spa: None,
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Serve the built SPA via a no-agent ``omnigent server`` and yield its URL.

    No ``--agent`` (and therefore no runner / LLM credentials) — the PWA
    contract this test checks is agent-independent, so this stays fast and
    creds-free where ``live_server`` cannot. ``--database-uri`` /
    ``--artifact-location`` point at the pytest tmp dir so the user's default
    ``omnigent.db`` / ``artifacts`` are never touched.

    :param built_spa: Ensures the SPA bundle (incl. the PWA assets) is on disk
        before the server mounts it.
    :param request: Reads ``--ui-base-url`` to reuse an already-running server.
    :param tmp_path_factory: Per-session DB / artifact / log location.
    :returns: The server base URL, e.g. ``"http://127.0.0.1:51234"``.
    """
    override = request.config.getoption("--ui-base-url")
    if override:
        yield override
        return

    port = _free_port()
    server_tmp = tmp_path_factory.mktemp("pwa_e2e_server")
    log_path = server_tmp / "server.log"
    # PYTHONPATH forces the subprocess to import omnigent from the worktree, not
    # whatever is pip-installed in .venv — otherwise a branch's code changes
    # would silently run against stale code (same trick as ``live_server``).
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    base_url = f"http://127.0.0.1:{port}"
    # The log handle must outlive the Popen, so the context manager spans the
    # whole fixture (Popen → yield → teardown), closing on fixture exit.
    with open(log_path, "w", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from omnigent.cli import main; main()",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{server_tmp / 'test.db'}",
                "--artifact-location",
                str(server_tmp / "artifacts"),
            ],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_healthy(proc, base_url, log_path)
            yield base_url
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_pwa_installable_sw_registers_and_never_serves_the_shell(
    page: Page,
    pwa_server: str,
) -> None:
    """The installed PWA registers its worker, is installable, and the worker
    serves only ``version.json`` — never the app shell or navigations.

    :param page: Playwright page fixture (fresh context per test).
    :param pwa_server: Base URL of the no-agent server serving the built SPA.
    :returns: None.
    """
    base_url = pwa_server
    page.goto(base_url, wait_until="load")

    # 1. The service worker registers and activates.
    sw = page.evaluate(
        """async () => {
          const reg = await navigator.serviceWorker.ready;
          return { scriptURL: reg.active && reg.active.scriptURL, scope: reg.scope };
        }"""
    )
    assert sw["scriptURL"], "service worker never reached an active state"
    assert sw["scriptURL"].endswith("/sw.js")
    assert sw["scope"] == f"{base_url}/"

    # 2. Installable: manifest linked, served with the manifest MIME, and valid.
    manifest = page.evaluate(
        """async () => {
          const link = document.querySelector('link[rel="manifest"]');
          if (!link) return { linked: false };
          const res = await fetch(link.href);
          let body = null;
          try { body = await res.json(); } catch (e) { /* non-JSON */ }
          return {
            linked: true,
            status: res.status,
            contentType: res.headers.get("content-type") || "",
            name: body && body.name,
            display: body && body.display,
            icons: body && body.icons ? body.icons.length : 0,
          };
        }"""
    )
    assert manifest["linked"], "index.html has no <link rel=manifest>"
    assert manifest["status"] == 200
    assert manifest["contentType"].startswith("application/manifest+json")
    assert manifest["name"] == "Omnigent"
    assert manifest["display"] == "standalone"
    assert manifest["icons"] >= 2  # 192 + 512 is the installability minimum

    # 3. The worker caches ONLY version.json — never the app shell. A cached
    #    shell would white-screen users behind a stale deploy.
    cached_paths = page.evaluate(
        """async () => {
          const paths = [];
          for (const name of await caches.keys()) {
            const cache = await caches.open(name);
            for (const req of await cache.keys()) paths.push(new URL(req.url).pathname);
          }
          return paths.sort();
        }"""
    )
    assert cached_paths == ["/version.json"], f"worker cached unexpected entries: {cached_paths}"

    # 4. Navigations always hit the network — the worker must not intercept them
    #    even once it controls the page. Reload so the worker is controlling,
    #    then assert the navigation response came from the network, not the SW.
    resp = page.goto(base_url, wait_until="load")
    assert page.evaluate("() => !!navigator.serviceWorker.controller"), (
        "service worker did not take control of the page after reload"
    )
    assert resp is not None
    assert resp.from_service_worker is False, (
        "the service worker intercepted a navigation — navigations must hit the "
        "network so a deploy is never masked by a stale shell"
    )
