"""E2E regression guard: a pi-native session's explicit, provider-qualified
model selection must survive a cold-resume relaunch instead of being rewritten
through the configured ``default: pi`` provider.

Reported journey:

1. Authenticate Pi with its own ``openai-codex`` provider (OAuth).
2. Configure a different Omnigent provider (e.g. OpenRouter) with
   ``default: pi``.
3. Start a Pi-native session and select ``openai-codex/gpt-5.6-sol``.
4. Send a message -- it works (the web picker applies the model live inside
   the resident Pi via ``pi.setModel``, which resolves against Pi's own
   ``openai-codex`` login).
5. Leave the session idle so its terminal is reaped.
6. Send another message -- the runner cold-resumes the session and rebuilds
   the launch args from the persisted selection.

Actual: on the cold-resume relaunch the runner re-resolves the persisted
``openai-codex/gpt-5.6-sol`` through the ``default: pi`` provider and rewrites
it to ``omnigent/openai-codex/gpt-5.6-sol``. The next message fails with
``Pi model error: 400: {"message":"openai-codex/gpt-5.6-sol is not a valid
model ID","code":400}`` and the model picker is reduced to that single
malformed entry.

This module reproduces the *relaunch* precondition faithfully: it stands up a
real host daemon whose Pi is logged into ``openai-codex`` and whose
``config.yaml`` carries an OpenRouter gateway provider defaulted to ``pi``,
then creates a pi-native session that carries the explicit
``openai-codex/gpt-5.6-sol`` selection (``executor.model`` -- the same field
the runner reads back as the launch model on every relaunch). The runner
auto-launches the real ``pi`` CLI inside a private tmux server; the launched
pane's start command is the argv the relaunch decided on, read back here from
that server's socket.

Two facets are asserted, both against the FIXED behavior, so the module is RED
on the buggy build and turns GREEN once the explicit selection is preserved:

Facet A (surface ``cli``/``terminal`` -- the launched model / the 400):
    the launched Pi must be pointed at the user's explicit
    ``openai-codex/gpt-5.6-sol`` selection, NOT the ``default: pi``-routed
    ``omnigent/openai-codex/gpt-5.6-sol`` alias that upstream rejects with the
    reported 400.

Facet B (surface ``web`` -- the model picker):
    the model catalog Pi launches against must still offer Pi's real
    openai-codex selection. A launch on Pi's own login writes no managed
    ``models.json`` (Pi serves its own openai-codex catalog); a managed
    ``models.json``, when written, must not register the malformed
    ``omnigent/openai-codex/gpt-5.6-sol`` alias that collapses the picker.

It runs against the mock LLM (no real credentials); launching the real Pi
terminal needs ``pi`` / ``tmux`` / ``node`` on PATH, so the module skips
cleanly when any is absent.

    .venv/bin/python -m pytest tests/e2e/test_pi_native_cold_resume_model_rewrite_e2e.py -v
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
import tempfile
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

# Pi's OWN provider and the model the user explicitly selects. ``openai-codex``
# is one of Pi's built-in login providers (NOT an omnigent-managed provider id),
# so a provider-qualified pick that names it is the exact shape the bug mangles.
_PI_OWN_PROVIDER = "openai-codex"
_PI_MODEL_ID = "gpt-5.6-sol"
_EXPLICIT_SELECTION = f"{_PI_OWN_PROVIDER}/{_PI_MODEL_ID}"
# The malformed id the buggy cold-resume launch produces by routing the
# explicit selection through the ``default: pi`` omnigent provider -- the id
# upstream rejects with "not a valid model ID".
_MANGLED_MODEL_REF = f"omnigent/{_EXPLICIT_SELECTION}"

# Skip the whole module unless the real Pi terminal toolchain is present: the
# launch path shells out to node -> pi inside a runner-owned tmux pane.
pytestmark = [
    pytest.mark.skipif(
        (_reason := cli_unavailable_reason("pi")) is not None,
        reason=f"pi-native cold-resume e2e needs a runnable 'pi' CLI; {_reason}.",
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
    """
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _bridge_dir(home: Path, session_id: str) -> Path:
    """Return the session's bridge dir under the daemon HOME *home*."""
    return home / ".omnigent" / "pi-native" / _bridge_digest(session_id)


def _read_managed_models_config(bridge_dir: Path) -> dict | None:
    """Return the managed ``models.json`` Pi launches against, if written.

    ``pi_native_provider_launch`` writes it to ``<bridge_dir>/pi-agent/models.json``
    when the launch routes through an omnigent-managed provider; a launch on
    Pi's own login writes none.
    """
    try:
        raw = json.loads((bridge_dir / "pi-agent" / "models.json").read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _model_refs(models_config: dict) -> list[str]:
    """Return the provider-qualified ``provider/model`` refs in a models.json.

    These are exactly the refs Pi's in-session picker enumerates and the
    ``--provider``/``--model`` launch argv is built from.
    """
    refs: list[str] = []
    providers = models_config.get("providers", {})
    if not isinstance(providers, dict):
        return refs
    for provider_id, payload in providers.items():
        if not isinstance(payload, dict):
            continue
        for model in payload.get("models", []):
            if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"]:
                refs.append(f"{provider_id}/{model['id']}")
    return refs


def _terminal_tmux_sockets() -> list[Path]:
    """Sockets of the runner's private per-terminal tmux servers.

    Each terminal runs in its own tmux server on an isolated socket under the
    system temp dir (see ``omnigent.inner.terminal``), so the default tmux
    server never sees these panes.
    """
    return sorted(Path(tempfile.gettempdir()).glob("omnigent-terminal-*/tmux.sock"))


def _pi_pane_start_command(marker: str) -> str | None:
    """Return the launched pi pane's full start command, if the pane exists.

    The pane start command is the shell-quoted pi argv the relaunch decided on
    -- the exact seam the bug corrupts -- located by the session's bridge-dir
    *marker* across the runner's private tmux servers.
    """
    for socket_path in _terminal_tmux_sockets():
        try:
            listing = subprocess.run(
                [
                    "tmux",
                    "-S",
                    str(socket_path),
                    "list-panes",
                    "-a",
                    "-F",
                    "#{pane_start_command}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if listing.returncode != 0:
            continue
        for line in listing.stdout.splitlines():
            if marker in line:
                return line
    return None


def _capture_pane_diagnostics(marker: str, out_path: Path) -> None:
    """Best-effort capture of the session's tmux pane content after a failure."""
    chunks = []
    for socket_path in _terminal_tmux_sockets():
        try:
            listing = subprocess.run(
                [
                    "tmux",
                    "-S",
                    str(socket_path),
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
                continue
            for line in listing.stdout.splitlines():
                target, _, start_command = line.partition(" ")
                if marker not in start_command:
                    continue
                capture = subprocess.run(
                    ["tmux", "-S", str(socket_path), "capture-pane", "-p", "-t", target],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if capture.returncode == 0:
                    chunks.append(f"--- {target} ---\n{capture.stdout}")
        except (OSError, subprocess.TimeoutExpired):
            continue
    if chunks:
        with contextlib.suppress(OSError):
            out_path.write_text("\n".join(chunks))


class _ColdResumePiHost:
    """A spawned host daemon: Pi logged into openai-codex + a ``default: pi`` provider.

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


def _seed_cold_resume_pi_home(home: Path) -> str:
    """Seed *home* to reproduce the ticket's environment.

    Writes:
    - ``.pi/agent/auth.json`` + ``.pi/agent/models-store.json`` giving Pi its
      own ``openai-codex`` login with ``gpt-5.6-sol`` (so the explicit
      selection is a real, valid Pi model on Pi's own credentials);
    - ``.omnigent/config.yaml`` with a host block AND an OpenRouter gateway
      provider defaulted to ``pi`` -- the competing default the bug routes the
      explicit selection through.

    :returns: The host id written into ``config.yaml``.
    """
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-cold-resume-pi-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "host": {"host_id": host_id, "name": host_name},
                "providers": {
                    "openrouter": {
                        "kind": "gateway",
                        # The competing default from the ticket: an unrelated
                        # Omnigent provider claims the pi surface, so the
                        # cold-resume launch re-resolves the explicit
                        # openai-codex selection through it.
                        "default": "pi",
                        "openai": {
                            "base_url": "https://openrouter.ai/api/v1",
                            "api_key": "sk-or-e2e-fake",
                            "models": {"pi": "gpt-4o-mini"},
                        },
                    }
                },
            },
            default_flow_style=False,
            sort_keys=True,
        )
    )
    pi_agent = home / ".pi" / "agent"
    pi_agent.mkdir(parents=True, exist_ok=True)
    (pi_agent / "auth.json").write_text(
        json.dumps({_PI_OWN_PROVIDER: {"type": "oauth", "access": "e2e-fake-openai-codex-token"}})
    )
    (pi_agent / "models-store.json").write_text(
        json.dumps(
            {
                _PI_OWN_PROVIDER: {
                    "models": [
                        {
                            "id": _PI_MODEL_ID,
                            "name": "GPT-5.6 Sol",
                            "api": "openai-responses",
                            "provider": _PI_OWN_PROVIDER,
                            "baseUrl": "https://chatgpt.com/backend-api/codex",
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
    """Poll ``GET /v1/hosts`` until *host_id* is online."""
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


@pytest.fixture(scope="module")
def cold_resume_pi_host(
    live_server: str,
    http_client: httpx.Client,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_ColdResumePiHost]:
    """Spawn one host daemon reproducing the ticket's environment."""
    home = tmp_path_factory.mktemp("cold-resume-pi-home")
    host_id = _seed_cold_resume_pi_home(home)
    daemon_log = home / "host-daemon.log"
    # Pin BOTH HOME and OMNIGENT_CONFIG_HOME to the seeded dir so the daemon
    # reads the seeded config (openrouter default: pi) and Pi login, not any
    # ambient config the surrounding session exported.
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    # Prepend ABSOLUTE worktree roots to PYTHONPATH. The runner the daemon
    # spawns runs with cwd=<workspace>, so a relative PYTHONPATH entry dangles
    # and the runner fails with ``ModuleNotFoundError: omnigent``. Absolute
    # paths resolve from any cwd; in CI (checkout IS the worktree) redundant.
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
        yield _ColdResumePiHost(proc=proc, host_id=host_id, home=home, daemon_log=daemon_log)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _create_pi_native_session(http_client: httpx.Client, host: _ColdResumePiHost) -> str:
    """Create a pi-native terminal session carrying the explicit selection.

    ``executor.model`` is the field the runner reads back as the launch model
    on every (re)launch, so pinning it here reproduces the persisted picker
    selection a cold resume rebuilds its launch args from.

    :returns: The created session id.
    """
    spec_yaml = "\n".join(
        [
            "name: pi-native-ui",
            "prompt: |",
            "  Pi is running in the session terminal.",
            "executor:",
            "  harness: pi-native",
            f"  model: {_EXPLICIT_SELECTION}",
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
    return str(create.json()["session_id"])


def test_cold_resume_keeps_explicit_openai_codex_selection(
    cold_resume_pi_host: _ColdResumePiHost,
    http_client: httpx.Client,
) -> None:
    """The relaunch must keep the explicit selection, not the default-routed alias.

    Facet A (cli/terminal): the launched Pi must be pointed at the user's
    explicit ``openai-codex/gpt-5.6-sol`` -- NOT the ``default: pi``-routed
    ``omnigent/openai-codex/gpt-5.6-sol`` alias upstream rejects with a 400.

    Facet B (web): the model catalog Pi launches against -- what its
    in-session picker enumerates -- must still offer Pi's real openai-codex
    selection, NOT be reduced to the single malformed
    ``omnigent/openai-codex/gpt-5.6-sol`` entry.

    Facet A is read from the launched pane's start command (the argv the
    relaunch decided on); facet B from the managed ``models.json``, which a
    launch on Pi's own login legitimately does not write. The module is RED
    while the launch rewrites the selection and turns GREEN once it is
    preserved.
    """
    host = cold_resume_pi_host
    session_id = _create_pi_native_session(http_client, host)
    bridge_dir = _bridge_dir(host.home, session_id)
    marker = f"pi-native/{_bridge_digest(session_id)}"

    launch_command: str | None = None
    launched = False
    deadline = time.monotonic() + 150.0
    try:
        while time.monotonic() < deadline:
            launch_command = _pi_pane_start_command(marker)
            if launch_command is not None:
                # Confirm the runner actually launched pi (a real relaunch,
                # not just a created pane): the session reports its native
                # Pi session id once the process starts.
                session = http_client.get(f"/v1/sessions/{session_id}", timeout=10.0)
                session.raise_for_status()
                if session.json().get("external_session_id"):
                    launched = True
                    break
            if host.proc.poll() is not None:
                raise AssertionError(
                    f"host daemon exited (rc={host.proc.returncode}) before pi launched; "
                    f"log tail:\n{host.daemon_log.read_text()[-2000:]}"
                )
            time.sleep(1.0)

        assert launch_command is not None, (
            f"the runner never launched a pi terminal for session {session_id!r}: "
            f"the real pi CLI either never started or the launch path did not run; "
            f"daemon log tail:\n{host.daemon_log.read_text()[-2000:]}"
        )
        assert launched, (
            f"Pi never reported its native session ID for {session_id!r}; the launch "
            f"did not complete. launched pi command:\n{launch_command}\n"
            f"daemon log tail:\n{host.daemon_log.read_text()[-2000:]}"
        )

        # Facet A: the explicit provider-qualified selection must not be
        # rewritten into the omnigent default-provider alias upstream rejects.
        assert _MANGLED_MODEL_REF not in launch_command, (
            "the cold-resume launch REWROTE the explicit openai-codex selection "
            f"through the 'default: pi' provider: the launched pi is pointed at "
            f"{_MANGLED_MODEL_REF!r}, the id that fails with the reported 400 "
            f"('{_EXPLICIT_SELECTION} is not a valid model ID'). The explicit, "
            f"provider-qualified selection must survive the relaunch unchanged. "
            f"launched pi command: {launch_command}"
        )
        assert f"--model {_EXPLICIT_SELECTION}" in launch_command, (
            "the launched pi does not carry the explicit "
            f"{_EXPLICIT_SELECTION!r} selection: the user's pick was dropped or "
            f"rewritten. launched pi command: {launch_command}"
        )

        # Facet B: the catalog Pi launches against must not be collapsed to
        # the malformed alias. A launch on Pi's own login writes no managed
        # models.json (Pi then serves the seeded openai-codex catalog); a
        # managed models.json, when written, must not register the alias.
        models_config = _read_managed_models_config(bridge_dir)
        if models_config is not None:
            refs = _model_refs(models_config)
            assert _MANGLED_MODEL_REF not in refs, (
                "the managed model catalog registers the malformed "
                f"{_MANGLED_MODEL_REF!r} entry, so the model picker collapses to "
                f"it and the model cannot be changed from the UI. Pi's real "
                f"{_EXPLICIT_SELECTION!r} selection must remain available. "
                f"models.json refs: {refs}"
            )
    except Exception:
        _capture_pane_diagnostics(marker, host.home / "pi-pane-diagnostics.txt")
        raise
    finally:
        with contextlib.suppress(httpx.HTTPError):
            http_client.delete(f"/v1/sessions/{session_id}", timeout=10.0).raise_for_status()
