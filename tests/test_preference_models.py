from pathlib import Path

import pytest

from omnigent.preference_models import adopted_model, adopted_model_args
from omnigent.preference_startup import PREFERENCE_SNAPSHOT_ENV
from omnigent.preference_sync import (
    PreferenceNamespace,
    SharedPreferenceEntry,
    SharedPreferenceKey,
)
from tests._helpers.preferences import preference_snapshot


def model_snapshot(root: Path, model: str) -> Path:
    return preference_snapshot(
        root,
        identity=PreferenceNamespace(authority_id="test", user_id="alice", workspace_id="team"),
        shared=[
            SharedPreferenceEntry(
                key=SharedPreferenceKey(kind="model", name="model"), version=1, value=model
            )
        ],
    )


@pytest.mark.parametrize(
    "harness",
    [
        "claude-native",
        "codex-native",
        "pi-native",
        "opencode-native",
        "cursor-native",
        "kiro-native",
        "devin-native",
        "antigravity-native",
        "goose-native",
        "hermes-native",
        "qwen-native",
        "kimi-native",
    ],
)
def test_adopted_model_preserves_precedence(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(PREFERENCE_SNAPSHOT_ENV, raising=False)
    assert adopted_model(harness, tmp_path) is None
    path = model_snapshot(tmp_path / "shared", "portable")
    monkeypatch.setenv(PREFERENCE_SNAPSHOT_ENV, str(path))
    assert adopted_model(harness, tmp_path) == "portable"
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent/config.yaml").write_text(
        "model: project\nopencode_model: open-project\n"
    )
    assert adopted_model(harness, tmp_path) == (
        "open-project" if harness == "opencode-native" else "project"
    )
    assert adopted_model(harness, tmp_path, spec_model="spec") == "spec"
    assert (
        adopted_model(harness, tmp_path, spec_model="spec", session_model="session") == "session"
    )
    assert (
        adopted_model(harness, tmp_path, session_model="session", args=("--model", "explicit"))
        == "explicit"
    )
    assert adopted_model(harness, tmp_path, args=("--model=explicit",)) == "explicit"


@pytest.mark.parametrize("harness", ["hermes-native", "qwen-native", "kimi-native"])
def test_adopted_model_args_keep_order_and_explicit_choice(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv(
        PREFERENCE_SNAPSHOT_ENV, str(model_snapshot(tmp_path / "shared", "portable"))
    )
    assert adopted_model_args(harness, tmp_path, ("--example",)) == [
        "--example",
        "--model",
        "portable",
    ]
    assert adopted_model_args(harness, tmp_path, ("-m", "explicit")) == ["-m", "explicit"]
    assert adopted_model_args(harness, tmp_path, (), session_model="session") == [
        "--model",
        "session",
    ]
    monkeypatch.delenv(PREFERENCE_SNAPSHOT_ENV)
    assert adopted_model_args(harness, tmp_path, ("--example",)) == ["--example"]
