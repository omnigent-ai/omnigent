"""E2E regression test: claude-native must honor ``CLAUDE_CONFIG_DIR``.

Reproduces the user-reported bug: a user keeps a second, logged-in Claude
Code profile under ``CLAUDE_CONFIG_DIR`` (e.g. a work account), exports it,
and starts ``omni host`` from that shell. When they then start a Claude Code
(claude-native) session from the web UI, the session silently runs on the
**default** ``~/.claude`` profile instead:

* the host->runner environment strip (``_RUNNER_ENV_ALLOWLIST`` in
  ``omnigent/host/connect.py``, and the CLI->daemon allowlist in
  ``omnigent/cli.py``) drops ``CLAUDE_CONFIG_DIR``, so the pane's ``claude``
  process launches without it and uses the default ``~/.claude`` account;
* workspace trust / onboarding pre-accept is seeded into ``~/.claude.json``
  (``ensure_claude_workspace_trusted`` hard-codes ``Path.home()``) instead of
  ``$CLAUDE_CONFIG_DIR/.claude.json``, so the exported profile never sees the
  folder as trusted;
* the per-session ``--settings`` file chains the user's ``statusLine`` from
  the hard-coded ``~/.claude/settings.json`` (``_USER_CLAUDE_SETTINGS_PATH``)
  instead of the exported profile's ``settings.json``.

This test drives the REAL user journey end to end: a real ``omnigent
server`` subprocess, a real host daemon subprocess started with
``CLAUDE_CONFIG_DIR`` exported (exactly the reporter's shell), a real runner
spawned by that host, and a claude-native session created the web-UI way
(built-in ``claude-native-ui`` agent + ``host_id`` + workspace) whose
terminal the runner auto-creates.

The Claude CLI itself is replaced with a tiny stub that records the
environment and argv of every invocation and parks, so the test needs no
Claude login. Everything upstream of the stub — the daemon/runner env
construction, the trust-file write, and the settings composition — is the
product's own behavior.

Desired behavior (asserted): the pane's ``claude`` receives the exported
``CLAUDE_CONFIG_DIR``, workspace trust lands in
``$CLAUDE_CONFIG_DIR/.claude.json``, and the per-session settings chain the
exported profile's ``statusLine``. On the buggy build the pane's env has no
``CLAUDE_CONFIG_DIR``, trust lands in ``~/.claude.json``, and the settings
chain the default profile's ``statusLine`` — so this test FAILS with all
observed values in the failure message.

Run::

    .venv/bin/python -m pytest \\
        tests/e2e/test_claude_native_config_dir_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

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

_HEALTH_TIMEOUT_S = 120.0
_HOST_ONLINE_TIMEOUT_S = 90.0
# Terminal auto-create includes host auth probes (which park against the
# stub until their internal timeout), bridge prep + tmux boot; generous.
_PANE_TIMEOUT_S = 300.0
_POLL_S = 1.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native terminals run inside tmux; tmux not installed",
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


def _write_claude_stub(stub_bin: Path, launches: Path) -> None:
    """Install a recording ``claude`` stub, first on PATH for the daemon.

    Every invocation appends one record file with the env it received and
    its argv. ``--version`` answers the host capability probe; headless
    ``-p`` invocations (the model-catalog probe) print ``{}`` and exit;
    everything else parks so the tmux pane stays alive.

    :param stub_bin: Directory to place the stub in (prepended to PATH).
    :param launches: Directory the stub writes one ``launch.<pid>`` record
        per invocation into.
    """
    stub = stub_bin / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        'case " $* " in *" --version "*) echo "2.1.236 (Claude Code)"; exit 0;; esac\n'
        "interactive=1\n"
        'for a in "$@"; do [ "$a" = "-p" ] && interactive=0; done\n'
        f'REC="{launches}/launch.$$"\n'
        "{\n"
        '  echo "INTERACTIVE=$interactive"\n'
        '  echo "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR-__UNSET__}"\n'
        '  echo "HOME=$HOME"\n'
        '  echo "RUNNER_ID=${OMNIGENT_RUNNER_ID-__UNSET__}"\n'
        '  echo "FIRSTARG=${1-__NONE__}"\n'
        '  for a in "$@"; do printf \'ARG:%s\\n\' "$a"; done\n'
        '} > "$REC"\n'
        'if [ "$interactive" = "0" ]; then echo \'{}\'; exit 0; fi\n'
        "exec sleep 600\n"
    )
    stub.chmod(0o755)


def _parse_launch(record: Path) -> tuple[dict[str, str], list[str]]:
    """Parse one stub launch record into (env fields, argv).

    :param record: A ``launch.<pid>`` file written by the stub.
    :returns: Mapping of the recorded env fields, and the argv list.
    """
    lines = record.read_text().splitlines()
    fields = dict(
        line.split("=", 1) for line in lines if "=" in line and not line.startswith("ARG:")
    )
    argv = [line[len("ARG:") :] for line in lines if line.startswith("ARG:")]
    return fields, argv


def _pane_launch(launches: Path) -> tuple[dict[str, str], list[str]] | None:
    """The interactive terminal pane launch, once recorded.

    Filters out the host-side probes: ``claude --version`` never records,
    headless catalog probes record ``INTERACTIVE=0``, and ``claude auth …``
    records ``FIRSTARG=auth``.

    :param launches: The stub's record directory.
    :returns: (env fields, argv) of the pane's ``claude``, or ``None``.
    """
    for record in sorted(launches.glob("launch.*")):
        fields, argv = _parse_launch(record)
        if fields.get("INTERACTIVE") == "1" and fields.get("FIRSTARG") != "auth":
            return fields, argv
    return None


def _kill_parked_stubs(launches: Path) -> None:
    """Kill the parked ``sleep`` processes recorded by the stub.

    The stub ``exec``s sleep, so each record's filename PID is the parked
    process itself; killing exactly those avoids touching anything else.

    :param launches: The stub's record directory.
    """
    for record in launches.glob("launch.*"):
        with contextlib.suppress(ValueError, ProcessLookupError, PermissionError):
            os.kill(int(record.suffix.lstrip(".")), signal.SIGKILL)


def test_claude_native_session_honors_claude_config_dir(tmp_path: Path) -> None:
    """
    A claude-native session must run on the exported ``CLAUDE_CONFIG_DIR`` profile.

    Journey (the reporter's): export ``CLAUDE_CONFIG_DIR`` pointing at a
    second, logged-in Claude profile; start the host daemon from that shell;
    start a Claude Code session from the web UI in a folder Claude has not
    seen before.

    Expected: the pane's ``claude`` process receives the exported
    ``CLAUDE_CONFIG_DIR``; workspace trust is seeded into
    ``$CLAUDE_CONFIG_DIR/.claude.json``; the per-session ``--settings`` file
    chains the exported profile's ``statusLine``. Buggy behavior: the env
    strip drops the variable (the session runs the default ``~/.claude``
    account), trust is written to ``~/.claude.json``, and the settings chain
    the default profile's ``statusLine``.

    :param tmp_path: Per-test temp dir (server DB, homes, stub claude).
    """
    home = tmp_path / "home"
    work_profile = home / "profiles" / "work"
    default_claude_dir = home / ".claude"
    workspace = tmp_path / "ws" / "fresh-project"
    stub_bin = tmp_path / "bin"
    launches = tmp_path / "launches"
    for directory in (
        home,
        work_profile,
        default_claude_dir,
        workspace,
        stub_bin,
        launches,
        home / ".omnigent",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    # Journey step 1: a second, logged-in "work" profile under
    # CLAUDE_CONFIG_DIR, distinguishable from the machine's default
    # ~/.claude profile (the "personal account") by account email and by a
    # marker statusLine in each profile's settings.json.
    (work_profile / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "lastOnboardingVersion": "2.0.0",
                "oauthAccount": {"emailAddress": "work@example.com"},
                "projects": {},
            }
        )
    )
    (work_profile / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "echo WORK-PROFILE-STATUSLINE"}})
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "lastOnboardingVersion": "2.0.0",
                "oauthAccount": {"emailAddress": "personal@example.com"},
                "projects": {},
            }
        )
    )
    (default_claude_dir / "settings.json").write_text(
        json.dumps(
            {"statusLine": {"type": "command", "command": "echo DEFAULT-PROFILE-STATUSLINE"}}
        )
    )

    _write_claude_stub(stub_bin, launches)

    host_id = uuid.uuid4().hex
    (home / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": f"e2e-host-{host_id[:8]}"}})
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    daemon_log_path = tmp_path / "daemon.log"
    server_log = (tmp_path / "server.log").open("w")
    daemon_log = daemon_log_path.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    daemon_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
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
                f"sqlite:///{tmp_path}/chat.db",
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({}),
            cwd=str(_REPO_ROOT),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        # Journey step 2: the host daemon starts from the shell that exported
        # CLAUDE_CONFIG_DIR. The var is handed straight to the daemon, which
        # is *more generous* than the real ``omni host`` path — the
        # CLI->daemon env strip would already have dropped it — so a fixed
        # daemon->runner path is sufficient for this test to pass.
        daemon_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", base_url],
            env=_localhost_env(
                {
                    "HOME": str(home),
                    "CLAUDE_CONFIG_DIR": str(work_profile),
                    "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "OMNIGENT_PROCESS_LOG_FILE": str(daemon_log_path),
                }
            ),
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=daemon_log,
        )

        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            try:
                hosts = _http.get(f"{base_url}/v1/hosts", timeout=2.0)
                if hosts.status_code == 200 and any(
                    h["host_id"] == host_id and h["status"] == "online"
                    for h in hosts.json().get("hosts", [])
                ):
                    online = True
                    break
            except httpx.HTTPError:
                # The server/daemon is still booting; transient connection
                # errors are expected while polling and simply retried.
                pass
            time.sleep(_POLL_S)
        assert online, (
            f"host never came online; daemon log:\n{daemon_log_path.read_text()[-3000:]}"
        )

        # Journey step 3: start a Claude Code session the web-UI way — the
        # built-in claude-native agent + host_id + a fresh workspace. The
        # host launches a runner, which auto-creates the Claude terminal.
        agents = _http.get(f"{base_url}/v1/agents", timeout=10.0)
        agents.raise_for_status()
        agent_id = next(a["id"] for a in agents.json()["data"] if a["name"] == "claude-native-ui")
        create = _http.post(
            f"{base_url}/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=_PANE_TIMEOUT_S,
        )
        create.raise_for_status()

        # Journey step 4: wait for the pane's claude process, then observe
        # which profile the session actually runs on.
        deadline = time.monotonic() + _PANE_TIMEOUT_S
        pane: tuple[dict[str, str], list[str]] | None = None
        while time.monotonic() < deadline:
            pane = _pane_launch(launches)
            if pane is not None:
                break
            time.sleep(_POLL_S)
        assert pane is not None, (
            f"pane claude never launched; daemon log:\n{daemon_log_path.read_text()[-8000:]}"
        )
        pane_fields, pane_argv = pane

        # The trust write happens around terminal bring-up; give it a short
        # grace period to land in either profile before reading final state.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            default_cfg = json.loads((home / ".claude.json").read_text())
            work_cfg = json.loads((work_profile / ".claude.json").read_text())
            if str(workspace) in default_cfg.get("projects", {}) or str(workspace) in work_cfg.get(
                "projects", {}
            ):
                break
            time.sleep(_POLL_S)

        settings_marker = "no --settings flag on the pane launch"
        if "--settings" in pane_argv:
            settings_path = Path(pane_argv[pane_argv.index("--settings") + 1])
            if settings_path.exists():
                settings_text = settings_path.read_text()
                if "WORK-PROFILE-STATUSLINE" in settings_text:
                    settings_marker = "WORK-PROFILE-STATUSLINE"
                elif "DEFAULT-PROFILE-STATUSLINE" in settings_text:
                    settings_marker = "DEFAULT-PROFILE-STATUSLINE"
                else:
                    settings_marker = "neither profile's statusLine"
            else:
                settings_marker = f"--settings file missing: {settings_path}"

        failures: list[str] = []
        if pane_fields.get("CLAUDE_CONFIG_DIR") != str(work_profile):
            failures.append(
                "the pane's claude launched with CLAUDE_CONFIG_DIR="
                f"{pane_fields.get('CLAUDE_CONFIG_DIR')!r} instead of the exported "
                f"work profile {str(work_profile)!r} — the host->runner env strip "
                "dropped it, so the session runs on the default ~/.claude account"
            )
        if str(workspace) not in work_cfg.get("projects", {}):
            where = (
                "the default ~/.claude.json"
                if str(workspace) in default_cfg.get("projects", {})
                else "no profile at all"
            )
            failures.append(
                "workspace trust / onboarding pre-accept was not seeded into "
                f"$CLAUDE_CONFIG_DIR/.claude.json — it went to {where}, so the "
                "exported profile never sees the folder as trusted"
            )
        if settings_marker != "WORK-PROFILE-STATUSLINE":
            failures.append(
                "the per-session --settings file chains the user statusLine from "
                f"the wrong profile: got {settings_marker!r}, expected the exported "
                "profile's WORK-PROFILE-STATUSLINE (settings are read from the "
                "hard-coded ~/.claude/settings.json)"
            )
        assert not failures, (
            "claude-native ignored the exported CLAUDE_CONFIG_DIR:\n- "
            + "\n- ".join(failures)
            + f"\npane argv: {pane_argv}"
        )
    finally:
        _terminate(daemon_proc)
        _terminate(server_proc)
        _kill_parked_stubs(launches)
        server_log.close()
        daemon_log.close()
