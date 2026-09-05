import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from reconcile import Client, candidate_reason, main, reconcile

REPO = "example/project"
NAME = "omnigent-ui-preview-pr-123"
OLD = "2026-01-01T00:00:00Z"
CUTOFF = datetime(2026, 2, 1, tzinfo=timezone.utc)
RECENT = "2026-02-01T23:00:00Z"
NON_PREVIEWS = (
    "omnigent-repro",
    "omnigent-ui-preview-dev",
    "customer-app",
    NAME + "-backup",
    "other-" + NAME,
    "omnigent-ui-preview-pr-0123",
    "omnigent-ui-preview-pr-0",
    "omnigent-ui-preview-pr-123\n",
    "",
)


def app(number=123):
    name = f"omnigent-ui-preview-pr-{number}"
    return {
        "id": f"preview-id-{number}",
        "name": name,
        "description": f"https://github.com/{REPO}/pull/{number}",
        "creator": "ci-principal",
        "create_time": OLD,
        "update_time": OLD,
        "default_source_code_path": f"/Workspace/Users/ci-principal/apps/{name}",
        "compute_status": {"state": "ACTIVE"},
    }


def pr(number=123):
    return {
        "number": number,
        "state": "closed",
        "merged": False,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "closed_at": OLD,
        "labels": [],
        "user": {"login": "test-bot"},
        "head": {"ref": "test-branch"},
    }


class FakeClient:
    repo = REPO

    def __init__(self):
        self.app = app()
        self.inventory = [self.app]
        self.pull = pr()
        self.deletes = []
        self.gets = 0
        self.requested_names = []
        self.before_second_get = lambda: None
        self.busy = None

    def apps(self, *args):
        if args[0] == "list":
            return copy.deepcopy(self.inventory)
        if args[0] == "get":
            self.gets += 1
            self.requested_names.append(args[1])
            if self.gets == 2:
                self.before_second_get()
            return copy.deepcopy(next(item for item in self.inventory if item["name"] == args[1]))
        if args[0] == "delete":
            self.deletes.append(args[1])
            return None
        raise AssertionError(args)

    def pr(self, number):
        return copy.deepcopy(self.pull if number == 123 else pr(number))

    def busy_reason(self, _app, _pr):
        return self.busy


class ReconcileTest(unittest.TestCase):
    def run_reconcile(self, client, *, apply=False):
        with redirect_stdout(io.StringIO()):
            return reconcile(client, [NAME], cutoff=CUTOFF, apply=apply)[0]

    def test_dry_run_never_deletes(self):
        client = FakeClient()
        row = self.run_reconcile(client)
        self.assertEqual(row["action"], "candidate")
        self.assertEqual(client.deletes, [])

    def test_apply_rechecks_then_deletes(self):
        client = FakeClient()
        row = self.run_reconcile(client, apply=True)
        self.assertEqual(row["action"], "deleted")
        self.assertEqual(client.gets, 2)
        self.assertEqual(client.deletes, [NAME])

    def test_new_app_is_retained_if_merge_is_unconfirmed_on_recheck(self):
        client = FakeClient()
        client.app.update(create_time=RECENT)
        client.pull.update(merged=True)
        client.before_second_get = lambda: client.pull.update(merged=False)
        row = self.run_reconcile(client, apply=True)
        self.assertEqual(row["action"], "retain")
        self.assertEqual(client.deletes, [])

    def test_old_app_still_expires_when_pr_reopens(self):
        client = FakeClient()
        client.before_second_get = lambda: client.pull.update(state="open")
        self.assertEqual(self.run_reconcile(client, apply=True)["action"], "deleted")
        self.assertEqual(client.deletes, [NAME])

    def test_recreated_or_modified_app_is_retained(self):
        for change in (
            {"id": "replacement"},
            {"create_time": "2026-01-02T00:00:00Z"},
            {"update_time": "2026-01-02T00:00:00Z"},
        ):
            with self.subTest(change=change):
                client = FakeClient()
                client.before_second_get = lambda change=change, client=client: client.app.update(
                    change
                )
                row = self.run_reconcile(client, apply=True)
                self.assertEqual(row["reason"], "app changed during review")
                self.assertEqual(client.deletes, [])

    def test_pending_workflow_or_deployment_is_retained(self):
        client = FakeClient()
        client.busy = "preview workflow is still in progress"
        row = self.run_reconcile(client, apply=True)
        self.assertEqual(row["action"], "retain")
        self.assertEqual(client.deletes, [])

    def test_activity_starting_during_recheck_retains_app(self):
        client = FakeClient()
        client.before_second_get = lambda: setattr(client, "busy", "deployment started")
        self.assertEqual(self.run_reconcile(client, apply=True)["action"], "retain")
        self.assertEqual(client.deletes, [])

    def test_lookup_errors_never_delete_or_expose_error_body(self):
        client = FakeClient()
        with patch.object(client, "pr", side_effect=RuntimeError("secret token")):
            row = self.run_reconcile(client, apply=True)
        self.assertEqual(row["action"], "error")
        self.assertNotIn("secret", str(row))
        self.assertEqual(client.deletes, [])

    def test_non_preview_apps_are_excluded(self):
        for name in NON_PREVIEWS:
            with self.subTest(name=name):
                item = app() | {"name": name}
                self.assertIsNotNone(candidate_reason(item, pr(), REPO, CUTOFF))

    def test_invalid_provenance_or_external_resources_are_retained(self):
        changes = [
            {"description": "https://github.com/other/project/pull/123"},
            {"id": None},
            {"creator": None},
            {"default_source_code_path": "/Workspace/Users/ci-principal"},
            {"resources": [{"name": "persistent-database"}]},
            {"compute_status": {"state": "STARTING"}},
            {"compute_status": {}},
            {"pending_deployment": {"deployment_id": "pending"}},
            {"pending_update": {"update_id": "pending"}},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNotNone(candidate_reason(app() | change, pr(), REPO, CUTOFF))

    def test_invalid_pr_identity_or_state_is_retained(self):
        for change in (
            {"state": None},
            {"state": "unknown"},
            {"number": 124},
            {"html_url": "https://github.com/other/project/pull/123"},
        ):
            with self.subTest(change=change):
                self.assertIsNotNone(candidate_reason(app(), pr() | change, REPO, CUTOFF))

    def test_merged_pr_expires_even_for_a_fresh_app(self):
        self.assertIsNone(
            candidate_reason(
                app() | {"create_time": RECENT}, pr() | {"merged": True}, REPO, CUTOFF
            )
        )

    def test_old_apps_expire_for_open_and_closed_unmerged_prs(self):
        for state in ("open", "closed"):
            with self.subTest(state=state):
                self.assertIsNone(candidate_reason(app(), pr() | {"state": state}, REPO, CUTOFF))

    def test_unmerged_apps_are_retained_until_strictly_older_than_ttl(self):
        for state in ("open", "closed"):
            for created_at in (RECENT, "2026-02-01T00:00:00Z"):
                with self.subTest(state=state, created_at=created_at):
                    self.assertIsNotNone(
                        candidate_reason(
                            app() | {"create_time": created_at},
                            pr() | {"state": state},
                            REPO,
                            CUTOFF,
                        )
                    )
        self.assertIsNone(
            candidate_reason(
                app() | {"create_time": "2026-01-31T23:59:59.999999Z"}, pr(), REPO, CUTOFF
            )
        )

    def test_age_uses_app_creation_not_pr_age_or_latest_deployment(self):
        old_pr = pr() | {"created_at": OLD, "closed_at": OLD}
        self.assertIsNotNone(
            candidate_reason(app() | {"create_time": RECENT}, old_pr, REPO, CUTOFF)
        )
        recent_pr = pr() | {"created_at": RECENT, "closed_at": RECENT}
        redeployed_app = app() | {
            "update_time": RECENT,
            "active_deployment": {"create_time": RECENT},
        }
        self.assertIsNone(candidate_reason(redeployed_app, recent_pr, REPO, CUTOFF))

    def test_invalid_or_missing_creation_timestamp_is_retained(self):
        for merged in (True, False):
            for created_at in (None, "invalid", "2026-01-01T00:00:00", 0):
                with self.subTest(merged=merged, created_at=created_at):
                    self.assertIsNotNone(
                        candidate_reason(
                            app() | {"create_time": created_at},
                            pr() | {"merged": merged},
                            REPO,
                            CUTOFF,
                        )
                    )
        item = app()
        del item["create_time"]
        self.assertIsNotNone(candidate_reason(item, pr(), REPO, CUTOFF))

    def test_cli_apply_discovers_all_ui_previews_and_excludes_other_apps(self):
        client = FakeClient()
        client.inventory.extend([app(124), *(app() | {"name": name} for name in NON_PREVIEWS)])
        with (
            patch("sys.argv", ["reconcile.py", "--repo", REPO, "--apply"]),
            patch("reconcile.Client", return_value=client),
            patch("reconcile.datetime", wraps=datetime) as clock,
            redirect_stdout(io.StringIO()),
        ):
            clock.now.return_value = CUTOFF + timedelta(hours=24)
            self.assertEqual(main(), 0)
        self.assertEqual(client.deletes, [NAME, "omnigent-ui-preview-pr-124"])
        self.assertEqual(set(client.requested_names), {NAME, "omnigent-ui-preview-pr-124"})
        self.assertEqual(client.gets, 4)

    def test_cli_matching_name_without_preview_provenance_is_retained(self):
        client = FakeClient()
        client.app.update(description="A different app with a preview-like name")
        with (
            patch("sys.argv", ["reconcile.py", "--repo", REPO, "--apply"]),
            patch("reconcile.Client", return_value=client),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(client.deletes, [])

    def test_cli_explicit_scope_and_report(self):
        client = FakeClient()
        client.inventory.append(app(124))
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            with (
                patch(
                    "sys.argv",
                    ["reconcile.py", "--app", NAME, "--apply", "--report", str(report_path)],
                ),
                patch("reconcile.Client", return_value=client),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)
            report = json.loads(report_path.read_text())
        self.assertEqual(client.deletes, [NAME])
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["action"], "deleted")
        self.assertEqual(report[0]["app_created_at"], OLD)

    def test_cli_returns_failure_for_lookup_errors(self):
        client = FakeClient()
        with (
            patch("sys.argv", ["reconcile.py", "--apply"]),
            patch("reconcile.Client", return_value=client),
            patch.object(client, "pr", side_effect=RuntimeError("lookup failed")),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 1)
        self.assertEqual(client.deletes, [])

    def test_cli_defaults_to_dry_run(self):
        client = FakeClient()
        with (
            patch("sys.argv", ["reconcile.py", "--repo", REPO]),
            patch("reconcile.Client", return_value=client),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(client.deletes, [])

    def test_cli_rejects_invalid_ttl_and_non_preview_targets(self):
        for args in (["--ttl-hours", "0"], ["--app", "omnigent-repro"]):
            with self.subTest(args=args), patch("sys.argv", ["reconcile.py", *args]):
                with (
                    patch("sys.stderr", new=io.StringIO()),
                    self.assertRaises(SystemExit) as error,
                ):
                    main()
                self.assertEqual(error.exception.code, 2)


class ClientTest(unittest.TestCase):
    def test_all_deployments_must_have_known_terminal_states(self):
        for state in ("IN_PROGRESS", "CANCELING", "UNKNOWN", None):
            with self.subTest(state=state):
                client = Client(REPO)
                with patch.object(client, "apps", return_value=[{"status": {"state": state}}]):
                    self.assertIsNotNone(client.busy_reason(app(), pr()))

    def test_active_workflow_on_later_page_blocks_cleanup(self):
        client = Client(REPO)
        pages = [
            {"workflow_runs": [{"status": "completed"}]},
            {"workflow_runs": [{"status": "queued"}]},
        ]
        with (
            patch.object(client, "apps", return_value=[]),
            patch.object(client, "run", return_value=pages) as run,
        ):
            self.assertEqual(
                client.busy_reason(app(), pr()), "preview workflow is still in progress"
            )
        self.assertIn("--paginate", run.call_args.args[0])
        self.assertIn("--slurp", run.call_args.args[0])

    def test_missing_workflow_results_block_cleanup(self):
        client = Client(REPO)
        for pages in (None, [], [{}], [{"workflow_runs": None}]):
            with (
                self.subTest(pages=pages),
                patch.object(client, "apps", return_value=[]),
                patch.object(client, "run", return_value=pages),
            ):
                self.assertIsNotNone(client.busy_reason(app(), pr()))

    def test_terminal_deployments_and_completed_workflows_allow_review(self):
        client = Client(REPO)
        deployments = [
            {"status": {"state": state}} for state in ("SUCCEEDED", "FAILED", "CANCELLED")
        ]
        with (
            patch.object(client, "apps", return_value=deployments),
            patch.object(client, "run", return_value=[{"workflow_runs": []}]),
        ):
            self.assertIsNone(client.busy_reason(app(), pr()))

    def test_delete_always_includes_explicit_app_name_and_profile(self):
        client = Client(REPO, "test-workspace")
        with patch.object(client, "run") as run:
            client.apps("delete", NAME, "--auto-approve")
        self.assertEqual(
            run.call_args.args[0],
            [
                "databricks",
                "apps",
                "delete",
                NAME,
                "--auto-approve",
                "--profile",
                "test-workspace",
                "--output",
                "json",
            ],
        )


if __name__ == "__main__":
    unittest.main()
