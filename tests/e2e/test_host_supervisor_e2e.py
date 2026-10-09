"""Real-process coverage for the persistent host supervisor upgrade path."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e.conftest import lookup_agent_id, upload_agent

_OLD_VERSION = "9.9.0.dev0"
_NEW_VERSION = "9.9.0.dev1"


def _wait_for_session_host_version(
    client: httpx.Client,
    session_id: str,
    version: str,
    *,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Wait until the server's live host registry reports *version*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/health", params={"session_id": session_id})
        if response.status_code == 200:
            session = response.json().get("session", {})
            if session.get("host_online") is True and session.get("host_version") == version:
                return session
        time.sleep(0.2)
    raise AssertionError(f"host version {version!r} was not reported for {session_id}")


def _host_pid(state_dir: Path, host_id: str) -> int | None:
    """Read the foreground host's daemon record from the isolated data dir."""
    for path in (state_dir / "daemons").glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("host_id") == host_id:
            pid = record.get("pid")
            return pid if isinstance(pid, int) else None
    return None


def _write_overlay_package(root: Path, repository: Path, version_path: Path) -> None:
    """Expose the repository package while overriding only ``omnigent.version``."""
    package = root / "omnigent"
    package.mkdir(parents=True)
    (package / "version.py").write_text(f'VERSION = "{_OLD_VERSION}"\n')
    real_init = repository / "omnigent" / "__init__.py"
    (package / "__init__.py").write_text(
        "from pathlib import Path\n"
        "from pkgutil import extend_path\n"
        "__path__ = extend_path(__path__, __name__)\n"
        f"_real = Path({str(real_init)!r})\n"
        "exec(compile(_real.read_text(), str(_real), 'exec'), globals(), globals())\n"
    )
    version_path.write_text(f'VERSION = "{_OLD_VERSION}"\n')


def _write_supervisor_bootstrap(path: Path, *, version_path: Path) -> None:
    """Create a test-only supervisor subclass with a hermetic fake updater."""
    upgrade_code = (
        "from pathlib import Path; "
        f"Path({str(version_path)!r}).write_text('VERSION = \\\"{_NEW_VERSION}\\\"\\n')"
    )
    path.write_text(
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "import sys\n"
        "from omnigent.host.supervisor import HostSupervisor\n"
        "\n"
        "class TestSupervisor(HostSupervisor):\n"
        "    @property\n"
        "    def upgrade_command(self):\n"
        f"        return [sys.executable, '-c', {upgrade_code!r}]\n"
        "\n"
        "def now():\n"
        "    hour = 4 if Path(sys.argv[3]).read_text().strip() == 'due' else 2\n"
        "    return datetime(2026, 1, 2, hour, 0, tzinfo=timezone.utc)\n"
        "\n"
        "supervisor = TestSupervisor(\n"
        "    sys.argv[1],\n"
        "    state_path=Path(sys.argv[2]),\n"
        "    now=now,\n"
        ")\n"
        "raise SystemExit(supervisor.run())\n"
    )


@pytest.mark.usefixtures("live_server")
def test_host_supervisor_relaunches_real_host_with_upgraded_version(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A scheduled upgrade must replace the real child and its hello version."""
    repository = Path(__file__).resolve().parents[2]
    overlay = tmp_path / "overlay"
    version_path = overlay / "omnigent" / "version.py"
    _write_overlay_package(overlay, repository, version_path)

    state_dir = tmp_path / ".omnigent"
    state_dir.mkdir()
    host_suffix = os.urandom(8).hex()
    host_id = os.urandom(16).hex()
    (state_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": f"e2e-supervisor-{host_suffix}"}},
            sort_keys=True,
        )
    )
    bootstrap = tmp_path / "supervisor_bootstrap.py"
    _write_supervisor_bootstrap(bootstrap, version_path=version_path)
    clock_path = tmp_path / "clock"
    clock_path.write_text("before\n")
    log_path = tmp_path / "supervisor.log"
    python_path = os.pathsep.join(
        str(path)
        for path in (
            overlay,
            repository,
            repository / "sdks/python-client",
            repository / "sdks/ui",
        )
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OMNIGENT_CONFIG_HOME": str(state_dir),
        "OMNIGENT_DATA_DIR": str(state_dir),
        "OMNIGENT_WRAPPER_BYPASS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": python_path,
    }

    with log_path.open("w") as log:
        supervisor = subprocess.Popen(
            [
                sys.executable,
                "-P",
                str(bootstrap),
                live_server,
                str(tmp_path / "upgrade-date"),
                str(clock_path),
            ],
            cwd=repository,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    try:
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            hosts = http_client.get("/v1/hosts").json().get("hosts", [])
            if any(
                host.get("host_id") == host_id and host.get("status") == "online" for host in hosts
            ):
                break
            if supervisor.poll() is not None:
                raise AssertionError(
                    f"supervisor exited with {supervisor.returncode}; log:\n{log_path.read_text()}"
                )
            time.sleep(0.2)
        else:
            raise AssertionError(
                f"host {host_id!r} did not register; log:\n{log_path.read_text()}"
            )

        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "supervisor-test.yaml").write_text(
            "name: supervisor-test\n"
            "description: supervisor e2e fixture\n"
            "executor:\n"
            "  harness: openai-agents\n"
            "  model: gpt-5.4\n"
            "prompt: Reply with one word.\n"
        )
        agent_name = upload_agent(http_client, agent_dir)
        agent_id = lookup_agent_id(http_client, agent_name)
        session = http_client.post("/v1/sessions", json={"agent_id": agent_id})
        session.raise_for_status()
        session_id = session.json()["id"]
        launch = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(tmp_path)},
            timeout=60,
        )
        launch.raise_for_status()

        old_session = _wait_for_session_host_version(http_client, session_id, _OLD_VERSION)
        old_pid: int | None = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and old_pid is None:
            old_pid = _host_pid(state_dir, host_id)
            time.sleep(0.1)
        assert old_pid is not None
        assert old_session["host_version"] == _OLD_VERSION

        clock_path.write_text("due\n")
        new_session = _wait_for_session_host_version(
            http_client, session_id, _NEW_VERSION, timeout=60.0
        )
        new_pid: int | None = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and new_pid is None:
            candidate = _host_pid(state_dir, host_id)
            if candidate is not None and candidate != old_pid:
                new_pid = candidate
                break
            time.sleep(0.1)
        assert new_pid is not None
        assert new_session["host_version"] == _NEW_VERSION
        assert new_pid != old_pid
        assert http_client.get(f"/v1/hosts/{host_id}").json()["host_id"] == host_id
    finally:
        if supervisor.poll() is None:
            supervisor.send_signal(signal.SIGTERM)
            try:
                supervisor.wait(timeout=15)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)
