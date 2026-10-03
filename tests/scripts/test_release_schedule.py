"""Exercise scheduled release planning and reruns without calling GitHub."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/release.yml"
_SOURCE_SHA = "a" * 40
_RELEASE_SHA = "b" * 40
_PICKED_SHA = "c" * 40


def _run_step(
    tmp_path: Path,
    step_id: str,
    responses: dict[str, str | None],
    *,
    job: str = "plan",
    **inputs: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"][job]["steps"]
        if step.get("id") == step_id or step["name"] == step_id
    )
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        """
import json, os, sys
from pathlib import Path

responses = json.loads(os.environ["API_RESPONSES"])
with open(os.environ["API_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
endpoint = next(arg for arg in sys.argv if arg.startswith("repos/")).split("/", 3)[3]
query = sys.argv[sys.argv.index("--jq") + 1] if "--jq" in sys.argv else "raw"
key = endpoint + " " + query
if key not in responses:
    Path(os.environ["UNEXPECTED_API"]).write_text(key)
    sys.exit(99)
body = responses[key]
if body is None:
    print('{"message":"Not Found"}')
    sys.exit(1)
print(body)
"""
    )
    gh.chmod(0o755)
    output = tmp_path / f"{step_id}.output"
    unexpected = tmp_path / "unexpected-api"
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "API_RESPONSES": json.dumps(responses),
            "API_CALLS": str(tmp_path / "api-calls"),
            "UNEXPECTED_API": str(unexpected),
            "GITHUB_REPOSITORY": "example/project",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        }
        | inputs,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert not unexpected.exists(), unexpected.read_text()
    outputs = (
        dict(line.split("=", 1) for line in output.read_text().splitlines())
        if output.exists()
        else {}
    )
    return result, outputs


@pytest.mark.parametrize(
    ("event", "requested", "marker", "expected"),
    [
        ("schedule", "", "0.6.0.dev0", "0.6.0"),
        ("schedule", "", "0.6.0", None),
        ("schedule", "", "0.6.0rc1.dev0", None),
        ("schedule", "", "0.6.0.dev1", None),
        ("schedule", "", None, None),
        ("workflow_dispatch", "0.6.0rc1", None, "0.6.0rc1"),
    ],
    ids=["pinned-sha", "stable-marker", "rc-marker", "dev1-marker", "api-error", "manual-rc"],
)
def test_release_version(
    tmp_path: Path, event: str, requested: str, marker: str | None, expected: str | None
) -> None:
    result, outputs = _run_step(
        tmp_path,
        "derive",
        {
            f"contents/pyproject.toml?ref={_SOURCE_SHA} raw": (
                f'[project]\nversion = "{marker}"' if marker else None
            ),
            "contents/pyproject.toml?ref=main raw": '[project]\nversion = "0.7.0.dev0"',
        }
        if event == "schedule"
        else {},
        EVENT_NAME=event,
        SOURCE_SHA=_SOURCE_SHA,
        VERSION=requested,
    )
    if expected is None:
        assert result.returncode != 0
        assert outputs == {}
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {
            "version": expected,
            "tag": f"v{expected}",
            "branch": "release/v0.6.0",
            "prerelease": str("rc" in expected).lower(),
        }


@pytest.mark.parametrize(
    ("head", "tag_head", "version", "done"),
    [
        (None, None, "0.6.0", "false"),
        (_RELEASE_SHA, _RELEASE_SHA, "0.6.0", "true"),
        (_PICKED_SHA, _RELEASE_SHA, "0.6.0", None),
        (_PICKED_SHA, None, "0.6.1", "false"),
        (_PICKED_SHA, None, "0.6.0rc2", "false"),
    ],
    ids=["initial-cut", "rerun-after-main-bump", "cherry-pick-same-tag", "patch", "next-rc"],
)
def test_release_branch_and_tag_state(
    tmp_path: Path, head: str | None, tag_head: str | None, version: str, done: str | None
) -> None:
    tag = f"v{version}"
    responses = {
        "git/ref/heads/release/v0.6.0 .object.sha": head,
        f"git/ref/tags/{tag} .object.sha": tag_head,
    }
    if head is None:
        responses[f"commits/{_SOURCE_SHA} .sha"] = _SOURCE_SHA
    if tag_head:
        responses[f"git/ref/tags/{tag} .object.type"] = "commit"
        responses[f"contents/pyproject.toml?ref={tag} raw"] = f'version = "{version}"'
    result, outputs = _run_step(
        tmp_path,
        "state",
        responses,
        VERSION=version,
        TAG=tag,
        BRANCH="release/v0.6.0",
        REF="main",
        SOURCE_REF=_SOURCE_SHA,
    )
    if done is None:
        assert result.returncode != 0
        assert "which is not the converged branch head" in result.stdout
        assert outputs == {}
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {
            "branch_exists": str(head is not None).lower(),
            "base_sha": head or _SOURCE_SHA,
            "already_done": done,
        }


@pytest.mark.parametrize(
    ("release", "main", "needed"),
    [
        ("0.6.0", "0.6.0.dev0", "true"),
        ("0.6.0", "0.7.0.dev0", "false"),
        ("0.5.1", "0.6.0.dev0", "false"),
    ],
)
def test_main_bump_only_when_release_is_ahead(
    tmp_path: Path, release: str, main: str, needed: str
) -> None:
    python = tmp_path / "python3"
    python.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        'if sys.argv[1:3] != ["-m", "pip"]:\n'
        "    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
    )
    python.chmod(0o755)
    result, outputs = _run_step(
        tmp_path,
        "main-bump",
        {"contents/pyproject.toml?ref=main raw": f'version = "{main}"'},
        VERSION=release,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"needed": needed}


@pytest.mark.parametrize("reviewers", [None, "0", "", "invalid", "1"])
def test_approval_requires_reviewers(tmp_path: Path, reviewers: str | None) -> None:
    query = '[.protection_rules[] | select(.type == "required_reviewers") | .reviewers[]] | length'
    result, _ = _run_step(
        tmp_path,
        "Require a protected approval environment",
        {f"environments/release-approval {query}": reviewers},
    )
    assert (result.returncode == 0) == (reviewers == "1"), result.stdout + result.stderr


@pytest.mark.parametrize("exists", [False, True])
def test_prepare_only_creates_a_missing_branch(tmp_path: Path, exists: bool) -> None:
    result, _ = _run_step(
        tmp_path,
        "Create the release branch without a tag",
        {} if exists else {"git/refs raw": "{}"},
        job="prepare",
        BRANCH="release/v0.6.0",
        BASE_SHA=_SOURCE_SHA,
        BRANCH_EXISTS=str(exists).lower(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = tmp_path / "api-calls"
    if exists:
        assert not calls.exists()
    else:
        assert [json.loads(line) for line in calls.read_text().splitlines()] == [
            [
                "api",
                "--method",
                "POST",
                "repos/example/project/git/refs",
                "-f",
                "ref=refs/heads/release/v0.6.0",
                "-f",
                f"sha={_SOURCE_SHA}",
            ]
        ]


def test_approval_resolves_backports_merged_during_the_wait(tmp_path: Path) -> None:
    result, outputs = _run_step(
        tmp_path,
        "head",
        {"git/ref/heads/release/v0.6.0 .object.sha": _PICKED_SHA},
        job="release-plan",
        BRANCH="release/v0.6.0",
        BASE_SHA=_SOURCE_SHA,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs == {"sha": _PICKED_SHA}


def test_stable_cut_requires_approval_after_preparation() -> None:
    jobs = yaml.safe_load(_WORKFLOW.read_text())["jobs"]
    assert {"prepare", "prepare-notes", "bump-main"} <= set(jobs["approve"]["needs"])
    assert jobs["bump-main"]["uses"] == "./.github/workflows/bump-version.yml"
    assert (
        "(needs.plan.outputs.bump_main != 'true' || needs.bump-main.result == 'success')"
        in jobs["approve"]["if"]
    )
    assert jobs["approve"]["environment"]["name"] == "release-approval"
    assert "approve" in jobs["release-plan"]["needs"]
    assert "needs.approve.result == 'success'" in jobs["release-plan"]["if"]
    assert "release-plan" in jobs["cut"]["needs"]
    assert (
        "(inputs.skip_benchmark || needs.benchmark-candidate.result == 'success')"
        in jobs["cut"]["if"]
    )
    for job in ["benchmark-seed", "benchmark-candidate", "benchmark", "cut"]:
        checkout = next(
            step
            for step in jobs[job]["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == "${{ needs.release-plan.outputs.base_sha }}"


@pytest.mark.parametrize("concurrent_backport", [False, True])
def test_cut_push_is_atomic(tmp_path: Path, concurrent_backport: bool) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    remote = tmp_path / "remote.git"
    env = os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=checkout, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init")
    git("config", "user.name", "Release test")
    git("config", "user.email", "release-test@example.com")
    (checkout / "version.txt").write_text("0.6.0.dev0\n")
    git("add", ".")
    git("commit", "-m", "Initial branch")
    source = git("rev-parse", "HEAD")
    git("push", str(remote), "HEAD:refs/heads/release/v0.6.0")
    if concurrent_backport:
        (checkout / "backport.txt").write_text("A fix merged while benchmarks ran.\n")
        git("add", ".")
        git("commit", "-m", "Backport fix")
        picked = git("rev-parse", "HEAD")
        git("push", str(remote), "HEAD:refs/heads/release/v0.6.0")
        git("checkout", "--detach", source)
    (checkout / "version.txt").write_text("0.6.0\n")
    git(
        "config",
        f"url.{remote}.insteadOf",
        "https://x-access-token:local-test@github.com/example/project.git",
    )
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["cut"]["steps"]
        if step["name"] == "Commit, tag, and push"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=10,
        env=env
        | {
            "PUSH_TOKEN": "local-test",
            "GITHUB_REPOSITORY": "example/project",
            "VERSION": "0.6.0",
            "TAG": "v0.6.0",
            "BRANCH": "release/v0.6.0",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
    )
    branch = git("--git-dir", str(remote), "rev-parse", "refs/heads/release/v0.6.0")
    tags = git("--git-dir", str(remote), "tag", "--list")
    if concurrent_backport:
        assert result.returncode != 0
        assert branch == picked
        assert tags == ""
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert branch == git("rev-parse", "HEAD")
        assert tags == "v0.6.0"
        assert git("--git-dir", str(remote), "rev-parse", "v0.6.0") == branch
