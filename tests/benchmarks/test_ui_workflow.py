"""Exercise the actual changed-path detection shell, including rename sources."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = yaml.safe_load((_ROOT / ".github/workflows/benchmark-ui.yml").read_text())
_DETECT = _WORKFLOW["jobs"]["detect"]["steps"][0]["run"]


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("jq"), reason="Needs bash and jq")
@pytest.mark.parametrize(
    ("files", "api_failure", "expected"),
    [
        ([], False, "false"),
        ([{"filename": "web/src/index.css"}], False, "true"),
        ([{"filename": "omnigent/server/app.py"}], False, "true"),
        ([{"filename": "docs/ui.md"}], False, "false"),
        (
            [{"filename": "docs/styles.css", "previous_filename": "web/src/index.css"}],
            False,
            "true",
        ),
        (
            [{"filename": "web/src/index.css", "previous_filename": "docs/styles.css"}],
            False,
            "true",
        ),
        ([], True, "true"),
        (
            [{"filename": "web/src/index.css"}]
            + [{"filename": f"docs/long/path/to/file-{i}.md"} for i in range(3000)],
            False,
            "true",
        ),
    ],
    ids=("empty", "ui", "server", "docs", "rename-out", "rename-in", "api-error", "large-pr"),
)
def test_detect_ui_benchmark_changes(
    tmp_path: Path, files: list[dict[str, str]], api_failure: bool, expected: str
) -> None:
    changes = tmp_path / "changes.json"
    changes.write_text(json.dumps(files))
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$API_FAILURE" = true ]; then exit 1; fi\n'
        'jq -r "${@: -1}" "$CHANGED_FILES_JSON"\n'
    )
    gh.chmod(0o755)
    output = tmp_path / "github_output"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", _DETECT],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "API_FAILURE": str(api_failure).lower(),
            "CHANGED_FILES_JSON": str(changes),
            "GITHUB_OUTPUT": str(output),
            "REPO": "owner/repo",
            "PR": "123",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"ui={expected}\n"
