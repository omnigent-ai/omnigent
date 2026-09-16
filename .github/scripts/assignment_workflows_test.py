#!/usr/bin/env python3
"""Exercise assignment code embedded in workflows with offline GitHub stubs."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow_steps(name: str) -> list[dict]:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


class AssignmentWorkflowsTest(unittest.TestCase):
    def test_site_workflows_filter_paused_reviewers(self) -> None:
        for name, variable in [("doc-sync.yml", "REVIEWER"), ("feature-blog.yml", "reviewer")]:
            run = next(
                step["run"]
                for step in workflow_steps(name)
                if "if ! jq -e --arg login" in step.get("run", "")
            )
            guard = run[run.index("if ! jq -e --arg login") :]
            guard = guard[: guard.index("fi") + 2]
            for login, expected in [("Paused", ""), ("active", "active")]:
                with (
                    self.subTest(workflow=name, login=login),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    (root / ".github").mkdir()
                    (root / ".github/areas.json").write_text('{"assignment_paused":["PAUSED"]}')
                    result = subprocess.run(
                        [
                            "bash",
                            "-euo",
                            "pipefail",
                            "-c",
                            guard + '\nprintf "%s" "$' + variable + '"',
                        ],
                        cwd=root,
                        env={**os.environ, variable: login, "GITHUB_WORKSPACE": directory},
                        text=True,
                        capture_output=True,
                        check=True,
                    )
                    self.assertEqual(result.stdout, expected)

    def test_autoformat_skips_paused_author_assignment(self) -> None:
        run = next(step["run"] for step in workflow_steps("autoformat-pr.yml") if "run" in step)
        assignment = run[
            run.index("assignment_note=") : run.index(".github/scripts/pr-template/format_body.py")
        ]
        for author, expected in [("Paused", False), ("active", True)]:
            with self.subTest(author=author), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".github").mkdir()
                (root / ".github/areas.json").write_text('{"assignment_paused":["PAUSED"]}')
                script = 'gh() { printf "%s\\n" "$*" > assigned.txt; }\n' + assignment
                subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", script],
                    cwd=root,
                    env={**os.environ, "author": author, "REPO": "test/repo", "PR_NUMBER": "1"},
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertEqual((root / "assigned.txt").exists(), expected)

    def test_legacy_triage_assignment(self) -> None:
        steps = workflow_steps("issue-triage.yml")
        generate = next(step["run"] for step in steps if step.get("id") == "assignees")
        apply = next(
            step["run"] for step in steps if "# Refresh state after" in step.get("run", "")
        )
        apply = apply[
            apply.index("# Refresh state after") : apply.index("# Finally, close the issue")
        ]
        for author, existing, paused, apply_labels, needs_info, expected in [
            ("community", [], ["PAUSED"], True, False, ["active"]),
            ("paused", [], ["PAUSED"], True, False, ["active"]),
            ("active", [], ["PAUSED"], True, False, ["active"]),
            ("paused", ["paused"], ["PAUSED"], True, False, []),
            ("active", ["human"], [], True, False, []),
            ("community", [], ["paused", "active"], True, False, []),
            ("community", [], [], True, False, ["paused"]),
            ("community", [], ["paused"], False, False, []),
            ("community", [], ["paused"], True, True, []),
        ]:
            with (
                self.subTest(
                    author=author,
                    existing=existing,
                    paused=paused,
                    apply=apply_labels,
                    needs_info=needs_info,
                ),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / ".github").mkdir()
                config = {
                    "assignment_paused": paused,
                    "areas": [
                        {
                            "key": "runner",
                            "label": "comp:runner",
                            "definition": "Runner",
                            "owners": ["paused", "active"],
                        }
                    ],
                }
                (root / ".github/areas.json").write_text(json.dumps(config))
                (root / ".github/MAINTAINER").write_text("paused\nactive\n")
                (root / "issue.json").write_text(json.dumps({"author": {"login": author}}))
                (root / "status.json").write_text(
                    json.dumps({"state": "OPEN", "assignees": existing})
                )
                (root / "triage_result.json").write_text(
                    json.dumps(
                        {
                            "apply_labels": apply_labels,
                            "ranked_owners": [],
                            "needs_info": needs_info,
                        }
                    )
                )
                (root / "load.json").write_text(json.dumps([{"assignees": [{"login": "active"}]}]))
                gh = root / "gh"
                gh.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
if args[:2] == ['issue', 'view']:
    print(pathlib.Path('status.json').read_text())
elif args[:2] == ['issue', 'list']:
    print(pathlib.Path('load.json').read_text())
elif args[:2] == ['issue', 'edit']:
    with open('assigned.txt', 'a') as output:
        output.write(args[args.index('--add-assignee') + 1] + '\\n')
else:
    raise AssertionError(args)
""")
                gh.chmod(0o755)
                env = {
                    **os.environ,
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "REPO": "test/repo",
                    "ISSUE_NUMBER": "1",
                }
                for script in (generate, apply):
                    subprocess.run(
                        [
                            "bash",
                            "-euo",
                            "pipefail",
                            "-c",
                            script.replace("/tmp/", directory + "/"),
                        ],
                        cwd=root,
                        env=env,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                owners = json.loads((root / "owners.json").read_text())
                self.assertFalse({u.lower() for u in paused} & {u.lower() for u in owners})
                if "PAUSED" in paused:
                    self.assertNotIn("paused", (root / "areas_prompt.txt").read_text())
                assignments = root / "assigned.txt"
                self.assertEqual(
                    assignments.read_text().splitlines() if assignments.exists() else [],
                    expected,
                )

    def test_nightly_failure_monitor(self) -> None:
        script = next(
            step["with"]["script"]
            for step in workflow_steps("nightly-failure-monitor.yml")
            if "script" in step.get("with", {})
        )
        harness = r"""
const fs = require('fs');
const {script, paused} = JSON.parse(fs.readFileSync(0, 'utf8'));
const assigned = [];
let created = 0;
const github = {rest: {
  issues: {
    listForRepo: async () => ({data: []}),
    getLabel: async () => ({}),
    create: async () => { created++; return {data: {number: 1}}; },
    addAssignees: async ({assignees}) => assigned.push(...assignees),
  },
  actions: {listWorkflowRuns: async () => ({data: {
    workflow_runs: [{id: 2, conclusion: 'failure'}],
  }})},
}};
const context = {repo: {owner: 'test', repo: 'repo'}, workflow: 'Nightly', payload: {
  repository: {default_branch: 'main'}, workflow_run: {
    id: 1, event: 'schedule', head_branch: 'main', conclusion: 'failure',
    name: 'Nightly', head_sha: '123456789', run_number: 3,
  },
}};
const core = {info() {}, warning() {}};
const fakeRequire = (name) => {
  if (name !== 'fs') throw new Error(name);
  return {readFileSync: () => JSON.stringify({assignment_paused: paused})};
};
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
const execute = new AsyncFunction('github', 'context', 'core', 'require', script);
execute(github, context, core, fakeRequire)
  .then(() => process.stdout.write(JSON.stringify({created, assigned})))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""
        for paused, expected in [([], ["PattaraS"]), (["PATTARAS"], [])]:
            with self.subTest(paused=paused):
                result = subprocess.run(
                    ["node", "-e", harness],
                    input=json.dumps({"script": script, "paused": paused}),
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertEqual(json.loads(result.stdout), {"created": 1, "assigned": expected})


if __name__ == "__main__":
    unittest.main()
