"""Tests for the first-class Qoder ACP harness spawn builders."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_qoder_cn_spawn_env, _build_qoder_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(harness: str) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name=f"test-{harness}",
        instructions="Test Qoder.",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )


@pytest.mark.parametrize(
    "harness,builder,binary,label,token",
    [
        ("qoder", _build_qoder_spawn_env, "qodercli", "Qoder", "QODER_PERSONAL_ACCESS_TOKEN"),
        (
            "qoder-cn",
            _build_qoder_cn_spawn_env,
            "qoderclicn",
            "Qoder CN",
            "QODERCN_PERSONAL_ACCESS_TOKEN",
        ),
    ],
)
def test_qoder_spawn_env_uses_acp_and_vendor_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: str,
    builder,
    binary: str,
    label: str,
    token: str,
) -> None:
    resolved = tmp_path / "Qoder CLI" / binary
    monkeypatch.setattr(
        "omnigent._platform.resolve_cli_binary",
        lambda candidate: str(resolved) if candidate == binary else None,
    )

    env = builder(_spec(harness), cwd=tmp_path)

    assert shlex.split(env["HARNESS_ACP_COMMAND"]) == [str(resolved), "--acp"]
    assert env["HARNESS_ACP_NAME"] == label
    assert env["HARNESS_ACP_SESSION_ID_MODE"] == "server"
    assert json.loads(env["HARNESS_ACP_ENV_PASSTHROUGH"]) == [token]
    assert env["HARNESS_ACP_CWD"] == str(tmp_path)
    assert "HARNESS_ACP_MODEL" not in env


def test_qoder_builder_falls_back_to_path_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent._platform.resolve_cli_binary", lambda _candidate: None)
    env = _build_qoder_spawn_env(_spec("qoder"))
    assert shlex.split(env["HARNESS_ACP_COMMAND"]) == ["qodercli", "--acp"]
