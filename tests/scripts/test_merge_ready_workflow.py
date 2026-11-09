"""Exercise the Merge Ready workflow's head resolution and publication policy."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
    assert evaluate["needs"] == ["resolve-head", "request-automerge"]
    assert "!cancelled()" in evaluate["if"]
    assert "needs.resolve-head.result == 'success'" in evaluate["if"]
    assert "needs.resolve-head.result == 'skipped'" in evaluate["if"]
    assert "needs.resolve-head.outputs.skip != 'true'" in evaluate["if"]
    assert "needs.request-automerge.result == 'success'" in evaluate["if"]
    assert "needs.request-automerge.result == 'skipped'" in evaluate["if"]
    assert evaluate["concurrency"]["cancel-in-progress"] is False
    assert evaluate["concurrency"]["queue"] == "max"
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


def test_expiry_dispatch_skips_full_evaluation_and_uses_the_locked_head() -> None:
    inputs = WORKFLOW["on" if "on" in WORKFLOW else True]["workflow_dispatch"]["inputs"]
    assert inputs["expire_only"]["type"] == "boolean"
    assert inputs["expire_only"]["default"] is False
    expiry = _step("evaluate", "expiry")
    assert expiry["if"] == "steps.ctx.outputs.skip != 'true' && inputs.expire_only == true"
    assert expiry["env"] == {
        "PR": "${{ steps.ctx.outputs.pr }}",
        "SHA": "${{ steps.ctx.outputs.sha }}",
    }
    assert 'require("./.github/scripts/merge-ready/expiry.js")' in expiry["with"]["script"]
    assert "pr: Number(process.env.PR), sha: process.env.SHA" in expiry["with"]["script"]
    for step_id in ("labels", "eligible"):
        assert "inputs.expire_only != true" in _step("evaluate", step_id)["if"]
    for step_id in ("eval", "gate"):
        assert "steps.eligible.outputs.run == 'true'" in _step("evaluate", step_id)["if"]
    for step_id in ("parser", "dependency"):
        assert (
            "steps.labels.outputs.dependency_required == 'true'"
            in _step("evaluate", step_id)["if"]
        )
    steps = WORKFLOW["jobs"]["evaluate"]["steps"]
    for name in ("Post Merge Ready status on PR head SHA", "Fail job when gate is red"):
        step = next(step for step in steps if step["name"] == name)
        assert "steps.eligible.outputs.run == 'true'" in step["if"]
    enable = next(step for step in steps if step["name"] == "Enable requested auto-merge")
    assert "steps.labels.outputs.automerge == 'true'" in enable["if"]


@pytest.mark.parametrize("expires", ["2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
def test_expiry_action_executes_with_workflow_inputs(expires: str) -> None:
    step = _step("evaluate", "expiry")
    script = """
const assert = require("node:assert/strict");
const { script, expires, sha } = JSON.parse(process.argv[1]);
const writes = [];
const github = {
  graphql: async (_query, variables) => {
    assert.deepEqual(variables, { owner: "example", repo: "project", pr: 123 });
    return { repository: { pullRequest: {
      state: "OPEN", headRefOid: sha,
      commits: { nodes: [{ commit: { oid: sha, status: { contexts: [{
        context: "Merge Ready", state: "SUCCESS",
        description: `All required checks green; expires ${expires}`,
      }] } } }] },
    } } };
  },
  rest: { repos: { createCommitStatus: async (request) => writes.push(request) } },
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
await new AsyncFunction("require", "github", "context", "core", script)(
  require, github, { repo: { owner: "example", repo: "project" } }, { info: () => {} },
);
console.log(JSON.stringify(writes));
"""
    # github-script supplies CommonJS require and awaits the workflow's script body.
    process = subprocess.run(
        [
            "node",
            "-e",
            "(async () => {" + script + "})()",
            json.dumps(
                {
                    "script": step["with"]["script"],
                    "expires": expires,
                    "sha": HEAD,
                }
            ),
        ],
        cwd=ROOT,
        env=dict(os.environ, PR="123", SHA=HEAD),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    writes = json.loads(process.stdout)
    if expires.startswith("2000"):
        assert writes == [
            {
                "owner": "example",
                "repo": "project",
                "sha": HEAD,
                "context": "Merge Ready",
                "state": "failure",
                "description": "CI freshness not satisfied (24h); update branch and run fresh CI",
            }
        ]
    else:
        assert writes == []


def test_merge_requests_survive_replacement_of_a_pending_evaluator() -> None:
    request = WORKFLOW["jobs"]["request-automerge"]
    assert "needs" not in request
    assert "contains(github.event.comment.body, '/merge')" in request["if"]
    assert "github.event.issue.state == 'open'" in request["if"]
    assert "concurrency" not in request
    assert "concurrency" not in WORKFLOW
    assert "statuses" not in request["permissions"]
    resolve = WORKFLOW["jobs"]["resolve-head"]
    assert resolve["needs"] == "request-automerge"
    assert "!cancelled()" in resolve["if"]
    assert "needs.request-automerge.result == 'success'" in resolve["if"]
    assert "needs.request-automerge.result == 'skipped'" in resolve["if"]
    record = next(step for step in request["steps"] if step["name"] == "Record /merge request")
    assert record["if"] == "steps.authz.outputs.authorized == 'true'"
    assert "steps.command.outputs.skip != 'true'" in _step("request-automerge", "authz")["if"]

    steps = WORKFLOW["jobs"]["evaluate"]["steps"]
    publish = next(
        step for step in steps if step["name"] == "Post Merge Ready status on PR head SHA"
    )
    enable = next(step for step in steps if step["name"] == "Enable requested auto-merge")
    gate = _step("evaluate", "gate")
    assert gate["env"]["EVAL"] == "${{ steps.eval.outcome }}"
    for name in ("FAILED", "FRESHNESS_FAILED", "API_FAILED", "EXPIRES_AT"):
        assert gate["env"][name] == "${{ steps.eval.outputs." + name.lower() + " }}"
    assert publish["env"]["SHA"] == "${{ steps.ctx.outputs.sha }}"
    assert publish["env"]["STATE"] == "${{ steps.gate.outputs.state }}"
    assert publish["env"]["DESC"] == "${{ steps.gate.outputs.short_desc }}"
    assert "steps.gate.outputs.post_status == 'true'" in publish["if"]
    assert steps.index(publish) < steps.index(enable)
    assert not publish.get("continue-on-error", False)
    assert "always()" not in enable["if"]
    assert "!cancelled()" not in enable["if"]
    assert "github.event" not in enable["if"]
    assert "steps.labels.outputs.automerge == 'true'" in enable["if"]
    assert enable["env"]["SHA"] == "${{ steps.ctx.outputs.sha }}"
    assert enable["env"]["GATE_STATE"] == "${{ steps.gate.outputs.state }}"
    assert enable["env"]["EXPIRES_AT"] == "${{ steps.eval.outputs.expires_at }}"


@pytest.mark.parametrize("body", ["Please run /merge later", "/mergefoo", "`/merge`"])
def test_incidental_mentions_do_not_record_merge_intent(tmp_path: Path, body: str) -> None:
    process, outputs, calls = _run_step(
        tmp_path,
        _step("request-automerge", "command")["run"],
        env={"COMMENT_BODY": body},
    )
    assert process.returncode == 0
    assert outputs == {"skip": "true"}
    assert not calls


def test_required_lint_lane_runs_sweep_tests_and_shell_validation() -> None:
    lint = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())
    job = lint["jobs"]["pre-commit"]
    assert job["name"] == "Pre-commit checks"
    sweep = next(
        step
        for step in job["steps"]
        if step.get("run") == "node --test .github/scripts/merge-ready/sweep.test.js"
    )
    syntax = next(
        step for step in job["steps"] if ".github/scripts/merge-ready/*.sh" in step.get("run", "")
    )
    assert 'bash -n "$script"' in syntax["run"]
    for step in (sweep, syntax):
        assert "if" not in step
        assert not step.get("continue-on-error", False)


def test_sweep_schedule_and_manual_dry_run_policy() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/merge-ready-sweep.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"] == [{"cron": "7,22,37,52 * * * *"}]
    dry_run = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is True
    step = next(
        step
        for step in workflow["jobs"]["sweep"]["steps"]
        if step["name"] == "Reevaluate expired green statuses"
    )
    assert "if" not in step
    assert step["env"]["DRY_RUN"] == "${{ inputs.dry_run }}"
    assert (
        'dryRun: context.eventName !== "schedule" && process.env.DRY_RUN !== "false"'
        in step["with"]["script"]
    )


Reply = tuple[list[str], str, int, str | None]


def _reply(prefix: list[str], output: str = "", code: int = 0, now: str | None = None) -> Reply:
    return prefix, output, code, now


def _pull_reply(*, state: str = "open", sha: str = HEAD, labelled: bool = True) -> Reply:
    return _reply(
        ["api", "repos/example/project/pulls/123"],
        json.dumps(
            {
                "state": state,
                "head": {"sha": sha},
                "labels": [{"name": "automerge"}] if labelled else [],
            }
        ),
    )


def _run_helper(
    tmp_path: Path,
    script: str,
    replies: list[Reply],
    *,
    state: str = "success",
    expires: str = "2026-10-19T12:00:01Z",
) -> tuple[dict[str, str], list[list[str]], str]:
    (tmp_path / "replies.json").write_text(json.dumps(replies))
    (tmp_path / "clock").write_text("2026-10-19T12:00:00Z")
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['MERGE_READY_TEST_DIR'])\n"
        "calls = root / 'calls'\n"
        "index = len(calls.read_text().splitlines()) if calls.exists() else 0\n"
        "with calls.open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "replies = json.loads((root / 'replies.json').read_text())\n"
        "if index >= len(replies):\n"
        "    (root / 'unexpected').write_text('Unexpected gh call: ' + str(sys.argv[1:]))\n"
        "    sys.exit(99)\n"
        "prefix, output, code, now = replies[index]\n"
        "if sys.argv[1:1+len(prefix)] != prefix:\n"
        "    (root / 'unexpected').write_text('Wrong gh call: ' + str(sys.argv[1:]))\n"
        "    sys.exit(99)\n"
        "if now is not None: (root / 'clock').write_text(now)\n"
        "sys.stdout.write(output)\n"
        "sys.exit(code)\n"
    )
    gh.chmod(0o755)
    date = tmp_path / "date"
    date.write_text('#!/bin/sh\ncat "$MERGE_READY_TEST_DIR/clock"\n')
    date.chmod(0o755)
    output_path = tmp_path / "outputs"
    process = subprocess.run(
        ["bash", str(ROOT / ".github/scripts/merge-ready" / script)],
        env=dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            MERGE_READY_TEST_DIR=str(tmp_path),
            GITHUB_OUTPUT=str(output_path),
            GH_TOKEN="test-token",
            REPO="example/project",
            PR="123",
            AUTHOR="maintainer",
            SHA=HEAD,
            GATE_STATE=state,
            EXPIRES_AT=expires,
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert not (tmp_path / "unexpected").exists(), (
        (tmp_path / "unexpected").read_text() if (tmp_path / "unexpected").exists() else ""
    )
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert len(calls) == len(replies), process.stdout + process.stderr
    outputs = (
        dict(line.split("=", 1) for line in output_path.read_text().splitlines())
        if output_path.exists()
        else {}
    )
    return outputs, calls, process.stdout


@pytest.mark.parametrize("permission", ["admin", "maintain", "write", "read", "none"])
def test_only_authorized_commands_can_record_intent(tmp_path: Path, permission: str) -> None:
    authorized = permission in {"admin", "maintain", "write"}
    replies = [
        _reply(["api", "repos/example/project/collaborators/maintainer/permission"], permission)
    ]
    if not authorized:
        replies.append(_reply(["pr", "comment", "123"]))
    outputs, _, _ = _run_helper(tmp_path, "authorize-merge-comment.sh", replies)
    assert outputs == {"authorized": str(authorized).lower()}


def test_command_records_durable_intent_without_attempting_a_merge(tmp_path: Path) -> None:
    _, calls, _ = _run_helper(
        tmp_path,
        "enable-automerge-comment.sh",
        [_reply(["pr", "edit", "123"]), _reply(["pr", "comment", "123"])],
    )
    assert calls[0][-2:] == ["--add-label", "automerge"]
    assert "recorded" in calls[1][-1]
    assert not any(call[:2] == ["pr", "merge"] for call in calls)


@pytest.mark.parametrize("state", ["success", "failure"])
def test_surviving_evaluation_enables_the_current_request(tmp_path: Path, state: str) -> None:
    _, calls, _ = _run_helper(
        tmp_path,
        "enable-automerge-label.sh",
        [_pull_reply(), _reply(["pr", "merge", "123"])],
        state=state,
    )
    assert "--auto" in calls[1]
    assert calls[1][-2:] == ["--match-head-commit", HEAD]


@pytest.mark.parametrize("reason", ["closed", "head-changed", "label-removed"])
def test_obsolete_request_does_not_merge(tmp_path: Path, reason: str) -> None:
    _run_helper(
        tmp_path,
        "enable-automerge-label.sh",
        [
            _pull_reply(
                state="closed" if reason == "closed" else "open",
                sha="b" * 40 if reason == "head-changed" else HEAD,
                labelled=reason != "label-removed",
            )
        ],
    )


@pytest.mark.parametrize("expires", ["", "2026-10-19T12:00:00Z", "2026-10-19T11:59:59Z"])
def test_expired_green_cannot_merge_via_auto(tmp_path: Path, expires: str) -> None:
    _run_helper(tmp_path, "enable-automerge-label.sh", [_pull_reply()], expires=expires)


def test_direct_fallback_requires_fresh_green_and_the_evaluated_head(tmp_path: Path) -> None:
    _, calls, _ = _run_helper(
        tmp_path,
        "enable-automerge-label.sh",
        [
            _pull_reply(),
            _reply(["pr", "merge", "123"], "clean status", 1),
            _pull_reply(),
            _reply(["pr", "merge", "123"]),
        ],
    )
    assert "--auto" in calls[1]
    assert "--auto" not in calls[3]
    assert calls[1][-2:] == calls[3][-2:] == ["--match-head-commit", HEAD]


@pytest.mark.parametrize("reason", ["expired", "red", "head-changed", "label-removed"])
def test_direct_fallback_rechecks_readiness_and_intent(tmp_path: Path, reason: str) -> None:
    replies = [
        _pull_reply(),
        _reply(
            ["pr", "merge", "123"],
            "Base branch was modified",
            1,
            "2026-10-19T12:00:01Z" if reason == "expired" else None,
        ),
    ]
    if reason != "red":
        replies.append(
            _pull_reply(
                sha="b" * 40 if reason == "head-changed" else HEAD,
                labelled=reason != "label-removed",
            )
        )
    _, calls, _ = _run_helper(
        tmp_path,
        "enable-automerge-label.sh",
        replies,
        state="failure" if reason == "red" else "success",
    )
    assert sum(call[:2] == ["pr", "merge"] for call in calls) == 1
