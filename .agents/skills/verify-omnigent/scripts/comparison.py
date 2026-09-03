#!/usr/bin/env python3
"""Run bounded exact-base failure and performance comparisons."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_runner import (
    _changed_files,
    _clone_dependency_path,
    _electron_e2e_tests,
    profile_steps,
)
from runtime_support import (
    compare_snapshots,
    inherited_managed_fds,
    portable_argv,
    repository_snapshot,
    strict_environment,
    terminate_group,
)

SCHEMA_VERSION = 1
DEFAULT_MAX_P50_REGRESSION_PERCENT = 10.0
DEFAULT_MAX_P99_REGRESSION_PERCENT = 10.0
SUPPORTED_BENCHMARK_SCHEMA_VERSIONS = {6}
COMPARABLE_LANES = {
    "server",
    "harness-client",
    "cli",
}
DEPENDENCY_PATHS = (".venv",)
DEPENDENCY_INPUTS = (
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "web/package.json",
    "web/electron/package.json",
)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ComparisonInterrupted(RuntimeError):
    """Raised so signal delivery unwinds worktree and report contexts."""


class _MetricPair(TypedDict):
    base: object
    candidate: object


class _SuccessPair(TypedDict):
    base: bool
    candidate: bool


class _BenchmarkRow(TypedDict):
    journey: str
    backend_match: bool
    kind_match: bool
    p50_ms: _MetricPair
    p99_ms: _MetricPair
    http_requests_per_op: _MetricPair
    http_requests: _MetricPair
    network_routes: _MetricPair
    failures: _MetricPair
    functional_success: _SuccessPair


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )


def _merge_base(repo_root: Path, base_ref: str) -> str:
    base = _git(repo_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    head = _git(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    if base.returncode != 0 or head.returncode != 0:
        raise RuntimeError(f"Base ref {base_ref!r} or HEAD is unavailable.")
    result = _git(repo_root, "merge-base", "--all", base.stdout.strip(), head.stdout.strip())
    values = result.stdout.splitlines()
    if result.returncode != 0 or len(values) != 1:
        raise RuntimeError(f"Base ref {base_ref!r} has no unique merge base with HEAD.")
    return values[0]


def _digest_bytes(value: bytes | None) -> str | None:
    return hashlib.sha256(value).hexdigest() if value is not None else None


def _dependency_identity(repo_root: Path, base_commit: str) -> dict[str, object]:
    files: dict[str, dict[str, str | None]] = {}
    for relative in DEPENDENCY_INPUTS:
        current_path = repo_root / relative
        current = current_path.read_bytes() if current_path.is_file() else None
        base = subprocess.run(
            ("git", "show", f"{base_commit}:{relative}"),
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        base_bytes = base.stdout if base.returncode == 0 else None
        files[relative] = {
            "candidate_sha256": _digest_bytes(current),
            "base_sha256": _digest_bytes(base_bytes),
        }
    return {
        "matched": all(
            value["candidate_sha256"] == value["base_sha256"] for value in files.values()
        ),
        "files": files,
    }


@contextlib.contextmanager
def _base_worktree(
    repo_root: Path,
    base_commit: str,
    cleanup: dict[str, object],
) -> Iterator[Path]:
    temporary = Path(tempfile.mkdtemp(prefix="omnigent-verify-base-"))
    checkout = temporary / "checkout"
    try:
        result = subprocess.run(
            ("git", "worktree", "add", "--detach", str(checkout), base_commit),
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git worktree add failed")
        for relative in DEPENDENCY_PATHS:
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
        yield checkout
    finally:
        removed = True
        detail = None
        subprocess.run(
            ("git", "worktree", "unlock", str(checkout)),
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ("git", "worktree", "remove", "--force", str(checkout)),
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        removed = result.returncode == 0 or not checkout.exists()
        detail = result.stderr.strip() or None
        shutil.rmtree(temporary, ignore_errors=True)
        prune = subprocess.run(
            ("git", "worktree", "prune"),
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        removed = removed and prune.returncode == 0
        if prune.returncode != 0:
            detail = prune.stderr.strip() or "git worktree prune failed"
        listed = subprocess.run(
            ("git", "worktree", "list", "--porcelain"),
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        removed = (
            removed and listed.returncode == 0 and f"worktree {checkout}" not in listed.stdout
        )
        cleanup.update(
            {
                "status": "completed" if removed and not temporary.exists() else "failed",
                "detail": detail,
            }
        )


def _bounded_run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    timeout: float,
) -> tuple[int, bool]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            pass_fds=inherited_managed_fds(),
        )
        try:
            return process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            terminate_group(process.pid)
            process.wait()
            return 124, True
        finally:
            if process.poll() is None:
                terminate_group(process.pid)
                process.wait()


def _base_environment(run_dir: Path, checkout: Path) -> dict[str, str]:
    python_roots = (
        checkout,
        checkout / "sdks" / "python-client",
        checkout / "sdks" / "ui",
    )
    return strict_environment(
        run_dir,
        extra={
            "OMNIGENT_VERIFY_REPO_ROOT": str(checkout),
            "PYTHONPATH": os.pathsep.join(str(path) for path in python_roots),
            "PYTHONSAFEPATH": "1",
        },
    )


def _assert_base_import_identity(
    checkout: Path,
    run_dir: Path,
    timeout: float,
) -> None:
    interpreter = checkout / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        return
    script = """import importlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
packages = {
    "omnigent": root / "omnigent",
    "omnigent_client": root / "sdks/python-client/omnigent_client",
    "omnigent_ui_sdk": root / "sdks/ui/omnigent_ui_sdk",
}
for name, expected in packages.items():
    if not expected.exists():
        continue
    module = importlib.import_module(name)
    paths = list(getattr(module, "__path__", ()))
    if getattr(module, "__file__", None):
        paths.append(module.__file__)
    if not paths:
        raise RuntimeError(f"{name} has no filesystem identity")
    for value in paths:
        pathlib.Path(value).resolve().relative_to(root)
    print(name, *paths)
"""
    status, timed_out = _bounded_run(
        [str(interpreter), "-P", "-c", script, str(checkout)],
        cwd=checkout,
        env=_base_environment(run_dir, checkout),
        log_path=run_dir / "base-import-identity.log",
        timeout=min(timeout, 30),
    )
    if timed_out or status != 0:
        raise RuntimeError("Base Omnigent import did not resolve inside the exact-base worktree.")


def _authenticated_step_report(
    child_manifest: Path,
    lane: str,
    repo_root: Path,
    base_ref: str,
    changed_files: tuple[str, ...] | None = None,
    require_failure: bool = False,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest_raw = child_manifest.read_bytes()
    manifest = json.loads(manifest_raw)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("status") not in {"failed", "passed"}
        or manifest.get("run", {}).get("profile") != lane
    ):
        raise RuntimeError("Child manifest is not finalized authenticated lane evidence.")
    if require_failure and manifest.get("status") != "failed":
        raise RuntimeError("Candidate child manifest does not record a failed lane.")
    artifact = next(
        (
            item
            for item in manifest.get("artifacts", [])
            if isinstance(item, dict) and item.get("path") == "steps.json"
        ),
        None,
    )
    if not isinstance(artifact, dict) or not isinstance(artifact.get("sha256"), str):
        raise RuntimeError("Child manifest does not authenticate steps.json.")
    steps_path = child_manifest.parent / "steps.json"
    raw = steps_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
        raise RuntimeError("steps.json hash does not match the finalized child manifest.")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("profile") != lane
        or value.get("status") not in {"failed", "passed"}
    ):
        raise RuntimeError("Step report schema or profile is invalid.")
    steps = value.get("steps") if isinstance(value, dict) else None
    if not isinstance(steps, list):
        raise RuntimeError("Child produced no step report.")
    changed = (
        changed_files
        if changed_files is not None
        else (_changed_files(repo_root, base_ref) if lane == "quality-gates" else ())
    )
    resolved_run = child_manifest.parent.resolve()
    run_dir_arg = os.fspath(resolved_run)
    expected = profile_steps(
        lane,
        run_dir_arg,
        base_ref=base_ref,
        changed_files=changed,
        electron_e2e_tests=_electron_e2e_tests(repo_root) if lane == "desktop" else (),
        skill_root=str(Path(__file__).resolve().parent.parent),
    )
    seen: set[int] = set()
    failed = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("source_step_index"), int)
        and step.get("status") == "failed"
    ]
    for step in steps:
        if not isinstance(step, dict):
            raise RuntimeError("Step report contains a non-object step.")
        index = step.get("source_step_index")
        if not isinstance(index, int) or isinstance(index, bool) or index in seen:
            raise RuntimeError("Step report contains a missing or duplicate source index.")
        if index < 0 or index >= len(expected):
            raise RuntimeError("Step report contains an out-of-range source index.")
        seen.add(index)
        trusted = expected[index]
        expected_fields = {
            "name": trusted.name,
            "argv": portable_argv(
                trusted.argv,
                repo_root=repo_root,
                run_dir=child_manifest.parent,
                skill_root=Path(__file__).resolve().parent.parent,
            ),
            "environment": trusted.environment,
            "cwd": trusted.cwd,
            "isolated": trusted.isolated,
        }
        if any(step.get(key) != expected_value for key, expected_value in expected_fields.items()):
            raise RuntimeError(f"Step {index} does not match the trusted profile definition.")
        log = step.get("log")
        log_hash = step.get("log_sha256")
        if not isinstance(log, str) or not isinstance(log_hash, str):
            raise RuntimeError(f"Step {index} has no authenticated execution log.")
        log_path = (child_manifest.parent / log).resolve(strict=True)
        log_path.relative_to(child_manifest.parent.resolve(strict=True))
        if hashlib.sha256(log_path.read_bytes()).hexdigest() != log_hash:
            raise RuntimeError(f"Step {index} execution log hash is invalid.")
        if not isinstance(step.get("exit_status"), int) or step.get("status") not in {
            "passed",
            "failed",
        }:
            raise RuntimeError(f"Step {index} has no terminal execution result.")
    return value, failed


def _signature(step: dict[str, object], run_dir: Path, roots: tuple[Path, ...]) -> str:
    relative = step.get("log")
    if not isinstance(relative, str):
        raise RuntimeError("Failed step has no bounded log.")
    path = (run_dir / relative).resolve(strict=True)
    path.relative_to(run_dir.resolve(strict=True))
    text = _ANSI.sub("", path.read_text(encoding="utf-8", errors="replace"))
    for root in roots:
        text = text.replace(str(root), "<root>")
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compare_failure(
    repo_root: Path,
    skill_root: Path,
    run_dir: Path,
    lane: str,
    base_ref: str,
    child_manifest: Path,
    timeout: float,
    capability_file: Path,
) -> tuple[dict[str, object], int]:
    cleanup: dict[str, object] = {"status": "pending"}
    limitations: list[str] = []
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "classification": "could_not_compare",
        "lane": lane,
        "base_ref": base_ref,
        "base_commit": None,
        "failed_step_indices": [],
        "pr_signatures": {},
        "base_signatures": {},
        "base_manifest": None,
        "base_manifest_sha256": None,
        "dependency_identity": None,
        "cleanup": cleanup,
        "limitations": limitations,
    }
    output = run_dir / "baseline" / lane / "comparison.json"
    before: dict[str, object] | None = None
    try:
        before = repository_snapshot(repo_root, deep_dependencies=True)
        if lane not in COMPARABLE_LANES:
            raise RuntimeError(f"Lane {lane!r} does not support exact-base comparison.")
        base_commit = _merge_base(repo_root, base_ref)
        head = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
        report["base_commit"] = base_commit
        dependency_identity = _dependency_identity(repo_root, base_commit)
        report["dependency_identity"] = dependency_identity
        if dependency_identity.get("matched") is not True:
            raise RuntimeError(
                "Dependency inputs differ at the exact base; verification will not reuse "
                "candidate dependencies or install replacements."
            )
        if base_commit == head:
            local_changes = _changed_files(repo_root, "HEAD")
            if not any(
                path.startswith(
                    (
                        "omnigent/",
                        "sdks/python-client/",
                        "sdks/ui/",
                        "web/",
                        "tests/",
                    )
                )
                for path in local_changes
            ):
                raise RuntimeError(
                    "The requested base resolves to candidate HEAD without "
                    "uncommitted product changes."
                )
        _, failed = _authenticated_step_report(
            child_manifest,
            lane,
            repo_root,
            base_ref,
            require_failure=True,
        )
        if not failed:
            raise RuntimeError("The PR failure did not identify a safely repeatable failed step.")
        indices = sorted(
            index for step in failed if isinstance(index := step.get("source_step_index"), int)
        )
        if not indices or len(indices) != len(set(indices)):
            raise RuntimeError("The PR failed-step index set is empty or ambiguous.")
        report["failed_step_indices"] = indices
        child_run_dir = child_manifest.parent
        report["pr_signatures"] = {
            str(step["source_step_index"]): _signature(
                step,
                child_run_dir,
                (repo_root, child_run_dir),
            )
            for step in failed
        }

        baseline_root = run_dir / "baseline" / lane
        evidence_root = baseline_root / "evidence"
        with _base_worktree(repo_root, base_commit, cleanup) as checkout:
            env = _base_environment(baseline_root, checkout)
            _assert_base_import_identity(checkout, baseline_root, timeout)
            changed: tuple[str, ...] = ()
            if lane == "quality-gates":
                changed = _changed_files(repo_root, base_ref)
                for value in changed:
                    path = Path(value)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or value.startswith("-")
                        or not (checkout / path).exists()
                    ):
                        raise RuntimeError(
                            "A failed quality-gate step referenced an unsafe or PR-only path."
                        )
            request_path = baseline_root / "comparison-request.json"
            request = {
                "schema_version": 1,
                "profile": lane,
                "step_indices": indices,
                "changed_files": list(changed) if lane == "quality-gates" else None,
                "nonce": secrets.token_hex(32),
                "issuer_pid": os.getpid(),
            }
            payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            request["signature"] = hmac.digest(
                capability_file.read_bytes(),
                payload,
                "sha256",
            ).hex()
            _atomic_json(request_path, request)
            request_path.chmod(0o600)
            request_sha256 = hashlib.sha256(request_path.read_bytes()).hexdigest()
            env.update(
                {
                    "OMNIGENT_VERIFY_ADAPTER": "baseline-comparison",
                    "OMNIGENT_VERIFY_BASE_REF": "HEAD",
                    "OMNIGENT_VERIFY_EVIDENCE_DIR": str(evidence_root),
                    "OMNIGENT_VERIFY_REPO_ROOT": str(checkout),
                    "OMNIGENT_VERIFY_SKILL_ROOT": str(skill_root),
                    "OMNIGENT_VERIFY_STEP_TIMEOUT_SECONDS": str(timeout),
                }
            )
            exit_status, timed_out = _bounded_run(
                [
                    str(skill_root / "scripts" / "verify.sh"),
                    lane,
                    "--comparison-request",
                    str(request_path),
                    "--comparison-capability",
                    str(capability_file),
                ],
                cwd=checkout,
                env=env,
                log_path=baseline_root / "run.log",
                timeout=timeout + 15,
            )
            report["base_exit_status"] = exit_status
            report["timed_out"] = timed_out
            manifests = list(evidence_root.glob("*/manifest.json"))
            if len(manifests) != 1:
                raise RuntimeError("Base comparison produced no unique finalized manifest.")
            base_manifest = manifests[0]
            base_value = json.loads(base_manifest.read_text(encoding="utf-8"))
            report["base_manifest"] = base_manifest.relative_to(run_dir).as_posix()
            report["base_manifest_sha256"] = hashlib.sha256(base_manifest.read_bytes()).hexdigest()
            if timed_out or base_value.get("status") in {"blocked", "interrupted", "running"}:
                raise RuntimeError(
                    "Base comparison timed out or was blocked before the step completed."
                )
            base_report, base_failed = _authenticated_step_report(
                base_manifest,
                lane,
                checkout,
                "HEAD",
                changed,
            )
            if (
                base_report.get("comparison_request_sha256") != request_sha256
                or base_report.get("requested_step_indices") != indices
            ):
                raise RuntimeError("Base comparison did not execute exactly the requested steps.")
            base_steps = base_report.get("steps")
            if not isinstance(base_steps, list):
                raise RuntimeError("Base comparison did not return a step list.")
            base_indices = sorted(
                index
                for step in base_steps
                if isinstance(step, dict)
                and isinstance(index := step.get("source_step_index"), int)
            )
            if base_indices != indices:
                raise RuntimeError("Base comparison did not execute exactly the requested steps.")
            if exit_status == 0 and base_value.get("status") == "passed":
                report["classification"] = "pr_only_failure"
            else:
                if not base_failed:
                    raise RuntimeError("Base failed without an authenticated failed step.")
                report["base_signatures"] = {
                    str(step["source_step_index"]): _signature(
                        step,
                        base_manifest.parent,
                        (repo_root, checkout, base_manifest.parent),
                    )
                    for step in base_failed
                }
                report["classification"] = (
                    "baseline_reproduced"
                    if report["base_signatures"] == report["pr_signatures"]
                    else "baseline_differs"
                )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        limitations.append(f"Exact-base comparison unavailable: {exc}")
    finally:
        try:
            state_changes = (
                ["initial repository snapshot was unavailable"]
                if before is None
                else compare_snapshots(
                    before,
                    repository_snapshot(repo_root, deep_dependencies=True),
                )
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            state_changes = [f"final repository snapshot failed ({type(exc).__name__})"]
        report["repository_unchanged"] = not state_changes
        if state_changes:
            report["classification"] = "could_not_compare"
            limitations.append(
                "Comparison repository invariant failed: " + "; ".join(state_changes)
            )
        if cleanup["status"] == "pending":
            cleanup["status"] = "not_applicable"
        if (
            report["classification"] != "could_not_compare"
            and cleanup.get("status") != "completed"
        ):
            report["classification"] = "could_not_compare"
            limitations.append("Exact-base comparison cleanup was not confirmed complete.")
        _atomic_json(output, report)
    return report, 0 if report["classification"] != "could_not_compare" else 1


def _safe_positive_int(config: dict[str, object], key: str) -> int:
    value = config.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > 100_000:
        raise RuntimeError(f"Candidate benchmark config {key!r} is invalid.")
    return value


def _failure_count(journey: dict[str, object]) -> int:
    runs = journey.get("runs", [])
    if not isinstance(runs, list):
        return 0
    return sum(
        int(run.get("n_failures", 0))
        for run in runs
        if isinstance(run, dict) and isinstance(run.get("n_failures", 0), int)
    )


def _request_count(journey: dict[str, object]) -> int | None:
    runs = journey.get("runs", [])
    if not isinstance(runs, list):
        return None
    values: list[int] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        value = run.get("http_requests")
        if isinstance(value, int) and not isinstance(value, bool):
            values.append(value)
    return sum(values) if values else None


def _functional_success(journey: dict[str, object], configured_runs: object) -> bool:
    summary = journey.get("summary")
    runs = journey.get("runs")
    if (
        journey.get("skipped")
        or journey.get("skipped_reason")
        or not isinstance(summary, dict)
        or not isinstance(runs, list)
        or not runs
        or not isinstance(configured_runs, int)
        or isinstance(configured_runs, bool)
        or configured_runs <= 0
    ):
        return False
    runs_ok = summary.get("runs_ok")
    runs_total = summary.get("runs_total")
    if (
        not isinstance(runs_ok, int)
        or isinstance(runs_ok, bool)
        or not isinstance(runs_total, int)
        or isinstance(runs_total, bool)
        or runs_total <= 0
        or runs_ok != runs_total
        or len(runs) != runs_total
        or runs_total != configured_runs
    ):
        return False
    successful = sum(
        1
        for run in runs
        if isinstance(run, dict)
        and isinstance(run.get("n_failures"), int)
        and not isinstance(run.get("n_failures"), bool)
        and run["n_failures"] == 0
    )
    return successful == runs_ok == configured_runs


def _compare_benchmarks(
    baseline: dict[str, object],
    candidate: dict[str, object],
    thresholds: tuple[float, float] | None = (
        DEFAULT_MAX_P50_REGRESSION_PERCENT,
        DEFAULT_MAX_P99_REGRESSION_PERCENT,
    ),
) -> dict[str, object]:
    baseline_schema = baseline.get("schema_version")
    candidate_schema = candidate.get("schema_version")
    schema_match = (
        baseline_schema == candidate_schema
        and baseline_schema in SUPPORTED_BENCHMARK_SCHEMA_VERSIONS
    )
    config_match = baseline.get("config") == candidate.get("config")
    baseline_config = baseline.get("config", {})
    candidate_config = candidate.get("config", {})
    backend_match = (
        isinstance(baseline_config, dict)
        and isinstance(candidate_config, dict)
        and baseline_config.get("backend") == candidate_config.get("backend")
    )
    seed_match = (
        backend_match
        and isinstance(baseline_config, dict)
        and baseline_config.get("backend") == "sqlite"
    )
    host_match = baseline.get("host") == candidate.get("host")
    harness_match = baseline.get("harness") == candidate.get("harness")
    base_journeys = baseline.get("journeys")
    candidate_journeys = candidate.get("journeys")
    journeys_match = (
        isinstance(base_journeys, dict)
        and isinstance(candidate_journeys, dict)
        and set(base_journeys) == set(candidate_journeys)
    )
    rows: list[_BenchmarkRow] = []
    if isinstance(base_journeys, dict) and isinstance(candidate_journeys, dict):
        for name in sorted(set(base_journeys) & set(candidate_journeys)):
            base = base_journeys[name]
            current = candidate_journeys[name]
            if not isinstance(base, dict) or not isinstance(current, dict):
                continue
            base_summary = base.get("summary", {})
            current_summary = current.get("summary", {})
            rows.append(
                {
                    "journey": name,
                    "backend_match": base.get("backend") == current.get("backend"),
                    "kind_match": base.get("kind") == current.get("kind"),
                    "p50_ms": {
                        "base": base_summary.get("avg_p50_ms"),
                        "candidate": current_summary.get("avg_p50_ms"),
                    },
                    "p99_ms": {
                        "base": base_summary.get("avg_p99_ms"),
                        "candidate": current_summary.get("avg_p99_ms"),
                    },
                    "http_requests_per_op": {
                        "base": base_summary.get("avg_http_requests_per_op"),
                        "candidate": current_summary.get("avg_http_requests_per_op"),
                    },
                    "http_requests": {
                        "base": _request_count(base),
                        "candidate": _request_count(current),
                    },
                    "network_routes": {
                        "base": base_summary.get("network_routes"),
                        "candidate": current_summary.get("network_routes"),
                    },
                    "failures": {
                        "base": _failure_count(base),
                        "candidate": _failure_count(current),
                    },
                    "functional_success": {
                        "base": _functional_success(
                            base,
                            baseline_config.get("runs")
                            if isinstance(baseline_config, dict)
                            else None,
                        ),
                        "candidate": _functional_success(
                            current,
                            candidate_config.get("runs")
                            if isinstance(candidate_config, dict)
                            else None,
                        ),
                    },
                }
            )
    rows_match = bool(rows) and all(
        row["backend_match"]
        and row["kind_match"]
        and row["functional_success"]["base"]
        and row["functional_success"]["candidate"]
        for row in rows
    )
    matched = (
        schema_match
        and config_match
        and backend_match
        and seed_match
        and host_match
        and harness_match
        and journeys_match
        and rows_match
    )
    threshold_passed = False
    if matched and thresholds is not None:
        p50_limit, p99_limit = thresholds
        threshold_passed = all(
            isinstance(row["p50_ms"]["base"], (int, float))
            and isinstance(row["p50_ms"]["candidate"], (int, float))
            and isinstance(row["p99_ms"]["base"], (int, float))
            and isinstance(row["p99_ms"]["candidate"], (int, float))
            and row["p50_ms"]["candidate"] <= row["p50_ms"]["base"] * (1 + p50_limit / 100)
            and row["p99_ms"]["candidate"] <= row["p99_ms"]["base"] * (1 + p99_limit / 100)
            for row in rows
        )
    return {
        "matched": matched,
        "schema_identity": {
            "base": baseline_schema,
            "candidate": candidate_schema,
            "matched": schema_match,
            "supported": sorted(SUPPORTED_BENCHMARK_SCHEMA_VERSIONS),
        },
        "config_match": config_match,
        "backend_match": backend_match,
        "host_match": host_match,
        "harness_match": harness_match,
        "journey_set_match": journeys_match,
        "functional_success": rows_match,
        "commit_identity": {
            "base": baseline.get("git_sha"),
            "candidate": candidate.get("git_sha"),
        },
        "seed_identity": {
            "base": "fresh-disposable-sqlite",
            "candidate": "fresh-disposable-sqlite",
            "matched": seed_match,
        },
        "rows": rows,
        "thresholds": (
            {
                "max_p50_regression_percent": thresholds[0],
                "max_p99_regression_percent": thresholds[1],
            }
            if thresholds is not None
            else None
        ),
        "no_regression_claim": threshold_passed,
        "claim_reason": (
            "Explicit applicable latency thresholds passed."
            if threshold_passed
            else (
                "Matched comparison has no explicit applicable latency threshold."
                if matched and thresholds is None
                else (
                    "One or more explicit latency thresholds failed."
                    if matched
                    else (
                        "Settings, host, seed, backend, harness, or journey identity "
                        "did not match."
                    )
                )
            )
        ),
    }


def compare_performance(
    repo_root: Path,
    run_dir: Path,
    base_ref: str,
    candidate_path: Path,
    timeout: float,
    thresholds: tuple[float, float] = (
        DEFAULT_MAX_P50_REGRESSION_PERCENT,
        DEFAULT_MAX_P99_REGRESSION_PERCENT,
    ),
) -> tuple[dict[str, object], int]:
    benchmark_sha256: dict[str, object] = {"base": None, "candidate": None}
    cleanup: dict[str, object] = {"status": "pending"}
    limitations = ["Local SQLite is a development signal, not production latency evidence."]
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "base_ref": base_ref,
        "base_commit": None,
        "candidate_commit": None,
        "dependency_identity": None,
        "benchmark_sha256": benchmark_sha256,
        "baseline_benchmark": "baseline-benchmark.json",
        "candidate_benchmark": "benchmark.json",
        "comparison": None,
        "cleanup": cleanup,
        "limitations": limitations,
    }
    output = run_dir / "performance-comparison.json"
    before: dict[str, object] | None = None
    try:
        before = repository_snapshot(repo_root, deep_dependencies=True)
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        if not isinstance(candidate, dict):
            raise RuntimeError("Candidate benchmark is not a JSON object.")
        if candidate.get("schema_version") not in SUPPORTED_BENCHMARK_SCHEMA_VERSIONS:
            raise RuntimeError("Candidate benchmark schema is unsupported.")
        report["candidate_commit"] = candidate.get("git_sha")
        benchmark_sha256["candidate"] = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        config = candidate.get("config")
        journeys = candidate.get("journeys")
        if not isinstance(config, dict) or not isinstance(journeys, dict) or not journeys:
            raise RuntimeError("Candidate benchmark lacks config or journeys.")
        if config.get("backend") != "sqlite":
            raise RuntimeError(
                "Automatic base comparison currently supports disposable SQLite only."
            )
        base_commit = _merge_base(repo_root, base_ref)
        report["base_commit"] = base_commit
        candidate_head = _git(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
        if candidate_head.returncode != 0:
            raise RuntimeError("Candidate HEAD commit is unavailable.")
        candidate_head_sha = candidate_head.stdout.strip()
        dependency_identity = _dependency_identity(repo_root, base_commit)
        report["dependency_identity"] = dependency_identity
        if dependency_identity.get("matched") is not True:
            raise RuntimeError(
                "Dependency inputs differ at the exact base; a matched benchmark "
                "environment cannot be constructed without installation."
            )
        baseline_path = run_dir / "baseline-benchmark.json"
        with _base_worktree(repo_root, base_commit, cleanup) as checkout:
            _assert_base_import_identity(checkout, run_dir / "perf-baseline", timeout)
            argv = [
                "uv",
                "run",
                "--no-sync",
                "dev/benchmarks/omnigent/run.py",
                "--journeys",
                ",".join(name for name in journeys if isinstance(name, str)),
                "--iterations",
                str(_safe_positive_int(config, "iterations")),
                "--requests",
                str(_safe_positive_int(config, "requests")),
                "--concurrency",
                str(_safe_positive_int(config, "concurrency")),
                "--runs",
                str(_safe_positive_int(config, "runs")),
                "--warmup",
                str(_safe_positive_int(config, "warmup")),
                "--network-delay-ms",
                str(float(config.get("network_delay_ms", 0.0))),
                "--output",
                str(baseline_path),
            ]
            exit_status, timed_out = _bounded_run(
                argv,
                cwd=checkout,
                env=_base_environment(run_dir / "perf-baseline", checkout),
                log_path=run_dir / "baseline-benchmark.log",
                timeout=timeout,
            )
            if timed_out:
                raise RuntimeError("Baseline benchmark timed out.")
            if exit_status != 0 or not baseline_path.is_file():
                raise RuntimeError(f"Baseline benchmark failed with exit status {exit_status}.")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if not isinstance(baseline, dict):
            raise RuntimeError("Baseline benchmark is not a JSON object.")
        benchmark_sha256["base"] = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        comparison = _compare_benchmarks(baseline, candidate, thresholds)
        commit_identity: dict[str, dict[str, object]] = {
            "base": {
                "expected": base_commit,
                "observed": baseline.get("git_sha"),
                "matched": baseline.get("git_sha") == base_commit,
            },
            "candidate": {
                "expected": candidate_head_sha,
                "observed": candidate.get("git_sha"),
                "matched": candidate.get("git_sha") == candidate_head_sha,
            },
        }
        comparison["commit_identity"] = commit_identity
        if not all(value["matched"] is True for value in commit_identity.values()):
            comparison["matched"] = False
            comparison["claim_reason"] = (
                "Benchmark commit identity did not match the exact base or candidate checkout."
            )
        report["comparison"] = comparison
        report["status"] = (
            "passed" if comparison["matched"] and comparison["no_regression_claim"] else "blocked"
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        limitations.append(f"Performance comparison unavailable: {exc}")
    finally:
        try:
            state_changes = (
                ["initial repository snapshot was unavailable"]
                if before is None
                else compare_snapshots(
                    before,
                    repository_snapshot(repo_root, deep_dependencies=True),
                )
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            state_changes = [f"final repository snapshot failed ({type(exc).__name__})"]
        report["repository_unchanged"] = not state_changes
        if state_changes:
            report["status"] = "blocked"
            limitations.append(
                "Comparison repository invariant failed: " + "; ".join(state_changes)
            )
        if cleanup["status"] == "pending":
            cleanup["status"] = "not_applicable"
        if report["status"] == "passed" and cleanup.get("status") != "completed":
            report["status"] = "blocked"
            limitations.append("Performance comparison cleanup was not confirmed complete.")
        _atomic_json(output, report)
    return report, 0 if report.get("status") == "passed" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    baseline = subparsers.add_parser("baseline")
    baseline.add_argument("--repo-root", type=Path, required=True)
    baseline.add_argument("--skill-root", type=Path, required=True)
    baseline.add_argument("--run-dir", type=Path, required=True)
    baseline.add_argument("--lane", required=True)
    baseline.add_argument("--base-ref", required=True)
    baseline.add_argument("--child-manifest", type=Path, required=True)
    baseline.add_argument("--timeout", type=float, default=600)
    baseline.add_argument("--capability-file", type=Path, required=True)

    perf = subparsers.add_parser("perf")
    perf.add_argument("--repo-root", type=Path, required=True)
    perf.add_argument("--run-dir", type=Path, required=True)
    perf.add_argument("--base-ref", required=True)
    perf.add_argument("--candidate", type=Path, required=True)
    perf.add_argument("--timeout", type=float, default=900)
    perf.add_argument(
        "--max-p50-regression-percent",
        type=float,
        default=DEFAULT_MAX_P50_REGRESSION_PERCENT,
    )
    perf.add_argument(
        "--max-p99-regression-percent",
        type=float,
        default=DEFAULT_MAX_P99_REGRESSION_PERCENT,
    )
    return parser


def main() -> None:
    def interrupt(signum: int, _frame: object) -> None:
        raise ComparisonInterrupted(signal.Signals(signum).name)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)
    args = _parser().parse_args()
    if args.action == "baseline":
        _, status = compare_failure(
            args.repo_root.resolve(),
            args.skill_root.resolve(),
            args.run_dir.resolve(),
            args.lane,
            args.base_ref,
            args.child_manifest.resolve(),
            args.timeout,
            args.capability_file.resolve(),
        )
    else:
        _, status = compare_performance(
            args.repo_root.resolve(),
            args.run_dir.resolve(),
            args.base_ref,
            args.candidate.resolve(),
            args.timeout,
            (
                args.max_p50_regression_percent,
                args.max_p99_regression_percent,
            ),
        )
    raise SystemExit(status)


if __name__ == "__main__":
    main()
