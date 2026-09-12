"""E2E regression test: the pi:main terminal must not disappear from tmux.

Failure signature (logger ``omnigent.inner.terminal`` /
``_idle_watch_loop_threaded``)::

    tmux unavailable after 3 consecutive probes for terminal pi:main

Root-cause lead (a hypothesis; this test only reproduces the observable
failure, it does not fix it):

    The Pi ``main`` terminal is launched by ``_auto_create_pi_terminal``
    (``omnigent/runner/native/orchestration.py``) WITHOUT
    ``keep_alive_after_exit``. Its ``TerminalEnvSpec`` therefore leaves tmux on
    its defaults (``exit-empty on`` + ``remain-on-exit off``), so the instant
    the runner-owned ``pi`` CLI process exits -- a crash, ``/exit``, or any
    early exit -- tmux reaps the lone-pane session and the whole private tmux
    server exits. The pi:main terminal literally "disappears from tmux".

    The threaded idle watcher (``_idle_watch_loop_threaded``) then probes with
    ``capture-pane``; the probe fails, ``has-session`` confirms the session is
    gone, and after ``_IDLE_EXIT_FAILURE_THRESHOLD`` (3) consecutive failures
    the watcher logs the generic ``tmux unavailable after N consecutive
    probes for terminal pi:main`` and reports a required-terminal exit -- with
    no structured pane-dead reason.

    The claude-native terminal avoids exactly this by launching with
    ``keep_alive_after_exit=True`` (``remain-on-exit on``): an exiting CLI
    leaves a dead-but-capturable pane, so ``capture-pane`` still succeeds,
    ``_pane_is_dead()`` fires, and the exit is reported deterministically
    instead of via the generic "tmux unavailable" cascade.

This test drives the REAL journey end-to-end: it brings a host online, creates
a pi-native session so the runner launches the real ``pi`` CLI inside a
runner-owned tmux server, waits for the terminal to be live, then kills the
``pi`` process (modelling the organic exit/crash the KPI counts). It asserts
the FIXED behavior -- killing pi must NOT make the pi:main terminal vanish and
emit the "tmux unavailable ... pi:main" signature -- so the module is RED on
the buggy build (the reproduction) and turns GREEN once pi:main keeps its dead
pane and reports a diagnosable exit.

The pi login is a local (unmanaged) api-key stub, so no real LLM credential is
needed; the journey never sends a turn -- it only needs a live ``pi`` process
in tmux to kill. Launching the real Pi terminal needs ``pi`` / ``tmux`` /
``node`` on PATH; the module skips cleanly when any is absent.

    .venv/bin/python -m pytest tests/e2e/test_pi_main_terminal_tmux_disappears_e2e.py -v
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.helpers import POLL_INTERVAL_S

# Worktree root (this file lives at <worktree>/tests/e2e/). Used to build an
# absolute PYTHONPATH for the daemon so the runner it spawns -- whose cwd is
# the session workspace, not this worktree -- can still import omnigent.
_WORKTREE = Path(__file__).resolve().parents[2]

# The model the host's Pi is logged in with (seeded into models-store.json).
# Any usable Pi model keeps the launch happy; the journey never runs a turn.
_PI_MODEL_ID = "claude-sonnet-4-5"

# The exact log signature the buggy vanish-then-generic-log path emits.
_TMUX_UNAVAILABLE_RE = re.compile(
    r"tmux unavailable after \d+ consecutive probes for terminal pi:main"
)

# Skip the whole module unless the real Pi terminal toolchain is present:
# the launch path shells out to node -> pi inside a runner-owned tmux pane.
pytestmark = [
    pytest.mark.skipif(
        (_reason := cli_unavailable_reason("pi")) is not None,
        reason=f"pi-native tmux-disappear e2e needs a runnable 'pi' CLI; {_reason}.",
    ),
    # tmux is gated on presence only: its version flag is ``-V`` (not the
    # generic ``--version`` cli_unavailable_reason probes with), so that probe
    # false-negatives on a perfectly usable tmux.
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="pi-native terminal launch needs 'tmux' on PATH.",
    ),
    pytest.mark.skipif(
        (_node := cli_unavailable_reason("node")) is not None,
        reason=f"pi-native extension needs 'node'; {_node}.",
    ),
]


def _bridge_marker(session_id: str) -> str:
    """Return the hashed bridge-dir path segment for *session_id*.

    The harness writes a session's Pi bridge under
    ``~/.omnigent/pi-native/<sha256(session_id)[:32]>``; the launched ``pi``
    process references that dir via ``--extension`` / ``--session-dir``, so
    the segment is a reliable needle for finding the process in ``/proc``.

    :param session_id: The session/conversation id.
    :returns: ``"pi-native/<32-hex>"``.
    """
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return f"pi-native/{digest}"


def _find_pi_process(marker: str) -> tuple[int, list[str]] | None:
    """Scan ``/proc`` for the launched ``pi`` CLI naming *marker*.

    The pi CLI runs as ``node .../pi --extension <bridge>/... --approve
    --session-dir <bridge>/...``. Two *other* processes also name the bridge
    marker and must be skipped: the ``tmux new-session ... '<pi command>'``
    launcher (which embeds the whole pi command as ONE argv element, so
    ``--extension`` is not a standalone token there) and the ``python -m
    omnigent.runner...`` runner that spawned it.

    Match only the process where ``--extension`` is its own argv token and
    ``argv[0]`` is not tmux -- that is the real pi CLI.

    :param marker: The ``pi-native/<hash>`` bridge segment.
    :returns: ``(pid, argv)`` of the pi process, or ``None`` if not found yet.
    """
    needle = marker.encode()
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            raw = (pid_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if needle not in raw:
            continue
        argv = [chunk.decode(errors="replace") for chunk in raw.split(b"\x00") if chunk]
        if not argv:
            continue
        if "--extension" in argv and not os.path.basename(argv[0]).startswith("tmux"):
            return int(pid_dir.name), argv
    return None


def _kill_pi_processes(marker: str) -> None:
    """Best-effort SIGKILL of any process still naming *marker*.

    :param marker: The bridge segment; kills the pi CLI and any child that
        inherited it so the test leaves no orphaned tmux/pi tree.
    """
    needle = marker.encode()
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            raw = (pid_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in raw:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(pid_dir.name), signal.SIGKILL)


def _scan_home_logs_for(home: Path, pattern: re.Pattern[str]) -> str | None:
    """Return the first log line under *home* matching *pattern*, else ``None``.

    The "tmux unavailable ... pi:main" signature is emitted by the RUNNER
    process (the idle-watcher daemon thread), whose process log lands under
    ``<home>/.omnigent/logs/runner/``. Scanning every ``*.log`` under the
    daemon HOME finds it wherever the runner routed it.

    :param home: The daemon HOME whose ``.omnigent/logs`` tree holds the logs.
    :param pattern: The compiled signature regex.
    :returns: The matching line, or ``None``.
    """
    for log_path in home.rglob("*.log"):
        try:
            text = log_path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if pattern.search(line):
                return line
    return None


class _PiHost:
    """A spawned host daemon whose Pi is logged in (locally, unmanaged).

    :param proc: The daemon subprocess handle.
    :param host_id: The registered host id.
    :param home: The daemon's HOME (holds ``.pi/agent`` + ``.omnigent`` +
        the runner logs).
    :param daemon_log: Captured daemon log path.
    """

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        host_id: str,
        home: Path,
        daemon_log: Path,
    ) -> None:
        self.proc = proc
        self.host_id = host_id
        self.home = home
        self.daemon_log = daemon_log


def _seed_pi_home(home: Path) -> str:
    """Seed *home* with a locally-logged-in Pi and a host config.

    Writes ``.pi/agent/auth.json`` (an api-key login) and
    ``.pi/agent/models-store.json`` (one usable model) so Pi itself has a
    model to open with, while ``.omnigent/config.yaml`` carries only a host
    block. No real provider/credential is needed -- the journey never runs a
    turn, it just needs a live ``pi`` process in tmux to kill.

    :param home: The daemon HOME to populate.
    :returns: The host id written into ``config.yaml``.
    """
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-pi-tmux-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    pi_agent = home / ".pi" / "agent"
    pi_agent.mkdir(parents=True, exist_ok=True)
    (pi_agent / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "test-token"}})
    )
    (pi_agent / "models-store.json").write_text(
        json.dumps(
            {
                "anthropic": {
                    "models": [
                        {
                            "id": _PI_MODEL_ID,
                            "name": "Claude Sonnet 4.5",
                            "api": "anthropic-messages",
                            "provider": "anthropic",
                            "baseUrl": "https://api.anthropic.com",
                            "input": ["text", "image"],
                        }
                    ],
                    "checkedAt": 1750000000,
                }
            }
        )
    )
    return host_id


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float = 45.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* is online.

    :param client: HTTP client pointed at the server.
    :param host_id: Host id to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the host never appears online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


def _terminal_resource_present(client: httpx.Client, session_id: str) -> bool:
    """Return whether the session currently exposes a ``terminal`` resource.

    The pi:main terminal shows up in ``GET /v1/sessions/{id}/resources`` (the
    runner-authoritative inventory the web UI renders as the Terminal pane).
    When the terminal exits it is removed (``session.resource.deleted``), so a
    transition present -> absent is the user-visible "terminal disappeared".

    :param client: HTTP client pointed at the server.
    :param session_id: Session/conversation id.
    :returns: ``True`` while a terminal resource is listed.
    """
    resp = client.get(f"/v1/sessions/{session_id}/resources", timeout=30.0)
    if resp.status_code != 200:
        return False
    return any(item.get("type") == "terminal" for item in resp.json().get("data", []))


@pytest.fixture(scope="module")
def pi_host(
    live_server: str,
    http_client: httpx.Client,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_PiHost]:
    """Spawn one host daemon with a locally-logged-in Pi.

    :param live_server: Server URL the daemon registers with.
    :param http_client: HTTP client pointed at the server.
    :param tmp_path_factory: Module-scoped temp dir factory (the daemon HOME).
    :yields: The spawned :class:`_PiHost`.
    """
    home = tmp_path_factory.mktemp("pi-tmux-home")
    host_id = _seed_pi_home(home)
    daemon_log = home / "host-daemon.log"
    # Pin HOME + OMNIGENT_CONFIG_HOME + OMNIGENT_DATA_DIR to the seeded dir so
    # the daemon (and the runner it spawns) read the seeded config and Pi
    # login and route their process logs under this HOME, where the test can
    # scan them for the failure signature.
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    # Prepend ABSOLUTE worktree roots to PYTHONPATH. The runner the daemon
    # spawns runs with cwd=<workspace>, so any relative PYTHONPATH entry
    # dangles and the runner fails with ``ModuleNotFoundError: omnigent``.
    # Absolute paths resolve from any cwd; in CI (checkout == worktree) this
    # is redundant-but-harmless.
    _existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_WORKTREE),
            str(_WORKTREE / "sdks" / "python-client"),
            str(_WORKTREE / "sdks" / "ui"),
        ]
        + ([_existing] if _existing else [])
    )
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    try:
        _wait_for_host_online(http_client, host_id, timeout=45.0)
        yield _PiHost(proc=proc, host_id=host_id, home=home, daemon_log=daemon_log)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_pi_main_terminal_survives_pi_exit_without_tmux_unavailable(
    pi_host: _PiHost,
    http_client: httpx.Client,
) -> None:
    """Killing the pi CLI must NOT vaporize pi:main + log "tmux unavailable".

    Drives the real pi-native journey: create the session, let the runner
    auto-launch the real ``pi`` CLI inside a runner-owned tmux server, then
    kill that process. On the buggy build (no ``keep_alive_after_exit`` for
    pi:main) tmux reaps the whole server the instant pi exits, so the idle
    watcher's probes fail and it logs the generic ``tmux unavailable
    after N consecutive probes for terminal pi:main`` -- the reproduction. On
    the fixed build the dead pane persists and the exit is reported
    deterministically without that signature.

    :param pi_host: The spawned Pi host.
    :param http_client: HTTP client pointed at the server.
    """
    host = pi_host
    spec_yaml = "\n".join(
        [
            "name: pi-native-ui",
            "prompt: |",
            "  Pi is running in the session terminal.",
            "executor:",
            "  harness: pi-native",
            "spawn: true",
            "os_env:",
            "  type: caller_process",
            "  cwd: .",
            "  sandbox:",
            "    type: none",
            "",
        ]
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = spec_yaml.encode()
        info = tarfile.TarInfo("pi-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    workspace = host.home / "ws"
    workspace.mkdir(exist_ok=True)
    create = http_client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps(
                {
                    "host_id": host.host_id,
                    "workspace": str(workspace),
                    "labels": {
                        "omnigent.ui": "terminal",
                        "omnigent.wrapper": "pi-native-ui",
                    },
                }
            )
        },
        files={"bundle": ("pi-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=60.0,
    )
    assert create.status_code in (200, 201), f"session create failed: {create.text}"
    session_id = str(create.json()["session_id"])
    marker = _bridge_marker(session_id)

    try:
        # 1) Wait for the real pi CLI to launch inside the runner-owned tmux
        #    server, so the idle watcher is live before we kill it.
        pi_pid: int | None = None
        deadline = time.monotonic() + 150.0
        while time.monotonic() < deadline:
            found = _find_pi_process(marker)
            if found is not None:
                pi_pid = found[0]
                break
            if host.proc.poll() is not None:
                raise AssertionError(
                    f"host daemon exited (rc={host.proc.returncode}) before pi launched; "
                    f"log tail:\n{host.daemon_log.read_text()[-2000:]}"
                )
            time.sleep(1.0)
        assert pi_pid is not None, (
            "the launched 'pi' process never appeared for session "
            f"{session_id!r}; daemon log tail:\n{host.daemon_log.read_text()[-2000:]}"
        )

        # 2) Confirm the pi:main terminal is a live session resource (the
        #    Terminal pane the web UI renders), then give the idle watcher a
        #    couple of poll intervals to arm.
        term_deadline = time.monotonic() + 30.0
        while time.monotonic() < term_deadline:
            if _terminal_resource_present(http_client, session_id):
                break
            time.sleep(POLL_INTERVAL_S)
        assert _terminal_resource_present(http_client, session_id), (
            "pi:main terminal resource never appeared for session "
            f"{session_id!r}; the runner did not register the terminal."
        )
        time.sleep(2.5)

        # 3) Kill the pi CLI -- models the organic crash/exit the KPI counts.
        os.kill(pi_pid, signal.SIGKILL)

        # 4) Scan the runner logs for the failure signature while confirming the
        #    terminal exit is handled. On the buggy build the signature fires
        #    within ~3 probe intervals (~3-5s); scan generously past that.
        signature_line: str | None = None
        terminal_gone = False
        scan_deadline = time.monotonic() + 25.0
        while time.monotonic() < scan_deadline:
            hit = _scan_home_logs_for(host.home, _TMUX_UNAVAILABLE_RE)
            if hit is not None:
                signature_line = hit
                break
            if not _terminal_resource_present(http_client, session_id):
                terminal_gone = True
            time.sleep(0.5)

        # Sanity: the kill actually exercised the terminal-exit path (the
        # terminal is removed on both the buggy and fixed builds), so a green
        # result reflects the fix, not a no-op where pi never died.
        if signature_line is None and not terminal_gone:
            terminal_gone = not _terminal_resource_present(http_client, session_id)
        assert terminal_gone or signature_line is not None, (
            "pi:main terminal never exited after the pi CLI was killed -- the "
            "exit path was not exercised, so the reproduction is inconclusive."
        )

        # The reproduction / regression assertion.
        assert signature_line is None, (
            "Bug reproduced: killing the pi CLI vaporized the pi:main "
            "tmux server (launched without keep_alive_after_exit), and the idle "
            "watcher logged the generic tmux-unavailable signature instead of a "
            f"diagnosable pane-dead exit:\n    {signature_line}"
        )
    finally:
        _kill_pi_processes(marker)
