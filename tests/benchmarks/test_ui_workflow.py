"""Exercise workflow detection, baseline selection, and benchmark command modes."""

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
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="Needs bash")


def test_candidate_execution_waits_for_security_gate() -> None:
    jobs = _WORKFLOW["jobs"]
    assert jobs["gate"]["uses"] == "./.github/workflows/security-gate.yml"
    for name in ("detect", "benchmark"):
        dependencies = jobs[name]["needs"]
        assert "gate" in ([dependencies] if isinstance(dependencies, str) else dependencies)


@pytest.mark.skipif(not shutil.which("jq"), reason="Needs jq")
@pytest.mark.parametrize(
    ("files", "api_failure", "expected"),
    [
        ([], False, "false"),
        ([{"filename": "web/src/index.css"}], False, "true"),
        ([{"filename": "tests/browser_ui/test_ui_benchmark.py"}], False, "true"),
        ([{"filename": "omnigent/server/app.py"}], False, "true"),
        ([{"filename": "omnigent/stores/file_store/sqlalchemy_store.py"}], False, "true"),
        ([{"filename": "omnigent/db/__init__.py"}], False, "true"),
        ([{"filename": "omnigent/cli_auth.py"}], False, "true"),
        ([{"filename": "tests/_helpers/compat.py"}], False, "true"),
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
        # Below the API cap but larger than a pipe buffer.
        (
            [{"filename": "web/src/index.css"}]
            + [{"filename": f"docs/long/path/to/file-{i}.md"} for i in range(2998)],
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
        "db",
        "shared-server-module",
        "server-test-helper",
        "config",
        "config-backups",
        "docs",
        "rename-out",
        "rename-in",
        "api-error",
        "large-response",
    ),
)
def test_detect_ui_benchmark_changes(
    tmp_path: Path, files: list[dict[str, str]], api_failure: bool, expected: str
) -> None:
    output, called_api = _detect_changes(
        tmp_path, files, api_failure=api_failure, total_files=len(files)
    )
    assert output == f"ui={expected}\n"
    assert called_api


@pytest.mark.skipif(not shutil.which("jq"), reason="Needs jq")
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


@pytest.mark.parametrize(
    ("event", "compare_same_build", "baseline"),
    [
        ("pull_request", "", "baseline"),
        ("schedule", "", None),
        ("workflow_dispatch", "false", None),
        ("workflow_dispatch", "true", "candidate"),
    ],
    ids=["pr-comparison", "nightly", "manual-standalone", "manual-aa"],
)
def test_benchmark_command_selects_comparison_mode(
    tmp_path: Path, event: str, compare_same_build: str, baseline: str | None
) -> None:
    script = next(
        step["run"]
        for step in _WORKFLOW["jobs"]["benchmark"]["steps"]
        if step.get("id") == "benchmark"
    )
    captured = tmp_path / "uv-args"
    runner_temp = tmp_path / "runner temp"
    uv = tmp_path / "uv"
    uv.write_text('#!/usr/bin/env bash\nprintf "%s\\0" "$@" > "$UV_ARGS_OUTPUT"\n')
    uv.chmod(0o755)
    git = tmp_path / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  "rev-parse HEAD") echo candidate-revision ;;\n'
        '  "-C ui-baseline rev-parse HEAD") echo base-revision ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    git.chmod(0o755)
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "EVENT_NAME": event,
            "COMPARE_SAME_BUILD": compare_same_build,
            "RUNNER_TEMP": str(runner_temp),
            "UV_ARGS_OUTPUT": str(captured),
        },
        text=True,
        capture_output=True,
        check=True,
    )
    expected = [
        "run",
        "--no-sync",
        "dev/benchmarks/ui/run.py",
        "--web-dist",
        str(runner_temp / "ui-candidate"),
        "--revision",
        "candidate-revision",
        "--output-dir",
        "artifacts/ui-benchmark",
    ]
    if baseline:
        expected.extend(
            [
                "--baseline-dist",
                str(runner_temp / f"ui-{baseline}"),
                "--baseline-revision",
                "base-revision" if baseline == "baseline" else "candidate-revision",
            ]
        )
    assert [arg.decode() for arg in captured.read_bytes().split(b"\0")[:-1]] == expected


@pytest.mark.skipif(not shutil.which("git"), reason="Needs git")
def test_baseline_is_first_parent_of_shallow_test_merge(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "UI benchmark test",
        "GIT_AUTHOR_EMAIL": "benchmark@example.invalid",
        "GIT_COMMITTER_NAME": "UI benchmark test",
        "GIT_COMMITTER_EMAIL": "benchmark@example.invalid",
    }

    def git(directory: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=directory, env=env, text=True, capture_output=True, check=True
        ).stdout.strip()

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    git(source, "commit", "--allow-empty", "-m", "initial")
    git(source, "switch", "-c", "candidate")
    (source / "candidate.txt").write_text("candidate change\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "candidate")
    git(source, "switch", "main")
    (source / "base.txt").write_text("base change\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    expected_base = git(source, "rev-parse", "HEAD")
    git(source, "merge", "--no-ff", "candidate", "-m", "test merge")

    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "--depth=2", source.as_uri(), str(checkout))
    git(checkout, "checkout", "--detach", "HEAD")
    assert git(checkout, "rev-parse", "--is-shallow-repository") == "true"
    script = next(
        step["run"]
        for step in _WORKFLOW["jobs"]["benchmark"]["steps"]
        if step.get("id") == "baseline"
    )
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    baseline = checkout / "ui-baseline"
    assert git(baseline, "rev-parse", "HEAD") == expected_base
    assert (baseline / "base.txt").read_text() == "base change\n"
    assert not (baseline / "candidate.txt").exists()


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
