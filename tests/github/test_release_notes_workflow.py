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


def _step(name: str) -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return next(step for step in workflow["jobs"]["draft"]["steps"] if step.get("name") == name)


@pytest.mark.parametrize(
    ("tag", "source_ref", "valid"),
    [("v0.3.0", "a" * 40, True), ("v0.3.0rc1", "a" * 40, False), ("v0.3.0", "main", False)],
)
def test_preparation_requires_final_version_and_commit(tmp_path, tag, source_ref, valid) -> None:
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", _step("Resolve tag and guard")["run"]],
        env={
            **os.environ,
            "INPUT_TAG": tag,
            "INPUT_BASE": "",
            "INPUT_DRY_RUN": "",
            "PREPARATION": "true",
            "SOURCE_REF": source_ref,
            "EVENT_NAME": "schedule",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == valid
    if valid:
        assert "proceed=true" in output.read_text()
        assert "dry_run=false" in output.read_text()


@pytest.mark.parametrize("preparation", [True, False])
def test_preparation_harvest_defers_changelog(tmp_path, preparation) -> None:
    calls = tmp_path / "calls"
    script = _step("Harvest changelog and PR material")["run"].replace("/tmp/", f"{tmp_path}/")
    stub = """
    python3() {
      if [ "$1" = "-m" ]; then return; fi
      printf '%s\\n' "$@" > "$CALLS"
      printf 'notes' > "$MECHANICAL_NOTES"
    }
    """
    subprocess.run(
        ["bash", "-c", stub + script],
        env={
            **os.environ,
            "CALLS": str(calls),
            "MECHANICAL_NOTES": str(tmp_path / "mechanical_notes.md"),
            "TAG": "v0.3.0",
            "SOURCE_REPO": "o/o",
            "DRY_RUN": "false",
            "PREPARATION": str(preparation).lower(),
            "SOURCE_REF": "a" * 40 if preparation else "",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    args = calls.read_text().splitlines()
    assert ("--no-changelog-update" in args) == preparation
    assert ("--changelog-file" in args) != preparation
    assert ("--source-ref" in args) == preparation
    changelog_step = _step("Open or update the CHANGELOG.md PR")
    assert "!inputs.preparation" in changelog_step["if"]


@pytest.mark.parametrize(
    ("preparation", "existing"),
    [(True, None), (True, True), (True, False), (False, True), (False, False)],
)
def test_preparation_creates_draft_once_and_preserves_existing_notes(
    tmp_path, preparation, existing
) -> None:
    calls = tmp_path / "calls"
    output = tmp_path / "output"
    output.touch()
    stub = """
    gh() {
      printf '%s\\n' "$*" >> "$CALLS"
      if [[ "$*" != *"--method POST"* ]]; then printf '%s' "$MATCH"; fi
    }
    """
    subprocess.run(
        ["bash", "-c", stub + _step("Resolve draft release")["run"]],
        env={
            **os.environ,
            "CALLS": str(calls),
            "MATCH": "" if existing is None else json.dumps({"id": 123, "draft": existing}),
            "TAG": "v0.3.0",
            "SOURCE_REPO": "o/o",
            "PREPARATION": str(preparation).lower(),
            "SOURCE_REF": "a" * 40,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    requests = calls.read_text().splitlines()
    assert len(requests) == (2 if preparation and existing is None else 1)
    if preparation and existing is None:
        assert "--method POST repos/o/o/releases" in requests[1]
        assert "--field draft=true" in requests[1]
        assert "--field tag_name=v0.3.0" in requests[1]
        assert f"--field target_commitish={'a' * 40}" in requests[1]
        assert "--field body=@/tmp/release_notes.md" in requests[1]
    if preparation:
        assert output.read_text() == ""  # No output permits a subsequent draft overwrite.
    else:
        assert f"is_draft={str(existing).lower()}" in output.read_text()


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
