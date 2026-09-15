from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELPER = _REPO_ROOT / ".agents" / "skills" / "verify-omnigent" / "scripts" / "comparison.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_comparison", _HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.com",
        "commit",
        "-qm",
        message,
    )


def _comparison_repo(tmp_path: Path, base_behavior: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "behavior.txt").write_text(base_behavior + "\n", encoding="utf-8")
    _commit(repo, "base")
    _git(repo, "switch", "-q", "-c", "pr")
    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _commit(repo, "candidate")
    (repo / "candidate.txt").write_text("dirty candidate\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("preserve me\n", encoding="utf-8")
    return repo


def _fake_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "skill"
    script = skill / "scripts" / "verify.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

behavior = Path("behavior.txt").read_text(encoding="utf-8").strip()
if behavior == "timeout":
    time.sleep(60)
if behavior == "no-manifest":
    raise SystemExit(2)
root = Path(os.environ["OMNIGENT_VERIFY_EVIDENCE_DIR"]) / "base-run"
logs = root / "step-logs"
logs.mkdir(parents=True)
passed = behavior == "pass"
log = logs / "00.log"
log.write_text(("base passed" if passed else behavior) + "\\n")
request_path = Path(sys.argv[sys.argv.index("--comparison-request") + 1])
request_raw = request_path.read_bytes()
request = json.loads(request_raw)
request_path.unlink()
step = {
    "source_step_index": 0,
    "name": "Server API and app integration",
    "argv": [
        "uv", "run", "--no-sync", "pytest", "tests/server/test_app.py",
        "tests/server/integration/test_app.py", "-q",
        "--junitxml=<run-dir>/junit.xml",
    ],
    "environment": {},
    "cwd": ".",
    "isolated": False,
    "status": "passed" if passed else "failed",
    "exit_status": 0 if passed else 1,
    "log": "step-logs/00.log",
    "log_sha256": __import__("hashlib").sha256(log.read_bytes()).hexdigest(),
}
steps_path = root / "steps.json"
steps_path.write_text(json.dumps({
    "schema_version": 1,
    "profile": sys.argv[1],
    "status": "passed" if passed else "failed",
    "comparison_request_sha256": __import__("hashlib").sha256(request_raw).hexdigest(),
    "requested_step_indices": request["step_indices"],
    "steps": [step],
}) + "\\n")
(root / "manifest.json").write_text(json.dumps({
    "schema_version": 1,
    "status": "passed" if passed else "failed",
    "exit_status": 0 if passed else 1,
    "run": {"profile": sys.argv[1]},
    "cleanup": {"status": "not_applicable", "details": []},
    "artifacts": [{
        "path": "steps.json",
        "sha256": __import__("hashlib").sha256(steps_path.read_bytes()).hexdigest(),
    }],
}) + "\\n")
raise SystemExit(0 if passed else 1)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return skill


def _pr_child(tmp_path: Path, signature: str) -> Path:
    child = tmp_path / "pr-child"
    logs = child / "step-logs"
    logs.mkdir(parents=True)
    (logs / "00.log").write_text(signature + "\n", encoding="utf-8")
    log_hash = hashlib.sha256((logs / "00.log").read_bytes()).hexdigest()
    steps_path = child / "steps.json"
    steps_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "server",
                "status": "failed",
                "steps": [
                    {
                        "source_step_index": 0,
                        "name": "Server API and app integration",
                        "argv": [
                            "uv",
                            "run",
                            "--no-sync",
                            "pytest",
                            "tests/server/test_app.py",
                            "tests/server/integration/test_app.py",
                            "-q",
                            "--junitxml=<run-dir>/junit.xml",
                        ],
                        "environment": {},
                        "cwd": ".",
                        "isolated": False,
                        "status": "failed",
                        "exit_status": 1,
                        "log": "step-logs/00.log",
                        "log_sha256": log_hash,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = child / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "failed",
                "exit_status": 1,
                "run": {"profile": "server"},
                "artifacts": [
                    {
                        "path": "steps.json",
                        "sha256": hashlib.sha256(steps_path.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _capability(run_dir: Path) -> Path:
    path = run_dir / "environment" / "comparison-capability"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(32))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    ("base_behavior", "candidate_signature", "classification"),
    [
        ("pass", "candidate failure", "pr_only_failure"),
        ("same failure", "same failure", "baseline_reproduced"),
        ("different failure", "candidate failure", "baseline_differs"),
    ],
)
def test_exact_base_failure_classification_and_cleanup(
    tmp_path: Path,
    base_behavior: str,
    candidate_signature: str,
    classification: str,
) -> None:
    helper = _load_helper()
    repo = _comparison_repo(tmp_path, base_behavior)
    skill = _fake_skill(tmp_path)
    child = _pr_child(tmp_path, candidate_signature)
    run_dir = tmp_path / "run"
    capability = _capability(run_dir)
    status_before = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    worktrees_before = _git(repo, "worktree", "list", "--porcelain")

    report, exit_status = helper.compare_failure(
        repo,
        skill,
        run_dir,
        "server",
        "main",
        child,
        2,
        capability,
    )

    assert exit_status == 0
    assert report["classification"] == classification
    assert report["cleanup"]["status"] == "completed"
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert _git(repo, "worktree", "list", "--porcelain") == worktrees_before


def test_head_baseline_classifies_uncommitted_product_failure(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "behavior.txt").write_text("pass\n", encoding="utf-8")
    (repo / "omnigent").mkdir()
    (repo / "omnigent/change.py").write_text("BASE = True\n", encoding="utf-8")
    _commit(repo, "base")
    (repo / "behavior.txt").write_text("candidate failure\n", encoding="utf-8")
    (repo / "omnigent/change.py").write_text("BASE = False\n", encoding="utf-8")
    skill = _fake_skill(tmp_path)
    child = _pr_child(tmp_path, "candidate failure")
    run_dir = tmp_path / "run"

    report, exit_status = helper.compare_failure(
        repo,
        skill,
        run_dir,
        "server",
        "HEAD",
        child,
        2,
        _capability(run_dir),
    )

    assert exit_status == 0
    assert report["classification"] == "pr_only_failure"
    assert report["base_commit"] == _git(repo, "rev-parse", "HEAD")
    assert report["cleanup"]["status"] == "completed"


@pytest.mark.parametrize("base_behavior", ["timeout", "no-manifest"])
def test_exact_base_comparison_failure_is_bounded_and_cleaned(
    tmp_path: Path,
    base_behavior: str,
) -> None:
    helper = _load_helper()
    repo = _comparison_repo(tmp_path, base_behavior)
    skill = _fake_skill(tmp_path)
    child = _pr_child(tmp_path, "candidate failure")
    run_dir = tmp_path / "run"
    capability = _capability(run_dir)

    report, exit_status = helper.compare_failure(
        repo,
        skill,
        run_dir,
        "server",
        "main",
        child,
        0.1,
        capability,
    )

    assert exit_status != 0
    assert report["classification"] == "could_not_compare"
    assert report["cleanup"]["status"] == "completed"
    worktrees = _git(repo, "worktree", "list").splitlines()
    assert not any("omnigent-verify-base-" in line for line in worktrees)


def test_successful_classification_is_rejected_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    repo = _comparison_repo(tmp_path, "pass")
    skill = _fake_skill(tmp_path)
    child = _pr_child(tmp_path, "candidate failure")
    run_dir = tmp_path / "run"
    original = helper._base_worktree

    @contextlib.contextmanager
    def failed_cleanup(
        repo_root: Path,
        base_commit: str,
        cleanup: dict[str, object],
    ) -> Iterator[Path]:
        with original(repo_root, base_commit, cleanup) as checkout:
            yield checkout
        cleanup["status"] = "failed"

    monkeypatch.setattr(helper, "_base_worktree", failed_cleanup)

    report, exit_status = helper.compare_failure(
        repo,
        skill,
        run_dir,
        "server",
        "main",
        child,
        2,
        _capability(run_dir),
    )

    assert exit_status != 0
    assert report["classification"] == "could_not_compare"
    assert report["cleanup"]["status"] == "failed"
    assert "omnigent-verify-base-" not in _git(repo, "worktree", "list")


def test_tampered_step_report_cannot_forge_baseline_classification(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = _comparison_repo(tmp_path, "pass")
    skill = _fake_skill(tmp_path)
    child = _pr_child(tmp_path, "candidate failure")
    steps = child.parent / "steps.json"
    value = json.loads(steps.read_text(encoding="utf-8"))
    value["steps"][0]["source_step_index"] = 999
    steps.write_text(json.dumps(value), encoding="utf-8")
    run_dir = tmp_path / "run"

    report, exit_status = helper.compare_failure(
        repo,
        skill,
        run_dir,
        "server",
        "main",
        child,
        2,
        _capability(run_dir),
    )

    assert exit_status != 0
    assert report["classification"] == "could_not_compare"
    assert "hash does not match" in " ".join(report["limitations"])


@pytest.mark.parametrize("lane", ["quality-gates", "web-ui", "desktop"])
def test_unsupported_js_comparisons_are_never_misclassified(
    tmp_path: Path,
    lane: str,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"

    report, status = helper.compare_failure(
        _REPO_ROOT,
        _REPO_ROOT / ".agents/skills/verify-omnigent",
        run_dir,
        lane,
        "HEAD",
        tmp_path / "unused-manifest.json",
        1,
        _capability(run_dir),
    )

    assert status != 0
    assert report["classification"] == "could_not_compare"
    assert "does not support exact-base comparison" in " ".join(report["limitations"])


@pytest.mark.parametrize("tamper", ["out-of-range", "command", "empty"])
def test_rehashed_manifest_tampering_still_cannot_forge_classification(
    tmp_path: Path,
    tamper: str,
) -> None:
    helper = _load_helper()
    repo = _comparison_repo(tmp_path, "pass")
    skill = _fake_skill(tmp_path)
    child = _pr_child(tmp_path, "candidate failure")
    steps_path = child.parent / "steps.json"
    steps = json.loads(steps_path.read_text(encoding="utf-8"))
    if tamper == "out-of-range":
        steps["steps"][0]["source_step_index"] = 999
    elif tamper == "command":
        steps["steps"][0]["argv"] = ["attacker-controlled"]
    else:
        steps["steps"] = []
    steps_path.write_text(json.dumps(steps), encoding="utf-8")
    manifest = json.loads(child.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["sha256"] = hashlib.sha256(steps_path.read_bytes()).hexdigest()
    child.write_text(json.dumps(manifest), encoding="utf-8")
    run_dir = tmp_path / "run"

    report, status = helper.compare_failure(
        repo,
        skill,
        run_dir,
        "server",
        "main",
        child,
        2,
        _capability(run_dir),
    )

    assert status != 0
    assert report["classification"] == "could_not_compare"


def test_base_worktree_never_links_primary_node_modules(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = tmp_path / "dependency-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    metadata = repo / "node_modules" / ".modules.yaml"
    metadata.parent.mkdir()
    metadata.write_bytes(b'{"virtualStoreDir":".pnpm"}\n')
    primary_link = repo / "node_modules" / "package"
    primary_link.symlink_to("../store/package")
    before_metadata = metadata.read_bytes()
    before_link = os.readlink(primary_link)
    cleanup: dict[str, object] = {}

    with helper._base_worktree(repo, _git(repo, "rev-parse", "HEAD"), cleanup) as checkout:
        assert not (checkout / "node_modules").exists()

    assert metadata.read_bytes() == before_metadata
    assert os.readlink(primary_link) == before_link
    assert cleanup["status"] == "completed"


def test_base_worktree_clones_candidate_venv_without_write_through(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = tmp_path / "dependency-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    candidate_file = repo / ".venv/lib/python/site-packages/pkg.py"
    candidate_file.parent.mkdir(parents=True)
    candidate_file.write_text("candidate = True\n", encoding="utf-8")
    cleanup: dict[str, object] = {}

    with helper._base_worktree(repo, _git(repo, "rev-parse", "HEAD"), cleanup) as checkout:
        cloned = checkout / ".venv/lib/python/site-packages/pkg.py"
        assert cloned.is_file()
        assert not (checkout / ".venv").is_symlink()
        cloned.write_text("base write\n", encoding="utf-8")

    assert candidate_file.read_text(encoding="utf-8") == "candidate = True\n"
    assert cleanup["status"] == "completed"


def test_base_import_identity_rejects_candidate_editable_path(tmp_path: Path) -> None:
    helper = _load_helper()
    checkout = tmp_path / "base"
    candidate = tmp_path / "candidate"
    for root, identity in ((checkout, "base"), (candidate, "candidate")):
        package = root / "omnigent"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"IDENTITY = {identity!r}\n", encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(checkout / ".venv")],
        check=True,
    )
    interpreter = checkout / ".venv/bin/python"
    site_packages = subprocess.run(
        [str(interpreter), "-c", "import site; print(site.getsitepackages()[0])"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    Path(site_packages, "candidate-editable.pth").write_text(
        f"import sys; sys.path.insert(0, {str(candidate)!r})\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="did not resolve inside"):
        helper._assert_base_import_identity(checkout, tmp_path / "run", 10)


def test_comparison_signal_cleans_child_process_and_worktree(tmp_path: Path) -> None:
    repo = _comparison_repo(tmp_path, "timeout")
    skill = _fake_skill(tmp_path)
    child = _pr_child(tmp_path, "candidate failure")
    run_dir = tmp_path / "run"
    capability = _capability(run_dir)
    process = subprocess.Popen(
        [
            sys.executable,
            str(_HELPER),
            "baseline",
            "--repo-root",
            str(repo),
            "--skill-root",
            str(skill),
            "--run-dir",
            str(run_dir),
            "--lane",
            "server",
            "--base-ref",
            "main",
            "--child-manifest",
            str(child),
            "--timeout",
            "60",
            "--capability-file",
            str(capability),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if "omnigent-verify-base-" in _git(repo, "worktree", "list"):
            break
        time.sleep(0.05)
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=10)

    assert time.monotonic() - started < 10
    assert process.returncode != 0
    report = json.loads(
        (run_dir / "baseline" / "server" / "comparison.json").read_text(encoding="utf-8")
    )
    assert report["classification"] == "could_not_compare"
    assert report["cleanup"]["status"] == "completed"
    assert "omnigent-verify-base-" not in _git(repo, "worktree", "list")


def test_comparison_subprocess_propagates_managed_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    marker = tmp_path / "marker"
    descriptor = os.open(marker, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        monkeypatch.setenv("OMNIGENT_VERIFY_MANAGED_FD", str(descriptor))
        status, timed_out = helper._bounded_run(
            [
                sys.executable,
                "-c",
                "import os; os.fstat(int(os.environ['OMNIGENT_VERIFY_MANAGED_FD']))",
            ],
            cwd=tmp_path,
            env=dict(os.environ),
            log_path=tmp_path / "comparison.log",
            timeout=5,
        )
    finally:
        os.close(descriptor)

    assert status == 0
    assert timed_out is False


def _benchmark(*, backend: str = "sqlite", p50: float = 1.0) -> dict[str, Any]:
    return {
        "schema_version": 6,
        "git_sha": "a" * 40,
        "host": {"platform": "test", "python": "3.12", "cpu_count": 1},
        "harness": "http-only",
        "config": {
            "iterations": 25,
            "requests": 500,
            "concurrency": 1,
            "runs": 1,
            "warmup": 10,
            "with_runner": False,
            "backend": backend,
            "network_delay_ms": 0.0,
        },
        "journeys": {
            "list_sessions": {
                "backend": backend,
                "kind": "latency",
                "runs": [{"n_failures": 0, "http_requests": 25}],
                "summary": {
                    "avg_p50_ms": p50,
                    "avg_p99_ms": p50 * 2,
                    "avg_http_requests_per_op": 1.0,
                    "network_routes": [
                        {"route": "GET /v1/sessions", "requests": 75, "per_op": 1.0}
                    ],
                    "runs_ok": 1,
                    "runs_total": 1,
                },
            }
        },
    }


def test_performance_comparison_requires_matched_identity_and_never_invents_threshold() -> None:
    helper = _load_helper()
    matched = helper._compare_benchmarks(_benchmark(), _benchmark(p50=1.2))
    mismatched = helper._compare_benchmarks(_benchmark(), _benchmark(backend="mysql"))
    unsupported_seed = helper._compare_benchmarks(
        _benchmark(backend="mysql"),
        _benchmark(backend="mysql"),
    )

    assert matched["matched"] is True
    assert matched["rows"][0]["p50_ms"] == {"base": 1.0, "candidate": 1.2}
    assert matched["rows"][0]["p99_ms"] == {"base": 2.0, "candidate": 2.4}
    assert matched["rows"][0]["http_requests_per_op"]["candidate"] == 1.0
    assert matched["rows"][0]["http_requests"]["candidate"] == 25
    assert matched["rows"][0]["network_routes"]["candidate"][0]["route"] == "GET /v1/sessions"
    assert matched["no_regression_claim"] is False
    assert mismatched["matched"] is False
    assert "did not match" in mismatched["claim_reason"]
    assert unsupported_seed["matched"] is False
    assert unsupported_seed["seed_identity"]["matched"] is False


def test_performance_comparison_enforces_explicit_applicable_thresholds() -> None:
    helper = _load_helper()
    passing = _benchmark(p50=1.05)
    passing["verification_thresholds"] = {
        "max_p50_regression_percent": 10,
        "max_p99_regression_percent": 10,
    }
    failing = _benchmark(p50=1.2)
    failing["verification_thresholds"] = passing["verification_thresholds"]

    passed = helper._compare_benchmarks(_benchmark(), passing)
    failed = helper._compare_benchmarks(_benchmark(), failing)

    assert passed["matched"] is True
    assert passed["no_regression_claim"] is True
    assert failed["matched"] is True
    assert failed["no_regression_claim"] is False


def test_performance_schema_must_be_supported_and_equal() -> None:
    helper = _load_helper()
    unsupported = _benchmark()
    unsupported["schema_version"] = 999

    comparison = helper._compare_benchmarks(_benchmark(), unsupported)

    assert comparison["matched"] is False
    assert comparison["schema_identity"] == {
        "base": 6,
        "candidate": 999,
        "matched": False,
        "supported": [6],
    }
    assert comparison["no_regression_claim"] is False


def test_performance_comparison_rejects_contradictory_run_counts() -> None:
    helper = _load_helper()
    contradictory = _benchmark()
    summary = contradictory["journeys"]["list_sessions"]["summary"]
    summary["runs_ok"] = 2
    summary["runs_total"] = 2

    comparison = helper._compare_benchmarks(_benchmark(), contradictory)

    assert comparison["matched"] is False
    assert comparison["rows"][0]["functional_success"]["candidate"] is False
    assert comparison["no_regression_claim"] is False


def test_extreme_latency_regression_never_returns_green() -> None:
    helper = _load_helper()
    without_threshold = _benchmark(p50=1_000)
    with_threshold = _benchmark(p50=1_000)
    with_threshold["verification_thresholds"] = {
        "max_p50_regression_percent": 1_000_000,
        "max_p99_regression_percent": 1_000_000,
    }

    unscored = helper._compare_benchmarks(_benchmark(p50=1), without_threshold)
    failed = helper._compare_benchmarks(_benchmark(p50=1), with_threshold)

    assert unscored["rows"][0]["p50_ms"] == {"base": 1, "candidate": 1_000}
    assert unscored["rows"][0]["p99_ms"] == {"base": 2, "candidate": 2_000}
    assert unscored["no_regression_claim"] is False
    assert "thresholds failed" in unscored["claim_reason"]
    assert failed["no_regression_claim"] is False
    assert "thresholds failed" in failed["claim_reason"]
    assert failed["thresholds"] == {
        "max_p50_regression_percent": 10,
        "max_p99_regression_percent": 10,
    }


@pytest.mark.parametrize("defect", ["skipped", "partial", "failed-operation"])
def test_performance_comparison_requires_functional_success(defect: str) -> None:
    helper = _load_helper()
    candidate = _benchmark()
    journey = candidate["journeys"]["list_sessions"]
    if defect == "skipped":
        journey["skipped_reason"] = "fixture"
    elif defect == "partial":
        journey["summary"]["runs_ok"] = 0
    else:
        journey["runs"][0]["n_failures"] = 1

    comparison = helper._compare_benchmarks(_benchmark(), candidate)

    assert comparison["matched"] is False
    assert comparison["functional_success"] is False


def test_matched_performance_comparison_runs_at_exact_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    repo = tmp_path / "perf-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for relative, content in {
        "pyproject.toml": "[project]\nname='fixture'\n",
        "uv.lock": "version = 1\n",
        "package.json": '{"name":"fixture","packageManager":"pnpm@11.15.1"}\n',
        "pnpm-lock.yaml": "lockfileVersion: 9\n",
        "pnpm-workspace.yaml": "packages: []\n",
        "web/package.json": '{"name":"web"}\n',
        "web/electron/package.json": '{"name":"electron"}\n',
        "dev/benchmarks/omnigent/run.py": "# synthetic benchmark\n",
    }.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _commit(repo, "base")
    _git(repo, "switch", "-q", "-c", "pr")
    (repo / "product.py").write_text("changed = True\n", encoding="utf-8")
    _commit(repo, "candidate")
    candidate = _benchmark()
    candidate["git_sha"] = _git(repo, "rev-parse", "HEAD")
    run_dir = tmp_path / "perf-run"
    run_dir.mkdir()
    candidate_path = run_dir / "benchmark.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"""#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

report = json.loads({json.dumps(json.dumps(_benchmark()))})
report["git_sha"] = subprocess.run(
    ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
).stdout.strip()
output = Path(sys.argv[sys.argv.index("--output") + 1])
output.write_text(json.dumps(report) + "\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(
        "OMNIGENT_VERIFY_PNPM_CACHE_ROOT",
        str(tmp_path / "unprepared-pnpm-cache"),
    )

    report, exit_status = helper.compare_performance(
        repo,
        run_dir,
        "main",
        candidate_path,
        2,
    )

    assert exit_status == 0
    assert report["status"] == "passed"
    assert report["comparison"]["matched"] is True
    assert report["comparison"]["commit_identity"]["base"]["matched"] is True
    assert report["comparison"]["commit_identity"]["candidate"]["matched"] is True
    assert report["comparison"]["no_regression_claim"] is True
    assert report["base_commit"] != report["candidate_commit"]
    assert report["benchmark_sha256"]["base"]
    assert report["benchmark_sha256"]["candidate"]
    assert report["cleanup"]["status"] == "completed"
