"""E2E: pi-native pre-launch model options + spec model on the unmanaged-Pi
fallback path.

Both facets share one precondition: a host whose Pi is logged in from its
own ``~/.pi/agent`` (``auth.json`` + ``models-store.json`` with a usable
model) but which has **no** omnigent-configured provider, so
``resolve_pi_native_provider()`` returns ``None``. Nothing about that state
is unusual -- it is the default whenever a user logs into Pi directly and
never runs ``omnigent setup``.

Facet 1 (empty picker, surface ``web``):
    ``GET /v1/hosts/{id}/harnesses/pi-native/model-options`` must offer the
    host's usable Pi models so the pre-launch model picker in the
    Configure-Pi dialog lists them. It currently returns ``{"models": []}``
    because ``pi_native_model_options()`` early-returns ``[]`` on a ``None``
    provider (``omnigent/harnesses/pi_native/credentials.py``), so the picker shows
    only "Default".

Facet 2 (dropped pick, surface ``cli``):
    a pi-native session whose spec pins ``executor.model`` must launch the
    real ``pi`` CLI with ``--model <resolved>``. The launched argv currently
    LACKS ``--model`` because ``_auto_create_pi_terminal`` only appends the
    resolved launch args inside the ``if provider is not None`` branch
    (``omnigent/runner/native/orchestration.py``), so the pinned model is
    silently dropped and Pi opens its own default.

Facet 2 observes the launched argv through a test-owned Pi *user extension*
seeded into the daemon HOME's ``~/.pi/agent/extensions/`` (Pi's supported
auto-discovery): on load it atomically writes the real ``process.argv`` into
the session's bridge dir. This replaces polling ``/proc`` for the bridge
marker: Pi rewrites ``process.title`` early in startup, erasing its launch
argv from ``/proc/<pid>/cmdline``, so a /proc poll races the rewrite and can
miss the process entirely.

Both assertions are written against the FIXED behavior, so this module is
RED on the buggy build (facet 1: empty ``models``; facet 2: no ``--model``)
and turns GREEN once the fallback path is fixed. It runs against the mock
LLM (no real credentials), but launching the real Pi terminal needs
``pi`` / ``tmux`` / ``node`` on PATH; the module skips cleanly when any is
absent.

    .venv/bin/python -m pytest tests/e2e/test_pi_native_unmanaged_model.py -v
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
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

# The model the host's Pi is logged in with (seeded into models-store.json)
# and the model the pi-native spec pins. Any usable Pi model works; a fixed
# id keeps the argv assertion in facet 2 unambiguous.
_PI_MODEL_ID = "claude-sonnet-4-5"
_PINNED_SPEC_MODEL = f"anthropic/{_PI_MODEL_ID}"

# Test-owned argv observer: a seeded Pi user extension records the real
# process identity (pid, argv, cwd -- never environment) when the launched
# CLI loads it. Mechanism details: the module docstring.
_OBSERVER_EXTENSION_NAME = "omnigent-e2e-argv-observer.js"
_OBSERVER_OUTPUT_FILE = "e2e-argv-observation.json"
_OBSERVER_EXTENSION_SOURCE = """\
// Seeded by tests/e2e/test_pi_native_unmanaged_model.py and loaded through
// Pi's user-extension auto-discovery (~/.pi/agent/extensions/).
const fs = require("fs");
const path = require("path");

module.exports = function () {
    try {
        const argv = process.argv.slice();
        const flag = argv.indexOf("--extension");
        if (flag < 0 || flag + 1 >= argv.length) return;
        const bridgeDir = path.resolve(path.dirname(argv[flag + 1]));
        const out = path.join(bridgeDir, "__OBSERVER_OUTPUT_FILE__");
        const tmp = out + ".tmp";
        fs.writeFileSync(
            tmp,
            JSON.stringify({ pid: process.pid, argv: argv, cwd: process.cwd() })
        );
        fs.renameSync(tmp, out);
    } catch (err) {
        // Observation must never break the session under test.
    }
};
""".replace("__OBSERVER_OUTPUT_FILE__", _OBSERVER_OUTPUT_FILE)

# Skip the whole module unless the real Pi terminal toolchain is present:
# the launch path shells out to node -> pi inside a runner-owned tmux pane.
pytestmark = [
    pytest.mark.skipif(
        (_reason := cli_unavailable_reason("pi")) is not None,
        reason=f"pi-native unmanaged-model e2e needs a runnable 'pi' CLI; {_reason}.",
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


def _bridge_digest(session_id: str) -> str:
    """Return the hashed bridge-dir segment for *session_id*.

    The harness writes a session's Pi bridge under
    ``~/.omnigent/pi-native/<sha256(session_id)[:32]>``.

    :param session_id: The session/conversation id.
    :returns: The 32-hex digest segment.
    """
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _bridge_dir(home: Path, session_id: str) -> Path:
    """Return the session's bridge dir under the daemon HOME *home*.

    :param home: The spawned daemon's HOME.
    :param session_id: The session/conversation id.
    :returns: ``<home>/.omnigent/pi-native/<digest>``.
    """
    return home / ".omnigent" / "pi-native" / _bridge_digest(session_id)


def _bridge_marker(session_id: str) -> str:
    """Return the bridge-dir path segment used as a ``/proc`` sweep needle.

    The tmux launcher embeds the whole pi command -- including this segment
    via ``--extension`` / ``--session-dir`` -- as one argv element and never
    rewrites it, so the segment stays a reliable needle for the cleanup
    sweep. It is NOT usable to find the pi CLI itself: Pi's ``process.title``
    rewrite erases it from that process's cmdline.

    :param session_id: The session/conversation id.
    :returns: ``"pi-native/<32-hex>"``.
    """
    return f"pi-native/{_bridge_digest(session_id)}"


def _read_argv_observation(bridge_dir: Path) -> dict | None:
    """Return the observer extension's record for the session, if written yet.

    The extension publishes via write-temp-then-rename, so a visible record
    is always complete.

    :param bridge_dir: The session's bridge dir from :func:`_bridge_dir`.
    :returns: The parsed ``{pid, argv, cwd}`` record, or ``None``.
    """
    try:
        record = json.loads((bridge_dir / _OBSERVER_OUTPUT_FILE).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _live_observed_pid(observation: dict, *, marker: str, workspace: Path) -> int | None:
    """Return the recorded pid when the live process is still that session's Pi.

    A recorded pid alone is not safe to signal: it could have exited and been
    recycled by an unrelated process. The pid is trusted only when the live
    process's cwd is the session workspace and its cmdline is either the
    pre-rewrite launch argv (bridge marker present) or the post-rewrite bare
    ``pi`` title.

    :param observation: The observer record from :func:`_read_argv_observation`.
    :param marker: The session's bridge marker from :func:`_bridge_marker`.
    :param workspace: The session workspace the pane launched in.
    :returns: The verified pid, or ``None`` when unverifiable.
    """
    pid = observation.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    proc_dir = Path("/proc") / str(pid)
    try:
        cmdline = (proc_dir / "cmdline").read_bytes()
        cwd = os.path.realpath(proc_dir / "cwd")
    except OSError:
        return None
    if cwd != os.path.realpath(workspace):
        return None
    if marker.encode() not in cmdline and cmdline.rstrip(b"\x00") != b"pi":
        return None
    return pid


def _kill_pi_processes(marker: str) -> None:
    """Best-effort SIGKILL of any process still naming *marker*.

    This sweep catches the tmux launcher, whose cmdline embeds the pi
    command string and is never rewritten. The pi CLI itself erases the
    marker through its ``process.title`` rewrite and is killed via the
    verified observer pid in :func:`_cleanup_pi_session` instead.

    :param marker: The bridge segment from :func:`_bridge_marker`.
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


def _cleanup_pi_session(*, marker: str, observation: dict | None, workspace: Path) -> None:
    """Best-effort teardown of one session's launched Pi tree.

    Primary path: SIGKILL the pid the observer recorded, but only after the
    live process is re-verified as that session's Pi -- a bare recorded pid
    is never signaled, since it could have exited and been recycled.
    Secondary: the marker sweep for the tmux launcher.

    :param marker: The session's bridge marker from :func:`_bridge_marker`.
    :param observation: The observer record, or ``None`` when none was written.
    :param workspace: The session workspace the pane launched in.
    """
    if observation is not None:
        pid = _live_observed_pid(observation, marker=marker, workspace=workspace)
        if pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    _kill_pi_processes(marker)


def _capture_pane_diagnostics(marker: str, out_path: Path) -> None:
    """Best-effort capture of the session's tmux pane content after a failure.

    The daemon log already lives under the test basetemp and shows the runner
    side; this preserves what the Pi pane itself displayed. No-ops when tmux,
    the pane, or the ``pane_start_command`` format is unavailable.

    :param marker: The session's bridge marker (present in the pane's start
        command string).
    :param out_path: Destination under the test basetemp.
    """
    try:
        listing = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index}.#{pane_index} #{pane_start_command}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if listing.returncode != 0:
            return
        chunks = []
        for line in listing.stdout.splitlines():
            target, _, start_command = line.partition(" ")
            if marker not in start_command:
                continue
            capture = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", target],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if capture.returncode == 0:
                chunks.append(f"--- {target} ---\n{capture.stdout}")
        if chunks:
            out_path.write_text("\n".join(chunks))
    except (OSError, subprocess.TimeoutExpired):
        pass


class _UnmanagedPiHost:
    """A spawned host daemon whose Pi is logged in but omnigent-unmanaged.

    :param proc: The daemon subprocess handle.
    :param host_id: The registered host id.
    :param home: The daemon's HOME (holds ``.pi/agent`` + ``.omnigent``).
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


def _seed_unmanaged_pi_home(home: Path) -> str:
    """Seed *home* with a logged-in-but-unmanaged Pi and a host config.

    Writes ``.pi/agent/auth.json`` (an api-key login) and
    ``.pi/agent/models-store.json`` (one usable model) so Pi itself has
    models, while ``.omnigent/config.yaml`` carries only a host block and
    NO provider setup -- exactly the state where
    ``resolve_pi_native_provider()`` returns ``None``. Also seeds
    ``.pi/agent/extensions/`` with the test-owned argv observer (see the
    module constants), which every real Pi launch under this HOME
    auto-loads.

    :param home: The daemon HOME to populate.
    :returns: The host id written into ``config.yaml``.
    """
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-unmanaged-pi-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    pi_agent = home / ".pi" / "agent"
    pi_agent.mkdir(parents=True, exist_ok=True)
    extensions_dir = pi_agent / "extensions"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    (extensions_dir / _OBSERVER_EXTENSION_NAME).write_text(_OBSERVER_EXTENSION_SOURCE)
    (pi_agent / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "sk-e2e-unmanaged-fake"}})
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
            # Server isn't accepting connections yet (still starting up);
            # keep polling until the deadline.
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


@pytest.fixture(scope="module")
def unmanaged_pi_host(
    live_server: str,
    http_client: httpx.Client,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_UnmanagedPiHost]:
    """Spawn one host daemon with a logged-in-but-unmanaged Pi for both facets.

    :param live_server: Server URL the daemon registers with.
    :param http_client: HTTP client pointed at the server.
    :param tmp_path_factory: Module-scoped temp dir factory (the daemon HOME).
    :yields: The spawned :class:`_UnmanagedPiHost`.
    """
    home = tmp_path_factory.mktemp("unmanaged-pi-home")
    host_id = _seed_unmanaged_pi_home(home)
    daemon_log = home / "host-daemon.log"
    # Pin BOTH HOME and OMNIGENT_CONFIG_HOME to the seeded dir so the daemon
    # reads the seeded (provider-less) omnigent config and Pi login, not any
    # ambient OMNIGENT_CONFIG_HOME the surrounding session exported. Without
    # this the daemon inherits the caller's provider config and
    # resolve_pi_native_provider() would be None for the wrong reason (an
    # unresolvable managed provider) instead of the reported one (no managed
    # provider at all).
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    # Prepend ABSOLUTE worktree roots to PYTHONPATH. The runner the daemon
    # spawns runs with cwd=<workspace>, so any relative PYTHONPATH entry (the
    # ambient one here is ``sdks/python-client:sdks/ui``, and it omits the
    # worktree root) dangles and the runner fails with
    # ``ModuleNotFoundError: omnigent``. Absolute paths resolve from any cwd;
    # in CI, where the checkout is the worktree, this is redundant-but-harmless.
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
        yield _UnmanagedPiHost(proc=proc, host_id=host_id, home=home, daemon_log=daemon_log)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_facet1_prelaunch_model_options_offer_the_hosts_pi_models(
    unmanaged_pi_host: _UnmanagedPiHost,
    http_client: httpx.Client,
) -> None:
    """Facet 1: the pre-launch picker must list the host's usable Pi models.

    A host with a logged-in Pi (one model in ``models-store.json``) but no
    omnigent-managed provider must still surface that model to the
    pre-launch picker. The buggy build returns ``{"models": []}`` (only
    "Default" in the UI); the fix enumerates Pi's own models.

    :param unmanaged_pi_host: The spawned unmanaged-Pi host.
    :param http_client: HTTP client pointed at the server.
    """
    resp = http_client.get(
        f"/v1/hosts/{unmanaged_pi_host.host_id}/harnesses/pi-native/model-options",
        timeout=30.0,
    )
    assert resp.status_code == 200, f"model-options failed: {resp.status_code} {resp.text}"
    models = resp.json().get("models", [])
    assert models, (
        "pi-native pre-launch model-options returned an EMPTY catalog while the "
        f"host's Pi is logged in with {_PI_MODEL_ID!r} -- the Configure-Pi picker "
        "shows only 'Default'. pi_native_model_options() early-returns [] when "
        f"resolve_pi_native_provider() is None. Got: {resp.text}"
    )


def test_facet2_spec_pinned_model_reaches_the_launched_pi(
    unmanaged_pi_host: _UnmanagedPiHost,
    http_client: httpx.Client,
) -> None:
    """Facet 2: a spec-pinned ``executor.model`` must reach the ``pi`` argv.

    Create a pi-native terminal session whose spec pins ``executor.model``,
    let the runner auto-launch the real ``pi`` CLI, then read the argv the
    test-owned user extension recorded inside that process (the module
    header explains why /proc polling raced Pi's ``process.title`` rewrite).
    The buggy build launches ``pi`` with NO ``--model`` (the pick is dropped
    because ``_auto_create_pi_terminal`` only appends launch args when a
    provider is configured); the fix passes ``--model`` through.

    :param unmanaged_pi_host: The spawned unmanaged-Pi host.
    :param http_client: HTTP client pointed at the server.
    """
    host = unmanaged_pi_host
    spec_yaml = "\n".join(
        [
            "name: pi-native-ui",
            "prompt: |",
            "  Pi is running in the session terminal.",
            "executor:",
            "  harness: pi-native",
            f"  model: {_PINNED_SPEC_MODEL}",
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
    bridge_dir = _bridge_dir(host.home, session_id)

    observation: dict | None = None
    deadline = time.monotonic() + 150.0
    try:
        while time.monotonic() < deadline:
            observation = _read_argv_observation(bridge_dir)
            if observation is not None:
                break
            if host.proc.poll() is not None:
                raise AssertionError(
                    f"host daemon exited (rc={host.proc.returncode}) before pi launched; "
                    f"log tail:\n{host.daemon_log.read_text()[-2000:]}"
                )
            time.sleep(1.0)

        assert observation is not None, (
            "the test-owned Pi user extension never recorded a launch for session "
            f"{session_id!r}: the real pi CLI either never started or never "
            "auto-loaded ~/.pi/agent/extensions/ from the seeded HOME; daemon log "
            f"tail:\n{host.daemon_log.read_text()[-2000:]}"
        )
        pi_argv = [str(token) for token in observation.get("argv") or []]
        # npm exposes the package bin as .bin/pi (a shim or symlink); resolve
        # through it so both entry styles verify the real package identity.
        resolved_entry = os.path.realpath(pi_argv[1]) if len(pi_argv) > 1 else ""
        assert resolved_entry.endswith(os.path.join("pi-coding-agent", "dist", "cli.js")), (
            "the observed process is not the real Pi CLI -- expected its node "
            "entry point to resolve to .../pi-coding-agent/dist/cli.js, got "
            f"argv: {pi_argv} (resolved: {resolved_entry})"
        )
        assert "--model" in pi_argv, (
            "the spec-pinned model was silently DROPPED: the launched pi argv "
            f"carries no --model flag. _auto_create_pi_terminal appends --model "
            f"only when a provider is configured, so on the unmanaged-Pi path the "
            f"pin ({_PINNED_SPEC_MODEL!r}) never reaches Pi. argv: {pi_argv}"
        )
        model_idx = pi_argv.index("--model")
        assert model_idx + 1 < len(pi_argv), f"--model had no value in argv: {pi_argv}"
        assert pi_argv[model_idx + 1] == _PINNED_SPEC_MODEL, (
            f"the launched pi CLI resolved --model {pi_argv[model_idx + 1]!r} instead "
            f"of the spec-pinned {_PINNED_SPEC_MODEL!r}. argv: {pi_argv}"
        )
        assert observation.get("cwd") == os.path.realpath(workspace), (
            f"the observed Pi ran in {observation.get('cwd')!r} instead of the "
            f"session workspace {os.path.realpath(workspace)!r}"
        )
        assert _live_observed_pid(observation, marker=marker, workspace=workspace) is not None, (
            "the recorded Pi process is not verifiably alive for session "
            f"{session_id!r}: a persisted observation alone does not prove a "
            "running Pi (its /proc entry is gone or no longer matches this "
            "session's workspace/cmdline)"
        )
    except Exception:
        _capture_pane_diagnostics(marker, host.home / "pi-pane-diagnostics.txt")
        raise
    finally:
        _cleanup_pi_session(marker=marker, observation=observation, workspace=workspace)


def test_live_observed_pid_rejects_stale_or_recycled_pids(tmp_path: Path) -> None:
    """Negative control: stale or recycled recorded pids must never verify.

    Cleanup signals only a pid verified against the live process; these
    controls pin that a persisted record alone -- a dead pid, or a live but
    unrelated process that recycled it -- cannot satisfy the verification.

    :param tmp_path: Stand-in session workspace.
    """
    marker = _bridge_marker("conv_negative_control")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    assert (
        _live_observed_pid(
            {"pid": dead.pid, "argv": [], "cwd": os.path.realpath(workspace)},
            marker=marker,
            workspace=workspace,
        )
        is None
    )
    assert (
        _live_observed_pid(
            {"pid": os.getpid(), "argv": [], "cwd": os.path.realpath(workspace)},
            marker=marker,
            workspace=workspace,
        )
        is None
    )
