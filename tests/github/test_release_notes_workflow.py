from __future__ import annotations

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
        if step.get("name") == "Combine curated highlights and contributor groups"
    )
    script = step["run"].replace("/tmp/", f"{tmp_path}/")
    notes = tmp_path / "release_notes.md"
    candidate = tmp_path / "composed_notes.md"
    notes.write_text("complete fallback")
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
    assert notes.read_text() == ("complete fallback" if failure else "candidate")
    assert ("::warning::Release-note composition failed" in result.stdout) == bool(failure)
