#!/usr/bin/env python3
"""Run read-only source and Bazel checks against a sibling Universe checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SCHEMA_VERSION = 1
BAZEL_FLAGS = (
    "--tool_tag=ai-agent",
    "--test_output=errors",
    "--noshow_progress",
    "--noshow_loading_progress",
)
VENDORED_INVARIANT_TARGET = "//agentbricks/mas/python:test_vendored_invariants"
HOST_PATCH_TARGETS = {
    "//agentbricks/mas/python:test_host_slice_key_gate_patch",
    "//agentbricks/mas/python:test_managed_host_edge_bearer_patch",
}
HOST_RUNTIME_TARGETS = {
    "//agentbricks/mas/python:test_arca_sandbox_launcher",
    "//agentbricks/mas/python:test_databricks_host_store",
    "//agentbricks/mas/python:test_databricks_host_store_managed",
    "//agentbricks/mas/python:test_lakebox_sandbox_launcher",
}
HOST_ID_PATHS = {
    "omnigent/cli.py",
    "omnigent/host/connect.py",
    "omnigent/host/identity.py",
    "omnigent/server/routes/host_tunnel.py",
}
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _resolve_commit(repo_root: Path, ref: str) -> tuple[str | None, str | None]:
    result = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo_root)
    value = result.stdout.strip().lower()
    if result.returncode != 0 or not _SHA.fullmatch(value):
        detail = result.stderr.strip() or f"{ref!r} did not resolve to a commit"
        return None, detail
    return value, None


def _read_pin(path: Path) -> tuple[str | None, str | None]:
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        return None, f"{path.name} could not be read ({type(exc).__name__})"
    if not _SHA.fullmatch(value):
        return None, f"{path.name} does not contain a full commit SHA"
    return value, None


def discover_universe(repo_root: Path, explicit: str | None) -> tuple[Path | None, str | None]:
    """Find a Universe checkout without searching outside the OSS checkout's parent."""
    candidate = Path(explicit).expanduser() if explicit else repo_root.parent / "universe"
    try:
        candidate = candidate.resolve(strict=True)
    except OSError:
        return None, f"Universe checkout was not found at {candidate}"
    pin = candidate / "agentbricks" / "mas" / "third_party" / "sync" / "UPSTREAM_REF"
    if not pin.is_file():
        return None, f"{candidate.name} is not a MAS Universe checkout"
    return candidate, None


def _changed_files(
    repo_root: Path,
    base: str,
    requested: str,
    pathspec: str | None = None,
) -> tuple[list[str], str | None]:
    argv = [
        "git",
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies-harder",
        "--diff-filter=ACDMRTUXB",
        base,
        requested,
    ]
    if pathspec:
        argv.extend(("--", pathspec))
    result = subprocess.run(argv, cwd=repo_root, check=False, capture_output=True)
    if result.returncode != 0:
        return (
            [],
            result.stderr.decode("utf-8", "replace").strip() or "changed-file discovery failed",
        )
    fields = [os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]
    paths: list[str] = []
    cursor = 0
    while cursor < len(fields):
        status = fields[cursor]
        cursor += 1
        path_count = 2 if status[:1] in {"R", "C"} else 1
        if cursor + path_count > len(fields):
            return [], "git returned malformed name-status output"
        paths.extend(fields[cursor : cursor + path_count])
        cursor += path_count
    return sorted(dict.fromkeys(paths)), None


def _merge_base(repo_root: Path, base: str, requested: str) -> tuple[str | None, str | None]:
    result = subprocess.run(
        ["git", "merge-base", "--all", base, requested],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    values = result.stdout.splitlines()
    if result.returncode != 0 or len(values) != 1:
        detail = result.stderr.strip()
        return None, detail or "base and requested refs have no unique merge base"
    return values[0], None


def _patch_for_path(patches_root: Path, changed_path: str) -> Path | None:
    mappings = (
        ("omnigent/", "omnigent/"),
        ("sdks/python-client/omnigent_client/", "omnigent_client/omnigent_client/"),
        ("sdks/ui/omnigent_ui_sdk/", "omnigent_ui_sdk/omnigent_ui_sdk/"),
        ("web/", "omnigent_ui/"),
    )
    for source_prefix, patch_prefix in mappings:
        if changed_path.startswith(source_prefix):
            relative = changed_path.removeprefix(source_prefix)
            candidate = patches_root / f"{patch_prefix}{relative}.patch"
            return candidate if candidate.is_file() else None
    return None


def _required_sync_pins(changed_files: list[str]) -> list[str]:
    required = []
    if any(path.startswith(("omnigent/", "sdks/python-client/")) for path in changed_files):
        required.append("python")
    if any(path.startswith(("web/", "sdks/ui/")) for path in changed_files):
        required.append("ui")
    return required


def relevant_patches(
    patches_root: Path,
    python_changes: list[str],
    ui_changes: list[str],
) -> list[Path]:
    patches = {
        patch
        for path in [*python_changes, *ui_changes]
        if (patch := _patch_for_path(patches_root, path)) is not None
    }
    return sorted(patches)


def check_patch_apply(
    repo_root: Path,
    requested_commit: str,
    patches: list[Path],
    patches_root: Path,
) -> dict[str, object]:
    """Check patches against an archived commit, never the live Universe tree."""
    if not patches:
        return {"status": "not_applicable", "checks": []}

    checks: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="omnigent-universe-apply-") as temporary:
        tree = Path(temporary) / "tree"
        tree.mkdir()
        archive = Path(temporary) / "upstream.tar"
        with archive.open("wb") as handle:
            exported = subprocess.run(
                ["git", "archive", "--format=tar", requested_commit],
                cwd=repo_root,
                check=False,
                stdout=handle,
                stderr=subprocess.PIPE,
            )
        if exported.returncode != 0:
            return {
                "status": "unavailable",
                "checks": [],
                "reason": exported.stderr.decode("utf-8", "replace").strip(),
            }
        with tarfile.open(archive) as tar:
            tar.extractall(tree, filter="data")

        for patch in patches:
            result = _run(["git", "apply", "--check", "-p1", str(patch)], cwd=tree)
            checks.append(
                {
                    "patch": patch.relative_to(patches_root).as_posix(),
                    "status": "passed" if result.returncode == 0 else "failed",
                    "exit_status": result.returncode,
                    "detail": result.stderr.strip() or None,
                }
            )
    return {
        "status": "passed" if all(item["status"] == "passed" for item in checks) else "failed",
        "checks": checks,
    }


def classify_darwin_blocker(text: str) -> str | None:
    lowered = text.lower()
    if "jemalloc" in lowered and "features.h" in lowered:
        return "darwin_jemalloc_features_h"
    if "_psutil_osx" in lowered:
        return "darwin_psutil_extension"
    return None


def parse_bazel_test_result(
    *,
    exit_status: int,
    xml_path: Path,
    output: str,
    system: str,
) -> dict[str, object]:
    """Require both a zero Bazel exit and a non-empty passing test.xml."""
    blocker = classify_darwin_blocker(output) if system == "Darwin" else None
    if not xml_path.is_file():
        return {
            "status": "blocked" if blocker else "failed",
            "exit_status": exit_status,
            "tests": 0,
            "failures": 0,
            "errors": 0,
            "blocker": blocker,
            "reason": "Bazel produced no test.xml; zero tests were proven.",
        }
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError) as exc:
        return {
            "status": "failed",
            "exit_status": exit_status,
            "tests": 0,
            "failures": 0,
            "errors": 1,
            "blocker": blocker,
            "reason": f"test.xml could not be parsed ({type(exc).__name__}).",
        }

    cases = list(root.iter("testcase"))
    tests = len(cases)
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    xml_text = ET.tostring(root, encoding="unicode")
    blocker = blocker or (classify_darwin_blocker(xml_text) if system == "Darwin" else None)
    executed = tests - skipped
    if exit_status == 0 and executed > 0 and failures == 0 and errors == 0:
        status = "passed"
        reason = None
    elif blocker:
        status = "blocked"
        reason = "A known Darwin build or collection blocker prevented the test."
    elif tests == 0:
        status = "failed"
        reason = "test.xml recorded zero tests."
    elif executed == 0:
        status = "failed"
        reason = "test.xml recorded no non-skipped tests."
    else:
        status = "failed"
        reason = "Bazel or the recorded test cases failed."
    return {
        "status": status,
        "exit_status": exit_status,
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "blocker": blocker,
        "reason": reason,
    }


def _target_name(target: str) -> str:
    return target.rsplit(":", 1)[-1]


def _target_xml(universe_root: Path, target: str) -> Path:
    package, name = target.removeprefix("//").split(":", 1)
    return universe_root / "bazel-testlogs" / package / name / "test.xml"


def _run_bazel_test(
    universe_root: Path,
    target: str,
    output_dir: Path,
    system: str,
) -> dict[str, object]:
    log_path = output_dir / f"{_target_name(target)}.log"
    env = {**os.environ, "TMPDIR": "/tmp"}
    result = _run(["bazel", "test", target, *BAZEL_FLAGS], cwd=universe_root, env=env)
    output = result.stdout + result.stderr
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    parsed = parse_bazel_test_result(
        exit_status=result.returncode,
        xml_path=_target_xml(universe_root, target),
        output=output,
        system=system,
    )
    parsed.update(
        {
            "target": target,
            "log": f"universe-bazel/{log_path.name}",
            "test_xml": (f"bazel-testlogs/{target.removeprefix('//').replace(':', '/')}/test.xml"),
        }
    )
    return parsed


def _git_status_digest(repo_root: Path) -> tuple[str | None, str | None]:
    result = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_root,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or "git status failed"
    return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(), None


def _dirty_paths(repo_root: Path, *, include_untracked: bool) -> tuple[list[str], str | None]:
    result = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all" if include_untracked else "--untracked-files=no",
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return [], result.stderr.decode("utf-8", "replace").strip() or "git status failed"
    entries = [raw for raw in result.stdout.split(b"\0") if raw]
    paths = [os.fsdecode(entry[3:]) for entry in entries if len(entry) >= 4]
    return paths, None


def _blocked_target(target: str, blocker: str) -> dict[str, object]:
    return {
        "target": target,
        "status": "blocked",
        "exit_status": None,
        "tests": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "blocker": blocker,
        "reason": "Run this target on the supported Linux/Bazel lane.",
        "log": None,
        "test_xml": None,
    }


def run_preflight(
    repo_root: Path,
    run_dir: Path,
    oss_ref: str,
    base_ref: str,
    universe_override: str | None,
    system: str,
) -> tuple[dict[str, object], int]:
    limitations: list[str] = []
    requested_oss: dict[str, object] = {
        "ref": oss_ref,
        "commit": None,
        "base_ref": base_ref,
        "base_commit": None,
        "merge_base": None,
    }
    source_patch_apply: dict[str, object] = {"status": "unavailable", "checks": []}
    bazel_results: list[dict[str, object]] = []
    bazel: dict[str, object] = {"tests": bazel_results}
    runtime: dict[str, object] = {"status": "unverified", "targets": []}
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
        "requested_oss": requested_oss,
        "pins": {"python": None, "ui": None},
        "sync": {"status": "unknown"},
        "source_patch_apply": source_patch_apply,
        "bazel": bazel,
        "runtime_compatibility": runtime,
        "checkout_unchanged": None,
        "limitations": limitations,
    }
    oss_tracked_dirty, tracked_dirty_error = _dirty_paths(repo_root, include_untracked=False)
    oss_dirty, dirty_error = _dirty_paths(repo_root, include_untracked=True)
    if tracked_dirty_error or dirty_error or oss_tracked_dirty or oss_dirty:
        limitations.append(
            "OSS checkout has tracked or non-ignored untracked changes; commit, stash, "
            "or ignore generated outputs before Universe verification."
        )
        return report, 2
    requested, error = _resolve_commit(repo_root, oss_ref)
    if error or requested is None:
        limitations.append(f"Requested OSS ref could not be resolved: {error}")
        return report, 2
    requested_oss["commit"] = requested
    base, error = _resolve_commit(repo_root, base_ref)
    if error or base is None:
        limitations.append(f"OSS base ref could not be resolved: {error}")
        return report, 2
    requested_oss["base_commit"] = base
    merge_base, error = _merge_base(repo_root, base, requested)
    if error or merge_base is None:
        limitations.append(f"OSS refs have no unique merge base: {error}")
        return report, 2
    requested_oss["merge_base"] = merge_base

    universe_root, error = discover_universe(repo_root, universe_override)
    if error or universe_root is None:
        limitations.append(error or "Universe checkout discovery failed.")
        report["status"] = "unavailable"
        return report, 2
    report["checkout"] = universe_root.name
    universe_dirty, dirty_error = _dirty_paths(universe_root, include_untracked=True)
    if dirty_error or universe_dirty:
        limitations.append(
            "Universe checkout has tracked or non-ignored untracked changes; "
            "start from a clean checkout."
        )
        report["status"] = "unavailable"
        return report, 2
    all_changes, change_error = _changed_files(repo_root, merge_base, requested)
    python_changes = [
        path for path in all_changes if path.startswith(("omnigent/", "sdks/python-client/"))
    ]
    ui_changes = [path for path in all_changes if path.startswith(("web/", "sdks/ui/"))]
    if not python_changes and not ui_changes and change_error is None:
        report["changed_files"] = []
        report["sync"] = {
            "status": "not_applicable",
            "requested_commit": requested,
            "required_pins": [],
            "mismatched_pins": [],
            "vendored_python_commit": None,
            "vendored_ui_commit": None,
        }
        source_patch_apply.update({"status": "not_applicable", "checks": []})
        runtime.update({"status": "not_applicable", "targets": []})
        bazel_results.clear()
        report["checkout_unchanged"] = True
        report["status"] = "not_applicable"
        limitations.append(
            "The requested OSS diff changes only verification or deployment support; "
            "Universe Python and UI pins are not applicable."
        )
        return report, 0
    sync_root = universe_root / "agentbricks" / "mas" / "third_party" / "sync"
    python_pin, pin_error = _read_pin(sync_root / "UPSTREAM_REF")
    ui_pin, ui_error = _read_pin(sync_root / "UI_UPSTREAM_REF")
    report["pins"] = {"python": python_pin, "ui": ui_pin}
    if pin_error or ui_error:
        limitations.extend(error for error in (pin_error, ui_error) if error)
        report["status"] = "unavailable"
        return report, 2
    assert requested is not None and python_pin is not None and ui_pin is not None

    if change_error:
        limitations.append(f"Source change discovery failed: {change_error}")
    else:
        report["changed_files"] = sorted(dict.fromkeys([*python_changes, *ui_changes]))
        patches_root = sync_root / "local-patches"
        patches = relevant_patches(patches_root, python_changes, ui_changes)
        source_patch_apply = check_patch_apply(
            repo_root,
            requested,
            patches,
            patches_root,
        )
        report["source_patch_apply"] = source_patch_apply
        if source_patch_apply["status"] in {"failed", "unavailable"}:
            limitations.append(
                "One or more affected Universe patches could not be applied to "
                "the requested OSS commit."
            )

    pin_values = {"python": python_pin, "ui": ui_pin}
    required_pins = [
        (name, pin_values[name]) for name in _required_sync_pins([*python_changes, *ui_changes])
    ]
    mismatched_pins = [name for name, pin in required_pins if pin != requested]
    sync_status = "synced" if not mismatched_pins else "not_synced"
    report["sync"] = {
        "status": sync_status,
        "requested_commit": requested,
        "required_pins": [name for name, _pin in required_pins],
        "mismatched_pins": mismatched_pins,
        "vendored_python_commit": python_pin,
        "vendored_ui_commit": ui_pin,
    }
    if sync_status == "not_synced":
        limitations.append(
            "Universe pins required by the changed Python/UI surfaces are not synced "
            "to the requested OSS commit."
        )

    before_status, status_error = _git_status_digest(universe_root)
    if status_error:
        limitations.append(f"Universe checkout state could not be read: {status_error}")

    changed_value = report.get("changed_files", [])
    changed = set(changed_value) if isinstance(changed_value, list) else set()
    patch_targets = {VENDORED_INVARIANT_TARGET}
    runtime_targets: set[str] = set()
    if changed & HOST_ID_PATHS:
        patch_targets.update(HOST_PATCH_TARGETS)
        runtime_targets.update(HOST_RUNTIME_TARGETS)

    output_dir = run_dir / "universe-bazel"
    for target in sorted(patch_targets):
        if (
            system == "Darwin"
            and target == "//agentbricks/mas/python:test_managed_host_edge_bearer_patch"
        ):
            bazel_results.append(_blocked_target(target, "darwin_psutil_extension"))
            continue
        bazel_results.append(_run_bazel_test(universe_root, target, output_dir, system))
    for result in bazel_results:
        if result["status"] != "passed":
            limitations.append(
                f"{result['target']} did not pass: {result.get('reason') or result['status']}"
            )

    if sync_status == "not_synced":
        runtime["status"] = "not_synced"
        runtime["targets"] = sorted(runtime_targets)
    elif not runtime_targets:
        runtime["status"] = "unverified"
        limitations.append("No focused downstream runtime target is mapped for these OSS paths.")
    elif system == "Darwin":
        runtime["status"] = "blocked"
        runtime["targets"] = [
            _blocked_target(target, "darwin_jemalloc_features_h")
            for target in sorted(runtime_targets)
        ]
        limitations.append(
            "Darwin MAS runtime targets are blocked before test execution by the known "
            "jemalloc/features.h toolchain failure. Run this profile on Linux."
        )
    else:
        runtime_results = [
            _run_bazel_test(universe_root, target, output_dir, system)
            for target in sorted(runtime_targets)
        ]
        runtime["targets"] = runtime_results
        runtime["status"] = (
            "passed" if all(item["status"] == "passed" for item in runtime_results) else "failed"
        )
        for result in runtime_results:
            if result["status"] != "passed":
                limitations.append(
                    f"{result['target']} did not pass: {result.get('reason') or result['status']}"
                )

    after_status, after_error = _git_status_digest(universe_root)
    if after_error:
        limitations.append(f"Final Universe checkout state could not be read: {after_error}")
    report["checkout_unchanged"] = (
        before_status is not None and after_status is not None and before_status == after_status
    )
    if report["checkout_unchanged"] is False:
        limitations.append("Universe checkout status changed during verification.")

    source_status = source_patch_apply["status"]
    bazel_failed = any(item["status"] == "failed" for item in bazel_results)
    if source_status in {"failed", "unavailable"}:
        report["status"] = "source_incompatible"
    elif bazel_failed or report["checkout_unchanged"] is False:
        report["status"] = "failed"
    elif sync_status == "not_synced":
        report["status"] = "not_synced"
    elif runtime["status"] == "blocked":
        report["status"] = "blocked"
    elif runtime["status"] == "passed":
        report["status"] = "passed"
    else:
        report["status"] = "unverified"
    return report, 0 if report.get("status") == "passed" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--oss-ref", default="HEAD")
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument("--universe-root")
    return parser


def main() -> None:
    args = _parser().parse_args()
    run_dir = args.run_dir.resolve()
    try:
        report, exit_status = run_preflight(
            args.repo_root.resolve(),
            run_dir,
            args.oss_ref,
            args.base_ref,
            args.universe_root or os.environ.get("UNIVERSE_ROOT"),
            platform.system(),
        )
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "source_patch_apply": {"status": "unavailable", "checks": []},
            "runtime_compatibility": {"status": "unverified", "targets": []},
            "limitations": [f"Universe preflight failed ({type(exc).__name__}): {exc}"],
        }
        exit_status = 2
    _atomic_json(run_dir / "universe.json", report)
    source = report.get("source_patch_apply")
    runtime = report.get("runtime_compatibility")
    if not isinstance(source, dict) or not isinstance(runtime, dict):
        raise RuntimeError("Universe report has an invalid schema")
    print(
        "universe: "
        f"status={report['status']} "
        f"source={source['status']} "
        f"runtime={runtime['status']}"
    )
    raise SystemExit(exit_status)


if __name__ == "__main__":
    main()
