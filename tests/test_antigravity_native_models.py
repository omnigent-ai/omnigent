"""Tests for live ``agy models`` discovery."""

from __future__ import annotations

import subprocess
import sys

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

    monkeypatch.setattr(models.subprocess, "run", _run)

    assert models.list_agy_cli_model_options(env={"PATH": "/bin"}, timeout_s=3.5) == [
        {"id": "model-a", "displayName": "Model A", "isDefault": False}
    ]
    assert captured == {
        "argv": ["/opt/agy", "models"],
        "check": True,
        "capture_output": True,
        "text": True,
        "timeout": 3.5,
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
    """Stub the agy binary and record the discovery ``subprocess.run`` call."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(models, "agy_binary_path", lambda: "/opt/agy")

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="model-a\tModel A\n", stderr="")

    monkeypatch.setattr(models.subprocess, "run", _run)
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
    assert captured["timeout"] == 30.0


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
