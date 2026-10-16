from pathlib import Path

from dev.repro import _copy_prepared_agent


def test_copy_prepared_agent_into_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "worktree/dev/repro-agent"
    source.mkdir()
    target.mkdir(parents=True)
    for name in ("config.yaml", "AGENTS.md"):
        (source / name).write_text(f"prepared {name}\n")
        (target / name).write_text(f"committed {name}\n")

    _copy_prepared_agent(source, tmp_path / "worktree")

    assert (target / "config.yaml").read_text() == "prepared config.yaml\n"
    assert (target / "AGENTS.md").read_text() == "prepared AGENTS.md\n"
