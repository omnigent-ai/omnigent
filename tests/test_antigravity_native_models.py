"""Tests for live ``agy models`` discovery."""

from __future__ import annotations

import subprocess

import pytest

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
