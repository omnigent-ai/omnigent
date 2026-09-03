from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import io
import json
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELPER = (
    _REPO_ROOT / ".agents" / "skills" / "verify-omnigent" / "scripts" / "evidence_manifest.py"
)


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_evidence_manifest", _HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _manifest(run_dir: Path) -> dict[str, Any]:
    value = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _write_trace(path: Path, payload: str = '{"type":"fixture"}\n') -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("trace.trace", payload)


def _png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\x00\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )


def test_manifest_is_atomic_relative_hashed_and_finalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "outside-checkout" / "run-1"
    run_dir.mkdir(parents=True)
    fake_home = tmp_path / "fake-home"
    installed_skill = fake_home / ".claude/skills/verify-omnigent"
    monkeypatch.setenv("OMNIGENT_VERIFY_ADAPTER", "cursor")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("OMNIGENT_VERIFY_SKILL_ROOT", str(installed_skill))

    helper.initialize(run_dir, "smoke", "smoke")
    helper.prepare(
        run_dir,
        "verification",
        [
            "uv",
            "run",
            "pytest",
            f"--output={run_dir}/playwright",
            str(installed_skill / "scripts/helper.py"),
        ],
        ["tests/e2e_ui/test_evidence_contract.py"],
    )
    payload = _png()
    screenshot = run_dir / "playwright" / "test" / "screenshot.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(payload)
    (run_dir / "playwright" / "test" / "metadata.json").write_text(
        '{"schema_version": 1}\n',
        encoding="utf-8",
    )
    _write_trace(run_dir / "playwright" / "test" / "trace.zip")
    (run_dir / "run.log").write_text("ok\n", encoding="utf-8")
    (run_dir / "cleanup.json").write_text(
        '{"schema_version": 1, "status": "completed"}\n',
        encoding="utf-8",
    )
    (run_dir / "junit.xml").write_text(
        """\
<testsuite tests="2">
  <testcase classname="tests.e2e_ui.test_contract" name="test_pass" time="0.25" />
  <testcase classname="tests.e2e_ui.test_contract" name="test_skip" time="0">
    <skipped />
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (run_dir / "escaped-link").symlink_to(outside)

    helper.finalize(run_dir, "passed", 0, None)

    manifest = _manifest(run_dir)
    assert manifest["schema_version"] == 1
    assert manifest["status"] == "passed"
    assert manifest["adapter"]["identity"] == "cursor"
    assert manifest["cleanup"]["status"] == "completed"
    assert [result["status"] for result in manifest["tests"]] == ["passed", "skipped"]
    screenshot_item = next(
        item for item in manifest["artifacts"] if item["path"].endswith("screenshot.png")
    )
    assert screenshot_item == {
        "path": "playwright/test/screenshot.png",
        "mime": "image/png",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "authority": "playwright-test-bound",
    }
    assert all(not Path(item["path"]).is_absolute() for item in manifest["artifacts"])
    assert not any(item["path"] == "escaped-link" for item in manifest["artifacts"])
    serialized = json.dumps(manifest)
    assert str(run_dir) not in serialized
    assert str(fake_home) not in serialized
    assert manifest["run"]["command_argv"][-2:] == [
        "--output=<run-dir>/playwright",
        "<skill-root>/scripts/helper.py",
    ]
    assert base64.b64encode(payload).decode("ascii") not in serialized
    assert not list(run_dir.glob(".manifest.json.*.tmp"))


def test_collaboration_prepare_rejects_omitted_semantic_journeys(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "collaboration"
    run_dir.mkdir()
    helper.initialize(run_dir, "collaboration", "collaboration")
    required = set(helper.REQUIRED_PROFILE_TESTS["collaboration"])
    omitted = "tests/e2e_ui/collaboration/test_author_label.py"

    with pytest.raises(ValueError, match="omit required profile coverage"):
        helper.prepare(
            run_dir,
            "verification",
            ["uv", "run", "pytest"],
            sorted(required - {omitted}),
        )

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run"]["selected_tests"] == []
    helper.prepare(run_dir, "verification", ["uv", "run", "pytest"], sorted(required))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["run"]["selected_tests"]) == required


def test_interrupted_manifest_fails_closed_on_missing_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.delenv("OMNIGENT_VERIFY_ADAPTER", raising=False)

    helper.initialize(run_dir, "smoke", "smoke")
    helper.finalize(run_dir, "interrupted", 143, "TERM")

    manifest = _manifest(run_dir)
    assert manifest["status"] == "interrupted"
    assert manifest["signal"] == "TERM"
    assert manifest["cleanup"]["status"] == "unknown"
    assert manifest["tests"] == []
    assert manifest["adapter"]["identity"] == "unspecified"
    assert any("No JUnit report" in item for item in manifest["limitations"])
    assert any("No browser context metadata" in item for item in manifest["limitations"])


def test_ui_output_validation_requires_test_bound_evidence(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    missing = helper.validate_required_outputs(run_dir, "web-ui")
    assert "JUnit" in " ".join(missing)

    nodeid = "tests/e2e_ui/test_example.py::test_real"
    nodeid_sha256 = hashlib.sha256(nodeid.encode()).hexdigest()
    (run_dir / "junit.xml").write_text(
        "<testsuite tests='1'><testcase name='test_real'><properties>"
        f"<property name='omnigent_nodeid_sha256' value='{nodeid_sha256}'/>"
        "<property name='omnigent_browser_context_count' value='1'/>"
        "</properties></testcase></testsuite>\n",
        encoding="utf-8",
    )
    supplemental = run_dir / "supplemental"
    supplemental.mkdir()
    (supplemental / "metadata.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    (supplemental / "screenshot.png").write_bytes(b"png")
    _write_trace(supplemental / "trace.zip")
    assert len(helper.validate_required_outputs(run_dir, "web-ui")) == 6

    context = run_dir / "playwright" / f"test-{nodeid_sha256[:20]}" / "context-1"
    context.mkdir(parents=True)
    (context / "metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "nodeid": nodeid,
                "nodeid_sha256": nodeid_sha256,
                "context_style": "managed-sync",
                "lifecycle": "closed",
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    (context / "screenshot.png").write_bytes(_png())
    _write_trace(context / "trace.zip")

    assert helper.validate_required_outputs(run_dir, "web-ui") == []


def test_parameterized_ui_cases_require_exact_individual_contexts(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    nodeids = [
        "tests/e2e_ui/test_example.py::test_case[alpha-one]",
        "tests/e2e_ui/test_example.py::test_case[alpha-two]",
    ]
    identities = [hashlib.sha256(nodeid.encode()).hexdigest() for nodeid in nodeids]
    cases = "".join(
        f"<testcase name='test_case[{index}]'><properties>"
        f"<property name='omnigent_nodeid_sha256' value='{identity}'/>"
        "<property name='omnigent_browser_context_count' value='1'/>"
        "</properties></testcase>"
        for index, identity in enumerate(identities)
    )
    (run_dir / "junit.xml").write_text(
        f"<testsuite tests='2'>{cases}</testsuite>",
        encoding="utf-8",
    )
    context = run_dir / "playwright" / f"test-{identities[0][:20]}" / "context-1"
    context.mkdir(parents=True)
    (context / "metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "nodeid": nodeids[0],
                "nodeid_sha256": identities[0],
                "context_style": "managed-sync",
                "lifecycle": "closed",
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    (context / "screenshot.png").write_bytes(_png())
    _write_trace(context / "trace.zip")

    blockers = helper.validate_required_outputs(run_dir, "web-ui")

    assert "One or more executed UI tests have no correlated browser context." in blockers


def test_mixed_ui_suite_allows_only_declared_browserless_cases(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "mixed"
    run_dir.mkdir()
    browser_nodeid = "tests/e2e_ui/test_example.py::test_browser"
    browser_id = hashlib.sha256(browser_nodeid.encode()).hexdigest()
    browserless_nodeid = "tests/e2e_ui/test_example.py::test_browserless"
    browserless_id = hashlib.sha256(browserless_nodeid.encode()).hexdigest()
    (run_dir / "junit.xml").write_text(
        "<testsuite tests='2'>"
        "<testcase name='test_browser'><properties>"
        f"<property name='omnigent_nodeid_sha256' value='{browser_id}'/>"
        "<property name='omnigent_browser_context_count' value='1'/>"
        "</properties></testcase>"
        "<testcase name='test_browserless'><properties>"
        f"<property name='omnigent_nodeid_sha256' value='{browserless_id}'/>"
        "<property name='omnigent_browser_context_count' value='0'/>"
        "</properties></testcase></testsuite>",
        encoding="utf-8",
    )
    context = run_dir / "playwright" / f"test-{browser_id[:20]}" / "context-1"
    context.mkdir(parents=True)
    (context / "metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "nodeid": browser_nodeid,
                "nodeid_sha256": browser_id,
                "context_style": "managed-sync",
                "lifecycle": "closed",
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    (context / "screenshot.png").write_bytes(_png())
    trace = context / "trace.zip"
    _write_trace(trace)

    assert helper.validate_required_outputs(run_dir, "full-ui") == []
    trace.unlink()
    assert any(
        "invalid or uncorrelated" in blocker
        for blocker in helper.validate_required_outputs(run_dir, "full-ui")
    )


def test_all_skipped_pytest_profile_cannot_finalize_passed(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "server"
    run_dir.mkdir()
    helper.initialize(run_dir, "server", "server")
    (run_dir / "run.log").write_text("pytest skipped\n", encoding="utf-8")
    (run_dir / "junit.xml").write_text(
        '<testsuite tests="1" skipped="1">'
        '<testcase name="test_skip"><skipped/></testcase></testsuite>',
        encoding="utf-8",
    )

    helper.finalize(run_dir, "passed", 0, None)

    manifest = _manifest(run_dir)
    assert manifest["status"] == "failed"
    assert manifest["exit_status"] != 0
    assert any("no non-skipped test" in item for item in manifest["limitations"])


def test_candidate_filenames_do_not_forge_ui_evidence(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "ui"
    context = run_dir / "playwright/test-forged/context-1"
    context.mkdir(parents=True)
    (context / "metadata.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    (context / "screenshot.png").write_bytes(b"fake")
    _write_trace(context / "trace.zip")
    (run_dir / "junit.xml").write_text(
        '<testsuite tests="1"><testcase name="test_real"/></testsuite>',
        encoding="utf-8",
    )

    blockers = helper.validate_required_outputs(run_dir, "web-ui")

    assert any("invalid or uncorrelated" in blocker for blocker in blockers)


def test_perf_manifest_validates_and_hashes_current_benchmark(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "perf-run"
    run_dir.mkdir()
    benchmark = {
        "schema_version": 6,
        "git_sha": "a" * 40,
        "host": {"platform": "test"},
        "harness": "http-only",
        "config": {"backend": "sqlite", "runs": 3},
        "journeys": {
            "list_sessions": {
                "runs": [{"n_failures": 0}, {"n_failures": 0}, {"n_failures": 0}],
                "summary": {
                    "avg_p50_ms": 1.0,
                    "avg_p99_ms": 2.0,
                    "avg_http_requests_per_op": 1.0,
                    "network_routes": [],
                    "runs_ok": 3,
                    "runs_total": 3,
                },
            }
        },
    }
    payload = (json.dumps(benchmark) + "\n").encode()

    helper.initialize(run_dir, "perf", "perf")
    (run_dir / "benchmark.json").write_bytes(payload)
    (run_dir / "steps.json").write_text('{"status":"passed"}\n', encoding="utf-8")
    (run_dir / "run.log").write_text("benchmark passed\n", encoding="utf-8")

    assert helper.validate_required_outputs(run_dir, "perf") == []
    helper.finalize(run_dir, "passed", 0, None)

    manifest = _manifest(run_dir)
    artifact = next(item for item in manifest["artifacts"] if item["path"] == "benchmark.json")
    assert artifact["authority"] == "omnigent-benchmark"
    assert artifact["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["status"] == "passed"


@pytest.mark.parametrize(
    "defect",
    ["skipped", "partial", "failed-operation", "contradictory"],
)
def test_perf_manifest_rejects_incomplete_functional_runs(
    tmp_path: Path,
    defect: str,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / defect
    run_dir.mkdir()
    runs: list[dict[str, object]] = [{"n_failures": 0}]
    summary: dict[str, object] = {
        "avg_p50_ms": 1.0,
        "avg_p99_ms": 2.0,
        "avg_http_requests_per_op": 1.0,
        "network_routes": [],
        "runs_ok": 1,
        "runs_total": 1,
    }
    journey: dict[str, object] = {"runs": runs, "summary": summary}
    if defect == "skipped":
        journey["skipped_reason"] = "fixture"
    elif defect == "partial":
        summary["runs_ok"] = 0
    elif defect == "failed-operation":
        runs[0]["n_failures"] = 1
    else:
        summary["runs_total"] = 2
        summary["runs_ok"] = 2
    (run_dir / "benchmark.json").write_text(
        json.dumps(
            {
                "schema_version": 6,
                "git_sha": "a" * 40,
                "config": {"backend": "sqlite", "runs": 1},
                "journeys": {"fixture": journey},
            }
        ),
        encoding="utf-8",
    )

    assert helper.validate_required_outputs(run_dir, "perf")


def test_perf_manifest_propagates_failed_comparison_cleanup(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "perf"
    run_dir.mkdir()
    helper.initialize(run_dir, "perf", "perf")
    (run_dir / "run.log").write_text("comparison passed before cleanup\n", encoding="utf-8")
    (run_dir / "performance-comparison.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "cleanup": {"status": "failed", "worktree": "removed"},
            }
        ),
        encoding="utf-8",
    )

    helper.finalize(run_dir, "passed", 0, None)

    manifest = _manifest(run_dir)
    assert manifest["status"] == "failed"
    assert manifest["exit_status"] != 0
    assert manifest["cleanup"]["status"] == "failed"


def test_known_secret_inside_nested_trace_archive_is_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    secret = "fake-explicit-credential-value"
    nested_buffer = io.BytesIO()
    with zipfile.ZipFile(nested_buffer, "w") as nested:
        nested.writestr("trace.trace", f'{{"token":"{secret}"}}\n')
    trace = run_dir / "trace.zip"
    with zipfile.ZipFile(trace, "w") as outer:
        outer.writestr("resources/nested.zip", nested_buffer.getvalue())
    monkeypatch.setenv("DATABRICKS_TOKEN", secret)

    blockers = helper.validate_required_outputs(run_dir, "server")

    assert blockers
    assert not trace.exists()
    assert secret not in " ".join(blockers)


def test_trace_text_redaction_preserves_zip_and_rehashes_artifact(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    trace = run_dir / "trace.zip"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr(
            "trace.trace",
            '{"message":"token=managed-secret-value"}\n',
        )
        archive.writestr("resources/source.txt", "direct-sync-secret\n")
    before = hashlib.sha256(trace.read_bytes()).hexdigest()

    assert helper.sanitize_trace_archives(run_dir) == []

    with zipfile.ZipFile(trace) as archive:
        assert archive.testzip() is None
        payload = b"".join(archive.read(item) for item in archive.infolist())
    assert b"managed-secret-value" not in payload
    assert b"direct-sync-secret" not in payload
    inventory = helper._artifact_inventory(run_dir, [])
    assert inventory[0]["sha256"] == hashlib.sha256(trace.read_bytes()).hexdigest()
    assert inventory[0]["sha256"] != before


def test_five_level_nested_archive_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.setenv("DATABRICKS_TOKEN", "known-depth-scan-secret")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = b"clean"
    for depth in range(6):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"level-{depth}.zip", payload)
        payload = buffer.getvalue()
    outer = run_dir / "deep.zip"
    outer.write_bytes(payload)

    blockers = helper.remove_known_secret_artifacts(run_dir)

    assert blockers
    assert not outer.exists()


def test_zip_bomb_shaped_archive_is_rejected_before_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.setattr(helper, "MAX_ARCHIVE_EXPANDED_BYTES", 1024)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bomb = run_dir / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.txt", b"A" * 4096)

    blockers = helper.remove_known_secret_artifacts(run_dir)

    assert blockers
    assert not bomb.exists()


def test_oversized_top_level_artifact_is_failed_and_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.setattr(helper, "MAX_ARTIFACT_BYTES", 8)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifact = run_dir / "oversized.bin"
    artifact.write_bytes(b"x" * 9)

    blockers = helper.remove_known_secret_artifacts(run_dir)

    assert blockers
    assert not artifact.exists()


def test_secret_path_is_redacted_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    secret = "filename-secret-marker"
    monkeypatch.setenv("DATABRICKS_TOKEN", secret)
    run_dir = tmp_path / "run"
    artifact = run_dir / f"directory-{secret}" / f"file-{secret}.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("otherwise clean\n", encoding="utf-8")

    blockers = helper.remove_known_secret_artifacts(run_dir)

    assert blockers
    assert not artifact.exists()
    assert not artifact.parent.exists()
    assert secret not in " ".join(blockers)
    assert "<redacted-artifact-" in " ".join(blockers)


def test_unremovable_secret_artifact_reports_cleanup_failure_without_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    secret = "unremovable-secret-marker"
    monkeypatch.setenv("DATABRICKS_TOKEN", secret)
    monkeypatch.setenv("OMNIGENT_VERIFY_ADAPTER", "cursor")
    monkeypatch.setenv("OMNIGENT_VERIFY_REPO_ROOT", str(_REPO_ROOT))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    helper.initialize(run_dir, "backend", "backend")
    helper.prepare(run_dir, "verification", ["fixture"], [])
    artifact = run_dir / f"{secret}.txt"
    artifact.write_text(secret, encoding="utf-8")
    original_unlink = Path.unlink

    def deny_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == artifact:
            raise PermissionError("simulated")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_unlink)

    blockers = helper.remove_known_secret_artifacts(run_dir)

    assert artifact.exists()
    assert any(item.startswith("PRIVACY CLEANUP FAILURE:") for item in blockers)
    assert secret not in " ".join(blockers)
    limitations: list[str] = []
    inventory = helper._artifact_inventory(run_dir, limitations)
    assert all(secret not in item["path"] for item in inventory)
    assert secret not in " ".join(limitations)
    helper.finalize(run_dir, "passed", 0, None)
    manifest_text = (run_dir / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "failed"
    assert manifest["cleanup"]["status"] == "failed"
    assert secret not in manifest_text


def test_failed_trace_sanitization_is_privacy_failure_and_excluded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    trace = run_dir / "trace.zip"
    trace.write_bytes(b"not a zip")
    real_unlink = Path.unlink

    def deny_trace(path: Path, missing_ok: bool = False) -> None:
        if path == trace:
            raise PermissionError("private filesystem detail")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_trace)
    suspect: set[Path] = set()
    blockers = helper.sanitize_trace_archives(run_dir, suspect)
    limitations: list[str] = []
    inventory = helper._artifact_inventory(run_dir, limitations, suspect)

    assert blockers and blockers[0].startswith("PRIVACY CLEANUP FAILURE:")
    assert "private filesystem detail" not in " ".join(blockers)
    assert all(item["path"] != "trace.zip" for item in inventory)
    assert "trace.zip" not in " ".join(limitations)


def test_deleted_failed_trace_still_marks_final_privacy_cleanup_failed(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    helper.initialize(run_dir, "backend", "backend")
    trace = run_dir / "trace.zip"
    trace.write_bytes(b"not a zip")

    blockers = helper.validate_required_outputs(run_dir, "backend")
    assert blockers and not trace.exists()
    helper.finalize(run_dir, "passed", 0, None)

    manifest = _manifest(run_dir)
    assert manifest["status"] == "failed"
    assert manifest["cleanup"]["status"] == "failed"
    assert all(item["path"] != "trace.zip" for item in manifest["artifacts"])


def test_trace_json_sensitive_values_are_recursively_redacted(tmp_path: Path) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    trace = run_dir / "trace.zip"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr(
            "trace.trace",
            '{"token":"page-secret","nested":{"password":"nested-secret"},'
            '"items":[{"api_key":"array-secret"}]}\n',
        )

    assert helper.sanitize_trace_archives(run_dir) == []

    with zipfile.ZipFile(trace) as archive:
        document = json.loads(archive.read("trace.trace"))
        assert archive.testzip() is None
    assert document["token"] == "[redacted-sensitive-value]"
    assert document["nested"]["password"] == "[redacted-sensitive-value]"
    assert document["items"][0]["api_key"] == "[redacted-sensitive-value]"


def test_ui_evidence_decodes_png_and_requires_useful_trace(tmp_path: Path) -> None:
    helper = _load_helper()
    assert helper._valid_png(tmp_path / "missing.png") is False
    png = tmp_path / "screenshot.png"
    png.write_bytes(_png())
    assert helper._valid_png(png) is True
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert helper._valid_png(png) is False

    trace = tmp_path / "trace.zip"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("resources/empty.txt", b"")
    assert helper._valid_playwright_trace(trace) is False
    _write_trace(trace, '{"type":"before"}\n{"type":"after"}\n')
    assert helper._valid_playwright_trace(trace) is True


def test_surviving_sensitive_runtime_state_forces_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "server"
    run_dir.mkdir()
    helper.initialize(run_dir, "server", "server")
    (run_dir / "run.log").write_text("tests passed\n", encoding="utf-8")
    (run_dir / "junit.xml").write_text(
        "<testsuite tests='1'><testcase name='test_pass'/></testsuite>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        helper,
        "cleanup_strict_environment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated retained credential cache")
        ),
    )

    helper.finalize(run_dir, "passed", 0, None)

    manifest = _manifest(run_dir)
    assert manifest["status"] == "failed"
    assert manifest["exit_status"] != 0
    assert manifest["cleanup"]["status"] == "failed"


def test_parent_manifest_hashes_children_without_flattening_their_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    parent = tmp_path / "parent"
    child = parent / "children" / "server" / "server-run"
    child.mkdir(parents=True)
    monkeypatch.setenv("OMNIGENT_VERIFY_ADAPTER", "cursor")

    helper.initialize(parent, "auto", "auto")
    plan_path = parent / "lane-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "auto",
                "base_ref": "HEAD",
                "changed_files": ["omnigent/server/app.py"],
                "selected_lanes": ["server"],
                "decisions": [
                    {
                        "lane": "server",
                        "status": "selected",
                        "reasons": ["omnigent/server/app.py: server changed"],
                    }
                ],
                "blockers": [],
                "optional_lanes": [],
            }
        ),
        encoding="utf-8",
    )
    helper.record_plan(parent, plan_path)

    helper.initialize(child, "server", "server")
    (child / "run.log").write_text("server passed\n", encoding="utf-8")
    (child / "server-proof.json").write_text('{"ok": true}\n', encoding="utf-8")
    (child / "junit.xml").write_text(
        '<testsuite tests="1"><testcase classname="server" name="test_ok"/></testsuite>',
        encoding="utf-8",
    )
    helper.finalize(child, "passed", 0, None)
    child_bytes = (child / "manifest.json").read_bytes()

    helper.add_child(parent, "server", child / "manifest.json", 0)
    (parent / "run.log").write_text("orchestration passed\n", encoding="utf-8")
    helper.finalize(parent, "passed", 0, None)

    manifest = _manifest(parent)
    assert manifest["children"] == [
        {
            "lane": "server",
            "required": True,
            "manifest": "children/server/server-run/manifest.json",
            "manifest_sha256": hashlib.sha256(child_bytes).hexdigest(),
            "status": "passed",
            "exit_status": 0,
            "profile": "server",
        }
    ]
    assert all(not item["path"].startswith("children/") for item in manifest["artifacts"])
    assert not any("server-proof.json" in item["path"] for item in manifest["artifacts"])


@pytest.mark.parametrize("tamper", ["delete", "corrupt", "duplicate"])
def test_parent_pass_fails_when_required_child_evidence_is_invalid(
    tmp_path: Path,
    tamper: str,
) -> None:
    helper = _load_helper()
    parent = tmp_path / "parent"
    child = parent / "children/cli/child"
    child.mkdir(parents=True)
    helper.initialize(parent, "auto", "auto")
    _record_parent_plan(helper, parent, ["cli"])
    helper.initialize(child, "cli", "cli")
    (child / "run.log").write_text("cli passed\n", encoding="utf-8")
    helper.finalize(child, "passed", 0, None)
    helper.add_child(parent, "cli", child / "manifest.json", 0)
    child_manifest = child / "manifest.json"
    if tamper == "delete":
        child_manifest.unlink()
    elif tamper == "corrupt":
        child_manifest.write_text("{}\n", encoding="utf-8")
    else:
        duplicate = parent / "children/cli/duplicate"
        duplicate.mkdir()
        (duplicate / "manifest.json").write_bytes(child_manifest.read_bytes())
    (parent / "run.log").write_text("parent attempted pass\n", encoding="utf-8")

    helper.finalize(parent, "passed", 0, None)

    manifest = _manifest(parent)
    assert manifest["status"] == "failed"
    assert manifest["exit_status"] != 0
    assert manifest["children"][0]["status"] == "unavailable"


def test_missing_required_child_is_recorded_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    run_dir = tmp_path / "parent"
    run_dir.mkdir()
    monkeypatch.setenv("OMNIGENT_VERIFY_ADAPTER", "cursor")
    helper.initialize(run_dir, "all-surfaces", "all-surfaces")
    helper.add_child(
        run_dir,
        "desktop",
        run_dir / "children" / "desktop" / "missing.json",
        1,
    )

    manifest = _manifest(run_dir)
    assert manifest["children"][0]["status"] == "unavailable"
    assert any("no valid child manifest" in item for item in manifest["limitations"])


def test_universe_child_status_is_propagated_to_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    parent = tmp_path / "parent"
    child = parent / "children" / "universe" / "universe-run"
    child.mkdir(parents=True)
    monkeypatch.setenv("OMNIGENT_VERIFY_ADAPTER", "cursor")

    helper.initialize(parent, "auto", "auto")
    helper.initialize(child, "universe", "universe")
    report = {
        "schema_version": 1,
        "status": "not_synced",
        "sync": {"status": "not_synced"},
        "source_patch_apply": {"status": "passed", "checks": []},
        "runtime_compatibility": {"status": "not_synced", "targets": []},
        "limitations": ["Universe pin differs from the requested OSS commit."],
    }
    (child / "universe.json").write_text(
        json.dumps(report),
        encoding="utf-8",
    )
    (child / "run.log").write_text("not synced\n", encoding="utf-8")
    helper.finalize(child, "failed", 1, None)

    helper.add_child(parent, "universe", child / "manifest.json", 1)

    manifest = _manifest(parent)
    assert manifest["downstream_universe"]["status"] == "not_synced"
    assert manifest["downstream_universe"]["source_patch_apply"]["status"] == "passed"
    assert manifest["children"][0]["manifest_sha256"]


def _record_parent_plan(helper: ModuleType, parent: Path, lanes: list[str]) -> None:
    plan_path = parent / "lane-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "auto",
                "base_ref": "main",
                "changed_files": ["omnigent/server/app.py"],
                "selected_lanes": lanes,
                "decisions": [],
                "blockers": [],
                "optional_lanes": [],
            }
        ),
        encoding="utf-8",
    )
    helper.record_plan(parent, plan_path)
    helper.register_children(parent, lanes)


def test_blocked_parent_finalizes_every_selected_child(tmp_path: Path) -> None:
    helper = _load_helper()
    parent = tmp_path / "parent"
    parent.mkdir()
    helper.initialize(parent, "auto", "auto")
    _record_parent_plan(helper, parent, ["quality-gates", "server"])

    helper.finalize(parent, "blocked", 1, None)

    manifest = _manifest(parent)
    assert manifest["status"] == "blocked"
    assert [child["status"] for child in manifest["children"]] == ["blocked", "blocked"]
    assert all(child["status"] != "running" for child in manifest["children"])
    json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    assert not list(parent.glob(".manifest.json.*.tmp"))


def test_parent_reconciles_stale_running_child_after_interrupt(tmp_path: Path) -> None:
    helper = _load_helper()
    parent = tmp_path / "parent"
    child = parent / "children" / "server" / "server-run"
    child.mkdir(parents=True)
    helper.initialize(parent, "auto", "auto")
    _record_parent_plan(helper, parent, ["server"])
    helper.initialize(child, "server", "server")

    helper.finalize(parent, "interrupted", 143, "TERM")

    parent_manifest = _manifest(parent)
    child_manifest = _manifest(child)
    assert parent_manifest["status"] == "interrupted"
    assert parent_manifest["children"][0]["status"] == "interrupted"
    assert child_manifest["status"] == "interrupted"
    assert child_manifest["signal"] == "TERM"
    assert child_manifest["cleanup"]["status"] == "not_applicable"
    assert parent_manifest["cleanup"]["status"] == "completed"
