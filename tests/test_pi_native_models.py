from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent import pi_native
from omnigent.pi_native import pi_native_model_options


def test_pi_native_model_options_uses_workspace_catalog_and_marks_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".pi").mkdir()
    (workspace / ".pi" / "settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "databricks-mlflow",
                "defaultModel": "system.ai.gpt-5-6-luna",
            }
        ),
        encoding="utf-8",
    )
    (agent_dir / "settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "databricks-mlflow",
                "defaultModel": "system.ai.gpt-5-6-sol",
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def run(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["pi", "--list-models"],
            returncode=0,
            stdout=(
                "provider model context max-out thinking images\n"
                "databricks-mlflow system.ai.gpt-5-6-sol 1.1M 128K yes yes\n"
                "other-provider system.ai.gpt-5-6-luna 1.1M 128K yes yes\n"
                "databricks-mlflow system.ai.gpt-5-6-luna 1.1M 128K yes yes\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(pi_native, "pi_supports_approve", lambda _executable: True)

    options = pi_native_model_options(
        env={
            "PATH": "/bin",
            "OMNIGENT_PI_PATH": "/bin/echo",
            "PI_CODING_AGENT_DIR": str(agent_dir),
        },
        cwd=workspace,
        run=run,
    )

    assert [option["id"] for option in options] == [
        "system.ai.gpt-5-6-sol",
        "system.ai.gpt-5-6-luna",
    ]
    assert options[0]["provider"] == "databricks-mlflow"
    assert "isDefault" not in options[0]
    assert options[1]["provider"] == "databricks-mlflow"
    assert options[1]["isDefault"] is True
    assert captured["args"] == ["/bin/echo", "--approve", "--list-models"]
    assert captured["cwd"] == str(workspace)


def test_resolve_pi_native_local_model_selection_keeps_its_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pi_native,
        "pi_native_model_options",
        lambda **_kwargs: [
            {
                "id": "system.ai.gpt-5-6-sol",
                "model": "system.ai.gpt-5-6-sol",
                "provider": "databricks-mlflow",
                "isDefault": True,
            }
        ],
    )

    assert pi_native.resolve_pi_native_local_model_selection("system.ai.gpt-5-6-sol") == (
        "databricks-mlflow",
        "system.ai.gpt-5-6-sol",
    )
