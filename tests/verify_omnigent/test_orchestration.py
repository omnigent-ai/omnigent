from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / ".agents" / "skills" / "verify-omnigent" / "scripts"


def _load(name: str) -> ModuleType:
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"verify_omnigent_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fsmonitor_daemon_pids() -> set[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        int(line.split(None, 1)[0])
        for line in result.stdout.splitlines()
        if "fsmonitor--daemon run" in line
    }


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


def _diverged_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for path in ("tracked-staged.txt", "tracked-unstaged.txt"):
        (repo / path).write_text("base\n", encoding="utf-8")
    _commit(repo, "base")

    _git(repo, "switch", "-q", "-c", "pr")
    pr_only = repo / "omnigent" / "pr_only.py"
    pr_only.parent.mkdir()
    pr_only.write_text("PR_ONLY = True\n", encoding="utf-8")
    _commit(repo, "pr change")

    _git(repo, "switch", "-q", "main")
    base_only = repo / "web" / "base_only.ts"
    base_only.parent.mkdir()
    base_only.write_text("export const baseOnly = true;\n", encoding="utf-8")
    _commit(repo, "newer base change")

    _git(repo, "switch", "-q", "pr")
    (repo / "tracked-staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "tracked-staged.txt")
    (repo / "tracked-unstaged.txt").write_text("unstaged\n", encoding="utf-8")
    untracked = repo / "tests" / "host" / "test_untracked.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("def test_placeholder(): pass\n", encoding="utf-8")
    return repo


def test_disposable_repos_do_not_leak_fsmonitor_daemons(tmp_path: Path) -> None:
    before = _fsmonitor_daemon_pids()
    for index in range(3):
        repo = tmp_path / f"fsmonitor-{index}"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        assert _git(repo, "config", "--bool", "core.fsmonitor") == "false"
        _git(repo, "status", "--porcelain=v1")
    time.sleep(0.2)
    assert _fsmonitor_daemon_pids() - before == set()


def test_changed_files_select_every_applicable_lane() -> None:
    helper = _load("orchestration")
    plan = helper.select_lanes(
        [
            "omnigent/server/app.py",
            "omnigent/cli.py",
            "sdks/python-client/omnigent_client/client.py",
            "web/src/App.tsx",
            "web/electron/src/main.js",
        ]
    )

    assert plan["selected_lanes"] == [
        "quality-gates",
        "server",
        "harness-client",
        "cli",
        "web-ui",
        "desktop",
    ]
    assert plan["blockers"] == []
    assert all(
        decision["reasons"] != ["No changed file mapped to this lane."]
        for decision in plan["decisions"]
        if decision["status"] == "selected"
    )


def test_integrations_map_and_unknown_product_paths_fail_closed() -> None:
    helper = _load("orchestration")

    integration = helper.select_lanes(["integrations/slack/src/omnigent_slack/app.py"])
    unknown = helper.select_lanes(["config/new-runtime-surface.yaml"])

    assert integration["selected_lanes"] == [
        "quality-gates",
        "server",
        "harness-client",
    ]
    assert integration["blockers"] == []
    assert unknown["selected_lanes"] == []
    assert "did not map to a verification lane" in unknown["blockers"][0]


def test_executable_config_maps_to_quality_and_unknown_text_blocks() -> None:
    helper = _load("orchestration")

    config = helper.select_lanes([".pre-commit-config.yaml"])
    unknown = helper.select_lanes(["notes/internal-state.txt"])

    assert config["selected_lanes"] == ["quality-gates"]
    assert config["blockers"] == []
    assert unknown["selected_lanes"] == []
    assert unknown["blockers"]


def test_strict_environment_reuses_verified_browser_cache(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    home = tmp_path / "real-home"
    browser = (
        home
        / "Library/Caches/ms-playwright/chromium-123/chrome-mac-arm64"
        / "Chromium.app/Contents/MacOS/Chromium"
    )
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"chromium")
    browser.chmod(0o755)
    marker = home / "Library/Caches/ms-playwright/chromium-123/INSTALLATION_COMPLETE"
    marker.write_text("installed\n", encoding="utf-8")

    env = helper.strict_environment(
        tmp_path / "run",
        source={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert env["HOME"] != str(home)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(home / "Library/Caches/ms-playwright")
    assert not (tmp_path / "run/environment/cache/playwright").exists()
    before = helper.playwright_browser_snapshot(env)
    marker.write_text("mutated\n", encoding="utf-8")
    assert helper.playwright_browser_snapshot(env) != before


def test_dependency_binary_mutation_invalidates_snapshot(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    binary = repo / "web/node_modules/.bin/vite"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"before")
    _git(repo, "init", "-q", "-b", "main")
    before = helper.repository_snapshot(repo)

    binary.write_bytes(b"after")
    changes = helper.compare_snapshots(before, helper.repository_snapshot(repo))

    assert any("dependency metadata changed" in change for change in changes)


def test_nested_dependency_mutation_invalidates_deep_snapshot(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    nested = repo / ".venv/lib/python/site-packages/package/module.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("before\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    before = helper.repository_snapshot(repo, deep_dependencies=True)

    nested.write_text("after\n", encoding="utf-8")
    changes = helper.compare_snapshots(
        before,
        helper.repository_snapshot(repo, deep_dependencies=True),
    )

    assert "dependency trees changed: .venv" in changes


def test_detach_at_same_commit_changes_git_identity(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    before = helper.repository_snapshot(repo)

    _git(repo, "switch", "--detach", "-q")
    changes = helper.compare_snapshots(before, helper.repository_snapshot(repo))

    assert "Git refs, HEAD, or index changed" in changes


def test_staged_index_content_changes_git_identity(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _commit(repo, "base")
    before = helper.repository_snapshot(repo)
    tracked.write_text("after\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")

    after = helper.repository_snapshot(repo)

    assert before["git_identity"]["index"] != after["git_identity"]["index"]
    assert "Git refs, HEAD, or index changed" in helper.compare_snapshots(before, after)


def test_repository_snapshot_does_not_create_git_objects(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    (repo / "staged.txt").write_text("new staged content\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    objects = repo / ".git" / "objects"
    before = {path.relative_to(objects) for path in objects.rglob("*") if path.is_file()}

    helper.repository_snapshot(repo)

    after = {path.relative_to(objects) for path in objects.rglob("*") if path.is_file()}
    assert after == before


def test_local_pre_commit_uses_empty_disposable_cache(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks: []\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    source = home / ".cache/pre-commit"
    source.mkdir(parents=True)
    (source / "db.db").write_bytes(b"database")
    hook = source / "repo123/hook.py"
    hook.parent.mkdir()
    hook.write_text("original\n", encoding="utf-8")
    run_dir = tmp_path / "run"

    env = helper.strict_environment(
        run_dir,
        source={"HOME": str(home), "PATH": os.environ["PATH"]},
        extra={"OMNIGENT_VERIFY_REPO_ROOT": str(repo)},
        pre_commit=True,
    )
    private_cache = Path(env["PRE_COMMIT_HOME"])

    assert not private_cache.is_relative_to(run_dir)
    assert list(private_cache.iterdir()) == []
    assert hook.read_text(encoding="utf-8") == "original\n"
    helper.cleanup_strict_environment(run_dir)
    assert not private_cache.exists()


def test_remote_pre_commit_stages_only_exact_configured_revision(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    repo.mkdir()
    remote = "https://example.invalid/configured-hooks"
    revision = "v1.2.3"
    (repo / ".pre-commit-config.yaml").write_text(
        f"repos:\n  - repo: {remote}\n    rev: {revision}\n    hooks: []\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    cache = home / ".cache/pre-commit"
    configured = cache / "repo-configured"
    unrelated = cache / "repo-unrelated"
    configured.mkdir(parents=True)
    unrelated.mkdir(parents=True)
    (configured / "hook.py").write_text("configured\n", encoding="utf-8")
    (unrelated / "secret.txt").write_text("unrelated-global-marker\n", encoding="utf-8")
    with sqlite3.connect(cache / "db.db") as connection:
        connection.execute(
            "CREATE TABLE repos "
            "(repo TEXT NOT NULL, ref TEXT NOT NULL, path TEXT NOT NULL, "
            "PRIMARY KEY (repo, ref))"
        )
        connection.executemany(
            "INSERT INTO repos(repo, ref, path) VALUES (?, ?, ?)",
            [
                (remote, revision, str(configured)),
                ("https://example.invalid/unrelated", "v9", str(unrelated)),
            ],
        )
    helper.seal_pre_commit_cache(repo, cache)
    run_dir = tmp_path / "run"

    env = helper.strict_environment(
        run_dir,
        source={"HOME": str(home), "PATH": os.environ["PATH"]},
        extra={"OMNIGENT_VERIFY_REPO_ROOT": str(repo)},
        pre_commit=True,
    )
    private_cache = Path(env["PRE_COMMIT_HOME"])

    assert list(private_cache.glob("repo-*/hook.py"))
    assert not list(private_cache.rglob("secret.txt"))
    helper.cleanup_strict_environment(run_dir)
    assert not private_cache.exists()


def test_prepared_remote_system_hook_runs_from_disposable_cache(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    remote = "https://example.invalid/prepared-hooks"
    revision = "v1"
    (repo / ".pre-commit-config.yaml").write_text(
        f"repos:\n  - repo: {remote}\n    rev: {revision}\n"
        "    hooks:\n      - id: offline-check\n",
        encoding="utf-8",
    )
    (repo / "tracked.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    home = tmp_path / "home"
    cache = home / ".cache/pre-commit"
    prepared = cache / "repo-prepared"
    prepared.mkdir(parents=True)
    (prepared / ".pre-commit-hooks.yaml").write_text(
        "- id: offline-check\n"
        "  name: offline check\n"
        f"  entry: {sys.executable} -c 'raise SystemExit(0)'\n"
        "  language: system\n"
        "  pass_filenames: false\n",
        encoding="utf-8",
    )
    with sqlite3.connect(cache / "db.db") as connection:
        connection.execute(
            "CREATE TABLE repos "
            "(repo TEXT NOT NULL, ref TEXT NOT NULL, path TEXT NOT NULL, "
            "PRIMARY KEY (repo, ref))"
        )
        connection.execute(
            "INSERT INTO repos(repo, ref, path) VALUES (?, ?, ?)",
            (remote, revision, str(prepared)),
        )
    helper.seal_pre_commit_cache(repo, cache)
    run_dir = tmp_path / "run"
    env = helper.strict_environment(
        run_dir,
        source={"HOME": str(home), "PATH": os.environ["PATH"]},
        extra={"OMNIGENT_VERIFY_REPO_ROOT": str(repo)},
        pre_commit=True,
    )

    result = subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", "--all-files"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    helper.cleanup_strict_environment(run_dir)
    assert result.returncode == 0, result.stderr
    assert not Path(env["PRE_COMMIT_HOME"]).exists()


def test_hook_cache_publication_failure_preserves_last_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("runtime_support")
    parent = tmp_path / "cache"
    parent.mkdir()
    previous = parent / "pre-commit.version.previous"
    previous.mkdir()
    (previous / "marker").write_text("last-valid\n", encoding="utf-8")
    destination = parent / "pre-commit"
    destination.symlink_to(previous.name, target_is_directory=True)
    staging = parent / "pre-commit.version.new"
    staging.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    helper.seal_pre_commit_cache(repo, staging)
    real_replace = os.replace

    def fail_publish(source: str | Path, target: str | Path) -> None:
        if Path(source).name.startswith(".pre-commit.pointer."):
            raise OSError("simulated publication failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_publish)

    with pytest.raises(OSError, match="simulated"):
        helper.publish_pre_commit_cache(repo, staging, destination)

    assert (destination / "marker").read_text(encoding="utf-8") == "last-valid\n"
    assert destination.resolve() == previous
    assert staging.exists()


def test_hook_cache_publication_waits_for_active_reader_lease(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    parent = tmp_path / "cache"
    parent.mkdir()
    destination = parent / "pre-commit"
    previous = parent / "pre-commit.version.previous"
    previous.mkdir()
    destination.symlink_to(previous.name, target_is_directory=True)
    staging = parent / "pre-commit.version.new"
    staging.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    helper.seal_pre_commit_cache(repo, staging)
    published = threading.Event()

    def publish() -> None:
        helper.publish_pre_commit_cache(repo, staging, destination)
        published.set()

    with helper._hook_cache_lock(destination, exclusive=False):
        thread = threading.Thread(target=publish)
        thread.start()
        time.sleep(0.1)
        assert not published.is_set()
    thread.join(timeout=5)

    assert published.is_set()
    assert destination.resolve() == staging


def test_hook_cache_gc_failure_keeps_new_active_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("runtime_support")
    parent = tmp_path / "cache"
    parent.mkdir()
    previous = parent / "pre-commit.version.previous"
    previous.mkdir()
    destination = parent / "pre-commit"
    destination.symlink_to(previous.name, target_is_directory=True)
    staging = parent / "pre-commit.version.new"
    staging.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    helper.seal_pre_commit_cache(repo, staging)
    real_rmtree = helper.shutil.rmtree

    def fail_old(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path) == previous:
            raise OSError("simulated stale GC failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(helper.shutil, "rmtree", fail_old)
    helper.publish_pre_commit_cache(repo, staging, destination)

    assert destination.resolve() == staging
    assert previous.exists()


def test_credentialed_environment_profile_strips_conflicting_ambient_credentials(
    tmp_path: Path,
) -> None:
    helper = _load("runtime_support")
    home = tmp_path / "real-home"
    home.mkdir()
    source_config = home / ".databrickscfg"
    source_config.write_text(
        "[selected]\nhost = https://selected.invalid\ntoken = fake-selected-marker\n"
        "[other]\nhost = https://other.invalid\ntoken = fake-other-marker\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    env = helper.strict_environment(
        run_dir,
        source={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "CURSOR_API_KEY": "fake-cursor-marker",
            "OPENAI_API_KEY": "fake-openai-marker",
            "OPENAI_BASE_URL": "https://ambient-openai.invalid",
            "DATABRICKS_HOST": "https://ambient-databricks.invalid",
            "DATABRICKS_TOKEN": "fake-databricks-marker",
        },
        extra={
            "OMNIGENT_VERIFY_DATABRICKS_PROFILE": "selected",
            "OMNIGENT_VERIFY_HARNESS": "cursor",
        },
        credentialed=True,
    )

    staged = Path(env["DATABRICKS_CONFIG_FILE"])
    text = staged.read_text(encoding="utf-8")
    assert env["DATABRICKS_CONFIG_PROFILE"] == "selected"
    assert env["OMNIGENT_VERIFY_CREDENTIAL_MODE"] == "databricks-profile"
    assert (
        not {
            "CURSOR_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "DATABRICKS_HOST",
            "DATABRICKS_TOKEN",
        }
        & env.keys()
    )
    assert "fake-selected-marker" in text
    assert "fake-other-marker" not in text
    assert not staged.is_relative_to(run_dir)
    assert not any(
        "fake-selected-marker" in path.read_text(encoding="utf-8", errors="ignore")
        for path in run_dir.rglob("*")
        if path.is_file()
    )
    helper.cleanup_strict_environment(run_dir)
    assert not staged.exists()


@pytest.mark.parametrize(
    ("source", "harness", "mode", "present", "absent"),
    [
        (
            {
                "OPENAI_API_KEY": "fake-openai-marker",
                "OPENAI_BASE_URL": "https://openai.invalid",
                "DATABRICKS_HOST": "https://ignored.invalid",
                "DATABRICKS_TOKEN": "fake-ignored-marker",
            },
            "claude-sdk",
            "openai-environment",
            {"OPENAI_API_KEY", "OPENAI_BASE_URL"},
            {"DATABRICKS_HOST", "DATABRICKS_TOKEN", "CURSOR_API_KEY"},
        ),
        (
            {
                "DATABRICKS_HOST": "https://databricks.invalid",
                "DATABRICKS_TOKEN": "fake-databricks-marker",
                "CURSOR_API_KEY": "fake-ignored-marker",
            },
            "claude-sdk",
            "databricks-environment",
            {"DATABRICKS_HOST", "DATABRICKS_TOKEN"},
            {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CURSOR_API_KEY"},
        ),
        (
            {"CURSOR_API_KEY": "fake-cursor-marker"},
            "cursor",
            "cursor-api-key",
            {"CURSOR_API_KEY"},
            {
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "DATABRICKS_HOST",
                "DATABRICKS_TOKEN",
            },
        ),
    ],
)
def test_credentialed_environment_labels_non_profile_mode_used(
    tmp_path: Path,
    source: dict[str, str],
    harness: str,
    mode: str,
    present: set[str],
    absent: set[str],
) -> None:
    helper = _load("runtime_support")
    run_dir = tmp_path / mode
    env = helper.strict_environment(
        run_dir,
        source={"PATH": os.environ["PATH"], **source},
        extra={"OMNIGENT_VERIFY_HARNESS": harness},
        credentialed=True,
    )

    assert env["OMNIGENT_VERIFY_CREDENTIAL_MODE"] == mode
    assert present <= env.keys()
    assert not absent & env.keys()
    helper.cleanup_strict_environment(run_dir)
    assert not any(run_dir.rglob("*"))


def test_nested_credentialed_environment_preserves_selected_profile(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    (real_home / ".databrickscfg").write_text(
        "[selected]\nhost = https://selected.invalid\ntoken = nested-fake-marker\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    overrides = {
        "OMNIGENT_VERIFY_DATABRICKS_PROFILE": "selected",
        "OMNIGENT_VERIFY_HARNESS": "claude-sdk",
    }
    outer = helper.strict_environment(
        run_dir,
        source={
            "HOME": str(real_home),
            "PATH": os.environ["PATH"],
            "OPENAI_API_KEY": "ambient-openai-marker",
            "OPENAI_BASE_URL": "https://ambient.invalid",
            "DATABRICKS_HOST": "https://ambient-databricks.invalid",
            "DATABRICKS_TOKEN": "ambient-databricks-marker",
        },
        extra=overrides,
        credentialed=True,
    )
    staged = Path(outer["DATABRICKS_CONFIG_FILE"])

    nested = helper.strict_environment(
        run_dir,
        source=outer,
        extra=overrides,
        credentialed=True,
    )

    assert Path(nested["DATABRICKS_CONFIG_FILE"]) == staged
    assert "nested-fake-marker" in staged.read_text(encoding="utf-8")
    helper.cleanup_strict_environment(run_dir)
    assert not staged.exists()


def test_runtime_cleanup_verifies_sensitive_state_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("runtime_support")
    run_dir = tmp_path / "run"
    env = helper.strict_environment(
        run_dir,
        source={"PATH": os.environ["PATH"]},
    )
    home = Path(env["HOME"])
    real_rmtree = helper.shutil.rmtree
    monkeypatch.setattr(helper.shutil, "rmtree", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="survived cleanup"):
        helper.cleanup_strict_environment(run_dir)

    monkeypatch.setattr(helper.shutil, "rmtree", real_rmtree)
    helper.cleanup_strict_environment(run_dir)
    assert not home.exists()


def test_ignored_build_output_mutation_invalidates_snapshot(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = tmp_path / "repo"
    output = repo / "web/dist/index.js"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"before")
    (repo / ".gitignore").write_text("web/dist/\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base")
    before = helper.repository_snapshot(repo)

    output.write_bytes(b"after")
    changes = helper.compare_snapshots(before, helper.repository_snapshot(repo))

    assert any("build outputs changed" in change for change in changes)


def test_cli_profile_imports_code_from_target_checkout(tmp_path: Path) -> None:
    runner = _load("profile_runner")
    repo = tmp_path / "base-checkout"
    package = repo / "omnigent"
    driver = repo / ".claude/skills/cli-setup-verify/verify_cli.py"
    python = repo / ".venv/bin/python"
    package.mkdir(parents=True)
    driver.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    (package / "__init__.py").write_text("IDENTITY = 'base-checkout'\n", encoding="utf-8")
    driver.write_text(
        "import omnigent\nprint(omnigent.IDENTITY, omnigent.__file__)\n",
        encoding="utf-8",
    )
    python.symlink_to(Path(sys.executable))
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base")
    run_dir = tmp_path / "run"

    status = runner.run_profile(repo, run_dir, "cli")

    assert status == 0
    report = json.loads((run_dir / "steps.json").read_text(encoding="utf-8"))
    assert len(report["steps"]) == 4
    for step in report["steps"]:
        output = (run_dir / step["log"]).read_text(encoding="utf-8")
        assert "base-checkout" in output
        assert str(repo / "omnigent/__init__.py") in output


@pytest.mark.parametrize(
    "path",
    [
        ".agents/skills/other/scripts/__pycache__/helper.cpython-312.pyc",
        ".claude/skills/other/__pycache__/helper.pyc",
        ".cursor/skills/other/generated.pyo",
    ],
)
def test_hidden_skill_bytecode_remains_ignored_after_negations(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path],
        cwd=_REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0


def test_host_tests_map_to_the_host_harness_and_quality_lanes() -> None:
    helper = _load("orchestration")
    plan = helper.select_lanes(
        [
            "tests/host/test_connect.py",
            "tests/host/test_identity.py",
        ]
    )

    assert plan["selected_lanes"] == ["quality-gates", "harness-client"]
    assert plan["blockers"] == []


def test_routing_runtime_files_have_explicit_surface_mappings() -> None:
    helper = _load("orchestration")
    plan = helper.select_lanes(
        [
            "omnigent/gateway_inference.py",
            "omnigent/inner/hook_scripts/subagent_router.py",
            "omnigent/smart_routing_cli.py",
            "scripts/verify_smart_routing.sh",
            "tests/test_gateway_inference.py",
        ]
    )

    assert plan["blockers"] == []
    assert plan["selected_lanes"] == [
        "quality-gates",
        "server",
        "harness-client",
        "cli",
    ]


def test_setup_web_build_test_maps_to_full_web_quality_only() -> None:
    helper = _load("orchestration")
    runner = _load("profile_runner")

    plan = helper.select_lanes(["tests/test_setup_web_ui_build.py"])
    steps = runner.profile_steps(
        "quality-gates",
        ".artifacts/run",
        changed_files=("tests/test_setup_web_ui_build.py",),
    )

    assert plan["selected_lanes"] == ["quality-gates"]
    assert plan["blockers"] == []
    assert any(
        step.argv[:3] == ("node", "node_modules/vite/bin/vite.js", "build") for step in steps
    )


def test_quality_steps_execute_only_selected_meta_contracts() -> None:
    runner = _load("profile_runner")
    changed = (
        "tests/verify_omnigent/test_comparison.py",
        "tests/deploy/test_databricks_deploy_dry_run.py",
        "tests/e2e_ui/test_evidence_contract.py",
    )

    steps = runner.profile_steps("quality-gates", "/tmp/run", changed_files=changed)
    commands = [step.argv for step in steps]

    contract = next(
        command for command in commands if "tests/verify_omnigent/test_comparison.py" in command
    )
    assert "tests/deploy/test_databricks_deploy_dry_run.py" in contract
    assert any(
        "tests/e2e_ui/test_evidence_contract.py" in command and "--tracing=on" in command
        for command in commands
    )

    ordinary = runner.profile_steps(
        "quality-gates",
        "/tmp/run",
        changed_files=("omnigent/server/app.py",),
    )
    assert not any(
        any(
            focused in argument
            for focused in (
                "tests/verify_omnigent",
                "tests/deploy",
                "tests/e2e_ui/test_evidence_contract.py",
                "tests/harness_bench/test_bench.py",
                "tests/benchmarks/test_benchmark_smoke.py",
            )
        )
        for step in ordinary
        for argument in step.argv
    )


@pytest.mark.parametrize(
    ("implementation", "expected"),
    [
        (
            ".agents/skills/verify-omnigent/scripts/evidence_manifest.py",
            "tests/verify_omnigent",
        ),
        ("deploy/databricks/deploy.py", "tests/deploy/test_databricks_deploy_dry_run.py"),
        ("tests/e2e_ui/conftest.py", "tests/e2e_ui/test_evidence_contract.py"),
        ("tests/e2e_ui/playwright_evidence.py", "tests/e2e_ui/test_evidence_contract.py"),
        ("tests/harness_bench/report.py", "tests/harness_bench/test_bench.py"),
        (
            "dev/benchmarks/omnigent/run.py",
            "tests/benchmarks/test_benchmark_smoke.py",
        ),
    ],
)
def test_quality_implementation_changes_run_focused_contracts(
    implementation: str,
    expected: str,
) -> None:
    runner = _load("profile_runner")

    steps = runner.profile_steps(
        "quality-gates",
        "/tmp/run",
        changed_files=(implementation,),
    )

    assert any(expected in step.argv for step in steps)


def test_shared_playwright_evidence_maps_web_and_desktop() -> None:
    helper = _load("orchestration")

    plan = helper.select_lanes(["tests/e2e_ui/playwright_evidence.py"])

    assert "web-ui" in plan["selected_lanes"]
    assert "desktop" in plan["selected_lanes"]


def test_all_verification_vite_builds_use_runner_config_loader() -> None:
    runner = _load("profile_runner")
    commands = [
        step.argv
        for profile, changed_files in (
            ("quality-gates", ("web/src/App.tsx",)),
            ("desktop", None),
        )
        for step in runner.profile_steps(profile, "/evidence/run", changed_files=changed_files)
        if step.argv[:3] == ("node", "node_modules/vite/bin/vite.js", "build")
    ]

    assert len(commands) == 3
    assert all(command[command.index("--configLoader") + 1] == "runner" for command in commands)
    assert all(
        command[command.index("--outDir") + 1].startswith("/evidence/run/") for command in commands
    )


def test_representative_vite_build_preserves_source_node_modules(
    tmp_path: Path,
) -> None:
    helper = _load("runtime_support")
    from tests.e2e_ui import conftest as ui_conftest

    source = tmp_path / "source"
    vite = source / "web/node_modules/vite/bin/vite.js"
    vite.parent.mkdir(parents=True)
    vite.write_text(
        """const fs = require("node:fs");
const path = require("node:path");
const args = process.argv.slice(2);
const nodeModules = path.resolve(__dirname, "../..");
if (!args.includes("--configLoader") || args[args.indexOf("--configLoader") + 1] !== "runner") {
  fs.mkdirSync(path.join(nodeModules, ".vite-temp"));
  process.exit(9);
}
const output = args[args.indexOf("--outDir") + 1];
fs.mkdirSync(output, {recursive: true});
fs.writeFileSync(path.join(output, "index.html"), "built");
const cache = path.join(process.env.TMPDIR, "vite-cache");
fs.mkdirSync(cache);
fs.writeFileSync(path.join(cache, "config.lock"), "run-owned");
""",
        encoding="utf-8",
    )
    run_dir = tmp_path / "evidence/run"
    run_dir.mkdir(parents=True)
    output = run_dir / "build/e2e-web-ui"
    before = helper._tree_identity(source / "web/node_modules")
    env = helper.strict_environment(
        run_dir,
        source={"PATH": os.environ["PATH"]},
    )

    subprocess.run(
        ui_conftest._vite_build_command(vite, output),
        cwd=source / "web",
        env=env,
        check=True,
    )
    lock = Path(
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from tests.e2e_ui.conftest import _vite_build_lock_path; "
                "path=_vite_build_lock_path(); path.write_text('run-owned'); print(path)",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    assert helper._tree_identity(source / "web/node_modules") == before
    assert not (source / "web/node_modules/.vite-temp").exists()
    assert (output / "index.html").is_file()
    cache = Path(env["TMPDIR"]) / "vite-cache"
    assert cache.is_relative_to(Path(env["TMPDIR"]))
    assert lock.is_relative_to(Path(env["TMPDIR"]))
    assert (cache / "config.lock").is_file()
    assert lock.is_file()
    helper.cleanup_strict_environment(run_dir)
    assert not cache.exists()
    assert not lock.exists()
    assert (output / "index.html").is_file()


def test_real_vite_runner_loader_does_not_write_source_node_modules(
    tmp_path: Path,
) -> None:
    helper = _load("runtime_support")
    repo_root = Path(__file__).resolve().parents[2]
    candidates = sorted(
        (repo_root / "node_modules/.pnpm").glob("vite@*/node_modules/vite/bin/vite.js")
    )
    if not candidates:
        pytest.skip("offline Vite package is unavailable")
    project = tmp_path / "project"
    (project / "node_modules").mkdir(parents=True)
    (project / "index.html").write_text('<main id="app"></main>\n', encoding="utf-8")
    config = project / "vite.config.mjs"
    config.write_text("export default { build: { minify: false } };\n", encoding="utf-8")
    run_dir = tmp_path / "evidence"
    run_dir.mkdir()
    output = run_dir / "build"
    before = helper._tree_metadata_identity(repo_root / "node_modules")
    env = helper.strict_environment(run_dir, source={"PATH": os.environ["PATH"]})

    subprocess.run(
        [
            "node",
            str(candidates[-1]),
            "build",
            "--configLoader",
            "runner",
            "--config",
            str(config),
            "--outDir",
            str(output),
            "--emptyOutDir",
        ],
        cwd=project,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert helper._tree_metadata_identity(repo_root / "node_modules") == before
    assert not (project / "node_modules/.vite-temp").exists()
    assert (output / "index.html").is_file()
    helper.cleanup_strict_environment(run_dir)


def _fake_pnpm_package(root: Path, version: str) -> Path:
    package = root / f"pnpm-{version}"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "pnpm", "version": version}),
        encoding="utf-8",
    )
    (package / "bin/pnpm.mjs").write_text(
        f"console.log({json.dumps(version)});\n",
        encoding="utf-8",
    )
    return package


def _pnpm_repo(root: Path, version: str = "11.15.1") -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps({"packageManager": f"pnpm@{version}"}),
        encoding="utf-8",
    )
    return repo


def _publish_fake_pnpm(
    helper: ModuleType,
    repo: Path,
    candidate: Path,
    cache_parent: Path,
    name: str,
) -> Path:
    staging = cache_parent / f"pnpm.version.{name}"
    helper.prepare_pnpm_cache(repo, staging, candidates=(candidate,))
    destination = cache_parent / "pnpm"
    helper.publish_pnpm_cache(staging, destination)
    return destination


def test_strict_environment_requires_authenticated_exact_pnpm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("runtime_support")
    repo = _pnpm_repo(tmp_path)
    destination = tmp_path / "cache/pnpm"
    monkeypatch.setattr(helper, "_pnpm_cache_destination", lambda: destination)

    env = helper.strict_environment(
        tmp_path / "run",
        source={"PATH": os.environ["PATH"]},
        extra={"OMNIGENT_VERIFY_REPO_ROOT": str(repo)},
    )

    assert "OMNIGENT_VERIFY_TRUSTED_PNPM_TOOLS_ROOT" not in env
    verify_script = (
        Path(__file__).resolve().parents[2] / ".agents/skills/verify-omnigent/scripts/verify.sh"
    ).read_text(encoding="utf-8")
    assert ".agents/skills/verify-omnigent/scripts/verify.sh prepare-tools" in verify_script
    helper.cleanup_strict_environment(tmp_path / "run")


def test_pnpm_preparation_rejects_wrong_version(tmp_path: Path) -> None:
    helper = _load("runtime_support")
    repo = _pnpm_repo(tmp_path)
    wrong = _fake_pnpm_package(tmp_path / "local", "11.15.0")

    with pytest.raises(RuntimeError, match="not in a supported local cache"):
        helper.prepare_pnpm_cache(
            repo,
            tmp_path / "cache/pnpm.version.wrong",
            candidates=(wrong,),
        )


def test_tampered_pnpm_cache_is_not_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("runtime_support")
    repo = _pnpm_repo(tmp_path)
    candidate = _fake_pnpm_package(tmp_path / "local", "11.15.1")
    cache_parent = tmp_path / "cache"
    cache_parent.mkdir()
    destination = _publish_fake_pnpm(helper, repo, candidate, cache_parent, "sealed")
    monkeypatch.setattr(helper, "_pnpm_cache_destination", lambda: destination)
    published = destination.resolve(strict=True)
    (published / "11.15.1/package/bin/pnpm.mjs").write_text(
        "console.log('tampered');\n",
        encoding="utf-8",
    )

    assert helper.verified_pnpm_tools_root() is None
    env = helper.strict_environment(
        tmp_path / "run",
        source={"PATH": os.environ["PATH"]},
        extra={"OMNIGENT_VERIFY_REPO_ROOT": str(repo)},
    )
    assert "OMNIGENT_VERIFY_TRUSTED_PNPM_TOOLS_ROOT" not in env
    helper.cleanup_strict_environment(tmp_path / "run")


def test_concurrent_pnpm_publication_and_strict_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("runtime_support")
    repo = _pnpm_repo(tmp_path)
    candidate = _fake_pnpm_package(tmp_path / "local", "11.15.1")
    cache_parent = tmp_path / "cache"
    cache_parent.mkdir()
    destination = _publish_fake_pnpm(helper, repo, candidate, cache_parent, "seed")
    monkeypatch.setattr(helper, "_pnpm_cache_destination", lambda: destination)

    def publish(index: int) -> str:
        _publish_fake_pnpm(helper, repo, candidate, cache_parent, f"writer-{index}")
        return "published"

    def read(index: int) -> str:
        run_dir = tmp_path / f"run-{index}"
        run_dir.mkdir()
        env = helper.strict_environment(
            run_dir,
            source={"PATH": os.environ["PATH"]},
            extra={"OMNIGENT_VERIFY_REPO_ROOT": str(repo)},
        )
        result = subprocess.run(
            ["pnpm", "--version"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        helper.cleanup_strict_environment(run_dir)
        return result

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(publish if index < 3 else read, index) for index in range(8)]
    assert [future.result() for future in futures].count("11.15.1") == 5
    assert helper.verified_pnpm_tools_root() == destination.resolve(strict=True)


def test_strict_environment_executes_prepared_pinned_pnpm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("runtime_support")
    repo = _pnpm_repo(tmp_path)
    candidate = _fake_pnpm_package(tmp_path / "local", "11.15.1")
    cache_parent = tmp_path / "cache"
    cache_parent.mkdir()
    destination = _publish_fake_pnpm(helper, repo, candidate, cache_parent, "success")
    monkeypatch.setattr(helper, "_pnpm_cache_destination", lambda: destination)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    env = helper.strict_environment(
        run_dir,
        source={"PATH": os.environ["PATH"]},
        extra={"OMNIGENT_VERIFY_REPO_ROOT": str(repo)},
    )

    trusted = Path(env["OMNIGENT_VERIFY_TRUSTED_PNPM_TOOLS_ROOT"])
    assert trusted.is_relative_to(helper._strict_environment_paths(run_dir)[1])
    assert (
        subprocess.run(
            ["pnpm", "--version"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "11.15.1"
    )
    helper.cleanup_strict_environment(run_dir)
    assert not trusted.exists()


def test_verification_changes_select_all_lanes_and_unknown_product_paths_block() -> None:
    helper = _load("orchestration")
    verification = helper.select_lanes([".agents/skills/verify-omnigent/scripts/verify.sh"])
    assert verification["selected_lanes"] == [lane for lane in helper.LANE_ORDER if lane != "perf"]

    unknown = helper.select_lanes(["tests/tools/test_new_unmapped_surface.py"])
    assert unknown["selected_lanes"] == ["quality-gates"]
    assert "did not map to a verification lane" in unknown["blockers"][0]


def test_targeted_risk_surfaces_select_focused_auto_lanes() -> None:
    helper = _load("orchestration")
    plan = helper.select_lanes(
        [
            "tests/e2e_ui/scheduled/test_scheduled_tasks_page.py",
            "tests/e2e_ui/collaboration/test_sharing_journey.py",
            "tests/db/test_migration_workspace.py",
            "deploy/databricks/deploy.py",
            "omnigent/server/routes/sessions.py",
        ]
    )

    assert {"automations", "collaboration", "db-migration-deploy", "perf"} <= set(
        plan["selected_lanes"]
    )
    assert plan["blockers"] == []


@pytest.mark.parametrize(
    ("path", "required"),
    [
        (
            "omnigent/server/routes/accounts_auth.py",
            {"quality-gates", "server", "collaboration"},
        ),
        (
            "omnigent/server/routes/sessions/routes_permissions.py",
            {"quality-gates", "server", "collaboration", "perf"},
        ),
        (
            "omnigent/server/routes/roles/assign.py",
            {"quality-gates", "server", "collaboration"},
        ),
        (
            "omnigent/host/identity.py",
            {"quality-gates", "server", "harness-client", "collaboration"},
        ),
        (
            "sdks/python-client/omnigent_client/_client.py",
            {"quality-gates", "harness-client", "collaboration"},
        ),
        (
            "web/src/lib/permissionsApi.ts",
            {"quality-gates", "web-ui", "collaboration"},
        ),
        (
            "web/src/pages/LoginPage.tsx",
            {"quality-gates", "web-ui", "collaboration"},
        ),
        (
            "omnigent/server/oidc.py",
            {"quality-gates", "server", "collaboration"},
        ),
        (
            "omnigent/server/accounts_store.py",
            {"quality-gates", "server", "collaboration"},
        ),
        (
            "omnigent/server/routes/session_policies.py",
            {"quality-gates", "server", "collaboration"},
        ),
        (
            "tests/e2e_ui/auth/test_device_grant_reauth.py",
            {"quality-gates", "web-ui", "collaboration"},
        ),
        (
            "omnigent/server/routes/sessions/routes_core.py",
            {"quality-gates", "server", "perf", "collaboration"},
        ),
        (
            "omnigent/server/routes/sessions/routes_hooks.py",
            {"quality-gates", "server", "perf", "collaboration"},
        ),
        (
            "tests/server/integration/test_sessions_permission_request_hook.py",
            {"quality-gates", "server", "collaboration"},
        ),
        (
            "tests/e2e_ui/sessions/test_sidebar_ownership_gating.py",
            {"quality-gates", "web-ui", "collaboration"},
        ),
    ],
)
def test_auth_and_access_paths_select_collaboration(
    path: str,
    required: set[str],
) -> None:
    helper = _load("orchestration")
    plan = helper.select_lanes([path])

    assert required <= set(plan["selected_lanes"])
    assert plan["blockers"] == []


@pytest.mark.parametrize(
    "path",
    [
        "omnigent/runtime/identity_map.py",
        "omnigent/runtime/hooks.py",
        "omnigent/server/core.py",
        "omnigent/server/routes/authenticity_metrics.py",
        "omnigent/server/routes/sessions/routes_events.py",
        "web/src/components/IdentityBadge.tsx",
    ],
)
def test_unrelated_identity_like_names_do_not_select_collaboration(path: str) -> None:
    helper = _load("orchestration")
    plan = helper.select_lanes([path])

    assert "collaboration" not in plan["selected_lanes"]


def test_changed_files_exclude_base_only_changes(tmp_path: Path) -> None:
    orchestration = _load("orchestration")
    repo = _diverged_repo(tmp_path)

    files, blockers = orchestration.changed_files(repo, "main")

    assert blockers == []
    assert "web/base_only.ts" not in files


def test_changed_files_preserve_nul_delimited_special_names(tmp_path: Path) -> None:
    orchestration = _load("orchestration")
    repo = tmp_path / "special-paths"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    _git(repo, "switch", "-q", "-c", "pr")
    names = (
        "omnigent/server/line\nbreak.py",
        "omnigent/server/tab\tname.py",
        'omnigent/server/"quote"\\name.py',
    )
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed = True\n", encoding="utf-8")
    _commit(repo, "special paths")

    files, blockers = orchestration.changed_files(repo, "main")
    plan = orchestration.select_lanes(files)
    runner = _load("profile_runner")
    scoped = runner._changed_files(repo, "main")
    quality_steps = runner.profile_steps(
        "quality-gates",
        "run",
        changed_files=scoped,
    )

    assert blockers == []
    assert set(names).issubset(files)
    assert set(names).issubset(scoped)
    assert set(names).issubset(quality_steps[0].argv)
    assert "server" in plan["selected_lanes"]
    assert plan["blockers"] == []


def test_changed_files_include_both_rename_and_copy_paths(tmp_path: Path) -> None:
    orchestration = _load("orchestration")
    runner = _load("profile_runner")
    repo = tmp_path / "renames"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    implementation = repo / "omnigent/server/name with\nnewline.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("value = 1\n", encoding="utf-8")
    _commit(repo, "base")
    base = _git(repo, "rev-parse", "HEAD")
    destination = repo / "docs" / "renamed with space.md"
    destination.parent.mkdir()
    implementation.rename(destination)
    _git(repo, "add", "-A")
    _commit(repo, "rename implementation to docs")
    copied = repo / "omnigent/server/copied file.py"
    copied.write_text(destination.read_text(encoding="utf-8"), encoding="utf-8")
    _git(repo, "add", copied.relative_to(repo).as_posix())

    files, blockers = orchestration.changed_files(repo, base)
    scoped = runner._changed_files(repo, base)

    assert blockers == []
    assert "omnigent/server/name with\nnewline.py" in files
    assert "docs/renamed with space.md" in files
    assert "omnigent/server/copied file.py" in files
    assert set(files) == set(scoped)


def test_changed_files_include_pr_only_changes(tmp_path: Path) -> None:
    orchestration = _load("orchestration")
    repo = _diverged_repo(tmp_path)

    files, blockers = orchestration.changed_files(repo, "main")

    assert blockers == []
    assert "omnigent/pr_only.py" in files


def test_changed_files_include_each_local_path_once(tmp_path: Path) -> None:
    orchestration = _load("orchestration")
    runner = _load("profile_runner")
    repo = _diverged_repo(tmp_path)

    files, blockers = orchestration.changed_files(repo, "main")
    runner_files = runner._changed_files(repo, "main")

    assert blockers == []
    assert {
        "tracked-staged.txt",
        "tracked-unstaged.txt",
        "tests/host/test_untracked.py",
    } <= set(files)
    assert len(files) == len(set(files))
    assert set(runner_files) == set(files)
    assert len(runner_files) == len(set(runner_files))


def test_changed_files_fail_closed_for_missing_base(tmp_path: Path) -> None:
    orchestration = _load("orchestration")
    runner = _load("profile_runner")
    repo = _diverged_repo(tmp_path)

    files, blockers = orchestration.changed_files(repo, "missing-base")

    assert files == []
    assert blockers and "unavailable" in blockers[0]
    with pytest.raises(RuntimeError):
        runner._changed_files(repo, "missing-base")


def test_changed_files_fail_closed_without_merge_base(tmp_path: Path) -> None:
    orchestration = _load("orchestration")
    runner = _load("profile_runner")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    _commit(repo, "main")
    _git(repo, "switch", "--orphan", "disconnected")
    (repo / "main.txt").unlink(missing_ok=True)
    (repo / "disconnected.txt").write_text("disconnected\n", encoding="utf-8")
    _commit(repo, "disconnected")

    files, blockers = orchestration.changed_files(repo, "main")

    assert files == []
    assert blockers and "no unique merge base" in blockers[0]
    with pytest.raises(RuntimeError, match="no unique merge base"):
        runner._changed_files(repo, "main")


def test_all_surfaces_is_explicit_and_keeps_live_lanes_not_requested(tmp_path: Path) -> None:
    helper = _load("orchestration")
    plan = helper.build_plan(tmp_path, "all-surfaces", "unused")

    assert plan["selected_lanes"] == [
        lane for lane in helper.LANE_ORDER if lane in helper.ALL_SURFACES
    ]
    assert all(decision["status"] == "selected" for decision in plan["decisions"])
    assert {lane["status"] for lane in plan["optional_lanes"]} == {"not_requested"}
    assert {lane["lane"] for lane in plan["optional_lanes"]} >= {
        "harness-live",
        "cli-live-repl",
        "electron-release",
        "downstream-universe",
    }


def test_universe_lane_requires_explicit_opt_in(tmp_path: Path) -> None:
    helper = _load("orchestration")

    default = helper.build_plan(tmp_path, "all-surfaces", "unused")
    opted_in = helper.build_plan(
        tmp_path,
        "all-surfaces",
        "unused",
        with_universe=True,
    )

    assert "universe" not in default["selected_lanes"]
    assert opted_in["selected_lanes"][-1] == "universe"
    assert not any(lane["lane"] == "downstream-universe" for lane in opted_in["optional_lanes"])
    assert opted_in["decisions"][-1]["status"] == "selected"


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            "quality-gates",
            ("uv", "run", "--no-sync", "pre-commit", "run"),
        ),
        (
            "server",
            ("uv", "run", "--no-sync", "pytest", "tests/server/test_app.py"),
        ),
        (
            "db-migration-deploy",
            ("uv", "run", "--no-sync", "pytest", "tests/db", "tests/stores", "tests/deploy"),
        ),
        ("cli", (".venv/bin/python", ".claude/skills/cli-setup-verify/verify_cli.py")),
        ("web-ui", ("node_modules/.bin/vitest", "run")),
        ("desktop", ("node", "--test")),
        (
            "harness-client",
            ("uv", "run", "--no-sync", "pytest", "tests/harness_bench"),
        ),
        (
            "universe",
            (
                "python3",
                ".agents/skills/verify-omnigent/scripts/universe_preflight.py",
            ),
        ),
    ],
)
def test_profile_commands_are_grounded_in_checked_in_assets(
    profile: str,
    expected: tuple[str, ...],
) -> None:
    runner = _load("profile_runner")
    steps = runner.profile_steps(profile, ".artifacts/run")

    assert steps
    assert any(step.argv[: len(expected)] == expected for step in steps)
    assert all("/Users/" not in argument for step in steps for argument in step.argv)


def test_web_quality_gate_matches_merge_critical_commands() -> None:
    runner = _load("profile_runner")
    steps = runner.profile_steps(
        "quality-gates",
        ".artifacts/run",
        changed_files=("web/src/App.tsx", "tests/e2e_ui/chat/test_smoke.py"),
    )

    commands = [step.argv for step in steps]
    assert commands[0] == (
        "uv",
        "run",
        "--no-sync",
        "pre-commit",
        "run",
        "--files",
        "tests/e2e_ui/chat/test_smoke.py",
        "web/src/App.tsx",
    )
    assert ("node_modules/.bin/prettier", "--check", ".") in commands
    assert ("node_modules/.bin/oxlint", "--deny-warnings", ".") in commands
    assert ("node_modules/.bin/tsc", "-b", "--noEmit") in commands
    assert any(
        command[:3] == ("node", "node_modules/vite/bin/vite.js", "build") for command in commands
    )


def test_desktop_report_supports_external_evidence_root(tmp_path: Path) -> None:
    runner = _load("profile_runner")
    run_dir = tmp_path / "external-evidence" / "run"
    package = run_dir / "build/electron-package/app.bin"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"package")

    error = runner._desktop_build_report(run_dir)

    assert error is None
    report = json.loads((run_dir / "desktop-build.json").read_text(encoding="utf-8"))
    assert report["outputs"][0]["path"] == "build/electron-package/app.bin"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({}, "unsupported schema"),
        ({"schema_version": 1, "harnesses": []}, "no harness results"),
        (
            {
                "schema_version": 1,
                "has_drift": False,
                "dimensions": ["basic_turn"],
                "harnesses": [{"harness": "codex", "cells": []}],
            },
            "no capability cells",
        ),
    ],
)
def test_offline_harness_report_fails_closed(
    tmp_path: Path,
    payload: dict[str, object],
    error: str,
) -> None:
    runner = _load("profile_runner")
    report = tmp_path / "harness-matrix.json"
    report.write_text(json.dumps(payload), encoding="utf-8")

    assert error in runner._validate_offline_harness_report(report)


def test_offline_harness_report_requires_expected_cells(tmp_path: Path) -> None:
    runner = _load("profile_runner")
    report = tmp_path / "harness-matrix.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "has_drift": False,
                "dimensions": ["basic_turn"],
                "harnesses": [
                    {
                        "harness": "codex",
                        "cells": [
                            {
                                "dimension": "basic_turn",
                                "observed": "skipped",
                                "declared": "supported",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert runner._validate_offline_harness_report(report) is None


def test_python_quality_gate_does_not_require_web_build() -> None:
    runner = _load("profile_runner")
    steps = runner.profile_steps(
        "quality-gates",
        ".artifacts/run",
        changed_files=("omnigent/server/app.py",),
    )

    assert len(steps) == 1
    assert steps[0].argv[-2:] == ("--files", "omnigent/server/app.py")


def test_desktop_profile_runs_real_electron_playwright_journeys() -> None:
    runner = _load("profile_runner")
    steps = runner.profile_steps(
        "desktop",
        ".artifacts/run",
        electron_e2e_tests=(
            "web/electron/e2e/desktop_api_url_recovery.e2e.js",
            "web/electron/e2e/desktop_settings_shortcut.e2e.js",
        ),
    )

    commands = [step.argv for step in steps]
    assert any(
        command[:3] == ("node", "node_modules/vite/bin/vite.js", "build") for command in commands
    )
    assert (
        "node",
        "--test",
        "--test-concurrency=1",
        "web/electron/e2e/desktop_api_url_recovery.e2e.js",
        "web/electron/e2e/desktop_settings_shortcut.e2e.js",
    ) in commands


def test_live_harness_report_fails_closed_on_skipped_basic_turn(tmp_path: Path) -> None:
    runner = _load("profile_runner")
    report = tmp_path / "harness-live.json"
    report.write_text(
        json.dumps(
            {
                "harnesses": [
                    {
                        "harness": "cursor",
                        "skipped_reason": "cursor is not installed",
                        "cells": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert "unavailable" in runner._validate_live_harness_report(report)


def test_universe_profile_propagates_requested_refs() -> None:
    runner = _load("profile_runner")
    step = runner.profile_steps(
        "universe",
        ".artifacts/run",
        base_ref="origin/main",
        oss_ref="pr-head",
    )[0]

    assert step.argv[-4:] == ("--base-ref", "origin/main", "--oss-ref", "pr-head")


def _runner_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "runner-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks: []\n",
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("locked\n", encoding="utf-8")
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    _commit(repo, "base")
    return repo


def test_harness_live_nested_runtime_keeps_profile_until_step_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("runtime_support")
    runner = _load("profile_runner")
    repo = _runner_repo(tmp_path)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    (real_home / ".databrickscfg").write_text(
        "[selected]\nhost = https://selected.invalid\ntoken = harness-fake-marker\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "harness-live-run"
    run_dir.mkdir()
    outer = helper.strict_environment(
        run_dir,
        source={
            "HOME": str(real_home),
            "PATH": os.environ["PATH"],
            "OPENAI_API_KEY": "ambient-openai-marker",
            "OPENAI_BASE_URL": "https://ambient.invalid",
            "DATABRICKS_HOST": "https://ambient-databricks.invalid",
            "DATABRICKS_TOKEN": "ambient-databricks-marker",
        },
        extra={
            "OMNIGENT_VERIFY_DATABRICKS_PROFILE": "selected",
            "OMNIGENT_VERIFY_HARNESS": "claude-sdk",
        },
        credentialed=True,
    )
    staged = Path(outer["DATABRICKS_CONFIG_FILE"])
    monkeypatch.setattr(os, "environ", outer)
    monkeypatch.setattr(
        runner,
        "profile_steps",
        lambda *_args, **_kwargs: [
            runner.Step(
                "Read staged profile",
                (
                    sys.executable,
                    "-c",
                    "import json, os, pathlib; "
                    "value=pathlib.Path(os.environ['DATABRICKS_CONFIG_FILE']).read_text(); "
                    "report={'harnesses':[{'cells':["
                    "{'dimension':'basic_turn','observed':'supported'}]}]}; "
                    "(pathlib.Path(os.environ['OMNIGENT_VERIFY_RUN_DIR']) / "
                    "'harness-live.json').write_text(json.dumps(report)); "
                    "forbidden={'OPENAI_API_KEY','OPENAI_BASE_URL',"
                    "'DATABRICKS_HOST','DATABRICKS_TOKEN','CURSOR_API_KEY'}; "
                    "valid=('token = ' in value and "
                    "os.environ.get('DATABRICKS_CONFIG_PROFILE') == 'selected' and "
                    "os.environ.get('OMNIGENT_VERIFY_CREDENTIAL_MODE') == "
                    "'databricks-profile' and not forbidden.intersection(os.environ)); "
                    "raise SystemExit(0 if valid else 9)",
                ),
            )
        ],
    )

    result = runner.run_profile(repo, run_dir, "harness-live")

    assert result == 0
    report = json.loads((run_dir / "steps.json").read_text(encoding="utf-8"))
    assert report["steps"][0]["credential_mode"] == "databricks-profile"
    helper.cleanup_strict_environment(run_dir)
    assert not staged.exists()
    assert "harness-fake-marker" not in (run_dir / "steps.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("exit_status", [0, 9])
def test_profile_runner_preserves_status_and_lockfiles_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_status: int,
) -> None:
    runner = _load("profile_runner")
    repo = _runner_repo(tmp_path)
    run_dir = tmp_path / f"run-{exit_status}"
    run_dir.mkdir()
    before_status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    before_lock = (repo / "uv.lock").read_bytes()
    monkeypatch.setattr(
        runner,
        "profile_steps",
        lambda *_args, **_kwargs: [
            runner.Step(
                "Synthetic step",
                (sys.executable, "-c", f"raise SystemExit({exit_status})"),
            )
        ],
    )

    result = runner.run_profile(repo, run_dir, "server")

    assert result == exit_status
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert (repo / "uv.lock").read_bytes() == before_lock
    report = json.loads((run_dir / "steps.json").read_text(encoding="utf-8"))
    assert report["state_unchanged"] is True


def test_mutating_pre_commit_runs_only_in_disposable_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load("profile_runner")
    repo = _runner_repo(tmp_path)
    run_dir = tmp_path / "isolated-run"
    run_dir.mkdir()
    before_status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    before_lock = (repo / "uv.lock").read_bytes()
    monkeypatch.setattr(
        runner,
        "profile_steps",
        lambda *_args, **_kwargs: [
            runner.Step(
                "Synthetic mutating hook",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('uv.lock').write_text('rewritten\\n')",
                ),
                isolated=True,
            )
        ],
    )

    result = runner.run_profile(repo, run_dir, "quality-gates")

    assert result == 0
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert (repo / "uv.lock").read_bytes() == before_lock
    report = json.loads((run_dir / "steps.json").read_text(encoding="utf-8"))
    assert report["steps"][0]["isolation_cleanup"] == "completed"
    assert report["state_unchanged"] is True


def test_isolated_quality_step_cannot_mutate_shared_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load("profile_runner")
    repo = _runner_repo(tmp_path)
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _commit(repo, "ignore environment")
    (repo / ".venv").mkdir()
    original = repo / ".venv/original.bin"
    original.write_bytes(b"original dependency bytes\n")
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    run_dir = tmp_path / "isolated-venv-run"
    run_dir.mkdir()
    monkeypatch.setattr(
        runner,
        "profile_steps",
        lambda *_args, **_kwargs: [
            runner.Step(
                "Synthetic environment mutation",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('.venv/original.bin').write_text('changed'); "
                    "Path('.venv/marker.txt').write_text('isolated')",
                ),
                isolated=True,
            )
        ],
    )

    result = runner.run_profile(repo, run_dir, "quality-gates")

    assert result == 0
    assert not (repo / ".venv/marker.txt").exists()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    report = json.loads((run_dir / "steps.json").read_text(encoding="utf-8"))
    assert report["state_unchanged"] is True


def test_isolated_quality_supports_normal_pnpm_links_without_mutating_source(
    tmp_path: Path,
) -> None:
    runner = _load("profile_runner")
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    package = source_root / "node_modules/.pnpm/pkg@1/node_modules/pkg"
    package.mkdir(parents=True)
    original = package / "index.js"
    original.write_text("module.exports = 'original'\n", encoding="utf-8")
    dangling = source_root / "node_modules/.pnpm/node_modules/typebox"
    dangling.parent.mkdir(parents=True)
    dangling.symlink_to("../../typebox@1.1.38")
    modules_metadata = source_root / "node_modules/.modules.yaml"
    modules_metadata.write_text(
        json.dumps({"virtualStoreDir": ("../../../.worktrees/omnigent/stale/node_modules/.pnpm")}),
        encoding="utf-8",
    )
    original_metadata = modules_metadata.read_text(encoding="utf-8")
    workspace_link = source_root / "web/node_modules/pkg"
    workspace_link.parent.mkdir(parents=True)
    workspace_link.symlink_to("../../node_modules/.pnpm/pkg@1/node_modules/pkg")
    stale_checkout_link = source_root / "web/node_modules/pkg-stale-layout"
    stale_checkout_link.symlink_to(
        "../../../.worktrees/omnigent/stale/node_modules/.pnpm/pkg@1/node_modules/pkg"
    )
    shim = source_root / "web/node_modules/.bin/pkg"
    shim.parent.mkdir()
    stale_shim_target = (
        tmp_path / ".worktrees/omnigent/stale/node_modules/.pnpm/pkg@1/node_modules/pkg/index.js"
    )
    stale_shim_relative = os.path.relpath(stale_shim_target, shim.parent)
    shim.write_text(
        "#!/bin/sh\n"
        f'exec node "$basedir/{stale_shim_relative}"\n'
        f"# cmd-shim-target={stale_shim_target}\n",
        encoding="utf-8",
    )
    original_shim = shim.read_text(encoding="utf-8")
    target_root.mkdir()

    runner._clone_dependency_path(
        source_root / "node_modules",
        target_root / "node_modules",
        source_root=source_root,
        target_root=target_root,
    )
    runner._clone_dependency_path(
        source_root / "web/node_modules",
        target_root / "web/node_modules",
        source_root=source_root,
        target_root=target_root,
    )

    isolated = target_root / "web/node_modules/pkg/index.js"
    assert isolated.resolve().is_relative_to(target_root)
    assert (
        (target_root / "web/node_modules/pkg-stale-layout/index.js")
        .resolve()
        .is_relative_to(target_root)
    )
    isolated_dangling = target_root / "node_modules/.pnpm/node_modules/typebox"
    assert isolated_dangling.is_symlink()
    assert isolated_dangling.resolve(strict=False).is_relative_to(target_root)
    isolated_metadata = json.loads(
        (target_root / "node_modules/.modules.yaml").read_text(encoding="utf-8")
    )
    assert isolated_metadata["virtualStoreDir"] == ".pnpm"
    isolated_shim = target_root / "web/node_modules/.bin/pkg"
    assert str(target_root) in isolated_shim.read_text(encoding="utf-8")
    assert ".worktrees/omnigent/stale" not in isolated_shim.read_text(encoding="utf-8")
    isolated.write_text("changed\n", encoding="utf-8")
    assert original.read_text(encoding="utf-8") == "module.exports = 'original'\n"
    assert os.readlink(workspace_link) == "../../node_modules/.pnpm/pkg@1/node_modules/pkg"
    assert shim.read_text(encoding="utf-8") == original_shim
    assert modules_metadata.read_text(encoding="utf-8") == original_metadata


def test_isolated_venv_clones_external_python_runtime(tmp_path: Path) -> None:
    runner = _load("profile_runner")
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    runtime = tmp_path / "python-runtime"
    runtime_python = runtime / "bin/python3.12"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("python-runtime\n", encoding="utf-8")
    runtime_library = runtime / "lib/libpython3.12.dylib"
    runtime_library.parent.mkdir()
    runtime_library.write_text("runtime-library\n", encoding="utf-8")
    source_python = source_root / ".venv/bin/python"
    source_python.parent.mkdir(parents=True)
    source_python.symlink_to(runtime_python)
    target_root.mkdir()

    runner._clone_dependency_path(
        source_root / ".venv",
        target_root / ".venv",
        source_root=source_root,
        target_root=target_root,
    )

    isolated_python = target_root / ".venv/bin/python"
    assert isolated_python.resolve(strict=True).is_relative_to(target_root)
    isolated_library = target_root / ".venv/.python-runtime/lib/libpython3.12.dylib"
    assert isolated_library.read_text(encoding="utf-8") == "runtime-library\n"
    isolated_library.write_text("changed\n", encoding="utf-8")
    assert runtime_library.read_text(encoding="utf-8") == "runtime-library\n"


def test_profile_runner_times_out_and_finalizes_step_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load("profile_runner")
    repo = _runner_repo(tmp_path)
    run_dir = tmp_path / "timeout-run"
    run_dir.mkdir()
    monkeypatch.setenv("OMNIGENT_VERIFY_STEP_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setattr(
        runner,
        "profile_steps",
        lambda *_args, **_kwargs: [
            runner.Step(
                "Synthetic timeout",
                (sys.executable, "-c", "import time; time.sleep(60)"),
            )
        ],
    )

    assert runner.run_profile(repo, run_dir, "server") == 124
    report = json.loads((run_dir / "steps.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["steps"][0]["timed_out"] is True
    assert report["steps"][0]["exit_status"] == 124


def test_managed_marker_reaches_profile_step_and_cleans_fast_daemon(
    tmp_path: Path,
) -> None:
    helper = _load("runtime_support")
    repo = _runner_repo(tmp_path)
    run_dir = tmp_path / "managed-profile"
    run_dir.mkdir()
    ready = tmp_path / "daemon.pid"
    driver = tmp_path / "driver.py"
    driver.write_text(
        f"""\
import os
import sys
from pathlib import Path
sys.path.insert(0, {str(_SCRIPTS)!r})
import profile_runner

code = '''import os,time
from pathlib import Path
fd=int(os.environ["OMNIGENT_VERIFY_MANAGED_FD"])
os.fstat(fd)
first=os.fork()
if first == 0:
    os.setsid()
    second=os.fork()
    if second > 0:
        os._exit(0)
    Path({str(ready)!r}).write_text(str(os.getpid()))
    time.sleep(60)
os.waitpid(first, 0)
'''
profile_runner.profile_steps = lambda *_args, **_kwargs: [
    profile_runner.Step("nested daemon", (sys.executable, "-c", code))
]
raise SystemExit(profile_runner.run_profile(
    Path({str(repo)!r}), Path({str(run_dir)!r}), "server"
))
""",
        encoding="utf-8",
    )
    result = tmp_path / "managed-result.json"
    status = helper.managed_run(
        [sys.executable, str(driver)],
        cwd=repo,
        env={"PATH": os.environ["PATH"]},
        log_path=tmp_path / "managed.log",
        timeout=15,
        result_path=result,
    )

    assert status == 0
    assert ready.is_file()
    daemon_pid = int(ready.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(daemon_pid, 0)


def test_ambient_partial_controls_cannot_skip_profile_evidence_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load("profile_runner")
    repo = _runner_repo(tmp_path)
    run_dir = tmp_path / "partial-run"
    run_dir.mkdir()
    monkeypatch.setenv("OMNIGENT_VERIFY_PARTIAL_COMPARISON", "1")
    monkeypatch.setenv("OMNIGENT_VERIFY_ONLY_STEP_INDICES", "0")
    monkeypatch.setattr(
        runner,
        "profile_steps",
        lambda *_args, **_kwargs: [
            runner.Step("Previously failing step", (sys.executable, "-c", "pass"))
        ],
    )
    monkeypatch.setattr(runner, "_desktop_build_report", lambda *_args: "evidence required")

    assert runner.run_profile(repo, run_dir, "desktop") == 1
    report = json.loads((run_dir / "steps.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert any(step.get("name") == "Evidence validation" for step in report["steps"])


@pytest.mark.parametrize("profile", ["web-ui", "desktop"])
@pytest.mark.parametrize(
    ("marker_state", "expected_status"),
    [
        ("completed", 0),
        ("missing", 1),
        ("malformed", 1),
    ],
)
def test_ui_profile_runner_requires_run_owned_completed_cleanup_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    marker_state: str,
    expected_status: int,
) -> None:
    helper = _load("runtime_support")
    runner = _load("profile_runner")
    repo = _runner_repo(tmp_path)
    run_dir = tmp_path / f"{profile}-{marker_state}"
    run_dir.mkdir()
    marker = run_dir / "cleanup.json"
    monkeypatch.setenv("OMNIGENT_VERIFY_CLEANUP_MARKER", str(marker))
    if marker_state == "completed":
        command = (
            "import json, os; from pathlib import Path; "
            "Path(os.environ['OMNIGENT_VERIFY_CLEANUP_MARKER']).write_text("
            "json.dumps({'schema_version': 1, 'status': 'completed', 'exit_status': 0}))"
        )
    elif marker_state == "malformed":
        command = (
            "import os; from pathlib import Path; "
            "Path(os.environ['OMNIGENT_VERIFY_CLEANUP_MARKER']).write_text('{')"
        )
    else:
        command = "pass"
    monkeypatch.setattr(
        runner,
        "profile_steps",
        lambda *_args, **_kwargs: [
            runner.Step("Synthetic UI pytest", (sys.executable, "-c", command))
        ],
    )
    monkeypatch.setattr(runner, "playwright_browser_snapshot", lambda: {"verified": True})
    monkeypatch.setattr(runner, "_desktop_build_report", lambda *_args: None)

    result = runner.run_profile(repo, run_dir, profile)

    assert result == expected_status
    report = json.loads((run_dir / "steps.json").read_text(encoding="utf-8"))
    assert report["status"] == ("passed" if expected_status == 0 else "failed")
    if expected_status:
        assert any(step.get("name") == "Evidence validation" for step in report["steps"])
    helper.cleanup_strict_environment(run_dir)


@pytest.mark.parametrize("profile", ["web-ui", "desktop"])
def test_ui_profile_rejects_cleanup_marker_owned_by_another_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    runner = _load("profile_runner")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv(
        "OMNIGENT_VERIFY_CLEANUP_MARKER",
        str(tmp_path / "other-run" / "cleanup.json"),
    )

    with pytest.raises(RuntimeError, match="not owned"):
        runner._controlled_environment(tmp_path, run_dir, profile, {})


def test_comparison_request_requires_valid_private_capability(
    tmp_path: Path,
) -> None:
    runner = _load("profile_runner")
    request = tmp_path / "request.json"
    capability = tmp_path / "capability"
    capability.write_bytes(b"x" * 32)
    capability.chmod(0o600)
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "server",
                "step_indices": [0],
                "changed_files": None,
                "nonce": "n" * 64,
                "signature": "forged",
            }
        ),
        encoding="utf-8",
    )
    request.chmod(0o600)

    with pytest.raises(RuntimeError, match="signature is invalid"):
        runner._load_request(request, capability, "server")
