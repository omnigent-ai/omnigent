"""Credential-free CLI e2e: no server, agent, provider or harness is started."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_GUARDED_ENTRYPOINT = r"""
import importlib.abc
import runpy
import sys

def audit(event, args):
    if event.startswith(("socket.",
                         "subprocess.", "os.system", "os.exec", "os.spawn",
                         "os.posix_spawn", "os.fork")):
        raise AssertionError("Offline CLI attempted network/process activity")

class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("offline_bundle_trap", "omnigent.inner.loader",
                                "omnigent.runtime", "omnigent.policies",
                                "omnigent.tools.builtins", "omnigent.cli_config",
                                "omnigent.spec._omnigent_compat")):
            raise AssertionError("Offline CLI imported runtime or bundle code")

sys.addaudithook(audit)
sys.meta_path.insert(0, BlockRuntime())
sys.argv[0] = "omnigent"
runpy.run_module("omnigent", run_name="__main__")
"""


def test_offline_bundle_cli_happy_path(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.yaml").write_text(
        """spec_version: 1
name: orchestrator
instructions: prompt.md
executor:
  config: {harness: claude-sdk}
  auth: {type: api_key, api_key: "${OFFLINE_E2E_SECRET}"}
tools:
  agents: [worker]
  search:
    type: mcp
    url: https://example.invalid/mcp
    headers: {Authorization: "${OFFLINE_E2E_SECRET}"}
guardrails:
  labels: {access: {initial: yes, values: [yes, no]}}
  policies:
    gate:
      type: function
      handler:
        path: offline_bundle_trap.factory
        arguments: {token: "${OFFLINE_E2E_SECRET}"}
      condition: {access: yes}
      set_labels: [access]
""",
        encoding="utf-8",
    )
    (bundle / "prompt.md").write_text("Delegate to worker when appropriate.", encoding="utf-8")
    child = bundle / "agents" / "worker-directory"
    child.mkdir(parents=True)
    (child / "config.yaml").write_text(
        "spec_version: 1\nname: worker\nexecutor: {type: agents_sdk}\n",
        encoding="utf-8",
    )
    skill = bundle / "skills" / "summarize"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize text.\n---\nBe concise.\n",
        encoding="utf-8",
    )
    mcp = bundle / "tools" / "mcp"
    mcp.mkdir(parents=True)
    (mcp / "local.yaml").write_text(
        "name: local\ntransport: stdio\ncommand: never-start-this-command\n"
        "env: {TOKEN: '${OFFLINE_E2E_SECRET}'}\n",
        encoding="utf-8",
    )
    code = "raise AssertionError('Bundle code must never be imported')\n"
    (bundle / "offline_bundle_trap.py").write_text(code, encoding="utf-8")
    for dependency in ("yaml", "pydantic", "click", "hashlib"):
        (bundle / f"{dependency}.py").write_text(code, encoding="utf-8")
    python_tools = bundle / "tools" / "python"
    python_tools.mkdir()
    (python_tools / "untrusted.py").write_text(code, encoding="utf-8")
    before = {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    repo = Path(__file__).resolve().parents[2]
    secret = "offline-e2e-secret-not-a-real-credential"
    env = {
        **os.environ,
        "PYTHONPATH": str(repo),
        "PYTHONDONTWRITEBYTECODE": "",
        "OMNIGENT_WRAPPER_BYPASS": "1",
        "OMNIGENT_DATA_DIR": str(tmp_path / "must-not-be-created"),
        "OFFLINE_E2E_SECRET": secret,
    }
    outputs = []
    for arguments in ([str(bundle), "--offline", "--json"], ["config.yaml", "--json"]):
        result = subprocess.run(
            [sys.executable, "-c", _GUARDED_ENTRYPOINT, "validate", *arguments],
            cwd=bundle,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert secret not in result.stdout
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == 1
        assert payload["status"] == "valid"
        assert payload["diagnostics"] == []
        assert {check["code"] for check in payload["skipped_checks"]} == {
            "HOST_AUTH",
            "LIVE_SERVICES",
            "CODE",
            "PLUGINS",
        }
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    module_result = subprocess.run(
        [sys.executable, "-m", "omnigent", "validate", "--offline", "--json"],
        cwd=bundle,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert module_result.returncode == 0, module_result.stdout + module_result.stderr
    assert module_result.stdout == outputs[0]
    assert module_result.stderr == ""
    assert not (tmp_path / "must-not-be-created").exists()
    assert before == {
        p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()
    }
