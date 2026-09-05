"""Delete PR previews when their PR is merged or their app is older than its TTL."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

APP_PATTERN = re.compile(r"omnigent-ui-preview-pr-([1-9][0-9]*)\Z")
TERMINAL_DEPLOYMENTS = {"SUCCEEDED", "FAILED", "CANCELLED"}


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def candidate_reason(app: dict, pr: dict, repo: str, cutoff: datetime) -> str | None:
    """Return a retention reason, or None for a lifecycle-expired preview."""
    match = APP_PATTERN.fullmatch(app.get("name", ""))
    if not match:
        return "not a per-PR preview"
    number = int(match[1])
    expected_url = f"https://github.com/{repo}/pull/{number}"
    if app.get("description") != expected_url or pr.get("html_url") != expected_url:
        return "PR URL does not match the app description"
    if pr.get("number") != number or pr.get("state") not in {"open", "closed"}:
        return "PR identity or state is unconfirmed"
    try:
        created_at = timestamp(app["create_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return "missing or invalid app creation timestamp"
    if pr.get("merged") is not True and created_at >= cutoff:
        return "PR is unmerged and app has not exceeded its TTL"
    if not app.get("id") or not app.get("creator"):
        return "missing app identity"
    expected_source = f"/Workspace/Users/{app['creator']}/apps/{app['name']}"
    if app.get("default_source_code_path") != expected_source:
        return "source path does not match the preview convention"
    if app.get("resources"):
        return "app has attached resources; manual investigation required"
    if app.get("compute_status", {}).get("state") not in {"ACTIVE", "STOPPED", "ERROR"}:
        return "compute is transitioning or its state is unknown"
    if app.get("pending_deployment") or app.get("pending_update"):
        return "app has a pending deployment or update"
    return None


class Client:
    def __init__(self, repo: str, profile: str | None = None):
        self.repo = repo
        self.profile_args = ["--profile", profile] if profile else []

    @staticmethod
    def run(args: list[str]):
        result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            # CLI diagnostics can contain credentials or server response bodies.
            raise RuntimeError(f"{args[0]} command failed (exit {result.returncode})")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def apps(self, *args: str):
        return self.run(["databricks", "apps", *args, *self.profile_args, "--output", "json"])

    def pr(self, number: int) -> dict:
        return self.run(["gh", "api", f"repos/{self.repo}/pulls/{number}"])

    def busy_reason(self, app: dict, pr: dict) -> str | None:
        deployments = self.apps("list-deployments", app["name"])
        if not isinstance(deployments, list):
            return "deployment history is unavailable"
        if any(d.get("status", {}).get("state") not in TERMINAL_DEPLOYMENTS for d in deployments):
            return "deployment is in progress or its state is unknown"
        branch = pr.get("head", {}).get("ref")
        if not branch:
            return "PR head branch is unavailable"
        query = urlencode({"branch": branch, "per_page": 100})
        pages = self.run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repo}/actions/workflows/ui-preview.yml/runs?{query}",
            ]
        )
        if not isinstance(pages, list) or not pages:
            return "preview workflow history is unavailable"
        for page in pages:
            if not isinstance(page.get("workflow_runs"), list):
                return "preview workflow history is unavailable"
            if any(run.get("status") != "completed" for run in page["workflow_runs"]):
                return "preview workflow is still in progress"
        return None


def inspect(client: Client, name: str, cutoff: datetime) -> tuple[dict, dict, str | None]:
    match = APP_PATTERN.fullmatch(name)
    if not match:
        raise ValueError("not a per-PR preview name")
    app = client.apps("get", name)
    if app.get("name") != name:
        raise ValueError("app response name does not match request")
    pr = client.pr(int(match[1]))
    reason = candidate_reason(app, pr, client.repo, cutoff)
    if reason is None:
        reason = client.busy_reason(app, pr)
    return app, pr, reason


def reconcile(client: Client, names: list[str], *, cutoff: datetime, apply: bool) -> list[dict]:
    report = []
    for name in names:
        row = {"app": name, "action": "retain"}
        try:
            app, pr, reason = inspect(client, name, cutoff)
            row.update(
                pr=pr["html_url"],
                author=pr.get("user", {}).get("login"),
                closed_at=pr.get("closed_at"),
                merged=pr.get("merged") is True,
                app_created_at=app.get("create_time"),
                compute_state=app.get("compute_status", {}).get("state"),
            )
            if reason is not None:
                row["reason"] = reason
            elif not apply:
                row.update(action="candidate", reason="PR merged or app exceeded TTL")
            else:
                fresh_app, _, reason = inspect(client, name, cutoff)
                identity = ("id", "create_time", "update_time", "default_source_code_path")
                if reason is not None:
                    row["reason"] = f"recheck: {reason}"
                elif any(fresh_app.get(key) != app.get(key) for key in identity):
                    row["reason"] = "app changed during review"
                else:
                    client.apps("delete", name, "--auto-approve")
                    row.update(
                        action="deleted", reason="PR merged or app exceeded TTL; source preserved"
                    )
        except (
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OSError,
            subprocess.TimeoutExpired,
        ) as exc:
            row.update(action="error", reason=f"lookup/delete failed ({type(exc).__name__})")
        report.append(row)
        print(json.dumps(row), flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="omnigent-ai/omnigent")
    parser.add_argument("--profile")
    parser.add_argument("--ttl-hours", type=int, default=24)
    parser.add_argument(
        "--app", action="append", default=[], help="Limit cleanup to this exact app"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Delete all matching expired previews"
    )
    parser.add_argument("--report", type=Path, help="Write the JSON report to this file")
    args = parser.parse_args()
    if args.ttl_hours < 1:
        parser.error("--ttl-hours must be at least 1")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be owner/repository")
    if any(not APP_PATTERN.fullmatch(name) for name in args.app):
        parser.error("--app must be an exact omnigent-ui-preview-pr-<number> name")
    client = Client(args.repo, args.profile)
    names = list(dict.fromkeys(args.app))
    if not names:
        apps = client.apps("list")
        if not isinstance(apps, list):
            raise ValueError("unexpected app inventory response")
        names = [app["name"] for app in apps if APP_PATTERN.fullmatch(app.get("name", ""))]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.ttl_hours)
    report = reconcile(client, names, cutoff=cutoff, apply=args.apply)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    return int(any(row["action"] == "error" for row in report))


if __name__ == "__main__":
    raise SystemExit(main())
