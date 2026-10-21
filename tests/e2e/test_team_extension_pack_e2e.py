"""A separately built wheel travels through discovery, activation, and owned install."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
from build import ProjectBuilder

from omnigent.spec import load
from tests._helpers.live_server import isolated_local_server, isolated_process_env

_REPO_ROOT = Path(__file__).parents[2]
_EXTENSION_ID = "acme.incident-response"


def test_team_extension_wheel_activation_install_and_disable(tmp_path: Path) -> None:
    source = tmp_path / "independent-checkout"
    shutil.copytree(
        _REPO_ROOT / "examples/extensions/incident-response",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"),
    )
    wheel = ProjectBuilder(str(source)).build("wheel", str(tmp_path / "wheels"))
    site = tmp_path / "installed-wheel"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    pythonpath = os.pathsep.join((str(site), str(_REPO_ROOT)))
    cli_home = tmp_path / "cli-home"
    cli_home.mkdir()
    environment = isolated_process_env(cli_home, {"PYTHONPATH": pythonpath}, None)

    def command(*arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "omnigent", "extensions", *arguments],
            cwd=source,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    assert _EXTENSION_ID in command("list").stdout
    assert "agents: 1, policy presets: 1" in command("check", _EXTENSION_ID).stdout
    config = tmp_path / "server.yaml"
    command("enable", _EXTENSION_ID, "--config", str(config))
    exported = tmp_path / "triage.tar.gz"
    command("export", _EXTENSION_ID, f"{_EXTENSION_ID}.triage", "--output", str(exported))
    spec = load(exported.read_bytes(), dest=tmp_path / "portable-agent", expand_env=False)
    assert spec.name == "incident-triage"
    assert {skill.name for skill in spec.skills} == {"triage"}
    runtime = tmp_path / "server"
    server_env = {"PYTHONPATH": pythonpath}
    server_args = ["--config", str(config)]
    with isolated_local_server(
        runtime, env=server_env, cwd=source, extra_args=server_args
    ) as base_url:
        with httpx.Client(base_url=base_url, trust_env=False, timeout=30) as client:
            catalog = client.get(f"/v1/extensions/{_EXTENSION_ID}")
            assert catalog.status_code == 200, catalog.text
            template = catalog.json()["agents"][0]
            bundle_url = template["bundle_url"]
            bundle = client.get(bundle_url)
            assert bundle.status_code == 200
            assert bundle.content == exported.read_bytes()
            assert hashlib.sha256(bundle.content).hexdigest() == template["digest"]
            installed = client.post(bundle_url.removesuffix("/bundle") + "/install")
            assert installed.status_code == 200, installed.text
            agent_id = installed.json()["id"]
            own = client.get("/v1/agents?scope=user")
            assert agent_id in {agent["id"] for agent in own.json()["data"]}
            assert client.get(bundle_url.replace(template["digest"], "0" * 64)).status_code == 404
    log_directory = runtime / "server-home/.omnigent/logs/server"
    enabled_logs = set(log_directory.glob("*.log"))
    enabled_log = "\n".join(path.read_text() for path in enabled_logs)
    assert "Incident response extension started" in enabled_log
    assert "Incident response extension stopped" in enabled_log

    command("disable", _EXTENSION_ID, "--config", str(config))
    with isolated_local_server(
        runtime, env=server_env, cwd=source, extra_args=server_args
    ) as base_url:
        with httpx.Client(base_url=base_url, trust_env=False, timeout=30) as client:
            assert client.get("/v1/extensions").json()["data"] == []
            assert client.get(bundle_url).status_code == 404
            assert client.post(bundle_url.removesuffix("/bundle") + "/install").status_code == 404
            assert client.get(f"/v1/agents/{agent_id}").status_code == 200
            assert client.get("/health").status_code == 200
    disabled_logs = set(log_directory.glob("*.log")).difference(enabled_logs)
    assert disabled_logs
    assert all(
        "Incident response extension started" not in path.read_text() for path in disabled_logs
    )
