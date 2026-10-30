from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/draft-release-notes.yml"
pytestmark = pytest.mark.posix_only


@pytest.mark.parametrize("failure", ["composition", "move", None])
def test_composition_step_preserves_fallback_until_success(tmp_path, failure) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(
        step
        for step in workflow["jobs"]["draft"]["steps"]
        if step.get("name") == "Combine curated highlights and community thanks"
    )
    script = step["run"].replace("/tmp/", f"{tmp_path}/")
    notes = tmp_path / "release_notes.md"
    candidate = tmp_path / "composed_notes.md"
    notes.write_text("community fallback")
    stub = (
        "python3() {\n"
        f"  printf 'candidate' > {shlex.quote(str(candidate))}\n"
        f"  return {1 if failure == 'composition' else 0}\n"
        "}\n"
    )
    if failure == "move":
        stub += "mv() { return 1; }\n"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stub + script],
        env={**os.environ, "SOURCE_REPO": "o/o"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert notes.read_text() == ("community fallback" if failure else "candidate")
    assert ("::warning::Release-note composition failed" in result.stdout) == bool(failure)


@pytest.mark.parametrize(
    "raw",
    [
        "<!-- RELEASE_NOTES -->\n## Bug fixes\n- Fixed a crash (#1)\n<!-- /RELEASE_NOTES -->",
        "",
        "Agent failed",
    ],
)
def test_composition_step_omits_other_contributions(tmp_path, raw) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(
        step
        for step in workflow["jobs"]["draft"]["steps"]
        if step.get("name") == "Combine curated highlights and community thanks"
    )
    (tmp_path / "draft_out.txt").write_text(raw)
    (tmp_path / "pr_credits.json").write_text(
        json.dumps(
            [
                {"pr": 1, "author": "alice", "author_url": "https://github.com/alice"},
                {"pr": 2, "author": "bob", "author_url": "https://github.com/bob"},
            ]
        )
    )
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"].replace("/tmp/", f"{tmp_path}/")],
        cwd=WORKFLOW.parents[2],
        env={**os.environ, "SOURCE_REPO": "o/o"},
        capture_output=True,
        text=True,
        check=True,
    )
    notes = (tmp_path / "release_notes.md").read_text()
    assert "Other contributions" not in notes
    assert "### 💜 Thanks to our community" in notes
    assert "[@alice](https://github.com/alice)" in notes
    assert "[@bob](https://github.com/bob)" in notes
    assert "/pull/2)" not in notes
    assert ("Fixed a crash" in notes) == ("RELEASE_NOTES" in raw)
    assert notes.endswith("Full Changelog: https://github.com/o/o/blob/main/CHANGELOG.md\n")
