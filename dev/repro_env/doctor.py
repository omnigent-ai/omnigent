"""Collect environment facts against the existing reproduction plan."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from .runtime import write_json
from .transport import Relay


def _command(args: list[str], root: Path) -> str | None:
    try:
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None


def launch_observations(root: Path) -> dict:
    dirty = _command(["git", "status", "--porcelain", "--untracked-files=normal"], root)
    return {
        "captured_at": time.time(),
        "commit": _command(["git", "rev-parse", "HEAD"], root),
        "dirty": None if dirty is None else bool(dirty),
    }


def observe(output: Path, host_id: str | None) -> tuple[dict, list[str]]:
    """Refresh shell tools after installation; distinguish them from runtime observations."""
    facts = {"shell.os": platform.system(), "shell.arch": platform.machine()}
    if host_id:
        facts.update({"host.id": host_id, "host.registered": None, "host.online": None})
    errors = []
    for binary in ("claude", "codex", "pi", "node", "tmux"):
        path = shutil.which(binary)
        facts[f"shell.{binary}.installed"] = path is not None
        facts[f"shell.{binary}.version"] = (
            _command([path, "-V" if binary == "tmux" else "--version"], Path.cwd())
            if path
            else None
        )
    try:
        facts["shell.openai-agents.version"] = importlib.metadata.version("openai-agents")
        facts["shell.openai-agents.installed"] = True
    except importlib.metadata.PackageNotFoundError:
        facts["shell.openai-agents.version"] = None
        facts["shell.openai-agents.installed"] = False

    state_path = output / "environment.json"
    if not state_path.exists():
        facts["runtime.status"] = "not_provisioned"
        return facts, ["Prepared runtime is missing; use the existing workflow provisioning."]
    state = json.loads(state_path.read_text())
    facts.update(
        {
            "runtime.status": state.get("status"),
            "runtime.model_backend": state.get("model_backend"),
            "runtime.runner_id": state.get("runner_id"),
            "runtime.lease_active": state.get("expires_at", 0) > time.time(),
            "runtime.runner_online": None,
        }
    )
    launch_path = output / "launch-observations.json"
    if launch_path.exists():
        launch = json.loads(launch_path.read_text())
        facts.update(
            {
                "runtime.launch_commit": launch.get("commit"),
                "runtime.launch_dirty": launch.get("dirty"),
                "runtime.launch_captured_at": launch.get("captured_at"),
            }
        )
    if state.get("status") != "ready" or not facts["runtime.lease_active"]:
        return facts, [
            "Prepared runtime is stopped, expired, or starting; inspect its diagnostics."
        ]
    try:
        with (
            Relay(unix_target=output / "server.sock") as server,
            httpx.Client(trust_env=False, timeout=5) as client,
        ):
            base = f"http://127.0.0.1:{server.port}"
            response = client.get(f"{base}/v1/runners/{state['runner_id']}/status")
            response.raise_for_status()
            facts["runtime.runner_online"] = response.json().get("online")
            if host_id:
                response = client.get(f"{base}/v1/hosts")
                response.raise_for_status()
                host = next((h for h in response.json()["hosts"] if h["host_id"] == host_id), None)
                facts["host.registered"] = host is not None
                facts["host.online"] = (
                    False
                    if host is None
                    else None
                    if host.get("status") is None
                    else host["status"] == "online"
                )
    except (OSError, httpx.HTTPError, ValueError, KeyError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return facts, errors


def compare(requirements: list[dict], checks: list[tuple], facts: dict) -> list[dict]:
    results = {r["id"]: {"requirement": r, "comparisons": []} for r in requirements}
    for requirement_id, field, expected in checks:
        if requirement_id not in results:
            raise ValueError(
                f"Check {requirement_id!r} must name an environment/setup requirement"
            )
        if expected is None:
            raise ValueError(
                "Expected values cannot be null; leave unknown requirements unchecked"
            )
        actual = facts.get(field)
        status = "unknown" if actual is None else "mismatch"
        if type(actual) is type(expected) and actual == expected:
            status = "match"
        results[requirement_id]["comparisons"].append(
            {
                "field": field,
                "expected": expected,
                "actual": actual,
                "status": status,
            }
        )
    for result in results.values():
        statuses = [c["status"] for c in result["comparisons"]]
        result["status"] = (
            "unchecked"
            if not statuses
            else "mismatch"
            if "mismatch" in statuses
            else "unknown"
            if "unknown" in statuses
            else "checks_match"
        )
    return list(results.values())


def doctor(
    output: Path, plan_path: Path, checks: list[list[str]], host_id: str | None, *, json_only=False
) -> Path:
    raw = plan_path.read_bytes()
    plan = json.loads(raw)
    requirements = plan["requirements"]
    selected = [r for r in requirements if r["phase"] in ("environment", "setup")]
    ids = [r["id"] for r in selected]
    if not selected or len(ids) != len(set(ids)):
        raise ValueError("Plan needs environment/setup requirements with unique IDs")
    parsed_checks = [(req, field, json.loads(expected)) for req, field, expected in checks]
    compare(selected, parsed_checks, {})
    facts, errors = observe(output, host_id)
    report = {
        "schema_version": 1,
        "captured_at": time.time(),
        "run_id": plan["run_id"],
        "snapshot_sha256": plan["snapshot_sha256"],
        "plan_file_sha256": hashlib.sha256(raw).hexdigest(),
        "facts": facts,
        "requirements": compare(selected, parsed_checks, facts),
        "errors": errors,
        "limitations": [
            "checks_match covers only the agent-selected comparisons, not the whole requirement.",
            "Shell tools/OS describe this invocation's sandbox, not a tested product session.",
            "Runtime backend is configured state; session routing still needs journey evidence.",
            "Launch commit identifies the checkout, not SPA provenance or later file changes.",
            "Unknown facts and unchecked requirements need investigation.",
            "Missing tools: install/configure through existing setup and recheck.",
            "This report neither gates execution nor verifies the report interpretation or bug.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = output / f"environment-check-{time.time_ns()}.json"
    write_json(destination, report)
    print(json.dumps(report, indent=2), flush=True)
    if not json_only:
        print(f"Saved environment observations: {destination}", flush=True)
    return destination
