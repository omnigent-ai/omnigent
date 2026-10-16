from pathlib import Path

import pytest

from dev.repro import _copy_prepared_agent


@pytest.mark.parametrize("target_exists", [True, False])
def test_copy_prepared_agent_into_worktree(tmp_path: Path, target_exists: bool) -> None:
    source = tmp_path / "source"
    target = tmp_path / "worktree/dev/repro-agent"
    source.mkdir()
    if target_exists:
        target.mkdir(parents=True)
    for name in ("config.yaml", "AGENTS.md"):
        (source / name).write_text(f"prepared {name}\n")
        if target_exists:
            (target / name).write_text(f"committed {name}\n")

    _copy_prepared_agent(source, tmp_path / "worktree")

    assert (target / "config.yaml").read_text() == "prepared config.yaml\n"
    assert (target / "AGENTS.md").read_text() == "prepared AGENTS.md\n"
