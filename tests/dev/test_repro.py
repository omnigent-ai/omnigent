import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import dev.repro as repro


def test_prepared_agent_does_not_dirty_repro_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    agent = repo / "dev/repro-agent"
    agent.mkdir(parents=True)
    (agent / "config.yaml").write_text("name: repro\n")
    (agent / "AGENTS.md").write_text("committed\n")

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        )

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    (agent / "AGENTS.md").write_text("prepared\n")

    launched: dict[str, object] = {}
    real_run = subprocess.run

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "omnigent":
            launched.update(cmd=cmd, cwd=kwargs["cwd"])
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(repro, "_REPO_ROOT", repo)
    monkeypatch.setattr(
        repro,
        "_parse_args",
        lambda: SimpleNamespace(bug_url="OMNI-1", server="https://example.test", public=False),
    )
    monkeypatch.setattr(repro.subprocess, "run", run)

    with pytest.raises(SystemExit, match="0"):
        repro.main()

    worktree = Path(str(launched["cwd"]))
    assert launched["cmd"][2] == str(agent)  # type: ignore[index]
    assert git_status(worktree) == ""


def git_status(worktree: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
