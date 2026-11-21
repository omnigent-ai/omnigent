"""The local lint gate must stay self-installing.

CI runs the same hooks, so a clone that silently stops installing them goes
red on push instead of on commit. These pin the two places that wire it up.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pre_commit_config_installs_the_commit_and_push_hooks() -> None:
    """A plain ``pre-commit install`` has to wire the push gate as well."""
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())

    assert set(config["default_install_hook_types"]) == {"pre-commit", "pre-push"}


def test_hooks_run_at_both_stages() -> None:
    """No hook opts out of the push stage, or the push gate would be weaker than CI."""
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())
    assert "default_stages" not in config

    narrowed = [
        hook["id"]
        for repo in config["repos"]
        for hook in repo["hooks"]
        if "stages" in hook and "pre-push" not in hook["stages"]
    ]
    assert narrowed == [], f"hooks that skip the push stage: {narrowed}"


def test_just_ensure_installs_the_hooks() -> None:
    """``just ensure`` is the documented setup path, so it has to install them."""
    justfile = (_REPO_ROOT / "justfile").read_text()

    ensure_line = re.search(r"^ensure:(.*)$", justfile, re.M)
    assert ensure_line is not None
    assert "_ensure-hooks" in ensure_line.group(1).split()
    assert re.search(r"^_ensure-hooks:.*\n\s+uv run --no-sync pre-commit install$", justfile, re.M)
