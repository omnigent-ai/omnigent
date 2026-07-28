from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent import pi_native
from omnigent.pi_native import pi_native_model_options

_PROVIDER = "databricks-mlflow"
_DEFAULT_MODEL = "system.ai.gpt-5-6-luna"
_OTHER_MODEL = "system.ai.gpt-5-6-sol"
_CATALOG = f"""provider model context max-out thinking images
{_PROVIDER} {_OTHER_MODEL} 1.1M 128K yes yes
other-provider {_DEFAULT_MODEL} 1.1M 128K yes yes
{_PROVIDER} {_DEFAULT_MODEL} 1.1M 128K yes yes
"""


def _write_settings(path: Path, model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"defaultProvider": _PROVIDER, "defaultModel": model}),
        encoding="utf-8",
    )


def test_pi_native_model_options_uses_workspace_catalog_and_marks_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_dir = tmp_path / "pi-agent"
    workspace = tmp_path / "workspace"
    _write_settings(agent_dir / "settings.json", _OTHER_MODEL)
    _write_settings(workspace / ".pi" / "settings.json", _DEFAULT_MODEL)
    captured: dict[str, object] = {}

    def run(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 0, _CATALOG, "")

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

    assert [option["id"] for option in options] == [_OTHER_MODEL, _DEFAULT_MODEL]
    assert options[0]["provider"] == _PROVIDER
    assert "isDefault" not in options[0]
    assert options[1]["provider"] == _PROVIDER
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
                "id": _OTHER_MODEL,
                "model": _OTHER_MODEL,
                "provider": _PROVIDER,
                "isDefault": True,
            }
        ],
    )

    assert pi_native.resolve_pi_native_local_model_selection(_OTHER_MODEL) == (
        _PROVIDER,
        _OTHER_MODEL,
    )
