from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_ROOT = _REPO_ROOT / ".agents" / "skills" / "verify-omnigent"


def _stage_repo(tmp_path: Path, backend_body: str) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    scripts = root / ".agents" / "skills" / "verify-omnigent" / "scripts"
    features = scripts.parent / "features"
    backend = root / "scripts" / "backend-smoke.sh"
    scripts.mkdir(parents=True)
    features.mkdir()
    backend.parent.mkdir()
    shutil.copy2(_SKILL_ROOT / "scripts" / "verify.sh", scripts / "verify.sh")
    shutil.copy2(
        _SKILL_ROOT / "scripts" / "evidence_manifest.py",
        scripts / "evidence_manifest.py",
    )
    shutil.copy2(
        _SKILL_ROOT / "scripts" / "orchestration.py",
        scripts / "orchestration.py",
    )
    shutil.copy2(
        _SKILL_ROOT / "scripts" / "profile_runner.py",
        scripts / "profile_runner.py",
    )
    shutil.copy2(
        _SKILL_ROOT / "scripts" / "universe_preflight.py",
        scripts / "universe_preflight.py",
    )
    shutil.copy2(
        _SKILL_ROOT / "scripts" / "comparison.py",
        scripts / "comparison.py",
    )
    shutil.copy2(
        _SKILL_ROOT / "scripts" / "runtime_support.py",
        scripts / "runtime_support.py",
    )
    for name in ("README.md", "approvals-and-policies.md", "harnesses-and-clients.md"):
        (features / name).write_text("# contract\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    backend.write_text(
        f"#!/usr/bin/env bash\nset -euo pipefail\n{backend_body}\n",
        encoding="utf-8",
    )
    (scripts / "verify.sh").chmod(0o755)
    (scripts / "evidence_manifest.py").chmod(0o755)
    (scripts / "orchestration.py").chmod(0o755)
    (scripts / "profile_runner.py").chmod(0o755)
    (scripts / "universe_preflight.py").chmod(0o755)
    (scripts / "comparison.py").chmod(0o755)
    (scripts / "runtime_support.py").chmod(0o755)
    backend.chmod(0o755)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    return root, scripts / "verify.sh"


def _run(
    root: Path,
    script: Path,
    evidence_root: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "OMNIGENT_VERIFY_ADAPTER": "codex",
        "OMNIGENT_VERIFY_EVIDENCE_DIR": str(evidence_root),
        **(extra_env or {}),
    }
    return subprocess.run(
        [str(script), *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )


def _only_manifest(evidence_root: Path) -> dict[str, Any]:
    manifests = list(evidence_root.glob("*/manifest.json"))
    assert len(manifests) == 1
    value = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def test_verify_script_finalizes_passing_and_failing_runs(tmp_path: Path) -> None:
    passing_root, passing_script = _stage_repo(tmp_path / "passing", 'echo "backend passed"')
    passing_evidence = tmp_path / "passing-evidence"
    passed = _run(passing_root, passing_script, passing_evidence, "backend")
    assert passed.returncode == 0, passed.stderr
    passed_manifest = _only_manifest(passing_evidence)
    assert passed_manifest["status"] == "passed"
    assert passed_manifest["exit_status"] == 0
    assert passed_manifest["adapter"]["identity"] == "codex"
    assert passed_manifest["run"]["command_argv"][0] == "env"
    assert passed_manifest["run"]["command_argv"][1].startswith("PORT=")
    assert passed_manifest["run"]["command_argv"][2:] == ["scripts/backend-smoke.sh"]
    assert "OMNIGENT_VERIFY_SUMMARY schema=1 status=passed" in passed.stdout

    failing_root, failing_script = _stage_repo(tmp_path / "failing", 'echo "nope"; exit 7')
    failing_evidence = tmp_path / "failing-evidence"
    failed = _run(failing_root, failing_script, failing_evidence, "backend")
    assert failed.returncode == 7
    failed_manifest = _only_manifest(failing_evidence)
    assert failed_manifest["status"] == "failed"
    assert failed_manifest["exit_status"] == 7
    assert "OMNIGENT_VERIFY_SUMMARY schema=1 status=failed" in failed.stdout


def test_manifest_finalize_failure_forces_nonzero_and_never_claims_passed(
    tmp_path: Path,
) -> None:
    root, script = _stage_repo(tmp_path, 'echo "backend passed"')
    helper = script.parent / "evidence_manifest.py"
    real_helper = helper.with_name("evidence_manifest_real.py")
    helper.rename(real_helper)
    helper.write_text(
        """#!/usr/bin/env python3
import os
import sys

if sys.argv[1:2] == ["finalize"]:
    raise SystemExit(9)
os.execv(
    sys.executable,
    [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "evidence_manifest_real.py"),
        *sys.argv[1:],
    ],
)
""",
        encoding="utf-8",
    )

    result = _run(root, script, tmp_path / "evidence", "backend")

    assert result.returncode != 0
    assert "failed to finalize manifest" in result.stderr
    assert "status=passed" not in result.stdout


def test_installed_skill_resolves_invocation_checkout_for_link_and_copy(
    tmp_path: Path,
) -> None:
    canonical_root, canonical_script = _stage_repo(
        tmp_path / "canonical",
        'echo "canonical backend"',
    )
    target_root, _ = _stage_repo(tmp_path / "target", 'echo "target backend"')
    installed_root = tmp_path / "installed"
    installed_root.mkdir()
    linked = installed_root / "linked"
    linked.symlink_to(canonical_script.parent.parent, target_is_directory=True)
    copied = installed_root / "copied"
    shutil.copytree(canonical_script.parent.parent, copied)

    for name, installed in (("linked", linked), ("copied", copied)):
        env = {
            key: value for key, value in os.environ.items() if key != "OMNIGENT_VERIFY_REPO_ROOT"
        }
        env.update(
            {
                "OMNIGENT_VERIFY_ADAPTER": "codex",
                "OMNIGENT_VERIFY_EVIDENCE_DIR": str(tmp_path / f"{name}-evidence"),
            }
        )
        result = subprocess.run(
            [str(installed / "scripts/verify.sh"), "backend"],
            cwd=target_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "target backend" in result.stdout
        assert "canonical backend" not in result.stdout

    assert canonical_root != target_root


def test_verify_script_doctor_writes_manifest(tmp_path: Path) -> None:
    root, script = _stage_repo(tmp_path, 'echo "unused"')
    evidence_root = tmp_path / "evidence"
    result = _run(root, script, evidence_root, "doctor", "backend")
    assert result.returncode == 0, result.stderr
    manifest = _only_manifest(evidence_root)
    assert manifest["status"] == "passed"
    assert manifest["run"]["phase"] == "finished"
    assert manifest["run"]["command_argv"] == ["doctor", "backend"]
    assert manifest["cleanup"]["status"] == "not_applicable"


def test_direct_profile_receives_only_strict_run_environment(tmp_path: Path) -> None:
    body = """python3 - <<'PY'
import json
import os
import zipfile
from pathlib import Path

keys = [
    "HOME",
    "PRE_COMMIT_HOME",
    "PLAYWRIGHT_BROWSERS_PATH",
    "AWS_SECRET_ACCESS_KEY",
    "TEST_SECRET",
]
Path(os.environ["OMNIGENT_VERIFY_RUN_DIR"], "child-env.json").write_text(
    json.dumps({key: os.environ.get(key) for key in keys}),
    encoding="utf-8",
)
PY"""
    root, script = _stage_repo(tmp_path, body)
    evidence_root = tmp_path / "evidence"
    result = _run(
        root,
        script,
        evidence_root,
        "backend",
        extra_env={
            "AWS_SECRET_ACCESS_KEY": "must-not-leak",
            "TEST_SECRET": "must-not-leak",
            "PRE_COMMIT_HOME": "/real/pre-commit",
            "PLAYWRIGHT_BROWSERS_PATH": "/real/playwright",
        },
    )

    assert result.returncode == 0, result.stderr
    run_dir = next(evidence_root.iterdir())
    child_env = json.loads((run_dir / "child-env.json").read_text(encoding="utf-8"))
    assert not Path(child_env["HOME"]).is_relative_to(run_dir)
    assert not Path(child_env["PRE_COMMIT_HOME"]).is_relative_to(run_dir)
    assert "omnigent-verify-runtime." in child_env["HOME"]
    assert not Path(child_env["HOME"]).exists()
    assert child_env["PLAYWRIGHT_BROWSERS_PATH"] != "/real/playwright"
    assert (
        child_env["PLAYWRIGHT_BROWSERS_PATH"] is None
        or Path(child_env["PLAYWRIGHT_BROWSERS_PATH"]).is_dir()
    )
    assert child_env["AWS_SECRET_ACCESS_KEY"] is None
    assert child_env["TEST_SECRET"] is None


@pytest.mark.parametrize(
    "profile",
    ["smoke", "collaboration", "automations", "core", "full-ui"],
)
def test_direct_ui_profiles_receive_run_owned_web_dist(
    tmp_path: Path,
    profile: str,
) -> None:
    root, script = _stage_repo(tmp_path, "exit 2")
    script.write_text(
        script.read_text(encoding="utf-8").replace('doctor "$profile"', "true", 1),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import hashlib
import binascii
import json
import os
import struct
import zipfile
import zlib
from pathlib import Path

run = Path(os.environ["OMNIGENT_VERIFY_RUN_DIR"])
dist = Path(os.environ["OMNIGENT_WEB_UI_DIST"])
(run / "ui-env.json").write_text(json.dumps({"dist": str(dist)}), encoding="utf-8")
nodeid = "tests/e2e_ui/test_fixture.py::test_case[param-one]"
identity = hashlib.sha256(nodeid.encode()).hexdigest()
context = run / "playwright" / f"test-{identity[:20]}" / "context-1"
context.mkdir(parents=True)
(context / "metadata.json").write_text(json.dumps({
    "schema_version": 1,
    "nodeid": nodeid,
    "nodeid_sha256": identity,
    "context_style": "managed-sync",
    "lifecycle": "closed",
    "events": [],
}), encoding="utf-8")
def png_chunk(kind, payload):
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xffffffff)
    )
(context / "screenshot.png").write_bytes(
    b"\\x89PNG\\r\\n\\x1a\\n"
    + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    + png_chunk(b"IDAT", zlib.compress(b"\\0\\0\\0\\0\\xff"))
    + png_chunk(b"IEND", b"")
)
with zipfile.ZipFile(context / "trace.zip", "w") as archive:
    archive.writestr("trace.trace", '{"type":"fixture"}\\n')
(run / "junit.xml").write_text(
    "<testsuite tests='1'><testcase name='test_case[param-one]'><properties>"
    f"<property name='omnigent_nodeid_sha256' value='{identity}'/>"
    "<property name='omnigent_browser_context_count' value='1'/>"
    "</properties></testcase></testsuite>",
    encoding="utf-8",
)
(run / "cleanup.json").write_text(
    '{"schema_version":1,"status":"completed"}',
    encoding="utf-8",
)
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    evidence = tmp_path / "external-evidence"

    result = _run(
        root,
        script,
        evidence,
        profile,
        extra_env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    run_dir = next(evidence.iterdir())
    child = json.loads((run_dir / "ui-env.json").read_text(encoding="utf-8"))
    assert child["dist"] == str(run_dir / "build/e2e-web-ui")
    if profile == "collaboration":
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert set(manifest["run"]["selected_tests"]) == {
            "tests/e2e_ui/collaboration/test_permissions_modal.py",
            "tests/e2e_ui/collaboration/test_sharing_journey.py",
            "tests/e2e_ui/collaboration/test_sharing_mode_off.py",
            "tests/e2e_ui/collaboration/test_collab_realtime.py",
            "tests/e2e_ui/collaboration/test_author_label.py",
        }


def test_source_mutation_forces_failed_manifest_and_nonzero_exit(tmp_path: Path) -> None:
    root, script = _stage_repo(
        tmp_path,
        "printf 'mutated\\n' >> pyproject.toml",
    )
    evidence_root = tmp_path / "evidence"

    result = _run(root, script, evidence_root, "backend")

    assert result.returncode != 0
    manifest = _only_manifest(evidence_root)
    assert manifest["status"] == "failed"
    assert manifest["repository"]["unchanged"] is False
    assert "Repository mutation invariant failed" in " ".join(manifest["limitations"])


def test_deleted_active_manifest_is_restored_and_fails_closed(tmp_path: Path) -> None:
    root, script = _stage_repo(
        tmp_path,
        'rm -f "$OMNIGENT_VERIFY_RUN_DIR/manifest.json"',
    )
    evidence = tmp_path / "evidence"

    result = _run(root, script, evidence, "backend")

    assert result.returncode != 0
    manifest = _only_manifest(evidence)
    assert manifest["status"] == "failed"
    assert "manifest was modified by a managed child" in result.stdout


def test_regenerated_exposed_snapshot_cannot_hide_source_mutation(tmp_path: Path) -> None:
    body = """printf 'mutated\\n' >> pyproject.toml
python3 .agents/skills/verify-omnigent/scripts/runtime_support.py snapshot \
  --repo-root . \
  --output "$OMNIGENT_VERIFY_RUN_DIR/repository-before.json"
"""
    root, script = _stage_repo(tmp_path, body)
    evidence = tmp_path / "evidence"

    result = _run(root, script, evidence, "backend")

    assert result.returncode != 0
    manifest = _only_manifest(evidence)
    assert manifest["status"] == "failed"
    assert manifest["repository"]["unchanged"] is False


def test_git_ref_creation_fails_repository_invariant(tmp_path: Path) -> None:
    root, script = _stage_repo(tmp_path, "git tag verifier-leaked-ref")
    evidence = tmp_path / "evidence"
    try:
        result = _run(root, script, evidence, "backend")

        assert result.returncode != 0
        manifest = _only_manifest(evidence)
        assert manifest["status"] == "failed"
        assert manifest["repository"]["unchanged"] is False
        assert "verifier-leaked-ref" in _git(root, "tag", "--list")
    finally:
        subprocess.run(
            ["git", "tag", "-d", "verifier-leaked-ref"],
            cwd=root,
            check=False,
            capture_output=True,
        )


def test_tracked_symlink_escaping_checkout_blocks_before_execution(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("original\n", encoding="utf-8")
    root, script = _stage_repo(
        tmp_path / "fixture",
        'printf "changed\\n" > escaped.txt',
    )
    escaped = root / "escaped.txt"
    escaped.symlink_to(outside)
    subprocess.run(["git", "add", "escaped.txt"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "tracked external symlink",
        ],
        cwd=root,
        check=True,
    )

    result = _run(root, script, tmp_path / "evidence", "backend")

    assert result.returncode != 0
    assert outside.read_text(encoding="utf-8") == "original\n"
    run_dir = next((tmp_path / "evidence").iterdir())
    assert "escapes the checkout" in (run_dir / "run.log").read_text(encoding="utf-8")


def test_verify_script_can_target_clean_external_checkout(tmp_path: Path) -> None:
    _, script = _stage_repo(tmp_path / "canonical", 'echo "wrong checkout"; exit 9')
    target = tmp_path / "target"
    backend = target / "scripts" / "backend-smoke.sh"
    backend.parent.mkdir(parents=True)
    (target / "pyproject.toml").write_text(
        "[project]\nname='external-fixture'\n",
        encoding="utf-8",
    )
    backend.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\necho "external checkout passed"\n',
        encoding="utf-8",
    )
    backend.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=target,
        check=True,
    )
    evidence_root = tmp_path / "evidence"

    result = _run(
        target,
        script,
        evidence_root,
        "backend",
        extra_env={"OMNIGENT_VERIFY_REPO_ROOT": str(target)},
    )

    assert result.returncode == 0, result.stderr
    assert "external checkout passed" in result.stdout
    manifest = _only_manifest(evidence_root)
    assert manifest["status"] == "passed"
    assert manifest["repository"]["dirty"] is False


def test_doctor_fails_closed_with_exact_missing_prerequisite(
    tmp_path: Path,
) -> None:
    root, script = _stage_repo(tmp_path, 'echo "unused"')
    (root / "scripts" / "backend-smoke.sh").unlink()
    evidence_root = tmp_path / "evidence"

    result = _run(root, script, evidence_root, "doctor", "backend")

    assert result.returncode != 0
    assert "missing file scripts/backend-smoke.sh" in result.stdout
    manifest = _only_manifest(evidence_root)
    assert manifest["status"] == "blocked"
    assert manifest["exit_status"] != 0


@pytest.mark.parametrize(
    "name",
    [
        "OMNIGENT_VERIFY_PARTIAL_COMPARISON",
        "OMNIGENT_VERIFY_ONLY_STEP_INDICES",
        "OMNIGENT_VERIFY_CHANGED_FILES_JSON",
        "OMNIGENT_VERIFY_DISABLE_BASELINE",
    ],
)
def test_caller_supplied_internal_controls_are_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    root, script = _stage_repo(tmp_path, 'echo "must not run"; exit 99')
    evidence_root = tmp_path / "evidence"

    result = _run(
        root,
        script,
        evidence_root,
        "backend",
        extra_env={name: "1"},
    )

    assert result.returncode == 2
    assert f"caller-supplied internal control {name} is forbidden" in result.stderr
    assert not list(evidence_root.glob("*/manifest.json"))


@pytest.mark.parametrize(
    ("signum", "signal_name", "exit_status"),
    [
        (signal.SIGINT, "INT", 130),
        (signal.SIGTERM, "TERM", 143),
        (signal.SIGHUP, "HUP", 129),
    ],
)
def test_verify_script_finalizes_interrupted_run(
    tmp_path: Path,
    signum: int,
    signal_name: str,
    exit_status: int,
) -> None:
    ready = tmp_path / "ready"
    backend_body = f"""python3 - {str(ready)!r} <<'PY' &
import os
import signal
import sys
import time
from pathlib import Path

for value in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(value, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
PY
wait"""
    root, script = _stage_repo(
        tmp_path,
        backend_body,
    )
    evidence_root = tmp_path / "evidence"
    env = {
        **os.environ,
        "OMNIGENT_VERIFY_ADAPTER": "claude-code",
        "OMNIGENT_VERIFY_EVIDENCE_DIR": str(evidence_root),
    }
    process = subprocess.Popen(
        [str(script), "backend"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready.exists():
        process.kill()
        pytest.fail("fake backend did not start")
    process.send_signal(signum)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == exit_status, stderr
    manifest = _only_manifest(evidence_root)
    assert manifest["status"] == "interrupted"
    assert manifest["signal"] == signal_name
    assert manifest["exit_status"] == exit_status
    assert "OMNIGENT_VERIFY_SUMMARY schema=1 status=interrupted" in stdout
    descendant_pid = int(ready.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


@pytest.mark.parametrize(
    ("signum", "signal_name", "exit_status"),
    [
        (signal.SIGINT, "INT", 130),
        (signal.SIGTERM, "TERM", 143),
        (signal.SIGHUP, "HUP", 129),
    ],
)
def test_verify_kills_signal_ignoring_descendant_in_escaped_session(
    tmp_path: Path,
    signum: int,
    signal_name: str,
    exit_status: int,
) -> None:
    ready = tmp_path / "escaped-ready"
    backend_body = f"""python3 - {str(ready)!r} <<'PY'
import subprocess
import sys
from pathlib import Path

code = '''import signal
import time
for value in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(value, signal.SIG_IGN)
time.sleep(60)
'''
child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
child.wait()
PY"""
    root, script = _stage_repo(tmp_path, backend_body)
    evidence_root = tmp_path / "evidence"
    process = subprocess.Popen(
        [str(script), "backend"],
        cwd=root,
        env={
            **os.environ,
            "OMNIGENT_VERIFY_ADAPTER": "claude-code",
            "OMNIGENT_VERIFY_EVIDENCE_DIR": str(evidence_root),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    descendant_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not ready.exists():
            pytest.fail("escaped descendant did not start")
        descendant_pid = int(ready.read_text(encoding="utf-8"))
        time.sleep(0.25)
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=12)

        assert process.returncode == exit_status, stderr
        manifest = _only_manifest(evidence_root)
        assert manifest["status"] == "interrupted"
        assert manifest["signal"] == signal_name
        assert "OMNIGENT_VERIFY_SUMMARY schema=1 status=interrupted" in stdout
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(descendant_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"escaped descendant {descendant_pid} survived")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if descendant_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("signum", "signal_name", "exit_status"),
    [
        (signal.SIGINT, "INT", 130),
        (signal.SIGTERM, "TERM", 143),
        (signal.SIGHUP, "HUP", 129),
    ],
)
def test_verify_kills_fast_double_forked_session(
    tmp_path: Path,
    signum: int,
    signal_name: str,
    exit_status: int,
) -> None:
    ready = tmp_path / "double-fork-ready"
    backend_body = f"""python3 - {str(ready)!r} <<'PY'
import os
import signal
import sys
import time
from pathlib import Path

first = os.fork()
if first == 0:
    os.setsid()
    second = os.fork()
    if second > 0:
        os._exit(0)
    for value in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(value, signal.SIG_IGN)
    Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(60)
    os._exit(0)
time.sleep(60)
PY"""
    root, script = _stage_repo(tmp_path, backend_body)
    evidence_root = tmp_path / "evidence"
    process = subprocess.Popen(
        [str(script), "backend"],
        cwd=root,
        env={
            **os.environ,
            "OMNIGENT_VERIFY_ADAPTER": "claude-code",
            "OMNIGENT_VERIFY_EVIDENCE_DIR": str(evidence_root),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    descendant_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        descendant_pid = int(ready.read_text(encoding="utf-8"))
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == exit_status, stderr
        manifest = _only_manifest(evidence_root)
        assert manifest["status"] == "interrupted"
        assert manifest["signal"] == signal_name
        assert "status=interrupted" in stdout
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if descendant_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGKILL)


def test_codex_installer_supports_host_and_bundle_sources(tmp_path: Path) -> None:
    installer = _SKILL_ROOT / "scripts" / "install-codex.sh"
    host_skills = tmp_path / "home" / ".codex" / "skills"
    host = subprocess.run(
        [str(installer), "--host", str(host_skills)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert host.returncode == 0, host.stderr
    installed = host_skills / "verify-omnigent"
    assert installed.is_symlink()
    assert installed.resolve() == _SKILL_ROOT.resolve()

    bundle = tmp_path / "agent"
    bundle.mkdir()
    (bundle / "config.yaml").write_text("spec_version: 1\nname: fixture\n", encoding="utf-8")
    copied = subprocess.run(
        [str(installer), "--bundle", str(bundle)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert copied.returncode == 0, copied.stderr
    bundled_skill = bundle / "skills" / "verify-omnigent"
    assert not bundled_skill.is_symlink()
    assert (bundled_skill / "SKILL.md").is_file()


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


def _stage_auto_repo(
    tmp_path: Path,
    *,
    base_behavior: str,
    candidate_behavior: str,
) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "repo"
    root.mkdir()
    skill = root / ".agents" / "skills" / "verify-omnigent"
    shutil.copytree(_SKILL_ROOT, skill)
    for script in (skill / "scripts").iterdir():
        if script.suffix in {".py", ".sh"}:
            script.chmod(0o755)
    (root / "tests" / "server" / "integration").mkdir(parents=True)
    (root / "tests" / "server" / "test_app.py").write_text("# fixture\n", encoding="utf-8")
    (root / "tests" / "server" / "integration" / "test_app.py").write_text(
        "# fixture\n",
        encoding="utf-8",
    )
    (root / "omnigent" / "server").mkdir(parents=True)
    (root / "omnigent" / "server" / "change.py").write_text("BASE = True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "package.json").write_text(
        '{"name":"fixture","packageManager":"pnpm@11.15.1"}\n',
        encoding="utf-8",
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        "node_modules/\n.artifacts/\n__pycache__/\n*.pyc\n",
        encoding="utf-8",
    )
    modules = root / "node_modules"
    (modules / ".pnpm").mkdir(parents=True)
    (modules / ".modules.yaml").write_text(
        '{"virtualStoreDir": ".pnpm", "packageManager": "pnpm@11.15.1"}\n',
        encoding="utf-8",
    )
    behavior = root / "docs" / "behavior.txt"
    behavior.parent.mkdir()
    behavior.write_text(base_behavior + "\n", encoding="utf-8")
    bin_dir = root / "fixture-bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
args="$*"
if [[ "$args" == *"pre-commit --version"* ]]; then
  echo "pre-commit 4.0"
  exit 0
fi
if [[ "$args" == *"python -c"* ]]; then
  exit 0
fi
if [[ "$args" == *"pre-commit run"* ]]; then
  echo "synthetic pre-commit passed"
  exit 0
fi
if [[ "$args" == *"pytest tests/server/test_app.py"* ]]; then
  behavior="$(< docs/behavior.txt)"
  if [[ "$behavior" == "pass" ]]; then
    for arg in "$@"; do
      if [[ "$arg" == --junitxml=* ]]; then
        junit="${arg#--junitxml=}"
        mkdir -p "$(dirname "$junit")"
        printf '%s\n' '<testsuite tests="1"><testcase name="test_server"/></testsuite>' >"$junit"
      fi
    done
    echo "synthetic server passed"
    exit 0
  fi
  if [[ "$behavior" == "sleep" ]]; then
    sleep 60
  fi
  echo "server failure"
  exit 1
fi
echo "unexpected uv invocation: $args" >&2
exit 2
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    node = bin_dir / "node"
    node.write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo "v24.0.0"; fi
exit 0
""",
        encoding="utf-8",
    )
    node.chmod(0o755)
    pnpm = bin_dir / "pnpm"
    pnpm.write_text("#!/usr/bin/env bash\necho 11.15.1\n", encoding="utf-8")
    pnpm.chmod(0o755)
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base")
    _git(root, "switch", "-q", "-c", "pr")
    behavior.write_text(candidate_behavior + "\n", encoding="utf-8")
    (root / "omnigent" / "server" / "change.py").write_text(
        "BASE = False\n",
        encoding="utf-8",
    )
    _commit(root, "candidate")
    return (
        root,
        skill / "scripts" / "verify.sh",
        {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )


def test_synthetic_auto_success_finalizes_every_child(tmp_path: Path) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior="pass",
        candidate_behavior="pass",
    )
    evidence = tmp_path / "evidence"

    result = _run(root, script, evidence, "auto", "--base-ref", "main", extra_env=env)

    assert result.returncode == 0, result.stdout
    manifest = _only_manifest(evidence)
    assert manifest["status"] == "passed"
    assert [child["status"] for child in manifest["children"]] == ["passed", "passed"]
    assert manifest["cleanup"]["status"] == "completed"
    assert "selected lanes: quality-gates, server" in result.stdout


def test_quality_doctor_checks_only_selected_web_prerequisites(tmp_path: Path) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior="pass",
        candidate_behavior="pass",
    )
    shutil.rmtree(root / "node_modules")
    (root / "fixture-bin" / "node").unlink()
    (root / "fixture-bin" / "pnpm").unlink()
    env["PATH"] = f"{root / 'fixture-bin'}:{Path(sys.executable).parent}:/usr/bin:/bin"
    evidence = tmp_path / "python-only-evidence"

    python_only = _run(
        root,
        script,
        evidence,
        "doctor",
        "quality-gates",
        "--base-ref",
        "main",
        extra_env=env,
    )

    assert python_only.returncode == 0, python_only.stdout
    (root / "web").mkdir()
    (root / "web" / "change.ts").write_text("export const changed = true;\n", encoding="utf-8")
    web_evidence = tmp_path / "web-evidence"
    web = _run(
        root,
        script,
        web_evidence,
        "doctor",
        "quality-gates",
        "--base-ref",
        "main",
        extra_env=env,
    )
    assert web.returncode != 0
    assert "command pnpm is unavailable" in web.stdout


def test_quality_doctor_ignores_path_pnpm_until_tools_are_prepared(
    tmp_path: Path,
) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior="pass",
        candidate_behavior="pass",
    )
    (root / "web").mkdir()
    (root / "web" / "change.ts").write_text("export const changed = true;\n", encoding="utf-8")
    (root / "fixture-bin" / "pnpm").write_text(
        "#!/usr/bin/env bash\necho 99.0.0\n",
        encoding="utf-8",
    )
    env["OMNIGENT_VERIFY_PNPM_CACHE_ROOT"] = str(tmp_path / "empty-pnpm-cache")

    result = _run(
        root,
        script,
        tmp_path / "evidence",
        "doctor",
        "quality-gates",
        "--base-ref",
        "main",
        extra_env=env,
    )

    assert result.returncode != 0
    assert "99.0.0" not in result.stdout
    assert ".agents/skills/verify-omnigent/scripts/verify.sh prepare-tools" in result.stdout


def test_quality_doctor_uses_prepared_pnpm_instead_of_path_shim(
    tmp_path: Path,
) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior="pass",
        candidate_behavior="pass",
    )
    (root / "web").mkdir()
    (root / "web" / "change.ts").write_text("export const changed = true;\n", encoding="utf-8")
    (root / "fixture-bin" / "pnpm").write_text(
        "#!/usr/bin/env bash\necho 99.0.0\n",
        encoding="utf-8",
    )
    (root / "fixture-bin" / "node").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "--version" ]]; then echo "v24.0.0"; exit 0; fi\n'
        'if [[ "${1:-}" == *pnpm.mjs ]]; then echo 11.15.1; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    helper_path = root / ".agents/skills/verify-omnigent/scripts/runtime_support.py"
    spec = importlib.util.spec_from_file_location("doctor_pnpm_runtime", helper_path)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    package = tmp_path / "local-pnpm"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "pnpm", "version": "11.15.1"}),
        encoding="utf-8",
    )
    (package / "bin/pnpm.mjs").write_text("console.log('11.15.1');\n", encoding="utf-8")
    cache_parent = tmp_path / "cache"
    cache_parent.mkdir()
    staging = cache_parent / "pnpm.version.prepared"
    helper.prepare_pnpm_cache(root, staging, candidates=(package,))
    destination = cache_parent / "pnpm"
    helper.publish_pnpm_cache(staging, destination)
    env["OMNIGENT_VERIFY_PNPM_CACHE_ROOT"] = str(destination)

    result = _run(
        root,
        script,
        tmp_path / "evidence",
        "doctor",
        "quality-gates",
        "--base-ref",
        "main",
        extra_env=env,
    )

    assert "99.0.0" not in result.stdout
    assert "prepare-tools" not in result.stdout
    assert "pnpm 11.15.1 does not match" not in result.stdout


def test_prepare_hooks_builds_authenticated_dedicated_cache(tmp_path: Path) -> None:
    root, script = _stage_repo(tmp_path, "true")
    remote = "https://example.invalid/pre-commit-hooks"
    revision = "v4.6.0"
    (root / ".pre-commit-config.yaml").write_text(
        f"repos:\n  - repo: {remote}\n    rev: {revision}\n    hooks:\n      - id: fixture\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sqlite3\n"
        "cache=pathlib.Path(os.environ['PRE_COMMIT_HOME'])\n"
        "prepared=cache/'repo-prepared'\n"
        "prepared.mkdir(parents=True)\n"
        "(prepared/'.pre-commit-hooks.yaml').write_text("
        '"- id: fixture\\n  name: fixture\\n  entry: true\\n  language: system\\n")\n'
        "with sqlite3.connect(cache/'db.db') as db:\n"
        " db.execute('CREATE TABLE repos "
        "(repo TEXT NOT NULL, ref TEXT NOT NULL, path TEXT NOT NULL, "
        "PRIMARY KEY (repo, ref))')\n"
        f" db.execute('INSERT INTO repos VALUES (?, ?, ?)', "
        f"({remote!r}, {revision!r}, str(prepared)))\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    result = _run(
        root,
        script,
        tmp_path / "unused-evidence",
        "prepare-hooks",
        extra_env={"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    cache = home / ".cache/verify-omnigent/pre-commit"
    assert result.returncode == 0, result.stderr
    seal = json.loads((cache / "omnigent-prepared-hooks.json").read_text())
    assert [(item["repo"], item["rev"]) for item in seal["hooks"]] == [(remote, revision)]
    assert not (tmp_path / "unused-evidence").exists()

    with sqlite3.connect(cache / "db.db") as connection:
        prepared = Path(connection.execute("SELECT path FROM repos").fetchone()[0])
    (prepared / "tampered.txt").write_text("tampered\n", encoding="utf-8")
    helper_path = root / ".agents/skills/verify-omnigent/scripts/runtime_support.py"
    spec = importlib.util.spec_from_file_location("prepared_runtime_support", helper_path)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    with pytest.raises(RuntimeError, match="not prepared"):
        helper.strict_environment(
            tmp_path / "run",
            source={"HOME": str(home), "PATH": os.environ["PATH"]},
            extra={"OMNIGENT_VERIFY_REPO_ROOT": str(root)},
            pre_commit=True,
        )
    helper.cleanup_strict_environment(tmp_path / "run")


def test_quality_doctor_names_exact_hook_preparation_command(tmp_path: Path) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior="pass",
        candidate_behavior="pass",
    )
    (root / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: https://example.invalid/hooks\n"
        "    rev: v1\n"
        "    hooks:\n"
        "      - id: fixture\n",
        encoding="utf-8",
    )
    home = tmp_path / "empty-home"
    home.mkdir()

    result = _run(
        root,
        script,
        tmp_path / "evidence",
        "doctor",
        "quality-gates",
        "--base-ref",
        "main",
        extra_env={**env, "HOME": str(home)},
    )

    assert result.returncode != 0
    assert ".agents/skills/verify-omnigent/scripts/verify.sh prepare-hooks" in result.stdout


@pytest.mark.parametrize(
    ("base_behavior", "classification"),
    [("pass", "pr_only_failure"), ("fail", "baseline_reproduced")],
)
def test_auto_classifies_exact_base_failure_without_waiving_it(
    tmp_path: Path,
    base_behavior: str,
    classification: str,
) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior=base_behavior,
        candidate_behavior="fail",
    )
    evidence = tmp_path / "evidence"

    result = _run(root, script, evidence, "auto", "--base-ref", "main", extra_env=env)

    assert result.returncode != 0
    manifest = _only_manifest(evidence)
    assert manifest["status"] == "failed"
    server = next(child for child in manifest["children"] if child["lane"] == "server")
    assert server["status"] == "failed"
    assert server["baseline_comparison"]["classification"] == classification
    comparison = json.loads(
        (next(evidence.glob("*")) / server["baseline_comparison"]["comparison"]).read_text(
            encoding="utf-8"
        )
    )
    assert comparison["cleanup"]["status"] == "completed"


def test_auto_blocks_before_lanes_when_prerequisite_is_missing(tmp_path: Path) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior="pass",
        candidate_behavior="pass",
    )
    (root / "tests" / "server" / "test_app.py").unlink()
    evidence = tmp_path / "evidence"

    result = _run(root, script, evidence, "auto", "--base-ref", "main", extra_env=env)

    assert result.returncode != 0
    assert "Restore tests/server/test_app.py" in result.stdout
    manifest = _only_manifest(evidence)
    assert manifest["status"] == "blocked"
    assert all(child["status"] == "blocked" for child in manifest["children"])
    assert all(child["manifest"] is None for child in manifest["children"])


def test_doctor_auto_records_plan_blocker_as_blocked(tmp_path: Path) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior="pass",
        candidate_behavior="pass",
    )
    unknown = root / "tests" / "tools" / "test_unknown_surface.py"
    unknown.parent.mkdir(parents=True)
    unknown.write_text("# unmapped fixture\n", encoding="utf-8")
    _commit(root, "add unmapped path")
    evidence = tmp_path / "evidence"

    result = _run(
        root,
        script,
        evidence,
        "doctor",
        "auto",
        "--base-ref",
        "main",
        extra_env=env,
    )

    assert result.returncode != 0
    assert _only_manifest(evidence)["status"] == "blocked"
    assert "next command:" in result.stdout


def test_interrupted_auto_finalizes_parent_and_active_child(tmp_path: Path) -> None:
    root, script, env = _stage_auto_repo(
        tmp_path,
        base_behavior="pass",
        candidate_behavior="sleep",
    )
    evidence = tmp_path / "evidence"
    process = subprocess.Popen(
        [str(script), "auto", "--base-ref", "main"],
        cwd=root,
        env={**os.environ, **env, "OMNIGENT_VERIFY_EVIDENCE_DIR": str(evidence)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 45
    parent_path: Path | None = None
    while time.monotonic() < deadline:
        manifests = list(evidence.glob("*/manifest.json"))
        if manifests:
            parent_path = manifests[0]
            value = json.loads(parent_path.read_text(encoding="utf-8"))
            server = next(
                (child for child in value.get("children", []) if child.get("lane") == "server"),
                None,
            )
            child_manifests = list(parent_path.parent.glob("children/server/*/manifest.json"))
            if server and server.get("status") == "running" and child_manifests:
                break
        time.sleep(0.05)
    else:
        process.kill()
        pytest.fail("synthetic server lane did not start")
    process.terminate()
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 143, stderr
    assert parent_path is not None
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    assert parent["status"] == "interrupted"
    assert all(child["status"] != "running" for child in parent["children"])
    server = next(child for child in parent["children"] if child["lane"] == "server")
    child = json.loads((parent_path.parent / server["manifest"]).read_text(encoding="utf-8"))
    assert child["status"] == "interrupted"
    assert "status=interrupted" in stdout
