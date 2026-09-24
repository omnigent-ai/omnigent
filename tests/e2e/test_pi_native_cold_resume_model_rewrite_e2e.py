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

_WORKTREE = Path(__file__).resolve().parents[2]

_PI_OWN_PROVIDER = "openai-codex"
_PI_MODEL_ID = "gpt-5.6-sol"
_EXPLICIT_SELECTION = f"{_PI_OWN_PROVIDER}/{_PI_MODEL_ID}"
_MANGLED_MODEL_REF = f"omnigent/{_EXPLICIT_SELECTION}"

pytestmark = [
    pytest.mark.skipif(
        (_reason := cli_unavailable_reason("pi")) is not None,
        reason=f"pi-native cold-resume e2e needs a runnable 'pi' CLI; {_reason}.",
    ),
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
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _bridge_dir(home: Path, session_id: str) -> Path:
    return home / ".omnigent" / "pi-native" / _bridge_digest(session_id)


def _read_managed_models_config(bridge_dir: Path) -> dict | None:
    try:
        raw = json.loads((bridge_dir / "pi-agent" / "models.json").read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _model_refs(models_config: dict) -> list[str]:
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
    return sorted(Path(tempfile.gettempdir()).glob("omnigent-terminal-*/tmux.sock"))


def _pi_pane_start_command(marker: str) -> str | None:
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
    home = tmp_path_factory.mktemp("cold-resume-pi-home")
    host_id = _seed_cold_resume_pi_home(home)
    daemon_log = home / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
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

        resources = http_client.get(f"/v1/sessions/{session_id}/resources/terminals", timeout=30.0)
        resources.raise_for_status()
        terminal_id = next(
            item["id"]
            for item in resources.json().get("data", [])
            if item.get("name") == "pi:main"
        )
        deleted = http_client.delete(
            f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}", timeout=30.0
        )
        deleted.raise_for_status()

        stopped_deadline = time.monotonic() + 30.0
        while time.monotonic() < stopped_deadline:
            if _pi_pane_start_command(marker) is None:
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            raise AssertionError("the initial Pi terminal did not stop before cold resume")

        ensured = http_client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": "pi",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            timeout=60.0,
        )
        ensured.raise_for_status()

        launch_command = None
        resumed_deadline = time.monotonic() + 150.0
        while time.monotonic() < resumed_deadline:
            launch_command = _pi_pane_start_command(marker)
            if launch_command is not None:
                break
            if host.proc.poll() is not None:
                raise AssertionError(
                    f"host daemon exited (rc={host.proc.returncode}) during cold resume; "
                    f"log tail:\n{host.daemon_log.read_text()[-2000:]}"
                )
            time.sleep(1.0)
        assert launch_command is not None, (
            f"the runner did not relaunch Pi for session {session_id!r}; "
            f"daemon log tail:\n{host.daemon_log.read_text()[-2000:]}"
        )

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
