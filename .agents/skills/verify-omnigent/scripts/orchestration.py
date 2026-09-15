#!/usr/bin/env python3
"""Select fail-closed Omnigent verification lanes from repository changes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

LANE_ORDER = (
    "quality-gates",
    "server",
    "harness-client",
    "cli",
    "web-ui",
    "desktop",
    "automations",
    "collaboration",
    "db-migration-deploy",
    "perf",
)
ALL_SURFACES = frozenset(
    (
        "quality-gates",
        "server",
        "harness-client",
        "cli",
        "web-ui",
        "desktop",
        "automations",
        "collaboration",
    )
)

OPTIONAL_LANES = (
    {
        "lane": "disposable-backend-install",
        "status": "not_requested",
        "reason": "A clean-room dependency install needs package-network access.",
        "opt_in": ".agents/skills/verify-omnigent/scripts/verify.sh backend",
    },
    {
        "lane": "harness-live",
        "status": "not_requested",
        "reason": "Live harness turns need a selected adapter and gateway credentials.",
        "opt_in": (
            "OMNIGENT_VERIFY_HARNESS=<name> "
            ".agents/skills/verify-omnigent/scripts/verify.sh harness-live"
        ),
    },
    {
        "lane": "cli-live-repl",
        "status": "not_requested",
        "reason": "The credentialed CLI REPL writes diagnostics under the inherited HOME.",
        "opt_in": (
            "Follow .claude/skills/cli-setup-verify/SKILL.md and run repl-commands "
            "with --inherit-home only after accepting that write boundary."
        ),
    },
    {
        "lane": "electron-release",
        "status": "not_requested",
        "reason": "Signing and notarization need Apple release credentials.",
        "opt_in": (
            "Set the credentials documented in web/electron/README.md, then run "
            "pnpm --dir web/electron run build:mac:release -- --publish never"
        ),
    },
    {
        "lane": "downstream-universe",
        "status": "not_requested",
        "reason": "Downstream compatibility reads a sibling checkout and remains explicit opt-in.",
        "opt_in": (
            ".agents/skills/verify-omnigent/scripts/verify.sh auto "
            "--with-universe --oss-ref <commit>"
        ),
    },
)

_VERIFICATION_PREFIXES = (
    ".agents/skills/verify-omnigent/",
    ".claude/skills/verify-omnigent/",
    ".cursor/skills/verify-omnigent/",
    "tests/verify_omnigent/",
)
_DESKTOP_PREFIXES = (
    "web/electron/",
    "tests/e2e_ui/desktop/",
    "tests/e2e_ui/browser/",
)
_CLI_PREFIXES = (
    "omnigent/onboarding/",
    "omnigent/repl/",
    "tests/cli/",
    "tests/onboarding/",
    ".claude/skills/cli-setup-verify/",
)
_HARNESS_PREFIXES = (
    "integrations/",
    "omnigent/inner/",
    "omnigent/runtime/harnesses/",
    "omnigent/runner/",
    "omnigent/host/",
    "sdks/",
    "tests/host/",
    "tests/harness_bench/",
    "tests/inner/",
    "tests/frontends/",
    "tests/e2e/",
)
_SERVER_PREFIXES = (
    "integrations/",
    "omnigent/server/",
    "omnigent/runtime/",
    "omnigent/db/",
    "omnigent/stores/",
    "omnigent/entities/",
    "tests/server/",
    "tests/runtime/",
    "tests/db/",
    "tests/stores/",
    "deploy/",
    "tests/deploy/",
)
_CLI_FILES = {
    "omnigent/cli.py",
    "omnigent/_terminal_picker_theme.py",
    "scripts/install_oss.sh",
}
_HARNESS_FILES = {
    "omnigent/claude_model_vocabulary.py",
    "omnigent/gateway_inference.py",
    "omnigent/model_fallbacks.py",
    "tests/test_claude_native_bridge.py",
    "tests/test_gateway_inference.py",
}
_SERVER_FILES = {
    "omnigent/gateway_inference.py",
    "omnigent/host/identity.py",
    "omnigent/runner/identity.py",
    "tests/test_gateway_inference.py",
}
_QUALITY_ONLY_FILES = {
    ".gitignore",
    ".pre-commit-config.yaml",
    "scripts/barrier1_apply_check.sh",
    "scripts/verify_smart_routing.sh",
}
_SHARED_PYTHON_FILES = {
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    "setup.py",
}
_SHARED_WEB_FILES = {
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
}
_SETUP_WEB_BUILD_TEST_FILES = {
    "tests/test_setup_web_ui_build.py",
}
_SHARED_PLAYWRIGHT_EVIDENCE_FILES = {
    "tests/e2e_ui/conftest.py",
    "tests/e2e_ui/playwright_evidence.py",
    "tests/e2e_ui/test_evidence_contract.py",
}
_AUTOMATION_PREFIXES = (
    "omnigent/automations/",
    "omnigent/server/routes/scheduled",
    "omnigent/stores/scheduled",
    "tests/e2e_ui/scheduled/",
    "tests/server/integration/test_scheduled",
    "tests/stores/test_scheduled",
    "tests/runner/test_scheduled",
)
_COLLABORATION_PREFIXES = (
    "omnigent/server/routes/sharing",
    "omnigent/server/routes/permissions",
    "omnigent/server/routes/sessions/routes_permissions",
    "omnigent/server/routes/account_auth/",
    "omnigent/server/routes/accounts_auth/",
    "omnigent/server/routes/auth/",
    "omnigent/server/routes/identity/",
    "omnigent/server/routes/roles/",
    "omnigent/server/routes/session_authorization/",
    "tests/e2e_ui/auth/",
    "tests/e2e_ui/collaboration/",
    "tests/e2e/test_sharing",
    "tests/e2e/test_managed_runner_http_auth",
    "tests/integration/test_sharing",
    "tests/server/integration/test_accounts_auth",
    "tests/server/integration/test_oidc",
    "tests/server/routes/test_auth",
    "tests/server/test_accounts",
    "tests/server/test_admin_list",
    "tests/server/test_device_auth",
    "tests/server/test_identity_migration",
    "tests/server/test_oidc",
    "tests/server/routes/test_sessions_sharing",
)
_COLLABORATION_FILES = {
    "omnigent/entities/account.py",
    "omnigent/entities/permission.py",
    "omnigent/host/identity.py",
    "omnigent/runner/identity.py",
    "omnigent/server/accounts_bootstrap.py",
    "omnigent/server/accounts_config.py",
    "omnigent/server/accounts_secret.py",
    "omnigent/server/accounts_store.py",
    "omnigent/server/auth.py",
    "omnigent/server/identity_migration.py",
    "omnigent/server/oidc.py",
    "omnigent/server/oidc_access.py",
    "omnigent/server/permissions.py",
    "omnigent/server/routes/_auth_helpers.py",
    "omnigent/server/routes/_session_create_validation.py",
    "omnigent/server/routes/accounts_auth.py",
    "omnigent/server/routes/auth.py",
    "omnigent/server/routes/device_auth.py",
    "omnigent/server/routes/session_policies.py",
    "omnigent/server/routes/sessions/routes_core.py",
    "omnigent/server/routes/sessions/routes_hooks.py",
    "sdks/python-client/omnigent_client/_client.py",
    "tests/e2e_ui/sessions/test_sidebar_ownership_gating.py",
    "tests/entities/test_permission.py",
    "tests/server/integration/test_runner_ownership.py",
    "tests/server/integration/test_sessions_permission_request_hook.py",
    "tests/server/integration/test_sessions_permissions.py",
    "tests/server/routes/test_accounts_auth_helpers.py",
    "tests/server/routes/test_shell_permission_gate.py",
    "tests/server/test_permissions.py",
    "tests/stores/test_permission_store.py",
    "web/src/components/PermissionsModal.tsx",
    "web/src/components/PermissionsModal.test.tsx",
    "web/src/components/PermissionsModal.safety.test.tsx",
    "web/src/hooks/usePermissions.ts",
    "web/src/hooks/usePermissions.test.tsx",
    "web/src/lib/identity.ts",
    "web/src/lib/identity.test.ts",
    "web/src/lib/accountsApi.ts",
    "web/src/lib/accountsApi.test.ts",
    "web/src/lib/permissionsApi.ts",
    "web/src/lib/permissionsApi.test.ts",
    "web/src/hooks/useIsAdmin.ts",
    "web/src/hooks/useMe.ts",
    "web/src/pages/LoginPage.tsx",
    "web/src/pages/LoginPage.test.tsx",
    "web/src/pages/MembersPage.tsx",
    "web/src/pages/MembersPage.test.tsx",
    "web/src/pages/RegisterPage.tsx",
    "web/src/pages/RegisterPage.test.tsx",
    "web/src/pages/SetupPage.tsx",
    "web/src/pages/SetupPage.test.tsx",
}
_DB_DEPLOY_PREFIXES = (
    "omnigent/db/",
    "omnigent/stores/",
    "deploy/",
    "tests/db/test_migration",
    "tests/deploy/",
)
_PERF_SENSITIVE_PREFIXES = (
    "dev/benchmarks/omnigent/",
    "omnigent/server/routes/sessions",
    "omnigent/server/routes/conversations",
    "omnigent/runtime/stream",
    "tests/benchmarks/",
)
_QUALITY_PREFIXES = (
    ".github/workflows/",
    "deploy/",
    "examples/",
    "omnigent/",
    "scripts/",
    "sdks/",
    "tests/",
    "web/",
)
_IGNORED_PREFIXES = (
    "designs/",
    "docs/",
)
_IGNORED_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
}


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _add_reason(
    reasons: dict[str, list[str]],
    lane: str,
    path: str,
    explanation: str,
) -> None:
    reasons[lane].append(f"{path}: {explanation}")


def select_lanes(changed_files: list[str]) -> dict[str, object]:
    """Map changed files to every applicable local verification lane."""
    normalized = sorted(
        dict.fromkeys(
            path[2:] if path.startswith("./") else path for path in changed_files if path
        )
    )
    reasons: dict[str, list[str]] = {lane: [] for lane in LANE_ORDER}
    blockers: list[str] = []

    for path in normalized:
        path_parts = Path(path).parts
        if Path(path).is_absolute() or ".." in path_parts or path.startswith("-"):
            blockers.append(
                f"Unsafe changed path {path!r} cannot be passed to verification tools."
            )
            continue
        if "/__pycache__/" in path or path.endswith((".pyc", ".pyo")):
            continue
        if _under(path, _VERIFICATION_PREFIXES):
            for lane in (lane for lane in LANE_ORDER if lane != "perf"):
                if not reasons[lane]:
                    _add_reason(
                        reasons,
                        lane,
                        path,
                        "the canonical verification path can change this lane",
                    )
            continue

        matched = False
        if _under(path, _DESKTOP_PREFIXES) or path == ".github/workflows/electron-build.yml":
            _add_reason(reasons, "desktop", path, "Electron shell or desktop journey changed")
            matched = True
        if path.startswith("web/") and not path.startswith("web/electron/"):
            _add_reason(reasons, "web-ui", path, "browser UI code or tests changed")
            matched = True
        if path.startswith("tests/e2e_ui/") and not _under(path, _DESKTOP_PREFIXES):
            _add_reason(reasons, "web-ui", path, "browser journey infrastructure changed")
            matched = True
        if path in _SHARED_PLAYWRIGHT_EVIDENCE_FILES:
            _add_reason(reasons, "desktop", path, "shared Playwright evidence changed")
            matched = True
        if path in _SETUP_WEB_BUILD_TEST_FILES:
            matched = True
        if _under(path, _CLI_PREFIXES) or path in _CLI_FILES or path.endswith("_cli.py"):
            _add_reason(reasons, "cli", path, "CLI setup, onboarding, or terminal flow changed")
            matched = True
        if (
            _under(path, _HARNESS_PREFIXES)
            or path in _HARNESS_FILES
            or (
                path.startswith("omnigent/")
                and ("native" in Path(path).name or "harness" in Path(path).name)
            )
        ):
            _add_reason(
                reasons, "harness-client", path, "harness, runner, host, or client changed"
            )
            matched = True
        if _under(path, _SERVER_PREFIXES) or path in _SERVER_FILES:
            _add_reason(
                reasons, "server", path, "server, runtime, storage, or deployment code changed"
            )
            matched = True
        if _under(path, _AUTOMATION_PREFIXES):
            _add_reason(reasons, "automations", path, "scheduled-task behavior changed")
            matched = True
        if _under(path, _COLLABORATION_PREFIXES) or path in _COLLABORATION_FILES:
            _add_reason(
                reasons,
                "collaboration",
                path,
                "authentication, identity, sharing, or permission behavior changed",
            )
            matched = True
        if _under(path, _DB_DEPLOY_PREFIXES):
            _add_reason(
                reasons,
                "db-migration-deploy",
                path,
                "database migration, schema, store, or deployment behavior changed",
            )
            matched = True
        if _under(path, _PERF_SENSITIVE_PREFIXES):
            _add_reason(reasons, "perf", path, "latency-sensitive request path changed")
            matched = True
        if path in _QUALITY_ONLY_FILES:
            matched = True
        if path in _SHARED_PYTHON_FILES:
            for lane in ("server", "harness-client", "cli"):
                _add_reason(reasons, lane, path, "shared Python dependency or build input changed")
            matched = True
        if path in _SHARED_WEB_FILES:
            for lane in ("web-ui", "desktop"):
                _add_reason(
                    reasons, lane, path, "shared JavaScript dependency or workspace input changed"
                )
            matched = True
        if (
            matched
            or _under(path, _QUALITY_PREFIXES)
            or path in _SHARED_PYTHON_FILES
            or path in _SHARED_WEB_FILES
        ):
            _add_reason(
                reasons,
                "quality-gates",
                path,
                "changed product or test code must pass repository quality checks",
            )

        if matched:
            continue
        if path in _IGNORED_FILES or _under(path, _IGNORED_PREFIXES):
            continue
        blockers.append(
            f"{path} did not map to a verification lane. Add an explicit mapping or "
            "run all-surfaces."
        )

    decisions: list[dict[str, object]] = []
    selected_lanes: list[str] = []
    for lane in LANE_ORDER:
        selected = bool(reasons[lane])
        if selected:
            selected_lanes.append(lane)
        decisions.append(
            {
                "lane": lane,
                "status": "selected" if selected else "not_applicable",
                "reasons": reasons[lane] or ["No changed file mapped to this lane."],
            }
        )
    return {
        "changed_files": normalized,
        "selected_lanes": selected_lanes,
        "decisions": decisions,
        "blockers": blockers,
    }


def changed_files(repo_root: Path, base_ref: str) -> tuple[list[str], list[str]]:
    """Return branch-point changes plus tracked local and untracked files."""
    base = subprocess.run(
        ["git", "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if base.returncode != 0:
        return [], [
            f"Base ref {base_ref!r} is unavailable. Fetch it or set "
            "OMNIGENT_VERIFY_BASE_REF to an existing commit."
        ]

    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        return [], ["HEAD is unavailable; changed-file discovery cannot continue."]

    merge_base = subprocess.run(
        ["git", "merge-base", "--all", base.stdout.strip(), head.stdout.strip()],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    merge_bases = merge_base.stdout.splitlines()
    if merge_base.returncode != 0 or len(merge_bases) != 1:
        detail = merge_base.stderr.strip()
        suffix = f" ({detail})" if detail else ""
        return [], [
            f"Base ref {base_ref!r} has no unique merge base with HEAD{suffix}. "
            "Fetch the complete history or choose the PR base ref."
        ]

    commands = (
        (
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies-harder",
            "--diff-filter=ACDMRTUXB",
            merge_bases[0],
            head.stdout.strip(),
            "--",
        ),
        (
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies-harder",
            "--diff-filter=ACDMRTUXB",
            "HEAD",
            "--",
        ),
        (
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies-harder",
            "--diff-filter=ACDMRTUXB",
            "--",
        ),
        ("git", "ls-files", "-z", "--others", "--exclude-standard"),
    )
    results = [
        subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        for command in commands
    ]
    failures = [
        result.stderr.decode("utf-8", "replace").strip() or "git changed-file discovery failed"
        for result in results
        if result.returncode != 0
    ]
    if failures:
        return [], failures
    paths: list[str] = []
    for index, result in enumerate(results):
        fields = [os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]
        if index == len(results) - 1:
            paths.extend(fields)
            continue
        cursor = 0
        while cursor < len(fields):
            status = fields[cursor]
            cursor += 1
            path_count = 2 if status[:1] in {"R", "C"} else 1
            if cursor + path_count > len(fields):
                return [], ["git returned malformed name-status output"]
            paths.extend(fields[cursor : cursor + path_count])
            cursor += path_count
    return list(dict.fromkeys(paths)), []


def build_plan(
    repo_root: Path,
    profile: str,
    base_ref: str,
    with_universe: bool = False,
) -> dict[str, object]:
    if profile == "all-surfaces":
        selected = [lane for lane in LANE_ORDER if lane in ALL_SURFACES]
        selection = {
            "changed_files": [],
            "selected_lanes": selected,
            "decisions": [
                {
                    "lane": lane,
                    "status": "selected",
                    "reasons": ["Explicit all-surfaces verification was requested."],
                }
                for lane in selected
            ],
            "blockers": [],
        }
    else:
        files, discovery_blockers = changed_files(repo_root, base_ref)
        selection = select_lanes(files)
        selected_blockers = selection.get("blockers")
        if not isinstance(selected_blockers, list) or not all(
            isinstance(item, str) for item in selected_blockers
        ):
            raise RuntimeError("lane selection returned invalid blockers")
        selection["blockers"] = [*discovery_blockers, *selected_blockers]
    if with_universe:
        selected_lanes = selection.get("selected_lanes")
        decisions = selection.get("decisions")
        if not isinstance(selected_lanes, list) or not isinstance(decisions, list):
            raise RuntimeError("lane selection returned invalid lists")
        selected_lanes.append("universe")
        decisions.append(
            {
                "lane": "universe",
                "status": "selected",
                "reasons": ["Explicit downstream Universe verification was requested."],
            }
        )
    return {
        "schema_version": 1,
        "profile": profile,
        "base_ref": base_ref if profile == "auto" else None,
        **selection,
        "optional_lanes": [
            dict(item)
            for item in OPTIONAL_LANES
            if not (with_universe and item["lane"] == "downstream-universe")
        ],
    }


def _atomic_write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--profile", choices=("auto", "all-surfaces"), required=True)
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument("--with-universe", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    plan = build_plan(
        args.repo_root.resolve(),
        args.profile,
        args.base_ref,
        args.with_universe,
    )
    _atomic_write(args.output, plan)
    selected_lanes = plan.get("selected_lanes")
    decisions = plan.get("decisions")
    blockers = plan.get("blockers")
    if (
        not isinstance(selected_lanes, list)
        or not all(isinstance(item, str) for item in selected_lanes)
        or not isinstance(decisions, list)
        or not all(isinstance(item, dict) for item in decisions)
        or not isinstance(blockers, list)
        or not all(isinstance(item, str) for item in blockers)
    ):
        raise RuntimeError("verification plan has an invalid schema")
    print("verification lanes: " + (", ".join(selected_lanes) if selected_lanes else "none"))
    for decision in decisions:
        print(f"  {decision['lane']}: {decision['status']}")
        reasons = decision.get("reasons")
        if not isinstance(reasons, list):
            raise RuntimeError("verification decision has invalid reasons")
        for reason in reasons:
            print(f"    - {reason}")
    for blocker in blockers:
        print(f"BLOCKER: {blocker}")
    raise SystemExit(3 if blockers else 0)


if __name__ == "__main__":
    main()
