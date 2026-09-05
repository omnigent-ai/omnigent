"""E2E: ACP harnesses must appear in the New Chat picker on a *remote* server.

On a remote Omnigent server, ACP harness rows (builtin CLI rows like
Devin/Grok and user-configured ``acp:<slug>`` agents) must appear in the New
Chat picker whenever the attached host can launch them — the host's
``host.hello`` readiness frame advertises that (``configured_harnesses``).
Native harness rows (claude/codex/...) always appear, because they are seeded
unconditionally and filtered per host; ACP rows must follow the same model.
The historical asymmetry lived in ``omnigent/server/app.py``: builtin ACP CLI
rows gated their seeding on the *server's own* ``PATH``, and a user-configured
``acp:<slug>`` agent only ever seeded from the *server's* config — so on a
remote server (no vendor CLI in its container, no ``acp:`` config) neither
kind of row existed, with no client-side workaround (``/v1/agents`` is
GET-only).

Unlike the rest of the suite this file does NOT use the shared ``live_server``
fixture: the bug only exists when the server host and the executing host are
*different machines*, so the fixture here models that split on one box:

- the **server** runs with an empty ``HOME`` / ``OMNIGENT_CONFIG_HOME`` and a
  ``PATH`` stripped of ``devin`` / ``grok`` (the remote Databricks-app case:
  no vendor CLIs, no ``acp:`` config on the server);
- an ``omnigent host`` **daemon** attaches to it with a stub ``devin`` binary
  first on ``PATH`` and an ``acp:`` config block declaring a Kilocode agent
  (the user's Mac with the vendor CLI installed and configured).

The user journey asserted (fails while the bug is live):

1. attach a host that has ``devin`` installed to a remote server;
2. open the web UI New Chat composer;
3. open the agent/harness picker (and its "More" submenu) — native rows are
   listed, and the ACP rows for what the host advertises must be there too.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from omnigent.db.utils import builtin_agent_id
from omnigent.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Deterministic ids the seeded rows must use (builtin seeding is keyed by the
# agent name; see ``builtin_agent_id`` / ``_ensure_default_acp_agents``).
_DEVIN_AGENT_ID = builtin_agent_id("devin")
_KILOCODE_AGENT_ID = builtin_agent_id("kilocode")
_CLAUDE_AGENT_ID = builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME)

# The host daemon's user-configured ACP agent (the Tier-B shape from the
# report: slug->command mapping lives in the HOST's config, not the server's).
_HOST_ACP_CONFIG = """\
acp:
  agents:
    - name: Kilocode
      command: kilocode-acp
"""

_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 90.0
_POLL_INTERVAL_S = 1.0


def _find_free_port() -> int:
    """Pick an OS-assigned free TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _path_without(binaries: tuple[str, ...]) -> str:
    """Return ``PATH`` minus every directory holding one of *binaries*.

    Models the remote server box, where the vendor ACP CLIs are not
    installed: any PATH entry that can resolve one of *binaries* is dropped.
    """
    kept: list[str] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        if any(
            os.path.isfile(os.path.join(directory, b))
            and os.access(os.path.join(directory, b), os.X_OK)
            for b in binaries
        ):
            continue
        kept.append(directory)
    return os.pathsep.join(kept)


def _strip_proxies(env: dict[str, str]) -> dict[str, str]:
    """Drop ambient proxy env vars so loopback traffic never detours.

    Some CI/dev environments export HTTP(S)_PROXY pointing at a local
    intercepting proxy; the spawned server and host daemon only talk to each
    other over 127.0.0.1 and must not route that through it.
    """
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        env.pop(var, None)
    return env


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM *proc* with a grace period, escalating to SIGKILL."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture(scope="module")
def remote_topology(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Spawn the remote-server + attached-host topology; yield (base_url, host_id).

    The server is "remote": isolated ``HOME`` / ``OMNIGENT_CONFIG_HOME`` and a
    ``PATH`` with no ``devin`` / ``grok``. The host daemon is the user's
    machine: a stub ``devin`` first on ``PATH`` plus an ``acp:`` config block.
    Yields once the host is online AND its readiness map reports
    ``devin: True`` — the exact signal the server has in hand and (while the
    bug is live) never consumes for seeding.

    :param built_spa: Ensures the SPA bundle is on disk before the server
        boots and mounts it.
    :param tmp_path_factory: Per-module temp dirs for the DB, artifacts,
        isolated homes, and logs.
    """
    work = tmp_path_factory.mktemp("acp_remote_seeding")
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # -- Server env: models the remote server box.
    server_home = work / "server-home"
    server_home.mkdir()
    server_cfg = work / "server-config"
    server_cfg.mkdir()
    server_env: dict[str, str] = {
        **os.environ,
        "HOME": str(server_home),
        "OMNIGENT_CONFIG_HOME": str(server_cfg),
        "PATH": _path_without(("devin", "grok")),
    }
    # An ambient XDG override could leak the developer's real config in.
    server_env.pop("XDG_CONFIG_HOME", None)
    _strip_proxies(server_env)
    apply_server_env(server_env, _REPO_ROOT)

    # Precondition: the ACP seeding gate must NOT resolve devin on the server
    # side, or this box can't model a remote server (e.g. devin installed in
    # /usr/local/bin, which resolve_cli_binary probes off-PATH too).
    probe = subprocess.run(
        [
            server_executable(),
            "-c",
            "import sys; from omnigent._platform import resolve_cli_binary; "
            "sys.exit(0 if resolve_cli_binary('devin') is None else 1)",
        ],
        env=server_env,
        cwd=compat_server_cwd(),
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(
            "devin resolves on the server host even off-PATH; cannot model a remote server"
        )

    server_log = open(work / "server.log", "w")  # noqa: SIM115 — lives for Popen lifetime
    server_proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{work / 'test.db'}",
            "--artifact-location",
            str(work / "artifacts"),
        ],
        env=server_env,
        cwd=compat_server_cwd(),
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )

    host_proc: subprocess.Popen[bytes] | None = None
    try:
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            deadline = time.monotonic() + _HEALTH_TIMEOUT_S
            while True:
                if server_proc.poll() is not None:
                    raise RuntimeError(
                        "server exited early:\n" + (work / "server.log").read_text()[-3000:]
                    )
                try:
                    if client.get(f"{base_url}/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "server never became healthy:\n"
                        + (work / "server.log").read_text()[-3000:]
                    )
                time.sleep(_POLL_INTERVAL_S)

            # -- Host daemon env: models the user's machine with devin
            # installed (stub first on PATH) and a configured acp agent.
            host_bin = work / "host-bin"
            host_bin.mkdir()
            devin_stub = host_bin / "devin"
            devin_stub.write_text("#!/bin/sh\nexit 0\n")
            devin_stub.chmod(0o755)
            host_cfg = work / "host-config"
            host_cfg.mkdir()
            (host_cfg / "config.yaml").write_text(_HOST_ACP_CONFIG)
            host_env: dict[str, str] = {
                **os.environ,
                "OMNIGENT_CONFIG_HOME": str(host_cfg),
                "PATH": f"{host_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            }
            _strip_proxies(host_env)
            host_log = open(work / "host.log", "w")  # noqa: SIM115 — lives for Popen lifetime
            host_proc = subprocess.Popen(
                [
                    runner_executable(),
                    "-m",
                    "omnigent.host._daemon_entry",
                    "--server",
                    base_url,
                ],
                env=apply_runner_env(host_env),
                cwd=compat_runner_cwd(),
                stdout=host_log,
                stderr=subprocess.STDOUT,
            )

            # Wait until the host is online AND advertises devin readiness —
            # the signal the seeding must act on.
            host_id: str | None = None
            deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
            while True:
                if host_proc.poll() is not None:
                    raise RuntimeError(
                        "host daemon exited early:\n" + (work / "host.log").read_text()[-3000:]
                    )
                try:
                    hosts = client.get(f"{base_url}/v1/hosts").json().get("hosts", [])
                except (httpx.HTTPError, ValueError):
                    hosts = []
                for h in hosts:
                    readiness = h.get("configured_harnesses") or {}
                    if h.get("status") == "online" and readiness.get("devin") is True:
                        host_id = str(h["host_id"])
                        break
                if host_id is not None:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "host never came online with devin readiness:\n"
                        + (work / "host.log").read_text()[-3000:]
                    )
                time.sleep(_POLL_INTERVAL_S)

        yield base_url, host_id
    finally:
        if host_proc is not None:
            _terminate(host_proc)
        _terminate(server_proc)


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright sync tests in one session;
    once one has run, pytest-asyncio can't start a loop on the main thread.
    Exceptions (including assertion failures) re-raise on the caller.
    """
    captured: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


async def _open_picker_with_more(page: Any, base_url: str, host_id: str) -> None:
    """Drive the landing composer to the fully-expanded harness picker.

    Seeds a recent workspace for *host_id* (so the composer auto-fills without
    the file browser), opens the picker, confirms the native Claude row is
    there (the control: natives always seed), and opens the "More" submenu
    where non-fully-supported harness rows fold.
    """
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ "{host_id}": ["/tmp"] }})
        );"""
    )
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    await expect(page.get_by_test_id(f"new-chat-landing-agent-{_CLAUDE_AGENT_ID}")).to_be_visible(
        timeout=30_000
    )
    # ACP rows are not fully-supported natives, so they render inside the
    # "More" flyout; open it so absence there is absence everywhere.
    await page.get_by_test_id("new-chat-landing-harness-more").click()


def _assert_agent_row_seeded(base_url: str, name: str, agent_id: str) -> None:
    """Assert ``GET /v1/agents`` carries the seeded builtin row *name*."""
    with httpx.Client(trust_env=False, timeout=10.0) as client:
        data = client.get(f"{base_url}/v1/agents").json()["data"]
    by_name = {a["name"]: a for a in data}
    assert name in by_name, (
        f"GET /v1/agents has no {name!r} row (got {sorted(by_name)}); the "
        "server never seeded it for the attached host"
    )
    assert by_name[name]["id"] == agent_id


def test_builtin_acp_cli_row_seeded_for_remote_host(
    remote_topology: tuple[str, str],
) -> None:
    """The picker offers Devin when the attached host has the devin CLI.

    The host's readiness frame advertises ``devin: True`` (verified by the
    fixture), yet while the bug is live the server seeds no ``devin`` agent
    row — the picker lists every native harness but no Devin, with no
    client-side workaround (``/v1/agents`` is GET-only). This drives the real
    journey: open the New Chat picker on the remote server and expect the
    Devin row among the harnesses.
    """
    base_url, host_id = remote_topology

    async def _drive() -> None:
        async with async_playwright() as pw:
            # --no-proxy-server: loopback must never detour through an
            # ambient CI proxy (chromium may honor proxy env vars on linux).
            browser = await pw.chromium.launch(args=["--no-proxy-server"])
            page = await browser.new_page()
            try:
                await _open_picker_with_more(page, base_url, host_id)
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-{_DEVIN_AGENT_ID}")
                ).to_be_visible(timeout=10_000)
            finally:
                # Close the context before the browser: a recording run's
                # video only flushes on context close (browser.close() alone
                # leaves an empty .webm).
                await page.context.close()
                await browser.close()

    _run_in_fresh_loop(_drive())
    _assert_agent_row_seeded(base_url, "devin", _DEVIN_AGENT_ID)


def test_host_configured_acp_agent_row_seeded_for_remote_host(
    remote_topology: tuple[str, str],
) -> None:
    """The picker offers a host-configured ``acp:<slug>`` agent (Kilocode).

    The attached host's ``acp:`` config declares a Kilocode agent, but the
    server only reads its *own* config when seeding, so no ``kilocode`` row
    exists on a remote server (the report's Tier B: the host must advertise
    its configured ACP slugs for the server to seed matching rows).
    """
    base_url, host_id = remote_topology

    async def _drive() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-proxy-server"])
            page = await browser.new_page()
            try:
                await _open_picker_with_more(page, base_url, host_id)
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-{_KILOCODE_AGENT_ID}")
                ).to_be_visible(timeout=10_000)
            finally:
                # Close the context before the browser: a recording run's
                # video only flushes on context close (browser.close() alone
                # leaves an empty .webm).
                await page.context.close()
                await browser.close()

    _run_in_fresh_loop(_drive())
    _assert_agent_row_seeded(base_url, "kilocode", _KILOCODE_AGENT_ID)


if sys.platform == "win32":  # pragma: no cover — suite runs on POSIX only
    pytest.skip(
        "POSIX-only: spawns a host daemon with a shell-stub devin", allow_module_level=True
    )

# The stub devin relies on /bin/sh; make the intent explicit if sh is absent.
if shutil.which("sh") is None:  # pragma: no cover
    pytest.skip("no `sh` to back the devin stub", allow_module_level=True)
