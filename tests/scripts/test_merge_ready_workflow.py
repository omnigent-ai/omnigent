"""Exercise the Merge Ready workflow's head resolution and publication policy."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/merge-ready.yml").read_text())
HEAD = "a" * 40


def _step(job: str, step_id: str) -> dict:
    return next(step for step in WORKFLOW["jobs"][job]["steps"] if step.get("id") == step_id)


def _run_step(
    tmp_path: Path,
    script: str,
    *,
    env: dict[str, str] | None = None,
    pull: dict | None = None,
    api_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[str]]:
    if not shutil.which("bash") or not shutil.which("jq"):
        pytest.skip("Workflow execution requires bash and jq")

    (tmp_path / "pull.json").write_text(
        json.dumps(pull if pull is not None else {"state": "open", "head": {"sha": HEAD}})
    )
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$MERGE_READY_TEST_DIR/calls"\n'
        '[[ "$*" == "api repos/example/project/pulls/123" ]] || exit 99\n'
        'cat "$MERGE_READY_TEST_DIR/pull.json"\n'
        'exit "$MERGE_READY_TEST_API_EXIT"\n'
    )
    gh.chmod(0o755)
    output_path = tmp_path / "output"
    process_env = os.environ.copy()
    process_env.pop("BASH_ENV", None)
    process_env.update(
        {
            "PATH": f"{tmp_path}{os.pathsep}{process_env['PATH']}",
            "GH_TOKEN": "test-token",
            "REPO": "example/project",
            "PR": "123",
            "SHA_INPUT": "",
            "GITHUB_OUTPUT": str(output_path),
            "MERGE_READY_TEST_DIR": str(tmp_path),
            "MERGE_READY_TEST_API_EXIT": str(api_exit),
            **(env or {}),
        }
    )
    process = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    outputs = (
        dict(line.split("=", 1) for line in output_path.read_text().splitlines())
        if output_path.exists()
        else {}
    )
    calls_path = tmp_path / "calls"
    calls = calls_path.read_text().splitlines() if calls_path.exists() else []
    return process, outputs, calls


@pytest.mark.parametrize("sha_input", ["", HEAD, HEAD[:7]])
def test_resolver_returns_full_current_head(tmp_path: Path, sha_input: str) -> None:
    process, outputs, calls = _run_step(
        tmp_path, _step("resolve-head", "head")["run"], env={"SHA_INPUT": sha_input}
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert outputs == {"sha": HEAD}
    assert calls == ["api repos/example/project/pulls/123"]


@pytest.mark.parametrize(
    ("state", "sha_input"), [("closed", HEAD), ("open", "b" * 40), ("open", "b" * 7)]
)
def test_resolver_skips_closed_or_superseded_head(
    tmp_path: Path, state: str, sha_input: str
) -> None:
    process, outputs, _ = _run_step(
        tmp_path,
        _step("resolve-head", "head")["run"],
        env={"SHA_INPUT": sha_input},
        pull={"state": state, "head": {"sha": HEAD}},
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert outputs["skip"] == "true"


@pytest.mark.parametrize("value", ["", "123/commits", "123; touch injected"])
def test_resolver_rejects_invalid_pr_without_api_call(tmp_path: Path, value: str) -> None:
    process, outputs, calls = _run_step(
        tmp_path, _step("resolve-head", "head")["run"], env={"PR": value}
    )
    assert process.returncode != 0
    assert not outputs
    assert not calls
    assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize("sha_input", ["main", "a" * 6, "a" * 41, "$(touch injected)"])
def test_resolver_rejects_invalid_sha(tmp_path: Path, sha_input: str) -> None:
    process, outputs, _ = _run_step(
        tmp_path, _step("resolve-head", "head")["run"], env={"SHA_INPUT": sha_input}
    )
    assert process.returncode != 0
    assert not outputs
    assert not (tmp_path / "injected").exists()


def test_resolver_does_not_publish_head_after_api_failure(tmp_path: Path) -> None:
    process, outputs, _ = _run_step(tmp_path, _step("resolve-head", "head")["run"], api_exit=1)
    assert process.returncode != 0
    assert not outputs


@pytest.mark.parametrize("event", ["workflow_dispatch", "issue_comment"])
def test_evaluation_keeps_the_head_that_acquired_the_lock(tmp_path: Path, event: str) -> None:
    context = {"github.event_name": event, "github.event.issue.number": "123"}
    script = re.sub(
        r"\$\{\{ (.*?) \}\}",
        lambda match: context.get(match.group(1), ""),
        _step("evaluate", "ctx")["run"],
    )
    process, outputs, calls = _run_step(
        tmp_path,
        script,
        env={"RESOLVED_SHA": HEAD, "PR_INPUT": "123", "COMMENT_BODY": "/merge"},
        pull={"state": "open", "head": {"sha": "b" * 40}},
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert outputs == {"pr": "123", "sha": HEAD}
    assert not calls


@pytest.mark.parametrize(
    ("event", "automerge", "post_red"),
    [
        ("workflow_dispatch", "false", "true"),
        ("issue_comment", "false", "true"),
        ("workflow_run", "true", "true"),
        ("workflow_run", "false", "false"),
    ],
)
def test_explicit_reevaluation_can_revoke_unlabelled_green(
    tmp_path: Path, event: str, automerge: str, post_red: str
) -> None:
    process, outputs, _ = _run_step(
        tmp_path,
        _step("evaluate", "eligible")["run"],
        env={"EVENT": event, "AUTOMERGE": automerge},
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert outputs == {"run": "true", "post_red": post_red}


def test_all_status_writers_share_the_head_lock_even_when_resolution_is_skipped() -> None:
    evaluate = WORKFLOW["jobs"]["evaluate"]
    assert evaluate["needs"] == "resolve-head"
    assert "!cancelled()" in evaluate["if"]
    assert "needs.resolve-head.result == 'success'" in evaluate["if"]
    assert "needs.resolve-head.result == 'skipped'" in evaluate["if"]
    assert "needs.resolve-head.outputs.skip != 'true'" in evaluate["if"]
    assert evaluate["concurrency"]["cancel-in-progress"] is False
    group = evaluate["concurrency"]["group"]
    for source in (
        "needs.resolve-head.outputs.sha",
        "github.event.pull_request.head.sha",
        "github.event.workflow_run.head_sha",
        "github.event.check_run.head_sha",
    ):
        assert source in group
    assert (
        _step("evaluate", "ctx")["env"]["RESOLVED_SHA"] == "${{ needs.resolve-head.outputs.sha }}"
    )
