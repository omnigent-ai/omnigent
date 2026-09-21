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
STEPS = {step.get("id"): step for step in WORKFLOW["jobs"]["review"]["steps"]}
DIAGNOSTICS = STEPS["diagnostics"]["run"]
RECEIPT = STEPS["receipt"]["run"]
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
  actions: {
    listArtifacts: () => {},
    getWorkflow: async () => ({data: {id: 42}}),
    getWorkflowRun: async (args) => ({data: input.runs[args.run_id]}),
  },
  issues: {
    listComments: () => {},
    createComment: async (args) => { calls.push({create: args}); },
  },
}};
github.paginate = async (method) =>
  method === github.rest.actions.listArtifacts ? input.artifacts : input.comments;
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
        artifacts=None,
        runs=None,
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
                    "artifacts": artifacts or [],
                    "runs": runs or {},
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
                "repository": {"default_branch": "main"},
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

    def resolve(self, comments=(), force=False, artifacts=(), runs=None):
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
            artifacts=artifacts,
            runs=runs,
            extra_env={"FORCE_REVIEW": "true" if force else ""},
        )

    def completion(self):
        artifact = {
            "name": f"ocr-completed-7878-{HEAD}",
            "expired": False,
            "workflow_run": {"id": 123},
        }
        run = {
            "workflow_id": 42,
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "event": "issue_comment",
            "html_url": "https://github.com/omnigent-ai/omnigent/actions/runs/123",
        }
        return artifact, run

    def test_completed_commit_skips_before_gateway_and_posts_one_notice(self):
        artifact, run = self.completion()
        for event in ("issue_comment", "workflow_dispatch"):
            with self.subTest(event=event):
                run["event"] = event
                result = self.resolve(artifacts=[artifact], runs={123: run})
                self.assertNotIn("error", result)
                self.assertEqual(result["outputs"], {})
                self.assertEqual(len(result["calls"]), 1)
                notice = result["calls"][0]["create"]
                self.assertIn("/ocr force", notice["body"])
                self.assertIn(run["html_url"], notice["body"])
                self.assertEqual(notice["issue_number"], 7878)
                result = self.resolve(
                    [self.completed_comment(notice["body"])],
                    artifacts=[artifact],
                    runs={123: run},
                )
                self.assertEqual(result["outputs"], {})
                self.assertEqual(result["calls"], [])

    def test_force_bypasses_completion_receipt(self):
        artifact, _ = self.completion()
        # No run fixture: looking up its provenance would fail.
        result = self.resolve(artifacts=[artifact], force=True)
        self.assertNotIn("error", result)
        self.assertEqual(result["outputs"], {"base": "main", "head": HEAD})
        self.assertEqual(result["calls"], [])

    def test_comment_text_cannot_suppress_review_even_from_actions_bot(self):
        for login, user_type in (
            ("github-actions[bot]", "Bot"),
            ("another-app[bot]", "Bot"),
            ("contributor", "User"),
        ):
            result = self.resolve([self.completed_comment(login=login, user_type=user_type)])
            self.assertNotIn("error", result)
            self.assertEqual(result["outputs"], {"base": "main", "head": HEAD})
            self.assertEqual(result["calls"], [])

    def test_only_successful_default_branch_workflow_receipts_suppress_review(self):
        cases = [
            ("artifact", {"name": f"ocr-completed-999-{HEAD}"}),
            ("artifact", {"name": f"ocr-completed-7878-{'b' * 40}"}),
            ("artifact", {"expired": True}),
            ("artifact", {"workflow_run": None}),
            ("run", {"workflow_id": 99}),
            ("run", {"status": "in_progress"}),
            ("run", {"conclusion": "failure"}),
            ("run", {"conclusion": "cancelled"}),
            ("run", {"head_branch": "untrusted-pr"}),
            ("run", {"event": "pull_request"}),
        ]
        for target, overrides in cases:
            with self.subTest(target=target, overrides=overrides):
                artifact, run = self.completion()
                (artifact if target == "artifact" else run).update(overrides)
                result = self.resolve(artifacts=[artifact], runs={123: run})
                self.assertNotIn("error", result)
                self.assertEqual(result["outputs"], {"base": "main", "head": HEAD})
                self.assertEqual(result["calls"], [])

    def run_python(self, script, root, extra_env=None):
        script = script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        script = script.replace("pathlib.Path('/tmp')", f"pathlib.Path({str(root)!r})")
        return subprocess.run(
            [sys.executable, "-c", script],
            env={
                **os.environ,
                "RUNNER_TEMP": str(root),
                "GITHUB_OUTPUT": str(root / "outputs"),
                **(extra_env or {}),
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_receipt_records_requested_commit_and_published_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = "https://github.com/omnigent-ai/omnigent/pull/7878#issuecomment-123"
            result = self.run_python(
                RECEIPT,
                root,
                {
                    "PR_NUMBER": "7878",
                    "REVIEW_HEAD": HEAD,
                    "SUMMARY_URL": summary,
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads((root / "ocr-completion.json").read_text()),
                {
                    "pr": 7878,
                    "head": HEAD,
                    "summary_url": summary,
                },
            )

    def test_diagnostics_redact_raw_and_escaped_credentials_and_fail_on_key(self):
        key = 'test-key/with"quote\nand-unicode-\u00e9'
        gateway = "https://private.example/serving"
        variants = (
            key,
            json.dumps(key)[1:-1],
            json.dumps(key, ensure_ascii=False)[1:-1],
            key.replace("/", r"\/"),
            json.dumps(key)[1:-1].replace("/", r"\/"),
            json.dumps(key, ensure_ascii=False)[1:-1].replace("/", r"\/"),
        )
        for value in variants:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw = value + "\n" + gateway + "\nhttps://private.example/anthropic"
                raw += "\n" + gateway.replace("/", r"\/")
                for name in ("ocr-result.json", "ocr-stderr.log"):
                    (root / name).write_text(raw)
                result = self.run_python(
                    DIAGNOSTICS,
                    root,
                    {
                        "LLM_API_KEY": key,
                        "GATEWAY_BASE_URL": gateway,
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only redacted copies", result.stderr)
                destination = Path((root / "outputs").read_text().strip().removeprefix("path="))
                for name in ("ocr-result.json", "ocr-stderr.log"):
                    sanitized = (destination / name).read_text()
                    self.assertNotIn(value, sanitized)
                    self.assertNotIn("private.example", sanitized)
                    self.assertIn("[REDACTED]", sanitized)
                    self.assertEqual((root / name).read_text(), raw)
                self.assertNotIn(value, result.stdout + result.stderr)

    def test_clean_or_missing_diagnostics(self):
        for files in ([], ["ocr-result.json"], ["ocr-result.json", "ocr-stderr.log"]):
            with self.subTest(files=files), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in files:
                    (root / name).write_text("safe diagnostic")
                result = self.run_python(
                    DIAGNOSTICS,
                    root,
                    {
                        "LLM_API_KEY": "test-secret",
                        "GATEWAY_BASE_URL": "https://private.example",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if not files:
                    self.assertFalse((root / "outputs").exists())
                    continue
                destination = Path((root / "outputs").read_text().strip().removeprefix("path="))
                self.assertEqual(sorted(p.name for p in destination.iterdir()), sorted(files))
                for name in files:
                    self.assertEqual((destination / name).read_text(), "safe diagnostic")

    def test_only_sanitized_diagnostics_are_uploaded(self):
        self.assertEqual(STEPS["ocr"]["with"]["upload_artifacts"], "false")
        upload = next(
            s
            for s in WORKFLOW["jobs"]["review"]["steps"]
            if s["name"] == "Upload redacted diagnostics"
        )
        self.assertEqual(upload["with"]["path"], "${{ steps.diagnostics.outputs.path }}")
        self.assertIn("!cancelled()", upload["if"])
        # The receipt has the implicit success() gate, so a redaction failure cannot complete it.
        self.assertNotIn("always()", STEPS["receipt"]["if"])
        self.assertNotIn("!cancelled()", STEPS["receipt"]["if"])

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

    def test_only_complete_matching_result_is_eligible_for_completion_receipt(self):
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
        step = STEPS["receipt"]
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
                    f"const steps = {json.dumps(steps)}; core.setOutput('receipt', {step['if']});"
                )
                result = self.run_script(self.comment(), script=script)
                self.assertEqual(result["outputs"], {"receipt": eligible})


if __name__ == "__main__":
    unittest.main()
