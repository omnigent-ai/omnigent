"""Verify host and setup readiness honor a configured Kimi executable."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_process_env
from tests._helpers.polling import poll_until
from tests._helpers.terminal_screen import TerminalScreen

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KIMI_READINESS_KEYS = ("kimi", "kimi-native", "native-kimi")


def _custom_path_only_env(tmp_path: Path) -> dict[str, str | None]:
    """Seed a logged-in Kimi shim outside every directory on PATH."""
    shim = tmp_path / "custom-tools" / "kimi"
    shim.parent.mkdir(parents=True)
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "kimi-code 0.39.1"; exit 0; fi\n'
        'printf "%s\\n" "$0" > custom-kimi-launched.txt\n'
        "exec /bin/sleep 120\n"
    )
    shim.chmod(0o755)

    kimi_home = tmp_path / "kimi-config"
    credentials = kimi_home / "credentials" / "kimi-code.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text('{"access_token": "e2e-custom-path-token"}\n')

    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    kimi_free_path = os.pathsep.join(
        directory
        for directory in path_dirs
        if directory and not (Path(directory) / "kimi").exists()
    )
    pythonpath = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    if inherited := os.environ.get("PYTHONPATH"):
        pythonpath.append(inherited)
    return {
        "PATH": kimi_free_path,
        "OMNIGENT_KIMI_PATH": str(shim),
        "HARNESS_KIMI_PATH": None,
        "KIMI_CODE_HOME": str(kimi_home),
        "PYTHONPATH": os.pathsep.join(pythonpath),
        "NO_COLOR": "1",
        "TERM": "xterm",
    }


@pytest.mark.timeout(180)
def test_host_readiness_honors_custom_kimi_path(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """Advertise custom-path Kimi as available over the real host tunnel."""
    env = _custom_path_only_env(tmp_path)
    with isolated_host(tmp_path / "host", live_server, env=env) as host:
        host.wait_online(http_client, timeout=60)
        response = http_client.get(f"/v1/hosts/{host.host_id}")
        assert response.status_code == 200, response.text
        configured = response.json().get("configured_harnesses")
        assert configured is not None, host.log_path.read_text()
        assert {key: configured.get(key) for key in _KIMI_READINESS_KEYS} == dict.fromkeys(
            _KIMI_READINESS_KEYS, True
        ), configured
        from omnigent.native.native_coding_agents import KIMI_NATIVE_AGENT_NAME

        agents = http_client.get("/v1/agents")
        agents.raise_for_status()
        agent_id = next(
            a["id"] for a in agents.json()["data"] if a["name"] == KIMI_NATIVE_AGENT_NAME
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        create = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host.host_id, "workspace": str(workspace)},
            timeout=60,
        )
        create.raise_for_status()
        session_id = create.json()["id"]
        try:
            marker = workspace / "custom-kimi-launched.txt"
            poll_until(marker.exists, timeout_s=45, interval_s=0.1)
            assert marker.read_text().strip() == env["OMNIGENT_KIMI_PATH"]
        finally:
            http_client.delete(f"/v1/sessions/{session_id}", timeout=30).raise_for_status()


@pytest.mark.timeout(180)
def test_setup_kimi_row_honors_custom_kimi_path(tmp_path: Path) -> None:
    """Show a logged-in custom-path Kimi install as signed in during setup."""
    overrides = _custom_path_only_env(tmp_path)
    env = isolated_process_env(tmp_path / "home", overrides, None)
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding="utf-8",
        dimensions=(50, 120),
        timeout=120,
        cwd=str(_REPO_ROOT),
    )
    screen = TerminalScreen(child)
    try:
        rendered = screen.wait_for(
            lambda text: any(
                "Kimi Code" in line
                and any(
                    marker in line
                    for marker in (
                        "Not installed",
                        "Not configured",
                        "Signed in",
                        "Needs upgrade",
                    )
                )
                for line in text.splitlines()
            ),
            timeout=120,
            what="the Kimi setup status",
        )
        kimi_rows = "\n".join(line for line in rendered.splitlines() if "Kimi Code" in line)
        assert "Not installed" not in kimi_rows, rendered
        assert "Signed in" in kimi_rows, rendered
    finally:
        with contextlib.suppress(Exception):
            child.sendcontrol("c")
        child.close(force=True)
