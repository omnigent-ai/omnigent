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
_DETECT = next(
    step["run"] for step in _WORKFLOW["jobs"]["detect"]["steps"] if step.get("id") == "changes"
)
pytestmark = pytest.mark.skipif(
    not shutil.which("bash") or not shutil.which("jq"), reason="Needs bash and jq"
)


@pytest.mark.parametrize(
    ("files", "api_failure", "expected"),
    [
        ([], False, "false"),
        ([{"filename": "web/src/index.css"}], False, "true"),
        ([{"filename": "tests/browser_ui/test_ui_benchmark.py"}], False, "true"),
        ([{"filename": "omnigent/server/app.py"}], False, "true"),
        ([{"filename": "omnigent/stores/file_store/sqlalchemy_store.py"}], False, "true"),
        ([{"filename": "uv.lock"}], False, "true"),
        (
            [
                {"filename": "package.json.bak"},
                {"filename": "uv.lock.orig"},
                {"filename": "pyproject.toml.rej"},
                {"filename": ".github/workflows/benchmark-ui.yml.bak"},
            ],
            False,
            "false",
        ),
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
    ids=(
        "empty",
        "ui",
        "browser-tests",
        "server",
        "stores",
        "config",
        "config-backups",
        "docs",
        "rename-out",
        "rename-in",
        "api-error",
        "large-pr",
    ),
)
def test_detect_ui_benchmark_changes(
    tmp_path: Path, files: list[dict[str, str]], api_failure: bool, expected: str
) -> None:
    output, called_api = _detect_changes(tmp_path, files, api_failure=api_failure)
    assert output == f"ui={expected}\n"
    assert called_api


@pytest.mark.parametrize(
    ("pr", "total_files", "expected", "uses_api"),
    [
        ("", 0, "true", False),
        ("123", 2999, "false", True),
        ("123", 3000, "true", False),
        ("123", 3001, "true", False),
    ],
    ids=["nightly-manual", "below-api-cap", "at-api-cap", "truncated-api"],
)
def test_detect_without_complete_file_list(
    tmp_path: Path, pr: str, total_files: int, expected: str, uses_api: bool
) -> None:
    output, called_api = _detect_changes(
        tmp_path, [{"filename": "docs/ui.md"}], pr=pr, total_files=total_files
    )
    assert output == f"ui={expected}\n"
    assert called_api == uses_api


def _detect_changes(
    tmp_path: Path,
    files: list[dict[str, str]],
    *,
    api_failure: bool = False,
    pr: str = "123",
    total_files: int = 0,
) -> tuple[str, bool]:
    changes = tmp_path / "changes.json"
    changes.write_text(json.dumps(files))
    called_api = tmp_path / "called_api"
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'touch "$CALLED_API"\n'
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
            "CALLED_API": str(called_api),
            "CHANGED_FILES_JSON": str(changes),
            "GITHUB_OUTPUT": str(output),
            "REPO": "owner/repo",
            "PR": pr,
            "PR_CHANGED_FILES": str(total_files),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return output.read_text(), called_api.exists()
