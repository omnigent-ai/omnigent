"""Exercise conservative diff selection and the matrices that consume it."""

import copy
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_only

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github/scripts/ci"
_SELECTOR = runpy.run_path(str(_CI / "select-test-suites.py"))
_POLLY = ".github/scripts/polly-review-summary.py"


def event_for(files):
    return {
        "repository": {"full_name": "test/repo"},
        "pull_request": {
            "number": 42,
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
            "changed_files": len(files),
        },
    }


def api_for(event, files):
    def api(endpoint):
        if "/files?" in endpoint:
            page = int(endpoint.split("page=")[-1])
            return files[(page - 1) * 100 : page * 100]
        return event["pull_request"]

    return api


@pytest.mark.parametrize(
    ("paths", "product"),
    [
        ([_POLLY], False),
        (
            [".github/workflows/polly-review.yml", "tests/scripts/test_polly_review_prompt.py"],
            False,
        ),
        ([_POLLY, "CHANGELOG.md"], False),
        ([_POLLY, "omnigent/runtime/prompt.py"], True),
        (["tests/conftest.py"], True),
        (["tests/e2e/conftest.py"], True),
        (["pyproject.toml"], True),
        (["uv.lock"], True),
        (["pnpm-lock.yaml"], True),
        ([".github/actions/setup-uv/action.yml"], True),
        ([".github/workflows/ci.yml"], True),
        ([".github/scripts/ci/select-test-suites.py"], True),
        (["tests/scripts/test_install_oss.py"], True),
        (["web/src/App.tsx"], True),
        (["tests/browser_ui/test_example.py"], True),
        (["unknown.txt"], True),
    ],
)
def test_selects_only_known_automation(paths, product):
    files = [{"filename": path, "status": "modified"} for path in paths]
    event = event_for(files)
    assert _SELECTOR["select"]("pull_request", event, api_for(event, files))[0] is product


@pytest.mark.parametrize("status", ["added", "modified", "removed", "renamed"])
def test_file_status_and_both_sides_of_renames(status):
    files = [{"filename": _POLLY, "status": status}]
    if status == "renamed":
        files[0]["previous_filename"] = "omnigent/runtime/prompt.py"
    event = event_for(files)
    assert _SELECTOR["select"]("pull_request", event, api_for(event, files))[0] is (
        status == "renamed"
    )


@pytest.mark.parametrize("event_name", ["push", "schedule", "workflow_dispatch"])
def test_non_pr_runs_never_filter_or_fetch(event_name):
    def unexpected_api(endpoint):
        pytest.fail("Non-PR runs should not fetch the diff")

    assert _SELECTOR["select"](event_name, {}, unexpected_api)[0] is True


@pytest.mark.parametrize(
    "fault",
    ["empty", "cap", "truncated", "duplicate", "renamed", "status", "before", "during", "api"],
)
def test_uncertain_diffs_run_everything(fault):
    files = [{"filename": _POLLY, "status": "modified"}]
    event = event_for(files)
    if fault in {"empty", "cap", "truncated"}:
        event["pull_request"]["changed_files"] = {"empty": 0, "cap": 3000, "truncated": 2}[fault]
    elif fault == "duplicate":
        files *= 2
        event["pull_request"]["changed_files"] = 2
    elif fault in {"renamed", "status"}:
        files[0]["status"] = "renamed" if fault == "renamed" else "unexpected"
    calls = 0

    def api(endpoint):
        nonlocal calls
        calls += 1
        if fault == "api":
            raise subprocess.TimeoutExpired("gh", 30)
        response = copy.deepcopy(api_for(event, files)(endpoint))
        if fault == "before" or (fault == "during" and calls == 3):
            response["head"]["sha"] = "c" * 40
        return response

    assert _SELECTOR["select"]("pull_request", event, api)[0] is True


def test_reads_all_pages_before_selecting():
    files = [{"filename": f"file-{i}", "status": "modified"} for i in range(101)]
    event = event_for(files)
    assert _SELECTOR["changed_paths"](event, api_for(event, files)) == {
        file["filename"] for file in files
    }


@pytest.mark.parametrize("script", ["e2e-shard-matrix.sh", "integration-matrix.sh"])
@pytest.mark.parametrize(
    ("product", "draft", "runs"),
    [(None, "false", True), ("false", "false", False), ("true", "true", False)],
)
def test_matrix_consumers(tmp_path, script, product, draft, runs):
    output = tmp_path / "output"
    env = dict(
        os.environ,
        GITHUB_OUTPUT=str(output),
        EVENT_NAME="pull_request",
        IS_DRAFT=draft,
        NUM_SHARDS="4",
    )
    env.pop("RUN_PRODUCT", None)
    if product is not None:
        env["RUN_PRODUCT"] = product
    subprocess.run(["bash", str(_CI / script)], env=env, check=True, capture_output=True)
    matrix = json.loads(output.read_text().removeprefix("matrix="))
    assert bool(matrix["include"]) is runs


@pytest.mark.parametrize("outcome", ["success", "failure", "in_progress", "cancelled"])
def test_merge_gate_requires_success_when_product_checks_are_absent(tmp_path, outcome):
    gh = tmp_path / "gh"
    checks = (
        "DCO\tcompleted\tsuccess\t2026-01-01\nPre-commit checks\tcompleted\tsuccess\t2026-01-01"
    )
    status = "in_progress" if outcome == "in_progress" else "completed"
    workflow = f"CI\t{status}\t{outcome}\t2026-01-01"
    gh.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"print({checks!r} if '/check-runs' in sys.argv[2] else {workflow!r})\n"
    )
    gh.chmod(0o755)
    result = subprocess.run(
        ["bash", str(_ROOT / ".github/scripts/merge-ready/evaluate-checks.sh")],
        env=dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            REPO="test/repo",
            SHA="b" * 40,
            GITHUB_OUTPUT=str(tmp_path / "output"),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if outcome == "success" else 1), result.stdout + result.stderr
    assert "Pytest (misc)" in result.stdout
