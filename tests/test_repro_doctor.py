"""Check reported prerequisites without turning connectivity into a bug verdict."""

import hashlib
import json
import subprocess
import sys
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from dev.repro_env import doctor
from dev.repro_env.runtime import write_json


def requirement(req_id="environment-1", phase="environment"):
    return {
        "id": req_id,
        "phase": phase,
        "description": "Reported environment requirement",
        "source_ids": ["report-1"],
        "certainty": "reported",
        "uncertainty": "",
        "method": "Use the existing product entry point",
        "substitution": None,
    }


def plan_file(tmp_path):
    plan = {
        "schema_version": 1,
        "run_id": "run-1",
        "snapshot_sha256": "snapshot",
        "requirements": [
            requirement(),
            requirement("setup-1", "setup"),
            requirement("trigger-1", "trigger"),
            requirement("observe-1", "observation"),
        ],
    }
    path = tmp_path / "plan.json"
    write_json(path, plan)
    return path


@pytest.mark.parametrize(
    "actual,expected,status",
    [
        ("Linux", "Darwin", "mismatch"),
        ("Linux", "Linux", "checks_match"),
        (None, "Linux", "unknown"),
        (False, 0, "mismatch"),
        (False, False, "checks_match"),
    ],
)
def test_exact_selected_comparisons(actual, expected, status):
    result = doctor.compare(
        [requirement()], [("environment-1", "shell.os", expected)], {"shell.os": actual}
    )
    assert result[0]["status"] == status
    assert result[0]["requirement"]["source_ids"] == ["report-1"]


def test_unchecked_and_unsupported_requirements_remain_visible():
    result = doctor.compare(
        [requirement(), requirement("setup-1", "setup")],
        [("environment-1", "session.policy", "inherited")],
        {},
    )
    assert [r["status"] for r in result] == ["unknown", "unchecked"]


@pytest.mark.parametrize(
    "check",
    [
        ("invented", "shell.os", "Linux"),
        ("environment-1", "shell.os", None),
    ],
)
def test_invalid_comparison_cannot_claim_coverage(check):
    with pytest.raises(ValueError):
        doctor.compare([requirement()], [check], {})


def test_plan_identity_and_history_survive_preparation(tmp_path, monkeypatch):
    plan = plan_file(tmp_path)
    observed = {"shell.claude.installed": False}
    monkeypatch.setattr(doctor, "observe", lambda *_: (dict(observed), []))
    output = tmp_path / "environment"
    checks = [["environment-1", "shell.claude.installed", "true"]]
    before = doctor.doctor(output, plan, checks, None)
    observed["shell.claude.installed"] = True
    after = doctor.doctor(output, plan, checks, None)
    assert before != after
    first, second = [json.loads(p.read_text()) for p in (before, after)]
    assert first["requirements"][0]["status"] == "mismatch"
    assert second["requirements"][0]["status"] == "checks_match"
    assert second["requirements"][1]["status"] == "unchecked"
    assert second["run_id"] == "run-1"
    assert second["snapshot_sha256"] == "snapshot"
    assert second["plan_file_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert len(second["requirements"]) == 2
    assert "ready_for_smoke" not in second
    assert json.loads(plan.read_text())["requirements"][0] == requirement()


def test_trigger_cannot_be_passed_off_as_environment_check(tmp_path, monkeypatch):
    observe = Mock(side_effect=AssertionError("must validate before observing"))
    monkeypatch.setattr(doctor, "observe", observe)
    with pytest.raises(ValueError, match="environment/setup"):
        doctor.doctor(tmp_path, plan_file(tmp_path), [["trigger-1", "shell.os", '"Linux"']], None)
    observe.assert_not_called()


def test_duplicate_requirement_ids_rejected(tmp_path):
    path = plan_file(tmp_path)
    plan = json.loads(path.read_text())
    plan["requirements"].append(requirement())
    write_json(path, plan)
    with pytest.raises(ValueError, match="unique IDs"):
        doctor.doctor(tmp_path, path, [], None)


def test_tool_availability_is_refreshed_after_install(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(doctor, "_command", lambda *_: "fixture-cli version")
    before, _ = doctor.observe(tmp_path / "runtime", None)
    assert before["shell.claude.installed"] is False
    binary = tmp_path / "claude"
    binary.write_text("test fixture")
    binary.chmod(0o700)
    after, _ = doctor.observe(tmp_path / "runtime", None)
    assert after["shell.claude.installed"] is True
    assert after["shell.claude.version"] == "fixture-cli version"
    assert after["shell.codex.installed"] is False
    result = doctor.compare(
        [requirement()], [("environment-1", "shell.claude.installed", True)], after
    )
    assert result[0]["status"] == "checks_match"


def mock_runtime(tmp_path, monkeypatch, hosts):
    write_json(
        tmp_path / "environment.json",
        {
            "status": "ready",
            "expires_at": time.time() + 60,
            "runner_id": "runner",
            "model_backend": "mock",
        },
    )
    monkeypatch.setattr(doctor, "_command", lambda *_: None)
    monkeypatch.setattr(doctor, "Relay", lambda **kwargs: nullcontext(SimpleNamespace(port=1234)))
    paths = []

    def respond(request):
        paths.append(request.url.path)
        body = {"hosts": hosts} if request.url.path == "/v1/hosts" else {"online": True}
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(doctor.httpx, "Client", lambda **kwargs: client)
    return paths


def test_online_runner_and_unrelated_host_do_not_satisfy_required_host(tmp_path, monkeypatch):
    mock_runtime(tmp_path, monkeypatch, [{"host_id": "unrelated", "status": "online"}])
    facts, errors = doctor.observe(tmp_path, "reported-host")
    assert not errors
    assert facts["runtime.runner_online"] is True
    assert facts["host.id"] == "reported-host"
    assert facts["host.registered"] is False
    assert facts["host.online"] is False


@pytest.mark.parametrize("status,online", [("online", True), ("offline", False)])
def test_observes_selected_hosts_actual_state(tmp_path, monkeypatch, status, online):
    mock_runtime(tmp_path, monkeypatch, [{"host_id": "selected", "status": status}])
    facts, errors = doctor.observe(tmp_path, "selected")
    assert not errors
    assert facts["host.registered"] is True
    assert facts["host.online"] is online


def test_no_host_requirement_does_not_impose_host_setup(tmp_path, monkeypatch):
    paths = mock_runtime(tmp_path, monkeypatch, [])
    facts, _ = doctor.observe(tmp_path, None)
    assert "host.online" not in facts
    assert paths == ["/v1/runners/runner/status"]


def test_missing_host_status_stays_unknown(tmp_path, monkeypatch):
    mock_runtime(tmp_path, monkeypatch, [{"host_id": "selected"}])
    facts, errors = doctor.observe(tmp_path, "selected")
    assert not errors
    assert facts["host.registered"] is True
    assert facts["host.online"] is None


def test_failed_observation_stays_unknown(tmp_path, monkeypatch):
    mock_runtime(tmp_path, monkeypatch, [])
    monkeypatch.setattr(doctor, "Relay", Mock(side_effect=OSError("socket unavailable")))
    facts, errors = doctor.observe(tmp_path, "selected")
    assert facts["host.online"] is None
    assert facts["runtime.runner_online"] is None
    assert errors == ["OSError: socket unavailable"]


def test_missing_runtime_is_preparation_work(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_command", lambda *_: None)
    facts, errors = doctor.observe(tmp_path, None)
    assert facts["runtime.status"] == "not_provisioned"
    assert "existing workflow provisioning" in errors[0]


def test_unavailable_launch_identity_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        doctor.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("git", 5))
    )
    result = doctor.launch_observations(tmp_path)
    assert result["commit"] is None
    assert result["dirty"] is None


def test_json_mode_is_parseable_and_missing_runtime_keeps_selected_host(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PATH", str(tmp_path))
    plan = plan_file(tmp_path)
    destination = doctor.doctor(tmp_path / "output", plan, [], "selected", json_only=True)
    result = json.loads(capsys.readouterr().out)
    assert result == json.loads(destination.read_text())
    assert result["facts"]["host.id"] == "selected"
    assert result["facts"]["host.online"] is None
    assert result["facts"]["runtime.status"] == "not_provisioned"


def test_native_tool_observations_use_tmux_version_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/tools/{name}")
    command = Mock(return_value="version")
    monkeypatch.setattr(doctor, "_command", command)
    facts, _ = doctor.observe(tmp_path, None)
    assert facts["shell.pi.installed"] is True
    assert facts["shell.tmux.version"] == "version"
    assert ["/tools/tmux", "-V"] in [call.args[0] for call in command.call_args_list]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_undecodable_command_output_is_unknown(tmp_path, stream):
    result = doctor._command(
        [sys.executable, "-c", f"import sys; sys.{stream}.buffer.write(b'\\xff')"], tmp_path
    )
    assert result is None


def test_launch_observations_with_non_utf8_tracked_content(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init")
    readme = tmp_path / "README.md"
    readme.write_text("Tracked text\n")
    git("add", "README.md")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    readme.write_bytes(readme.read_bytes() + b"\xe9\n")

    result = doctor.launch_observations(tmp_path)
    assert result["commit"] == git("rev-parse", "HEAD")
    assert result["dirty"] is True
