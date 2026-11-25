"""Tests for live ``agy models`` discovery."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.antigravity_native import credentials as agy_credentials
from omnigent.harnesses.antigravity_native import models


def test_parse_agy_model_options_skips_preamble_and_preserves_cli_order() -> None:
    """Only tab-separated rows become picker entries."""
    parsed = models.parse_agy_model_options(
        "Fetching available models...\n\n"
        "gemini-3-pro\tGemini 3 Pro\n"
        "claude-sonnet-4-6\tClaude Sonnet 4.6\n"
        "gpt-oss-120b-medium\tGPT-OSS 120B Medium\n"
    )

    assert parsed == [
        {"id": "gemini-3-pro", "displayName": "Gemini 3 Pro", "isDefault": False},
        {
            "id": "claude-sonnet-4-6",
            "displayName": "Claude Sonnet 4.6",
            "isDefault": False,
        },
        {
            "id": "gpt-oss-120b-medium",
            "displayName": "GPT-OSS 120B Medium",
            "isDefault": False,
        },
    ]


def test_parse_agy_model_options_deduplicates_and_rejects_empty_output() -> None:
    """Progress-only or malformed output cannot create a plausible picker."""
    assert models.parse_agy_model_options("x\tX\nx\tDifferent X\n") == [
        {"id": "x", "displayName": "X", "isDefault": False}
    ]
    with pytest.raises(ValueError, match="did not contain"):
        models.parse_agy_model_options("Fetching available models...\nmodel-without-label\n")


def test_parse_agy_model_options_excludes_orchestrator_slugs() -> None:
    """Orchestrator rows are dropped; look-alike ids that only share a stem stay."""
    parsed = models.parse_agy_model_options(
        "gemini-4-carbon-high\tGemini 4 Carbon (High)\n"
        "gemini-pro-orchestrator-neon\tOrchestrator (Neon)\n"
        "gemini-pro-orchestrator\tOrchestrator (Carbon)\n"
        "gemini-pro-orchestrator-argon\tOrchestrator (Argon)\n"
        "gemini-pro-orchestratorish\tNot An Orchestrator\n"
    )

    assert [option["id"] for option in parsed] == [
        "gemini-4-carbon-high",
        "gemini-pro-orchestratorish",
    ]


def test_parse_agy_model_options_rejects_orchestrator_only_output() -> None:
    """A catalog of only orchestrator rows offers no launchable picker choice."""
    with pytest.raises(ValueError, match="did not contain"):
        models.parse_agy_model_options("gemini-pro-orchestrator\tOrchestrator (Carbon)\n")


def test_list_agy_cli_model_options_invokes_resolved_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host probe retains the explicit environment and bounded timeout."""
    captured: dict[str, object] = {}

    monkeypatch.setattr(models, "agy_binary_path", lambda: "/opt/agy")

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="model-a\tModel A\n", stderr="")

    monkeypatch.setattr(models, "_run_discovery", _run)

    assert models.list_agy_cli_model_options(env={"PATH": "/bin"}, timeout_s=3.5) == [
        {"id": "model-a", "displayName": "Model A", "isDefault": False}
    ]
    assert captured == {
        "argv": ["/opt/agy", "models"],
        "timeout_s": 3.5,
        "env": {"PATH": "/bin"},
    }


def test_list_agy_cli_model_options_maps_missing_binary_to_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker catalog discovery can safely classify a missing agy executable."""
    monkeypatch.setattr(
        models,
        "agy_binary_path",
        lambda: (_ for _ in ()).throw(RuntimeError("install agy with curl")),
    )

    with pytest.raises(FileNotFoundError, match="agy CLI is unavailable"):
        models.list_agy_cli_model_options()


def _capture_agy_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub the agy binary and record the discovery command call."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(models, "agy_binary_path", lambda: "/opt/agy")

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="model-a\tModel A\n", stderr="")

    monkeypatch.setattr(models, "_run_discovery", _run)
    return captured


def test_list_agy_cli_model_options_uses_agy_login_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no configured credential, agy lists models on its own login."""
    monkeypatch.setattr(agy_credentials, "resolve_antigravity_credentials", lambda: None)
    captured = _capture_agy_run(monkeypatch)

    models.list_agy_cli_model_options()

    assert captured["argv"] == ["/opt/agy", "models"]
    assert captured["env"] is None


def test_list_agy_cli_model_options_routes_databricks_profile_through_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Databricks profile lists models through the same gateway wrapper as launch."""
    monkeypatch.setattr(
        agy_credentials,
        "resolve_antigravity_credentials",
        lambda: agy_credentials.AntigravityCredentials(
            "omnigent-adapter-pending", "https://workspace.example", profile="team"
        ),
    )
    captured = _capture_agy_run(monkeypatch)

    models.list_agy_cli_model_options()

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == sys.executable
    assert argv[argv.index("--profile") + 1] == "team"
    assert argv[argv.index("--") + 1 :] == ["/opt/agy", "models"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert "OMNIGENT_AGY_DATABRICKS_PROFILE" not in env
    assert captured["timeout_s"] == models._DISCOVERY_TIMEOUT_S


def test_list_agy_cli_model_options_reports_credential_repair_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved configuration needing repair fails discovery with a fixed message."""

    def _broken() -> None:
        raise OmnigentError("detail from the saved config", code=ErrorCode.INVALID_INPUT)

    monkeypatch.setattr(agy_credentials, "resolve_antigravity_credentials", _broken)
    monkeypatch.setattr(models, "agy_binary_path", lambda: "/opt/agy")

    with pytest.raises(ValueError, match="run omni setup") as raised:
        models.list_agy_cli_model_options()
    assert "detail from the saved config" not in str(raised.value)


def test_discovery_timeout_fits_the_host_model_options_deadline() -> None:
    """Discovery plus its termination grace ends before the host request expires."""
    from omnigent.inner import _proc
    from omnigent.server.routes import hosts

    budget = models._DISCOVERY_TIMEOUT_S + _proc._RUN_ISOLATED_REAP_TIMEOUT_S
    assert budget + 2.0 <= hosts._MODEL_OPTIONS_TIMEOUT_S


@pytest.mark.parametrize("timeout_s", [3.5, None], ids=["explicit", "default"])
def test_gateway_discovery_never_raises_the_caller_timeout(
    monkeypatch: pytest.MonkeyPatch, timeout_s: float | None
) -> None:
    """The Databricks route keeps the caller's (or default) discovery budget."""
    monkeypatch.setattr(
        agy_credentials,
        "resolve_antigravity_credentials",
        lambda: agy_credentials.AntigravityCredentials(
            "omnigent-adapter-pending", "https://workspace.example", profile="team"
        ),
    )
    captured = _capture_agy_run(monkeypatch)

    if timeout_s is None:
        models.list_agy_cli_model_options()
    else:
        models.list_agy_cli_model_options(timeout_s=timeout_s)

    assert captured["timeout_s"] == (timeout_s or models._DISCOVERY_TIMEOUT_S)


def _process_alive(pid: int) -> bool:
    from omnigent.inner._proc import process_alive

    return process_alive(pid)


def _record_group_signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, bool]]:
    """Record each group signal with whether its leader still existed (even as a zombie)."""
    from omnigent.inner import _proc

    calls: list[tuple[int, bool]] = []
    real_killpg = _proc._killpg_fn
    assert real_killpg is not None

    def _killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, Path(f"/proc/{pgid}").exists()))
        real_killpg(pgid, sig)

    # The process-tree helpers call the function captured at import, not os.killpg.
    monkeypatch.setattr(_proc, "_killpg_fn", _killpg)
    return calls


_requires_group_signals = pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="needs POSIX process-group signals"
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_discovery_timeout_removes_the_wrapped_process_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timed-out wrapper and the agy it started both stop, even ignoring SIGTERM."""
    pid_file = tmp_path / "child.pid"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        textwrap.dedent(
            f"""
            import pathlib, signal, subprocess, sys, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            stubborn = "import signal, time; signal.signal(15, signal.SIG_IGN); time.sleep(60)"
            child = subprocess.Popen([sys.executable, "-c", stubborn])
            pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
            time.sleep(60)
            """
        )
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        models._run_discovery([sys.executable, str(wrapper)], env=None, timeout_s=1.0)
    assert time.monotonic() - started < 10

    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_alive(child_pid)


def test_discovery_reports_a_failed_command() -> None:
    """A non-zero exit surfaces as ``CalledProcessError`` with its output."""
    with pytest.raises(subprocess.CalledProcessError) as raised:
        models._run_discovery(
            [sys.executable, "-c", "import sys; print('out'); sys.exit(3)"],
            env=None,
            timeout_s=10,
        )
    assert raised.value.returncode == 3
    assert raised.value.output == "out\n"


@_requires_group_signals
@pytest.mark.skipif(not Path("/proc/self").exists(), reason="needs /proc to observe leaders")
def test_discovery_timeout_after_the_leader_exits_cleans_up_safely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A leader that exits early leaves an orphan; teardown removes it without
    signalling a process-group id whose leader is already reaped."""
    calls = _record_group_signals(monkeypatch)
    pid_file = tmp_path / "orphan.pid"
    leader = tmp_path / "leader.py"
    leader.write_text(
        textwrap.dedent(
            f"""
            import pathlib, subprocess, sys
            stubborn = "import signal, time; signal.signal(15, signal.SIG_IGN); time.sleep(60)"
            orphan = subprocess.Popen([sys.executable, "-c", stubborn])
            pathlib.Path({str(pid_file)!r}).write_text(str(orphan.pid))
            """
        )
    )

    with pytest.raises(subprocess.TimeoutExpired):
        models._run_discovery([sys.executable, str(leader)], env=None, timeout_s=1.0)

    orphan_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _process_alive(orphan_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_alive(orphan_pid)
    assert calls, "the timed-out tree was never signalled as a group"
    assert all(leader_present for _pgid, leader_present in calls)


@_requires_group_signals
def test_successful_discovery_sends_no_group_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A discovery that finishes in time is reaped without any group teardown."""
    calls = _record_group_signals(monkeypatch)

    completed = models._run_discovery(
        [sys.executable, "-c", "print('m\\tM')"], env=None, timeout_s=10
    )

    assert completed.stdout == "m\tM\n"
    assert calls == []
