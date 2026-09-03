from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELPER = (
    _REPO_ROOT / ".agents" / "skills" / "verify-omnigent" / "scripts" / "universe_preflight.py"
)
_FIXTURES = Path(__file__).parent / "fixtures"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_universe_preflight", _HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_pin_mismatch_records_not_synced_without_runtime_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    pins = json.loads((_FIXTURES / "pin_mismatch.json").read_text(encoding="utf-8"))
    universe = tmp_path / "universe"
    sync = universe / "agentbricks" / "mas" / "third_party" / "sync"
    (sync / "local-patches").mkdir(parents=True)
    (sync / "UPSTREAM_REF").write_text(pins["vendored"], encoding="utf-8")
    (sync / "UI_UPSTREAM_REF").write_text(pins["vendored"], encoding="utf-8")

    monkeypatch.setattr(
        helper,
        "_resolve_commit",
        lambda _root, _ref: (pins["requested"], None),
    )
    monkeypatch.setattr(
        helper,
        "_changed_files",
        lambda *_args: (["omnigent/server/app.py"], None),
    )
    monkeypatch.setattr(helper, "_merge_base", lambda *_args: (pins["requested"], None))
    monkeypatch.setattr(helper, "_git_status_digest", lambda _root: ("stable", None))
    monkeypatch.setattr(helper, "_dirty_paths", lambda *_args, **_kwargs: ([], None))
    monkeypatch.setattr(
        helper,
        "_run_bazel_test",
        lambda _root, target, _output, _system: {
            "target": target,
            "status": "passed",
            "tests": 1,
            "failures": 0,
            "errors": 0,
        },
    )

    report, exit_status = helper.run_preflight(
        _REPO_ROOT,
        tmp_path / "run",
        "requested",
        "base",
        str(universe),
        "Linux",
    )

    assert exit_status != 0
    assert report["status"] == "not_synced"
    assert report["sync"]["status"] == "not_synced"
    assert report["runtime_compatibility"]["status"] == "not_synced"
    assert not any(
        isinstance(target, dict) and target.get("status") == "passed"
        for target in report["runtime_compatibility"]["targets"]
    )


def test_dirty_oss_product_change_blocks_universe_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.setattr(
        helper,
        "_dirty_paths",
        lambda root, **_kwargs: (["web/src/dirty.ts"], None) if root == tmp_path else ([], None),
    )

    report, exit_status = helper.run_preflight(
        tmp_path,
        tmp_path / "run",
        "HEAD",
        "HEAD",
        None,
        "Linux",
    )

    assert exit_status != 0
    assert "tracked or non-ignored untracked" in " ".join(report["limitations"])


def test_dirty_universe_checkout_blocks_before_bazel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    oss = tmp_path / "oss"
    universe = tmp_path / "universe"
    sync = universe / "agentbricks/mas/third_party/sync"
    sync.mkdir(parents=True)
    (sync / "UPSTREAM_REF").write_text("a" * 40, encoding="utf-8")
    monkeypatch.setattr(helper, "_resolve_commit", lambda *_args: ("a" * 40, None))
    monkeypatch.setattr(helper, "_merge_base", lambda *_args: ("a" * 40, None))
    monkeypatch.setattr(
        helper,
        "_dirty_paths",
        lambda root, **_kwargs: (
            (["agentbricks/mas/dirty.py"], None) if root == universe else ([], None)
        ),
    )

    report, exit_status = helper.run_preflight(
        oss,
        tmp_path / "run",
        "HEAD",
        "HEAD",
        str(universe),
        "Linux",
    )

    assert exit_status != 0
    assert report["status"] == "unavailable"
    assert "non-ignored untracked" in " ".join(report["limitations"])


@pytest.mark.parametrize("relative", [".bazelrc", "agentbricks/mas/new_source.py"])
def test_untracked_universe_inputs_are_dirty(tmp_path: Path, relative: str) -> None:
    helper = _load_helper()
    repo = tmp_path / "universe"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture\n", encoding="utf-8")

    dirty, error = helper._dirty_paths(repo, include_untracked=True)

    assert error is None
    assert relative in dirty


def test_ignored_universe_output_is_not_dirty(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = tmp_path / "universe"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("bazel-out/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    output = repo / "bazel-out/generated.txt"
    output.parent.mkdir()
    output.write_text("generated\n", encoding="utf-8")

    dirty, error = helper._dirty_paths(repo, include_untracked=True)

    assert error is None
    assert dirty == []


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["omnigent/server/app.py"], ["python"]),
        (["web/src/App.tsx"], ["ui"]),
        (["omnigent/server/app.py", "web/src/App.tsx"], ["python", "ui"]),
        (["deploy/databricks/deploy.py"], []),
    ],
)
def test_changed_surfaces_select_their_universe_pins(
    changed: list[str],
    expected: list[str],
) -> None:
    helper = _load_helper()

    assert helper._required_sync_pins(changed) == expected


def test_verification_or_deploy_only_diff_is_not_applicable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    pins = "a" * 40
    universe = tmp_path / "universe"
    sync = universe / "agentbricks" / "mas" / "third_party" / "sync"
    (sync / "local-patches").mkdir(parents=True)
    (sync / "UPSTREAM_REF").write_text(pins, encoding="utf-8")
    monkeypatch.setattr(helper, "_resolve_commit", lambda *_args: (pins, None))
    monkeypatch.setattr(helper, "_merge_base", lambda *_args: (pins, None))
    monkeypatch.setattr(
        helper,
        "_changed_files",
        lambda *_args: (["deploy/databricks/deploy.py"], None),
    )
    monkeypatch.setattr(helper, "_dirty_paths", lambda *_args, **_kwargs: ([], None))

    def reject_bazel(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("Bazel must not run")

    monkeypatch.setattr(helper, "_run_bazel_test", reject_bazel)

    report, status = helper.run_preflight(
        tmp_path / "oss",
        tmp_path / "run",
        "HEAD",
        "HEAD",
        str(universe),
        "Linux",
    )

    assert status == 0
    assert report["status"] == "not_applicable"
    assert report["sync"]["required_pins"] == []
    assert report["runtime_compatibility"]["status"] == "not_applicable"


def test_passing_bazel_xml_requires_positive_test_count() -> None:
    helper = _load_helper()
    result = helper.parse_bazel_test_result(
        exit_status=0,
        xml_path=_FIXTURES / "bazel_passing.xml",
        output="PASSED",
        system="Linux",
    )

    assert result["status"] == "passed"
    assert result["tests"] == 2
    assert result["failures"] == 0
    assert result["errors"] == 0


def test_all_skipped_bazel_xml_fails() -> None:
    helper = _load_helper()

    result = helper.parse_bazel_test_result(
        exit_status=0,
        xml_path=_FIXTURES / "bazel_all_skipped.xml",
        output="PASSED",
        system="Linux",
    )

    assert result["status"] == "failed"
    assert result["skipped"] == 2
    assert result["reason"] == "test.xml recorded no non-skipped tests."


def test_diverged_ref_change_discovery_uses_merge_base(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    _commit = [
        "git",
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.com",
        "commit",
        "-qm",
    ]
    subprocess.run([*_commit, "base"], cwd=repo, check=True)
    subprocess.run(["git", "switch", "-q", "-c", "requested"], cwd=repo, check=True)
    (repo / "requested.py").write_text("requested\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run([*_commit, "requested"], cwd=repo, check=True)
    requested = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "switch", "-q", "main"], cwd=repo, check=True)
    (repo / "base-only.py").write_text("base only\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run([*_commit, "base-only"], cwd=repo, check=True)
    main = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    merge_base, error = helper._merge_base(repo, main, requested)
    assert error is None and merge_base is not None
    changed, error = helper._changed_files(repo, merge_base, requested)

    assert error is None
    assert changed == ["requested.py"]


def test_universe_change_discovery_preserves_special_filenames(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    special = 'web/src/line\nbreak\tquote"\\name.ts'
    path = repo / special
    path.parent.mkdir(parents=True)
    path.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "change",
        ],
        cwd=repo,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    files, error = helper._changed_files(repo, base, head, "web")

    assert error is None
    assert files == [special]


def test_zero_test_jemalloc_failure_is_a_darwin_blocker(tmp_path: Path) -> None:
    helper = _load_helper()
    output = (_FIXTURES / "bazel_zero_test_jemalloc.log").read_text(encoding="utf-8")

    result = helper.parse_bazel_test_result(
        exit_status=1,
        xml_path=tmp_path / "missing.xml",
        output=output,
        system="Darwin",
    )

    assert result["status"] == "blocked"
    assert result["tests"] == 0
    assert result["blocker"] == "darwin_jemalloc_features_h"
    assert "zero tests" in result["reason"]


def test_psutil_collection_error_is_a_darwin_blocker() -> None:
    helper = _load_helper()
    result = helper.parse_bazel_test_result(
        exit_status=1,
        xml_path=_FIXTURES / "bazel_psutil_error.xml",
        output="",
        system="Darwin",
    )

    assert result["status"] == "blocked"
    assert result["tests"] == 1
    assert result["errors"] == 1
    assert result["blocker"] == "darwin_psutil_extension"


def test_patch_apply_check_uses_temporary_tree(tmp_path: Path) -> None:
    helper = _load_helper()
    repo = tmp_path / "oss"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source = repo / "omnigent" / "host" / "connect.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 'upstream'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
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
        cwd=repo,
        check=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    patches_root = tmp_path / "patches"
    patch = patches_root / "omnigent" / "host" / "connect.py.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text(
        """\
diff --git a/omnigent/host/connect.py b/omnigent/host/connect.py
--- a/omnigent/host/connect.py
+++ b/omnigent/host/connect.py
@@ -1 +1 @@
-value = 'upstream'
+value = 'downstream'
""",
        encoding="utf-8",
    )

    result = helper.check_patch_apply(repo, commit, [patch], patches_root)

    assert result["status"] == "passed"
    assert source.read_text(encoding="utf-8") == "value = 'upstream'\n"
