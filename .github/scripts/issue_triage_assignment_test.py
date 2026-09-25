#!/usr/bin/env python3
"""Exercise legacy issue-triage assignment with offline GitHub stubs."""

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


class IssueTriageAssignmentTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
