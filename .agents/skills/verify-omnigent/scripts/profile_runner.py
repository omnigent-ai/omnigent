#!/usr/bin/env python3
"""Run the grounded command sequence for one Omnigent verification profile."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_support import (
    CREDENTIAL_MODE_ENV,
    compare_snapshots,
    inherited_managed_fds,
    playwright_browser_snapshot,
    portable_argv,
    repository_snapshot,
    strict_environment,
    terminate_group,
)


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple[str, ...]
    environment: dict[str, str] = field(default_factory=dict)
    cwd: str = "."
    isolated: bool = False


def _ui_pytest(
    tests: tuple[str, ...],
    run_dir: str,
) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--no-sync",
        "pytest",
        *tests,
        "-v",
        "-m",
        "not visual",
        "--screenshot=on",
        "--tracing=on",
        "--video=retain-on-failure",
        "-o",
        "junit_family=legacy",
        f"--output={run_dir}/playwright",
        f"--junitxml={run_dir}/junit.xml",
    )


def profile_steps(
    profile: str,
    run_dir: str,
    harness: str | None = None,
    databricks_profile: str | None = None,
    base_ref: str = "HEAD",
    oss_ref: str = "HEAD",
    changed_files: tuple[str, ...] = (),
    electron_e2e_tests: tuple[str, ...] = (),
    skill_root: str = ".agents/skills/verify-omnigent",
) -> list[Step]:
    """Construct commands from checked-in CLI, Electron, UI, and bench assets."""
    if profile == "quality-gates":
        files = tuple(sorted(dict.fromkeys(changed_files)))
        pre_commit_scope = ("--files", *files) if files else ("--all-files",)
        steps = [
            Step(
                "Pre-commit checks",
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "pre-commit",
                    "run",
                    *pre_commit_scope,
                ),
                isolated=True,
            )
        ]
        web_changed = not files or any(
            path.startswith(("tests/e2e_ui/", "web/"))
            or path
            in {
                "package.json",
                "pnpm-lock.yaml",
                "pnpm-workspace.yaml",
                "tests/test_setup_web_ui_build.py",
            }
            for path in files
        )
        if web_changed:
            steps.extend(
                [
                    Step(
                        "Web format check",
                        ("node_modules/.bin/prettier", "--check", "."),
                        cwd="web",
                    ),
                    Step(
                        "Web lint",
                        ("node_modules/.bin/oxlint", "--deny-warnings", "."),
                        cwd="web",
                    ),
                    Step(
                        "Web type check",
                        ("node_modules/.bin/tsc", "-b", "--noEmit"),
                        cwd="web",
                    ),
                    Step(
                        "Web production build",
                        (
                            "node",
                            "node_modules/vite/bin/vite.js",
                            "build",
                            "--configLoader",
                            "runner",
                            "--outDir",
                            f"{run_dir}/build/web-quality",
                        ),
                        cwd="web",
                    ),
                ]
            )
        selected_contracts = {
            path
            for path in files
            if path.endswith(".py")
            and path.startswith(("tests/verify_omnigent/", "tests/deploy/"))
        }
        if any(
            path.startswith(
                (
                    ".agents/skills/verify-omnigent/",
                    ".claude/skills/verify-omnigent/",
                    ".cursor/skills/verify-omnigent/",
                )
            )
            for path in files
        ):
            selected_contracts.add("tests/verify_omnigent")
        if any(path.startswith("deploy/databricks/") and path.endswith(".py") for path in files):
            selected_contracts.add("tests/deploy/test_databricks_deploy_dry_run.py")
        if any(
            path.startswith(("tests/harness_bench/", "dev/benchmarks/omnigent/")) for path in files
        ):
            selected_contracts.update(
                (
                    "tests/harness_bench/test_bench.py",
                    "tests/benchmarks/test_benchmark_smoke.py",
                )
            )
        if selected_contracts:
            steps.append(
                Step(
                    "Selected verification and deploy contracts",
                    (
                        "uv",
                        "run",
                        "--no-sync",
                        "pytest",
                        *sorted(selected_contracts),
                        "-q",
                    ),
                )
            )
        evidence_contract = "tests/e2e_ui/test_evidence_contract.py"
        evidence_implementation_changed = any(
            path
            in {
                "tests/e2e_ui/conftest.py",
                "tests/e2e_ui/playwright_evidence.py",
                evidence_contract,
            }
            for path in files
        )
        if evidence_implementation_changed:
            steps.append(
                Step(
                    "Playwright evidence contract",
                    _ui_pytest((evidence_contract,), run_dir),
                )
            )
        return steps
    if profile == "perf":
        return [
            Step(
                "Performance benchmark",
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "dev/benchmarks/omnigent/run.py",
                    "--journeys",
                    "list_sessions,load_conversation_history,time_to_first_token",
                    "--iterations",
                    "25",
                    "--runs",
                    "3",
                    "--output",
                    f"{run_dir}/benchmark.json",
                ),
            ),
        ]
    if profile == "server":
        return [
            Step(
                "Server API and app integration",
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "pytest",
                    "tests/server/test_app.py",
                    "tests/server/integration/test_app.py",
                    "-q",
                    f"--junitxml={run_dir}/junit.xml",
                ),
            ),
        ]
    if profile == "db-migration-deploy":
        return [
            Step(
                "Database migration and deploy contracts",
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "pytest",
                    "tests/db",
                    "tests/stores",
                    "tests/deploy",
                    "-q",
                    f"--junitxml={run_dir}/junit.xml",
                ),
            ),
        ]
    if profile == "cli":
        driver = ".claude/skills/cli-setup-verify/verify_cli.py"
        common = (".venv/bin/python", driver, "--repo", ".")
        return [
            Step(
                "CLI isolation",
                (
                    *common,
                    "--scenario",
                    "check-isolation",
                    "--artifacts",
                    f"{run_dir}/cli/isolation",
                ),
            ),
            Step(
                "CLI cold start",
                (
                    *common,
                    "--scenario",
                    "cold-start",
                    "--strip-path",
                    "--artifacts",
                    f"{run_dir}/cli/cold-start",
                ),
            ),
            Step(
                "CLI top-level help",
                (
                    *common,
                    "--scenario",
                    "help-snapshot",
                    "--artifacts",
                    f"{run_dir}/cli/help",
                ),
            ),
            Step(
                "CLI server help",
                (
                    *common,
                    "--scenario",
                    "help-snapshot",
                    "--subcommand",
                    "server",
                    "--artifacts",
                    f"{run_dir}/cli/server-help",
                ),
            ),
        ]
    if profile == "web-ui":
        return [
            Step("Web unit tests", ("node_modules/.bin/vitest", "run"), cwd="web"),
            Step(
                "Web type check",
                ("node_modules/.bin/tsc", "-b", "--noEmit"),
                cwd="web",
            ),
            Step(
                "Web core journey",
                _ui_pytest(
                    (
                        "tests/e2e_ui/chat/test_smoke.py",
                        "tests/e2e_ui/start_session/test_start_session.py",
                        "tests/e2e_ui/files/test_file_autosave.py",
                        "tests/e2e_ui/collaboration/test_permissions_modal.py",
                        "tests/e2e_ui/collaboration/test_sharing_mode_off.py",
                    ),
                    run_dir,
                ),
            ),
        ]
    if profile == "desktop":
        steps = [
            Step(
                "Electron unit tests",
                ("node", "--test"),
                cwd="web/electron",
            ),
            Step(
                "Web production build",
                (
                    "node",
                    "node_modules/vite/bin/vite.js",
                    "build",
                    "--configLoader",
                    "runner",
                    "--outDir",
                    f"{run_dir}/build/web-desktop",
                ),
                cwd="web",
            ),
        ]
        if electron_e2e_tests:
            steps.append(
                Step(
                    "Electron JavaScript Playwright journeys",
                    ("node", "--test", "--test-concurrency=1", *electron_e2e_tests),
                )
            )
        steps.extend(
            [
                Step(
                    "Browser desktop-mode Playwright journeys",
                    _ui_pytest(
                        ("tests/e2e_ui/desktop", "tests/e2e_ui/browser"),
                        run_dir,
                    ),
                ),
                Step(
                    "Electron overlay build",
                    (
                        "node",
                        "node_modules/vite/bin/vite.js",
                        "build",
                        "--configLoader",
                        "runner",
                        "--config",
                        "vite.update-overlay.config.ts",
                        "--outDir",
                        f"{run_dir}/build/electron-overlay",
                    ),
                    cwd="web",
                ),
                Step(
                    "Electron current-platform package",
                    (
                        "node_modules/.bin/electron-builder",
                        "--publish",
                        "never",
                        f"--config.directories.output={run_dir}/build/electron-package",
                    ),
                    {"CSC_IDENTITY_AUTO_DISCOVERY": "false"},
                    cwd="web/electron",
                ),
            ]
        )
        return steps
    if profile == "harness-client":
        return [
            Step(
                "Harness bench tests",
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "pytest",
                    "tests/harness_bench",
                    "-q",
                    f"--junitxml={run_dir}/junit.xml",
                ),
            ),
            Step(
                "Offline harness capability matrix",
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "python",
                    "-m",
                    "tests.harness_bench",
                    "--no-live",
                    "--json",
                    "--report",
                    f"{run_dir}/harness-matrix.json",
                ),
            ),
        ]
    if profile == "harness-live":
        if not harness:
            raise ValueError("OMNIGENT_VERIFY_HARNESS is required for harness-live")
        command = [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "tests.harness_bench",
            "--live",
            "--harness",
            harness,
            "--json",
            "--report",
            f"{run_dir}/harness-live.json",
        ]
        if databricks_profile:
            command.extend(("--profile", databricks_profile))
        return [
            Step("Live harness capability probe", tuple(command)),
        ]
    if profile == "universe":
        return [
            Step(
                "Universe downstream preflight",
                (
                    "python3",
                    f"{skill_root}/scripts/universe_preflight.py",
                    "--repo-root",
                    ".",
                    "--run-dir",
                    run_dir,
                    "--base-ref",
                    base_ref,
                    "--oss-ref",
                    oss_ref,
                ),
            )
        ]
    raise ValueError(f"unsupported composite profile: {profile}")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _validate_live_harness_report(path: Path) -> str | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"Live harness report is unavailable ({type(exc).__name__})."
    harnesses = report.get("harnesses") if isinstance(report, dict) else None
    if not isinstance(harnesses, list) or not harnesses:
        return "Live harness report contains no harness result."
    for harness in harnesses:
        if not isinstance(harness, dict):
            return "Live harness report contains an invalid harness result."
        if harness.get("skipped_reason"):
            return f"Harness was unavailable: {harness['skipped_reason']}"
        cells = harness.get("cells")
        if not isinstance(cells, list):
            return "Live harness report contains no capability cells."
        basic = next(
            (
                cell
                for cell in cells
                if isinstance(cell, dict) and cell.get("dimension") == "basic_turn"
            ),
            None,
        )
        if basic is None or basic.get("observed") != "supported":
            observed = basic.get("observed") if isinstance(basic, dict) else "missing"
            return f"Required live basic turn did not pass (observed={observed})."
    return None


def _validate_offline_harness_report(path: Path) -> str | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"Offline harness report is unavailable ({type(exc).__name__})."
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        return "Offline harness report has an unsupported schema."
    harnesses = report.get("harnesses")
    expected_dimensions = report.get("dimensions")
    if not isinstance(harnesses, list) or not harnesses:
        return "Offline harness report contains no harness results."
    if (
        not isinstance(expected_dimensions, list)
        or not expected_dimensions
        or not all(isinstance(item, str) and item for item in expected_dimensions)
        or len(set(expected_dimensions)) != len(expected_dimensions)
        or "basic_turn" not in expected_dimensions
    ):
        return "Offline harness report has no supported capability dimension contract."
    if report.get("has_drift") is not False:
        return "Offline harness report does not confirm a drift-free declared matrix."
    for harness in harnesses:
        if not isinstance(harness, dict) or not isinstance(harness.get("harness"), str):
            return "Offline harness report contains an invalid harness result."
        cells = harness.get("cells")
        if not isinstance(cells, list) or not cells:
            return f"Offline harness {harness['harness']} contains no capability cells."
        dimensions = [cell.get("dimension") for cell in cells if isinstance(cell, dict)]
        if len(dimensions) != len(cells) or dimensions != expected_dimensions:
            return f"Offline harness {harness['harness']} has incomplete capability cells."
        if any(
            cell.get("observed") not in {"skipped", "not_applicable"}
            or cell.get("declared")
            not in {"supported", "unsupported", "partial", "not_applicable", "unknown"}
            for cell in cells
        ):
            return f"Offline harness {harness['harness']} has invalid offline verdicts."
    return None


def _desktop_build_report(run_dir: Path) -> str | None:
    dist = run_dir / "build" / "electron-package"
    outputs = []
    if dist.is_dir():
        for path in sorted(dist.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            outputs.append(
                {
                    "path": path.relative_to(run_dir).as_posix(),
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )
    _atomic_json(
        run_dir / "desktop-build.json",
        {
            "schema_version": 1,
            "outputs": outputs,
            "limitation": None,
        },
    )
    if not outputs:
        return "Electron packaging produced no regular file in the run-owned output directory."
    return None


def _changed_files(repo_root: Path, base_ref: str) -> tuple[str, ...]:
    base = subprocess.run(
        ("git", "rev-parse", "--verify", f"{base_ref}^{{commit}}"),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if base.returncode != 0:
        reason = base.stderr.strip() or f"base ref {base_ref!r} is unavailable"
        raise RuntimeError(reason)

    head = subprocess.run(
        ("git", "rev-parse", "--verify", "HEAD^{commit}"),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        reason = head.stderr.strip() or "HEAD is unavailable"
        raise RuntimeError(reason)

    merge_base = subprocess.run(
        ("git", "merge-base", "--all", base.stdout.strip(), head.stdout.strip()),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    merge_bases = merge_base.stdout.splitlines()
    if merge_base.returncode != 0 or len(merge_bases) != 1:
        reason = merge_base.stderr.strip() or (
            f"base ref {base_ref!r} has no unique merge base with HEAD"
        )
        raise RuntimeError(reason)

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
            "--cached",
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
    files: list[str] = []
    for index, command in enumerate(commands):
        result = subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            reason = (
                result.stderr.decode("utf-8", "replace").strip()
                or "git changed-file discovery failed"
            )
            raise RuntimeError(reason)
        fields = [os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]
        if index == len(commands) - 1:
            files.extend(fields)
            continue
        cursor = 0
        while cursor < len(fields):
            status = fields[cursor]
            cursor += 1
            path_count = 2 if status[:1] in {"R", "C"} else 1
            if cursor + path_count > len(fields):
                raise RuntimeError("git returned malformed name-status output")
            files.extend(fields[cursor : cursor + path_count])
            cursor += path_count
    return tuple(dict.fromkeys(files))


def _electron_e2e_tests(repo_root: Path) -> tuple[str, ...]:
    root = repo_root / "web" / "electron" / "e2e"
    return tuple(
        path.relative_to(repo_root).as_posix()
        for path in sorted(root.glob("*.e2e.js"))
        if path.is_file()
    )


def _validate_changed_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    for value in paths:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts or value.startswith("-"):
            raise RuntimeError(f"unsafe changed path {value!r}")
    return paths


_DEPENDENCY_PATHS = (
    ".venv",
    "node_modules",
    "web/node_modules",
    "web/electron/node_modules",
)


def _cow_copy(source: Path, target: Path) -> None:
    if sys.platform == "darwin":
        argv = ("cp", "-cR", str(source), str(target))
    elif sys.platform.startswith("linux"):
        argv = ("cp", "--reflink=always", "-a", str(source), str(target))
    else:
        raise RuntimeError(
            f"Safe copy-on-write dependency isolation is unsupported on {sys.platform}."
        )
    subprocess.run(argv, check=True, capture_output=True)


def _rebase_pnpm_shims(
    source: Path,
    target: Path,
    *,
    source_root: Path,
    target_root: Path,
) -> None:
    marker = "# cmd-shim-target="
    for shim in target.rglob("*"):
        if shim.is_symlink() or not shim.is_file() or ".bin" not in shim.parts:
            continue
        try:
            text = shim.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        target_line = next(
            (line[len(marker) :] for line in text.splitlines() if line.startswith(marker)),
            None,
        )
        if target_line is None:
            continue
        virtual_store = target_line.find("/node_modules/.pnpm/")
        if virtual_store < 0:
            continue
        relative_package = target_line[virtual_store + 1 :]
        local_package = source_root / relative_package
        if not local_package.exists():
            shim.unlink()
            continue
        rebased_package = target_root / relative_package
        source_shim = source / shim.relative_to(target)
        old_relative = os.path.relpath(target_line, source_shim.parent)
        new_relative = os.path.relpath(rebased_package, shim.parent)
        old_checkout = target_line[:virtual_store]
        rebased = text.replace(str(source_root), str(target_root))
        rebased = rebased.replace(old_checkout, str(target_root))
        rebased = rebased.replace(old_relative, new_relative)
        shim.write_text(rebased, encoding="utf-8")


def _rebase_pnpm_metadata(target: Path) -> None:
    metadata = target / ".modules.yaml"
    virtual_store = target / ".pnpm"
    if not metadata.is_file() or not virtual_store.is_dir():
        return
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("pnpm dependency metadata is unreadable") from exc
    if not isinstance(value, dict) or not isinstance(value.get("virtualStoreDir"), str):
        raise RuntimeError("pnpm dependency metadata has no virtual store identity")
    value["virtualStoreDir"] = ".pnpm"
    metadata.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _clone_dependency_path(
    source: Path,
    target: Path,
    *,
    source_root: Path | None = None,
    target_root: Path | None = None,
) -> None:
    source_root = (source_root or source.parent).resolve(strict=True)
    target_root = (target_root or target.parent).resolve(strict=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    _cow_copy(source, target)
    if target.is_symlink() or not target.exists():
        raise RuntimeError("Dependency isolation did not create an independent tree.")

    for link in (path for path in target.rglob("*") if path.is_symlink()):
        source_link = source / link.relative_to(target)
        source_target = (source_link.parent / os.readlink(source_link)).resolve(strict=False)
        try:
            relative_target = source_target.relative_to(source_root)
            rebased = target_root / relative_target
            link.unlink()
            link.symlink_to(
                os.path.relpath(rebased, link.parent.resolve()),
                target_is_directory=source_target.is_dir(),
            )
            continue
        except ValueError:
            pass
        # pnpm can leave workspace links pointing at an equivalent virtual-store
        # path in the checkout where install was run. Rebase that package identity
        # to this checkout when the same package exists locally.
        for index, part in enumerate(source_target.parts):
            if part != "node_modules":
                continue
            local_equivalent = source_root.joinpath(*source_target.parts[index:])
            if not local_equivalent.exists():
                continue
            rebased = target_root.joinpath(*source_target.parts[index:])
            link.unlink()
            link.symlink_to(
                os.path.relpath(rebased, link.parent.resolve()),
                target_is_directory=local_equivalent.is_dir(),
            )
            break
        else:
            try:
                source_target = source_link.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(
                    f"External dependency symlink cannot be safely cloned: "
                    f"{source_link.relative_to(source)}"
                ) from exc
            if (
                source.name == ".venv"
                and source_link.parent == source / "bin"
                and source_target.parent.name == "bin"
                and source_target.name.startswith("python")
            ):
                runtime = target / ".python-runtime"
                if not runtime.exists():
                    _cow_copy(source_target.parent.parent, runtime)
                link.unlink()
                link.symlink_to(os.path.relpath(runtime / "bin" / source_target.name, link.parent))
                continue
            link.unlink()
            _cow_copy(source_target, link)
    _rebase_pnpm_metadata(target)
    _rebase_pnpm_shims(
        source,
        target,
        source_root=source_root,
        target_root=target_root,
    )


def _controlled_environment(
    repo_root: Path,
    run_dir: Path,
    profile: str,
    overrides: dict[str, str],
) -> dict[str, str]:
    trusted_overrides = dict(overrides)
    if profile in {"server", "harness-client", "harness-live", "cli", "perf", "web-ui", "desktop"}:
        trusted_overrides["PYTHONPATH"] = str(repo_root.resolve())
    if profile in {
        "smoke",
        "collaboration",
        "automations",
        "core",
        "full-ui",
        "web-ui",
        "desktop",
    }:
        trusted_overrides["OMNIGENT_WEB_UI_DIST"] = str(
            (run_dir / "build" / "e2e-web-ui").resolve()
        )
    if profile in {"web-ui", "desktop"}:
        expected_marker = (run_dir / "cleanup.json").resolve()
        inherited_marker = os.environ.get("OMNIGENT_VERIFY_CLEANUP_MARKER")
        if inherited_marker is not None and Path(inherited_marker).resolve() != expected_marker:
            raise RuntimeError("UI cleanup marker is not owned by this verification run")
        trusted_overrides["OMNIGENT_VERIFY_CLEANUP_MARKER"] = str(expected_marker)
    return strict_environment(
        run_dir,
        extra={
            "OMNIGENT_VERIFY_REPO_ROOT": str(repo_root),
            "OMNIGENT_VERIFY_RUN_DIR": str(run_dir),
            **trusted_overrides,
        },
        credentialed=profile == "harness-live",
        pre_commit=profile == "quality-gates",
    )


def _validate_ui_cleanup_marker(run_dir: Path) -> str | None:
    marker = run_dir / "cleanup.json"
    if marker.is_symlink() or not marker.is_file():
        return "UI pytest did not create its run-owned cleanup marker"
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "UI pytest cleanup marker is malformed"
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("status") != "completed"
        or value.get("exit_status") != 0
    ):
        return "UI pytest cleanup marker does not confirm completed cleanup"
    return None


def _copy_working_changes(repo_root: Path, checkout: Path) -> None:
    diff = subprocess.run(
        ("git", "diff", "--binary", "HEAD", "--"),
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    if diff.stdout:
        subprocess.run(
            ("git", "apply", "--binary", "--whitespace=nowarn", "-"),
            cwd=checkout,
            input=diff.stdout,
            check=True,
            capture_output=True,
        )
    untracked = subprocess.run(
        ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for raw in untracked:
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("git returned an unsafe untracked path")
        source = repo_root / relative
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, target)


@contextlib.contextmanager
def _isolated_checkout(repo_root: Path, cleanup: dict[str, object]) -> Iterator[Path]:
    temporary = Path(tempfile.mkdtemp(prefix="omnigent-verify-quality-"))
    checkout = temporary / "checkout"
    added = False
    try:
        subprocess.run(
            ("git", "worktree", "add", "--detach", str(checkout), "HEAD"),
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        added = True
        _copy_working_changes(repo_root, checkout)
        for relative in _DEPENDENCY_PATHS:
            source = repo_root / relative
            target = checkout / relative
            if not source.exists() or target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _clone_dependency_path(
                source,
                target,
                source_root=repo_root,
                target_root=checkout,
            )
        isolated_root = checkout.resolve(strict=True)
        for relative in _DEPENDENCY_PATHS:
            dependency = checkout / relative
            if not dependency.exists():
                continue
            for link in (path for path in dependency.rglob("*") if path.is_symlink()):
                try:
                    link.resolve(strict=False).relative_to(isolated_root)
                except ValueError as exc:
                    raise RuntimeError(
                        f"Dependency symlink remains outside isolated checkout: "
                        f"{link.relative_to(checkout)}"
                    ) from exc
        yield checkout
    finally:
        removed = True
        if added:
            result = subprocess.run(
                ("git", "worktree", "remove", "--force", str(checkout)),
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
            removed = result.returncode == 0
        shutil.rmtree(temporary, ignore_errors=True)
        prune = subprocess.run(
            ("git", "worktree", "prune"),
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        removed = removed and prune.returncode == 0
        cleanup["status"] = "completed" if removed and not temporary.exists() else "failed"


def _safe_request_path(value: str) -> bool:
    path = Path(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and not value.startswith("-")
    )


def _load_request(
    path: Path | None,
    capability_path: Path | None,
    profile: str,
) -> tuple[set[int] | None, tuple[str, ...] | None, str | None]:
    if path is None:
        if capability_path is not None:
            raise RuntimeError("comparison capability was supplied without a request")
        return None, None, None
    if capability_path is None:
        raise RuntimeError("comparison request has no orchestration capability")
    if path.is_symlink() or capability_path.is_symlink():
        raise RuntimeError("comparison request paths cannot be symlinks")
    resolved = path.resolve(strict=True)
    resolved_capability = capability_path.resolve(strict=True)
    metadata = resolved.stat()
    capability_metadata = resolved_capability.stat()
    if not resolved.is_file() or metadata.st_uid != os.getuid():
        raise RuntimeError("comparison request is not an owned regular file")
    if (
        not resolved_capability.is_file()
        or capability_metadata.st_uid != os.getuid()
        or capability_metadata.st_mode & 0o077
    ):
        raise RuntimeError("comparison capability is not a private owned regular file")
    if metadata.st_mode & 0o077:
        raise RuntimeError("comparison request permissions are not private")
    resolved.relative_to(resolved_capability.parent.parent)
    raw = resolved.read_bytes()
    value = json.loads(raw)
    resolved.unlink()
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("comparison request schema is invalid")
    if value.get("profile") != profile:
        raise RuntimeError("comparison request profile does not match")
    indices = value.get("step_indices")
    if (
        not isinstance(indices, list)
        or not indices
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in indices
        )
        or len(indices) != len(set(indices))
    ):
        raise RuntimeError("comparison request step indices are invalid")
    changed = value.get("changed_files")
    if changed is not None and (
        not isinstance(changed, list)
        or not all(isinstance(item, str) and _safe_request_path(item) for item in changed)
    ):
        raise RuntimeError("comparison request changed paths are invalid")
    nonce = value.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 32:
        raise RuntimeError("comparison request capability is invalid")
    signature = value.pop("signature", None)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    expected_signature = hmac.digest(resolved_capability.read_bytes(), payload, "sha256").hex()
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected_signature):
        raise RuntimeError("comparison request signature is invalid")
    return (
        set(indices),
        tuple(changed) if changed is not None else None,
        hashlib.sha256(raw).hexdigest(),
    )


def _terminate_process(active: subprocess.Popen[str], signum: int = signal.SIGTERM) -> None:
    if active.poll() is not None:
        return
    terminate_group(active.pid, signum)
    active.wait()


def run_profile(
    repo_root: Path,
    run_dir: Path,
    profile: str,
    request_file: Path | None = None,
    capability_file: Path | None = None,
) -> int:
    relative_run_dir = os.fspath(run_dir.resolve())
    base_ref = os.environ.get("OMNIGENT_VERIFY_BASE_REF", "HEAD")
    selected_indices, changed_override, request_sha256 = _load_request(
        request_file,
        capability_file,
        profile,
    )
    changed_files = (
        changed_override
        if changed_override is not None
        else (_changed_files(repo_root, base_ref) if profile == "quality-gates" else ())
    )
    skill_root = os.environ.get(
        "OMNIGENT_VERIFY_SKILL_ROOT",
        ".agents/skills/verify-omnigent",
    )
    steps = profile_steps(
        profile,
        relative_run_dir,
        harness=os.environ.get("OMNIGENT_VERIFY_HARNESS"),
        databricks_profile=os.environ.get("OMNIGENT_VERIFY_DATABRICKS_PROFILE"),
        base_ref=base_ref,
        oss_ref=os.environ.get("OMNIGENT_VERIFY_OSS_REF", "HEAD"),
        changed_files=_validate_changed_paths(changed_files),
        electron_e2e_tests=_electron_e2e_tests(repo_root) if profile == "desktop" else (),
        skill_root=skill_root,
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "profile": profile,
        "status": "running",
        "comparison_request_sha256": request_sha256,
        "requested_step_indices": sorted(selected_indices)
        if selected_indices is not None
        else None,
        "steps": [],
    }
    report_path = run_dir / "steps.json"
    if profile in {"web-ui", "desktop"}:
        (run_dir / "cleanup.json").unlink(missing_ok=True)
    _atomic_json(report_path, report)
    active: subprocess.Popen[str] | None = None
    try:
        before = repository_snapshot(repo_root, deep_dependencies=True)
        browser_before = (
            playwright_browser_snapshot() if profile in {"web-ui", "desktop"} else None
        )
        if profile in {"web-ui", "desktop"} and browser_before is None:
            raise RuntimeError("verified Playwright browser installation is unavailable")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        report["status"] = "failed"
        report["state_unchanged"] = False
        report["state_error"] = f"Initial repository snapshot failed ({type(exc).__name__}): {exc}"
        _atomic_json(report_path, report)
        return 1
    timeout_seconds = float(os.environ.get("OMNIGENT_VERIFY_STEP_TIMEOUT_SECONDS", "1800"))
    if selected_indices is not None and not selected_indices.issubset(range(len(steps))):
        raise RuntimeError("comparison request contains out-of-range step indices")

    def forward_signal(signum: int, _frame: object) -> None:
        if active is not None and active.poll() is None:
            _terminate_process(active, signum)
        report["status"] = "interrupted"
        try:
            report["state_unchanged"] = not compare_snapshots(
                before,
                repository_snapshot(repo_root, deep_dependencies=True),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            report["state_unchanged"] = False
            report["state_error"] = (
                f"Final repository snapshot failed ({type(exc).__name__}): {exc}"
            )
        _atomic_json(report_path, report)
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward_signal)

    first_failure = 0
    step_results = report["steps"]
    assert isinstance(step_results, list)
    for index, step in enumerate(steps):
        if selected_indices is not None and index not in selected_indices:
            continue
        step_log = run_dir / "step-logs" / f"{index:02d}.log"
        step_log.parent.mkdir(parents=True, exist_ok=True)
        result = {
            "source_step_index": index,
            "name": step.name,
            "argv": portable_argv(
                step.argv,
                repo_root=repo_root,
                run_dir=run_dir,
                skill_root=Path(skill_root),
            ),
            "environment": step.environment,
            "cwd": step.cwd,
            "isolated": step.isolated,
            "log": step_log.relative_to(run_dir).as_posix(),
            "status": "running",
            "exit_status": None,
        }
        step_results.append(result)
        _atomic_json(report_path, report)
        print(f"==> {step.name}", flush=True)
        isolation_cleanup: dict[str, object] = {"status": "not_applicable"}
        try:
            manager = (
                _isolated_checkout(repo_root, isolation_cleanup)
                if step.isolated
                else contextlib.nullcontext(repo_root)
            )
            with manager as execution_root:
                cwd = execution_root / step.cwd
                with step_log.open("w", encoding="utf-8") as log:
                    controlled_env = _controlled_environment(
                        repo_root,
                        run_dir,
                        profile,
                        step.environment,
                    )
                    if profile == "harness-live":
                        credential_mode = controlled_env.get(CREDENTIAL_MODE_ENV)
                        if not credential_mode:
                            raise RuntimeError("harness-live has no deliberate credential mode")
                        result["credential_mode"] = credential_mode
                    started_process = subprocess.Popen(
                        step.argv,
                        cwd=cwd,
                        env=controlled_env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,
                        pass_fds=inherited_managed_fds(),
                    )
                    active = started_process
                    try:
                        exit_status = started_process.wait(timeout=timeout_seconds)
                    except subprocess.TimeoutExpired:
                        _terminate_process(started_process)
                        exit_status = 124
                        result["timed_out"] = True
                    finally:
                        active = None
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            exit_status = 1
            result["reason"] = f"Step startup failed ({type(exc).__name__}): {exc}"
            with step_log.open("a", encoding="utf-8") as log:
                log.write(f"{result['reason']}\n")
        result["isolation_cleanup"] = isolation_cleanup["status"]
        output = step_log.read_text(encoding="utf-8", errors="replace")
        print(output, end="" if output.endswith("\n") or not output else "\n")
        result["exit_status"] = exit_status
        result["status"] = "passed" if exit_status == 0 else "failed"
        result["log_sha256"] = hashlib.sha256(step_log.read_bytes()).hexdigest()
        if isolation_cleanup["status"] == "failed":
            result["status"] = "failed"
            result["exit_status"] = 1
            result["reason"] = "The isolated quality worktree was not cleaned up."
            exit_status = 1
        if exit_status != 0 and first_failure == 0:
            first_failure = exit_status
        _atomic_json(report_path, report)

    validation_error = None
    if selected_indices is None:
        if profile in {"web-ui", "desktop"} and first_failure == 0:
            validation_error = _validate_ui_cleanup_marker(run_dir)
        if profile == "harness-live" and first_failure == 0:
            validation_error = _validate_live_harness_report(run_dir / "harness-live.json")
        elif profile == "harness-client" and first_failure == 0:
            validation_error = _validate_offline_harness_report(run_dir / "harness-matrix.json")
        elif profile == "desktop" and first_failure == 0 and validation_error is None:
            validation_error = _desktop_build_report(run_dir)
    if validation_error:
        print(f"verification blocker: {validation_error}", file=sys.stderr)
        step_results.append(
            {
                "name": "Evidence validation",
                "argv": [],
                "environment": {},
                "status": "failed",
                "exit_status": 1,
                "reason": validation_error,
            }
        )
        first_failure = 1

    try:
        after = repository_snapshot(repo_root, deep_dependencies=True)
        state_errors = compare_snapshots(before, after)
        if profile in {"web-ui", "desktop"}:
            browser_after = playwright_browser_snapshot()
            if browser_before != browser_after:
                state_errors.append("Playwright browser installation changed")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        state_errors = [f"final repository snapshot failed ({type(exc).__name__}): {exc}"]
    report["state_unchanged"] = not state_errors
    if state_errors:
        state_error = "; ".join(state_errors)
        report["state_error"] = state_error
        print(f"verification blocker: {state_error}", file=sys.stderr)
        step_results.append(
            {
                "name": "Repository state invariant",
                "argv": [],
                "environment": {},
                "status": "failed",
                "exit_status": 1,
                "reason": state_error,
            }
        )
        first_failure = 1
    report["status"] = "passed" if first_failure == 0 else "failed"
    _atomic_json(report_path, report)
    return first_failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=(
            "quality-gates",
            "perf",
            "server",
            "db-migration-deploy",
            "cli",
            "web-ui",
            "desktop",
            "harness-client",
            "harness-live",
            "universe",
        ),
        required=True,
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--capability-file", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(
        run_profile(
            args.repo_root.resolve(),
            args.run_dir.resolve(),
            args.profile,
            args.request_file,
            args.capability_file,
        )
    )


if __name__ == "__main__":
    main()
