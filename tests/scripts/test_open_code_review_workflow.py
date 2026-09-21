"""Exercise OCR authorization and result handling without GitHub or an LLM."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/open-code-review.yml").read_text())
AUTHORIZE = WORKFLOW["jobs"]["request"]["steps"][0]["with"]["script"]
RESOLVE = WORKFLOW["jobs"]["review"]["steps"][0]["with"]["script"]
REPORT = next(s for s in WORKFLOW["jobs"]["review"]["steps"] if s.get("id") == "report")["run"]
MARK = WORKFLOW["jobs"]["review"]["steps"][-1]["with"]["script"]
HEAD = "a" * 40

NODE_RUNNER = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const outputs = {};
const calls = [];
const core = {setOutput: (key, value) => outputs[key] = value, notice: () => {}};
const github = {rest: {
  repos: {getCollaboratorPermissionLevel: async (args) => {
    calls.push(args);
    return {data: {permission: input.permission}};
  }},
  pulls: {get: async () => ({data: input.pr})},
  issues: {
    listComments: () => {},
    createComment: async (args) => { calls.push({create: args}); },
    getComment: async () => ({data: input.summary_comment}),
    updateComment: async (args) => { calls.push({update: args}); },
  },
}};
github.paginate = async () => input.comments;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
(async () => {
  let error;
  try {
    const run = new AsyncFunction('context', 'github', 'core', input.script);
    await run(input.context, github, core);
  } catch (err) { error = err.message; }
  process.stdout.write(JSON.stringify({outputs, calls, error}));
})();
"""


class OpenCodeReviewWorkflowTest(unittest.TestCase):
    def run_script(
        self,
        context,
        *,
        permission="read",
        allowlist=None,
        pr=None,
        script=AUTHORIZE,
        comments=None,
        summary_comment=None,
        extra_env=None,
    ):
        result = subprocess.run(
            ["node", "-e", NODE_RUNNER],
            input=json.dumps(
                {
                    "script": script,
                    "context": context,
                    "permission": permission,
                    "pr": pr,
                    "comments": comments or [],
                    "summary_comment": summary_comment,
                }
            ),
            env={
                **os.environ,
                "INPUT_PR": "7878",
                "PR_NUMBER": "7878",
                "REVIEW_ALLOWLIST": json.dumps(allowlist or []),
                "INPUT_FORCE": "false",
                "FORCE_REVIEW": "",
                "REVIEW_HEAD": HEAD,
                "SUMMARY_URL": "https://github.com/omnigent-ai/omnigent/pull/7878#issuecomment-123",
                **(extra_env or {}),
            },
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        return json.loads(result.stdout)

    def comment(self, body="/ocr", *, user_type="User", is_pr=True):
        return {
            "repo": {"owner": "omnigent-ai", "repo": "omnigent"},
            "eventName": "issue_comment",
            "payload": {
                "issue": {"number": 7878, "pull_request": {"url": "pr"} if is_pr else None},
                "comment": {"body": body, "user": {"login": "reviewer", "type": user_type}},
            },
        }

    def test_only_authorized_standalone_commands_reach_review_queue(self):
        for body in ("/ocr", " \t/ocr  ", "Please review.\n/ocr\nThanks.", "/ocr\r\n"):
            with self.subTest(body=body):
                result = self.run_script(self.comment(body), permission="write")
                self.assertEqual(result["outputs"], {"pr": "7878"})
        for body in (
            "Try /ocr",
            "`/ocr`",
            "> /ocr",
            "/ocr-other",
            "/ocr forcefully",
            "$(touch x)",
        ):
            with self.subTest(body=body):
                result = self.run_script(self.comment(body), permission="admin")
                self.assertEqual(result["outputs"], {})
                self.assertEqual(result["calls"], [])

    def test_read_and_triage_permissions_cannot_spend_quota(self):
        for permission in ("none", "read", "triage"):
            with self.subTest(permission=permission):
                result = self.run_script(self.comment(), permission=permission)
                self.assertEqual(result["outputs"], {})

    def test_force_is_an_explicit_authorized_command(self):
        for body in ("/ocr force", "  /ocr\tforce  \r\n", "/ocr\n/ocr force"):
            with self.subTest(body=body):
                result = self.run_script(self.comment(body), permission="write")
                self.assertEqual(result["outputs"], {"pr": "7878", "force": "true"})
                denied = self.run_script(self.comment(body), permission="read")
                self.assertEqual(denied["outputs"], {})
        for body in ("Try /ocr force", "> /ocr force", "/ocr force extra"):
            self.assertEqual(
                self.run_script(self.comment(body), permission="admin")["outputs"], {}
            )
        result = self.run_script(self.comment("/ocr\nforce"), permission="write")
        self.assertEqual(result["outputs"], {"pr": "7878"})

    def test_maintainers_and_allowlisted_users_can_review_fork_prs(self):
        for permission in ("admin", "maintain", "write"):
            with self.subTest(permission=permission):
                result = self.run_script(self.comment(), permission=permission)
                self.assertEqual(result["outputs"], {"pr": "7878"})
        result = self.run_script(self.comment(), allowlist=["reviewer"])
        self.assertEqual(result["outputs"], {"pr": "7878"})
        self.assertEqual(result["calls"], [])

    def test_bot_comments_and_issue_comments_cannot_trigger_review(self):
        for context in (self.comment(user_type="Bot"), self.comment(is_pr=False)):
            result = self.run_script(context, permission="admin", allowlist=["reviewer"])
            self.assertEqual(result["outputs"], {})
            self.assertEqual(result["calls"], [])

    def test_dispatch_requires_trusted_default_branch(self):
        context = self.comment()
        context.update(eventName="workflow_dispatch", ref="refs/heads/main")
        context["payload"] = {"repository": {"default_branch": "main"}}
        self.assertEqual(self.run_script(context)["outputs"], {"pr": "7878"})
        forced = self.run_script(context, extra_env={"INPUT_FORCE": "true"})
        self.assertEqual(forced["outputs"], {"pr": "7878", "force": "true"})
        context["ref"] = "refs/heads/untrusted-pr"
        result = self.run_script(context)
        self.assertEqual(result["outputs"], {})
        self.assertIn("default branch", result["error"])

    def test_queued_review_resolves_fresh_head_and_skips_closed_or_draft_prs(self):
        for state, draft in (("open", False), ("closed", False), ("open", True)):
            with self.subTest(state=state, draft=draft):
                pr = {
                    "state": state,
                    "draft": draft,
                    "base": {"ref": "main"},
                    "head": {"sha": "new-head"},
                }
                result = self.run_script(self.comment(), pr=pr, script=RESOLVE)
                expected = (
                    {"base": "main", "head": "new-head"} if state == "open" and not draft else {}
                )
                self.assertEqual(result["outputs"], expected)

    def completed_comment(self, marker=None, *, login="github-actions[bot]", user_type="Bot"):
        return {
            "body": marker or f"<!-- ocr-reviewed-sha: {HEAD} -->",
            "user": {"login": login, "type": user_type},
            "html_url": "https://github.com/omnigent-ai/omnigent/pull/7878#issuecomment-123",
        }

    def resolve(self, comments, force=False):
        pr = {
            "number": 7878,
            "state": "open",
            "draft": False,
            "base": {"ref": "main"},
            "head": {"sha": HEAD},
        }
        return self.run_script(
            self.comment(),
            script=RESOLVE,
            pr=pr,
            comments=comments,
            extra_env={"FORCE_REVIEW": "true" if force else ""},
        )

    def test_completed_commit_skips_before_gateway_and_posts_one_notice(self):
        completed = self.completed_comment()
        result = self.resolve([completed])
        self.assertEqual(result["outputs"], {})
        self.assertEqual(len(result["calls"]), 1)
        notice = result["calls"][0]["create"]
        self.assertIn("/ocr force", notice["body"])
        self.assertIn(completed["html_url"], notice["body"])
        self.assertEqual(notice["issue_number"], 7878)
        result = self.resolve([completed, self.completed_comment(notice["body"])])
        self.assertEqual(result["outputs"], {})
        self.assertEqual(result["calls"], [])

    def test_force_bypasses_completed_commit_marker(self):
        result = self.resolve([self.completed_comment()], force=True)
        self.assertEqual(result["outputs"], {"base": "main", "head": HEAD})
        self.assertEqual(result["calls"], [])

    def test_only_exact_completion_markers_from_our_bot_suppress_review(self):
        candidates = [
            [],
            [self.completed_comment(login="contributor", user_type="User")],
            [self.completed_comment(login="another-app[bot]")],
            [self.completed_comment(f"<!-- ocr-reviewed-sha: {'b' * 40} -->")],
            [self.completed_comment(f"<!-- ocr-skipped-sha: {HEAD} -->")],
            [self.completed_comment(f"> <!-- ocr-reviewed-sha: {HEAD} -->")],
        ]
        for comments in candidates:
            with self.subTest(comments=comments):
                result = self.resolve(comments)
                self.assertEqual(result["outputs"], {"base": "main", "head": HEAD})
                self.assertEqual(result["calls"], [])

    def test_completion_marker_is_appended_without_replacing_findings(self):
        summary = self.completed_comment("OCR findings")
        result = self.run_script(self.comment(), script=MARK, summary_comment=summary)
        self.assertNotIn("error", result)
        update = result["calls"][0]["update"]
        self.assertEqual(update["comment_id"], 123)
        self.assertEqual(update["body"], f"OCR findings\n\n<!-- ocr-reviewed-sha: {HEAD} -->")
        result = self.run_script(
            self.comment(), script=MARK, summary_comment=self.completed_comment()
        )
        self.assertEqual(result["calls"], [])

    def test_human_summary_cannot_be_marked_complete(self):
        summary = self.completed_comment("findings", login="human", user_type="User")
        result = self.run_script(self.comment(), script=MARK, summary_comment=summary)
        self.assertIn("Refusing to mark", result["error"])
        self.assertEqual(result["calls"], [])

    def run_report(self, status, log="", resolved_head=HEAD):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ocr-result.json").write_text(
                json.dumps(
                    {
                        "status": status,
                        "summary": {},
                        "manifest": {"input": {"resolved_head": resolved_head}},
                    }
                )
            )
            (root / "ocr-stderr.log").write_text(log)
            script = REPORT.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            script = script.replace("/tmp/ocr-", str(root / "ocr-"))
            result = subprocess.run(
                [sys.executable, "-c", script],
                env={
                    **os.environ,
                    "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
                    "GITHUB_OUTPUT": str(root / "outputs"),
                    "REVIEW_HEAD": HEAD,
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            outputs = root / "outputs"
            return (
                result,
                (root / "summary.md").read_text(),
                outputs.read_text() if outputs.exists() else "",
            )

    def test_partial_or_failed_review_is_not_a_green_check(self):
        for status in ("partial", "failed", "unknown"):
            with self.subTest(status=status):
                result, summary, outputs = self.run_report(status)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("incomplete", result.stderr)
                self.assertIn(status, summary)
                self.assertEqual(outputs, "")
        for status in ("complete", "skipped"):
            with self.subTest(status=status):
                result, _, _ = self.run_report(status)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_filter_failure_remains_visible_for_complete_review(self):
        for log in (
            "[ocr] Review filter: failed to parse LLM response",
            "[ocr] Review filter failed for group 'hooks': request timed out",
        ):
            with self.subTest(log=log):
                result, summary, outputs = self.run_report("complete", log)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("::warning::", result.stdout)
                self.assertIn("Finding-filter failure", summary)
                self.assertEqual(outputs, "")

    def test_only_complete_matching_result_is_eligible_for_completion_marker(self):
        result, _, outputs = self.run_report("complete")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs, f"head={HEAD}\n")
        result, _, outputs = self.run_report("skipped")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(outputs, "")
        for head in (None, "different-head"):
            result, _, outputs = self.run_report("complete", resolved_head=head)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match", result.stderr)
            self.assertEqual(outputs, "")

    def test_failed_publication_cannot_record_completion(self):
        step = WORKFLOW["jobs"]["review"]["steps"][-1]
        for outcome, failed, head, url, eligible in (
            ("success", "0", HEAD, "summary-url", True),
            ("failure", "0", HEAD, "summary-url", False),
            ("success", "1", HEAD, "summary-url", False),
            ("success", "", HEAD, "summary-url", False),
            ("success", "0", "", "summary-url", False),
            ("success", "0", HEAD, "", False),
        ):
            with self.subTest(outcome=outcome, failed=failed, head=head, url=url):
                steps = {
                    "ocr": {
                        "outcome": outcome,
                        "outputs": {"comments_failed": failed, "summary_comment_url": url},
                    },
                    "report": {"outputs": {"head": head}},
                }
                script = (
                    f"const steps = {json.dumps(steps)}; core.setOutput('mark', {step['if']});"
                )
                result = self.run_script(self.comment(), script=script)
                self.assertEqual(result["outputs"], {"mark": eligible})


if __name__ == "__main__":
    unittest.main()
