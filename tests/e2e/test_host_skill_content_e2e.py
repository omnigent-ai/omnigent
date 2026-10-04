"""Skill contents travels from a real host daemon through the REST API."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.mark.min_server_version("0.17.0")
def test_host_skill_content(live_server: str, tmp_path: Path) -> None:
    skill = tmp_path / "claude" / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review diffs.\n---\n# Synthetic private instructions"
    )
    (skill / "other.md").write_text("synthetic-other-file-secret")
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text("{}\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("OMNIGENT")}
    env.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "OMNIGENT_CONFIG_HOME": str(config),
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None, [str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH")]
                )
            ),
        }
    )
    with httpx.Client(base_url=live_server, timeout=20) as client:
        existing = {host["host_id"] for host in client.get("/v1/hosts").json()["hosts"]}
        with (tmp_path / "host.log").open("w") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
                cwd=tmp_path,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 45
                host_id = None
                while time.monotonic() < deadline:
                    assert child.poll() is None, "host exited before connecting"
                    hosts = client.get("/v1/hosts").json()["hosts"]
                    host_id = next(
                        (
                            host["host_id"]
                            for host in hosts
                            if host["host_id"] not in existing and host["status"] == "online"
                        ),
                        None,
                    )
                    if host_id:
                        break
                    time.sleep(0.1)
                assert host_id, "test host never connected"
                listing = client.get(f"/v1/skills?host_id={host_id}&harness=claude-native&path=~")
                assert listing.status_code == 200
                assert "Synthetic private instructions" not in listing.text
                response = client.get(f"/v1/hosts/{host_id}/harnesses/claude-native/skills/review")
                assert response.status_code == 200, response.text
                assert response.json() == {
                    "name": "review",
                    "description": "Review diffs.",
                    "content": "# Synthetic private instructions",
                    "truncated": False,
                }
                assert response.headers["cache-control"] == "no-store"
                assert "synthetic-other-file-secret" not in response.text
                assert str(tmp_path) not in response.text
            finally:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
    assert "Synthetic private instructions" not in (tmp_path / "host.log").read_text()
