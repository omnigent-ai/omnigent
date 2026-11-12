"""Tests for the feature-agent driver (``dev/feature.py``)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import dev.feature as feature
import dev.resolve as resolve
import omnigent.host.git_worktree as git_worktree
from dev.feature import build_payload, ticket_key


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://linear.app/omnigent/issue/OMNI-1234", "omni-1234"),
        ("https://linear.app/omnigent/issue/OMNI-1234/add-session-rename", "omni-1234"),
        ("  https://linear.app/omnigent/issue/omni-7/  ", "omni-7"),
    ],
)
def test_ticket_key_from_linear_issue_url(url: str, key: str) -> None:
    assert ticket_key(url) == key


@pytest.mark.parametrize(
    "url",
    [
        "",
        "OMNI-1234",
        "https://github.com/omnigent-ai/omnigent/issues/1234",
        "https://linear.app/omnigent/project/OMNI-1234",
        "http://linear.app/omnigent/issue/OMNI-1234",
        "https://evil.example.com/linear.app/omnigent/issue/OMNI-1234",
        "https://linear.app/omnigent/issue/OMNI-1234/slug/extra",
    ],
)
def test_ticket_key_rejects_non_linear_issue_urls(url: str) -> None:
    with pytest.raises(ValueError, match="not a Linear issue URL"):
        ticket_key(url)


def test_payload_carries_the_ticket_and_only_enabled_flags() -> None:
    url = "https://linear.app/omnigent/issue/OMNI-1234/slug"
    assert json.loads(build_payload(f" {url} ")) == {"feature_url": url}
    assert json.loads(build_payload(url, skip_push=True, public=True)) == {
        "feature_url": url,
        "skip_push": True,
        "public": True,
    }
    with pytest.raises(ValueError):
        build_payload("https://github.com/omnigent-ai/omnigent/issues/1")


URL = "https://linear.app/omnigent/issue/OMNI-1234/slug"


@pytest.fixture
def launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Run ``main()`` with git, worktree creation and the agent process recorded."""
    calls: dict[str, object] = {}

    def unique_branch(slug: str, root: Path, prefix: str = "fix") -> str:
        calls["branch"] = (slug, prefix)
        return f"{prefix}/{slug}"

    def create_worktree(**kwargs: str) -> SimpleNamespace:
        calls["worktree"] = kwargs
        return SimpleNamespace(worktree_path=str(tmp_path), branch=kwargs["branch_name"])

    def run(
        cmd: list[str], cwd: str, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        calls["run"] = (cmd, cwd, env)
        return subprocess.CompletedProcess(cmd, 3)

    # main() imports these lazily, so patching the source modules reaches it.
    monkeypatch.setattr(resolve, "_resolve_base_ref", lambda root: "a" * 40)
    monkeypatch.setattr(resolve, "_unique_branch", unique_branch)
    monkeypatch.setattr(git_worktree, "create_worktree", create_worktree)
    monkeypatch.setattr(feature.subprocess, "run", run)
    return calls


def test_main_runs_the_agent_in_a_feat_worktree(
    launch: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["dev/feature.py", URL, "--skip-push", "--server", "http://s"]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setenv("LINEAR_API_KEY", "lin-key")
    monkeypatch.delenv("DATABRICKS_LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("DATABRICKS_REPRO_LINEAR_TOKEN", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        feature.main()

    assert exit_info.value.code == 3
    assert launch["branch"] == ("omni-1234", "feat")
    assert launch["worktree"] == {
        "repo_path": str(feature._REPO_ROOT),
        "branch_name": "feat/omni-1234",
        "base_branch": "a" * 40,
    }
    cmd, cwd, env = launch["run"]
    # --server runners only see the Linear key under its allowlisted name.
    assert env["DATABRICKS_LINEAR_API_KEY"] == "lin-key"
    assert env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] == "DATABRICKS_LINEAR_API_KEY"
    assert cwd == str(tmp_path)
    assert cmd[:3] == ["omnigent", "run", str(feature._REPO_ROOT / "dev/feature-agent")]
    assert json.loads(cmd[cmd.index("-p") + 1]) == {"feature_url": URL, "skip_push": True}
    assert cmd[cmd.index("--server") + 1] == "http://s"


def _closed_stdin(prompt: str) -> str:
    raise EOFError


@pytest.mark.parametrize("answer", [lambda prompt: "n", _closed_stdin])
def test_main_stops_before_creating_a_worktree_when_declined(
    launch: dict[str, object], monkeypatch: pytest.MonkeyPatch, answer
) -> None:
    monkeypatch.setattr(sys, "argv", ["dev/feature.py", URL])
    monkeypatch.setattr("builtins.input", answer)

    with pytest.raises(SystemExit) as exit_info:
        feature.main()

    assert exit_info.value.code == 1
    assert "worktree" not in launch and "run" not in launch


def test_main_prints_the_cleanup_hint_when_omnigent_cannot_start(
    launch: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("omnigent")

    monkeypatch.setattr(sys, "argv", ["dev/feature.py", URL, "--yes"])
    monkeypatch.setattr(feature.subprocess, "run", missing)

    with pytest.raises(SystemExit) as exit_info:
        feature.main()

    assert exit_info.value.code == 1
    assert "Clean up: git worktree remove" in capsys.readouterr().out
