"""
Tests for the Kubernetes (entrypoint-as-host) sandbox launcher.

The official ``kubernetes`` client is an optional dependency, so the SDK-driven
tests inject a small fake package into ``sys.modules`` (no real cluster, no real
client). The entrypoint model needs only ``CoreV1Api`` + ``BatchV1Api``
create/read/delete/log fakes — there is no exec transport to fake.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

import omnigent.onboarding.sandboxes.kubernetes as k8s
from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.onboarding.sandboxes.base import (
    render_host_config_write_command,
)
from omnigent.onboarding.sandboxes.kubernetes import (
    KubernetesSandboxLauncher,
    build_job_manifest,
    build_token_secret_manifest,
)
from omnigent.onboarding.sandboxes.types import RepoWorkspace

_TOKEN = "launch-token-xyz"
_MANIFEST_KW = {
    "job_name": "omnigent-managed-abc-1a2b3c",
    "namespace": "omnigent-sandboxes",
    "image": "ghcr.io/omnigent-ai/omnigent-host:latest",
    "service_account": "omnigent-runner",
    "host_id": "host_abcdef",
    "host_name": "managed-abcdef",
    "server_url": "http://srv.example.com",
    "token_secret_name": "omnigent-managed-abc-1a2b3c-token",
    "harness_secret": "omnigent-creds",
    "env_literals": {},
    "node_selector": None,
    "workspace": "/home/omnigent/workspace",
}

# Minimal valid host_config exercised by the injection tests below.
_HOST_CONFIG: dict[str, object] = {"providers": {"litellm": {"kind": "gateway"}}}

# The writable-HOME emptyDir as rendered with the default sizeLimit.
_BOUNDED_HOME_VOLUME = {"name": "home", "emptyDir": {"sizeLimit": k8s._HOME_SIZE_LIMIT_DEFAULT}}


def _pod_spec(manifest: dict) -> dict:
    """Extract the Pod spec from a Job manifest."""
    return manifest["spec"]["template"]["spec"]


# ── pure manifest / rendering tests (no SDK) ────────────────


def test_build_job_manifest_is_a_batch_v1_job() -> None:
    """The manifest is a batch/v1 Job with backoffLimit and activeDeadlineSeconds."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["spec"]["backoffLimit"] == k8s._JOB_BACKOFF_LIMIT
    assert manifest["spec"]["activeDeadlineSeconds"] == k8s._JOB_ACTIVE_DEADLINE_S


def test_build_job_manifest_restart_policy_is_on_failure() -> None:
    """The Pod template uses OnFailure for automatic container restart."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    assert _pod_spec(manifest)["restartPolicy"] == "OnFailure"


def test_build_job_manifest_runs_host_under_reaper_as_container_command() -> None:
    """The main container's command execs the PID-1 reaper, which runs the host."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    containers = _pod_spec(manifest)["containers"]
    assert len(containers) == 1
    host = containers[0]
    assert host["name"] == "host"
    command = host["command"]
    assert command[:2] == ["bash", "-lc"]
    script = command[2]
    assert "exec python3 -c" in script
    assert "omnigent host --server http://srv.example.com" in script
    assert "os.wait()" in script


def test_build_job_manifest_has_no_liveness_probe() -> None:
    """No liveness probe: the reaper propagates child exit, OnFailure handles restarts."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    host = _pod_spec(manifest)["containers"][0]
    assert "livenessProbe" not in host


def test_build_job_manifest_init_container_prepares_and_clones_workspace() -> None:
    """The init container makes the workspace and clones the repo before the host."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        repos=[
            RepoWorkspace(url="https://github.com/org/repo.git", branch="main", repo_name="repo")
        ],
    )
    init = _pod_spec(manifest)["initContainers"]
    assert len(init) == 1
    assert init[0]["name"] == "workspace-prep"
    script = init[0]["command"][2]
    assert "mkdir -p /home/omnigent/workspace" in script
    assert "git clone --branch main --single-branch -- " in script
    assert "https://github.com/org/repo.git /home/omnigent/workspace/repo" in script
    # The per-user broker is wired before the clone, and the init container gets
    # the launch token (secretKeyRef) so it can reach the broker.
    assert "configure_clone_credentials" in script
    assert script.index("configure_clone_credentials") < script.index("git clone")
    init_env = init[0]["env"]
    assert any(
        e["name"] == "OMNIGENT_HOST_TOKEN" and "secretKeyRef" in e.get("valueFrom", {})
        for e in init_env
    )


def test_build_job_manifest_clones_multiple_repos_as_parallel_siblings() -> None:
    """Several repos → each clones into its own sibling dir, backgrounded (parallel)."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        repos=[
            RepoWorkspace(url="https://github.com/org/a.git", branch=None, repo_name="a"),
            RepoWorkspace(url="https://github.com/org/b.git", branch="main", repo_name="b"),
        ],
    )
    script = _pod_spec(manifest)["initContainers"][0]["command"][2]
    assert "https://github.com/org/a.git /home/omnigent/workspace/a" in script
    assert (
        "--branch main --single-branch -- https://github.com/org/b.git "
        "/home/omnigent/workspace/b" in script
    )
    # Both clones are backgrounded and joined, and the broker is wired ONCE
    # (a single `python3 -c` line configures the helper for every clone).
    assert script.count(" & pids=") == 2
    assert 'for p in $pids; do wait "$p" || rc=1; done' in script
    assert script.count("python3 -c") == 1


def _run_failed_clone_with_credential_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    wire_mutation: str | None = None,
    cwd: Path | None = None,
    host_token: str = "test-launch-token-sentinel",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], list[str], list[str], Path]:
    """Run workspace prep with a recording git that rejects the clone."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_log = tmp_path / "git-argv.jsonl"
    helper_argv_log = tmp_path / "helper-argv.jsonl"
    config_log = tmp_path / "git-config-snapshots"
    path_log = tmp_path / "git-config-paths"
    mode_log = tmp_path / "git-config-modes"
    for log in (argv_log, helper_argv_log, config_log, path_log, mode_log):
        log.touch(mode=0o600)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
from pathlib import Path
import sys

if sys.argv[1:2] == ["-c"]:
    mutation = os.environ.get("WIRE_MUTATION")
    if not mutation:
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    prefix = "import omnigent.git_credential_github as g; "
    if mutation == "renamed_installer":
        prefix += (
            "del g._install_broker_helper; "
            "g.configure_clone_credentials=lambda *_args:True; "
        )
    elif mutation == "missing_git_config":
        prefix += "del g._git_config; "
    elif mutation == "failed_config_write":
        prefix += "g._git_config=lambda *_args:None; "
    elif mutation == "failed_reset_write":
        prefix += (
            "orig=g._git_config; "
            "g._git_config=lambda *args:None if args[0]=='--replace-all' else orig(*args); "
        )
    elif mutation == "unexpected_none":
        prefix += "g.configure_clone_credentials=lambda *_args:None; "
    elif mutation == "truthy_non_bool":
        prefix += (
            "configure=g.configure_clone_credentials; "
            "g.configure_clone_credentials=lambda *args:configure(*args) and 1; "
        )
    elif mutation == "disconnected_broker":
        prefix += "g.configure_clone_credentials=lambda *_args:False; "
    os.execv(sys.executable, [sys.executable, "-c", prefix + sys.argv[2]])
with Path(os.environ["HELPER_ARGV_LOG"]).open("a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
"""
    )
    fake_python.chmod(0o755)
    fake_git = fake_bin / "git"
    fake_git.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
with Path(os.environ["ARGV_LOG"]).open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:3] == ["config", "--global", "--get-all"]:
    path = Path(os.environ["GIT_CONFIG_GLOBAL"])
    key = args[3]
    values = []
    for line in path.read_text().splitlines():
        config_args = json.loads(line)
        if len(config_args) >= 3 and config_args[1] == key:
            values.append(config_args[-1])
    if os.environ.get("WIRE_MUTATION") == "extra_helper":
        values.append("!unsafe-helper")
    print("\\n".join(values))
    raise SystemExit(0 if values else 1)
if args[:2] == ["config", "--global"]:
    path = Path(os.environ.get("GIT_CONFIG_GLOBAL", Path(os.environ["HOME"]) / ".gitconfig"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(args[2:]) + "\\n")
    with Path(os.environ["CONFIG_LOG"]).open("a") as handle:
        handle.write(path.read_text())
    with Path(os.environ["PATH_LOG"]).open("a") as handle:
        handle.write(str(path) + "\\n")
    raise SystemExit(0)
if args and args[0] == "clone":
    config_path = Path(
        os.environ.get("GIT_CONFIG_GLOBAL", Path(os.environ["HOME"]) / ".gitconfig")
    )
    helper = json.loads(config_path.read_text().splitlines()[-1])[-1]
    subprocess.run(
        ["bash", "-c", helper.removeprefix("!") + " get"],
        input="protocol=https\\nhost=github.com\\n\\n",
        text=True,
        check=True,
    )
    print("simulated clone failure", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(128)
"""
    )
    fake_git.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    if wire_mutation == "disconnected_broker":
        fallback_log = tmp_path / "fallback-used"
        (home / ".gitconfig").write_text(
            json.dumps(
                [
                    "--add",
                    "credential.https://github.com.helper",
                    '!f() { [ "$1" = get ] || return 0; '
                    '[ -n "$GIT_TOKEN" ] || return 1; '
                    'printf used > "$FALLBACK_LOG"; }; f',
                ]
            )
            + "\n"
        )
        monkeypatch.setenv("FALLBACK_LOG", str(fallback_log))
        monkeypatch.setenv("GIT_TOKEN", "shared-token-sentinel")
    workspace = home / "workspace"
    monkeypatch.setenv("ARGV_LOG", str(argv_log))
    monkeypatch.setenv("HELPER_ARGV_LOG", str(helper_argv_log))
    monkeypatch.setenv("CONFIG_LOG", str(config_log))
    monkeypatch.setenv("PATH_LOG", str(path_log))
    monkeypatch.setenv("MODE_LOG", str(mode_log))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_HOST_TOKEN", host_token)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    if wire_mutation is None:
        monkeypatch.delenv("WIRE_MUTATION", raising=False)
    else:
        monkeypatch.setenv("WIRE_MUTATION", wire_mutation)
    command = k8s._render_workspace_prep_command(
        str(workspace),
        [
            RepoWorkspace(
                url="https://github.com/org/private.git", branch=None, repo_name="private"
            )
        ],
        ":",
        "host-test",
    )
    mode_probe = f"""
mktemp() {{
  credential_path=$(command mktemp "$@") || return
  {shlex.quote(sys.executable)} - "$credential_path" "$MODE_LOG" <<'PY'
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
mode = oct(stat.S_IMODE(path.stat().st_mode)) if path.exists() else "missing"
with Path(sys.argv[2]).open("a") as handle:
    handle.write(mode + "\\n")
PY
  printf '%s\n' "$credential_path"
}}
"""
    result = subprocess.run(
        ["bash", "-c", "umask 022\n" + mode_probe + command[2]],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        cwd=cwd,
    )
    argv = [
        json.loads(line)
        for log in (argv_log, helper_argv_log)
        for line in log.read_text().splitlines()
    ]
    paths = path_log.read_text().splitlines()
    modes = mode_log.read_text().splitlines()
    return result, argv, paths, modes, config_log


def test_failed_clone_removes_scoped_credential_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone failure leaves no credential helper configuration behind."""
    result, _argv, paths, _modes, _config_log = _run_failed_clone_with_credential_probe(
        tmp_path, monkeypatch
    )
    assert result.returncode != 0
    assert paths
    assert all(not Path(path).exists() for path in paths)
    assert not (tmp_path / "home" / ".gitconfig").exists()


def test_clone_credentials_keep_launch_token_out_of_argv_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone helper reads the launch token without persisting or execing it."""
    _result, argv, _paths, _modes, config_log = _run_failed_clone_with_credential_probe(
        tmp_path, monkeypatch
    )
    token = os.environ["OMNIGENT_HOST_TOKEN"]
    assert all(token not in argument for call in argv for argument in call)
    assert token not in config_log.read_text()


def test_clone_credential_config_is_private_at_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone-scoped credential config is private before Git writes it."""
    _result, _argv, _paths, modes, _config_log = _run_failed_clone_with_credential_probe(
        tmp_path, monkeypatch
    )
    assert modes == ["0o600"]


@pytest.mark.parametrize(
    "wire_mutation",
    [
        "renamed_installer",
        "missing_git_config",
        "failed_config_write",
        "failed_reset_write",
        "extra_helper",
        "unexpected_none",
        "truthy_non_bool",
    ],
)
def test_credential_wiring_drift_aborts_before_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire_mutation: str
) -> None:
    """Missing private hooks or an unwritable config abort before clone."""
    result, argv, _paths, _modes, _config_log = _run_failed_clone_with_credential_probe(
        tmp_path, monkeypatch, wire_mutation=wire_mutation
    )
    assert result.returncode != 0
    assert not any(call and call[0] == "clone" for call in argv)


def test_empty_launch_token_aborts_before_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty launch token cannot silently fall back to ambient credentials."""
    result, argv, _paths, _modes, _config_log = _run_failed_clone_with_credential_probe(
        tmp_path, monkeypatch, host_token=""
    )
    assert result.returncode != 0
    assert not any(call and call[0] == "clone" for call in argv)


def test_disconnected_broker_keeps_ambient_git_token_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disconnected broker leaves the image's shared Git helper active."""
    result, argv, paths, _modes, _config_log = _run_failed_clone_with_credential_probe(
        tmp_path, monkeypatch, wire_mutation="disconnected_broker"
    )
    assert result.returncode != 0
    assert any(call and call[0] == "clone" for call in argv)
    assert paths == []
    assert (tmp_path / "fallback-used").read_text() == "used"


def test_credential_python_ignores_workspace_package_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential imports cannot be shadowed by the init container's working directory."""
    shadow_dir = tmp_path / "shadow"
    package = shadow_dir / "omnigent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    marker = tmp_path / "shadow-imported"
    (package / "git_credential_github.py").write_text(
        'import os\nfrom pathlib import Path\nPath(os.environ["SHADOW_MARKER"]).touch()\n'
    )
    monkeypatch.setenv("SHADOW_MARKER", str(marker))

    _run_failed_clone_with_credential_probe(tmp_path, monkeypatch, cwd=shadow_dir)

    assert not marker.exists()


def test_credential_python_ignores_pythonpath_package_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential wiring cannot import a package planted on PYTHONPATH."""
    shadow_dir = tmp_path / "shadow"
    package = shadow_dir / "omnigent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    marker = tmp_path / "shadow-imported"
    (package / "git_credential_github.py").write_text(
        'import os\nfrom pathlib import Path\nPath(os.environ["SHADOW_MARKER"]).touch()\n'
    )
    monkeypatch.setenv("PYTHONPATH", str(shadow_dir))
    monkeypatch.setenv("SHADOW_MARKER", str(marker))

    _run_failed_clone_with_credential_probe(tmp_path, monkeypatch)

    assert not marker.exists()


def _run_workspace_clone(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    """Run the real renderer with Git probing intact and clone side effects faked."""
    target = workspace / "repo"
    clone_log = workspace.parent / f"{workspace.name}-clone-log"
    command = k8s._render_workspace_prep_command(
        str(workspace),
        [RepoWorkspace(url="https://github.com/org/repo.git", branch=None, repo_name="repo")],
        ":",
        "host-test",
    )
    script = (
        'python3() { if [ "$1" = "-c" ]; then return 0; fi; command python3 "$@"; }\n'
        "git() {\n"
        '  if [ "$1" = "-C" ]; then command git "$@"; return; fi\n'
        '  printf "%s\\n" "$*" >> "$CLONE_LOG"\n'
        '  clone_dir="${@: -1}"\n'
        '  mkdir -p "$clone_dir/.git"\n'
        '  printf "ref: refs/heads/main\\n" > "$clone_dir/.git/HEAD"\n'
        "}\n" + command[2]
    )
    monkeypatch.setenv("CLONE_LOG", str(clone_log))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=os.environ.copy()
    )
    clones = clone_log.read_text().splitlines() if clone_log.exists() else []
    return result, target, clones


def _workspace_snapshot(workspace: Path) -> dict[str, tuple[str, bytes | str]]:
    """Capture files and links for byte-exact refusal and preservation checks."""
    snapshot = {}
    for path in workspace.rglob("*"):
        relative = str(path.relative_to(workspace))
        if path.is_symlink():
            snapshot[relative] = ("link", os.readlink(path))
            resolved = path.resolve()
            if resolved.is_dir():
                for linked_path in resolved.rglob("*"):
                    linked_relative = f"{relative}=>{linked_path.relative_to(resolved)}"
                    if linked_path.is_symlink():
                        snapshot[linked_relative] = ("link", os.readlink(linked_path))
                    elif linked_path.is_file():
                        snapshot[linked_relative] = ("file", linked_path.read_bytes())
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("git_directory", "preserve"),
        ("gitfile", "preserve"),
        ("real_worktree", "refuse"),
        ("separate_git_dir", "refuse"),
        ("symlink_checkout", "preserve"),
        ("missing_head", "refuse"),
        ("ancestor_checkout", "clone"),
        ("git_dir", "clone"),
        ("git_dir_work_tree", "clone"),
        ("symlink_empty", "refuse"),
        ("plain_empty", "clone"),
        ("bare", "refuse"),
        ("ancestor_core_worktree", "clone"),
        ("gitfile_to_ancestor_core_worktree", "refuse"),
        ("gitfile_to_ancestor", "refuse"),
        ("ambient_work_tree_malformed", "refuse"),
    ],
)
def test_workspace_prep_classifies_checkout_at_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    expected: str,
) -> None:
    """Only a target-local checkout is preserved; occupied unsafe targets are refused."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    workspace = tmp_path / "workspace"
    if shape in {
        "ancestor_checkout",
        "ancestor_core_worktree",
        "gitfile_to_ancestor",
        "gitfile_to_ancestor_core_worktree",
    }:
        ancestor = tmp_path / "ancestor"
        subprocess.run(["git", "init", "-q", str(ancestor)], check=True)
        workspace = ancestor / "workspace"
    target = workspace / "repo"

    if shape == "git_directory":
        subprocess.run(["git", "init", "-q", str(target)], check=True)
    elif shape == "gitfile":
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        separate_git_dir = target / ".git-data"
        (target / ".git").rename(separate_git_dir)
        (target / ".git").write_text(f"gitdir: {separate_git_dir}\n")
    elif shape == "real_worktree":
        main_checkout = tmp_path / "main-checkout"
        subprocess.run(["git", "init", "-q", str(main_checkout)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(main_checkout),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "Initial commit",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(main_checkout), "worktree", "add", "-q", "-b", "test", str(target)],
            check=True,
        )
    elif shape == "separate_git_dir":
        separate_git_dir = tmp_path / "explicit-separate.git"
        subprocess.run(
            ["git", "init", "-q", "--separate-git-dir", str(separate_git_dir), str(target)],
            check=True,
        )
    elif shape == "symlink_checkout":
        checkout = tmp_path / "checkout"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        workspace.mkdir(parents=True)
        target.symlink_to(checkout, target_is_directory=True)
    elif shape == "missing_head":
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        (target / ".git" / "HEAD").unlink()
        (target / "keep.txt").write_text("preserve me\n")
    elif shape in {"gitfile_to_ancestor", "gitfile_to_ancestor_core_worktree"}:
        target.mkdir(parents=True)
        if shape == "gitfile_to_ancestor_core_worktree":
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(tmp_path / "ancestor"),
                    "config",
                    "core.worktree",
                    str(target),
                ],
                check=True,
            )
        (target / ".git").write_text(f"gitdir: {tmp_path / 'ancestor' / '.git'}\n")
        (target / "keep.txt").write_text("preserve me\n")
    elif shape in {"ancestor_checkout", "plain_empty", "ancestor_core_worktree"}:
        target.mkdir(parents=True)
        if shape == "ancestor_core_worktree":
            subprocess.run(
                ["git", "-C", str(tmp_path / "ancestor"), "config", "core.worktree", str(target)],
                check=True,
            )
    elif shape in {"git_dir", "git_dir_work_tree"}:
        target.mkdir(parents=True)
        ambient = tmp_path / "ambient"
        subprocess.run(["git", "init", "-q", str(ambient)], check=True)
        monkeypatch.setenv("GIT_DIR", str(ambient / ".git"))
        if shape == "git_dir_work_tree":
            monkeypatch.setenv("GIT_WORK_TREE", str(target))
    elif shape == "ambient_work_tree_malformed":
        (target / ".git").mkdir(parents=True)
        (target / ".git" / "config").write_text("incomplete repository\n")
        (target / "keep.txt").write_text("preserve me\n")
        ambient_git_dir = target / ".ambient-git"
        subprocess.run(["git", "init", "--bare", "-q", str(ambient_git_dir)], check=True)
        monkeypatch.setenv("GIT_DIR", str(ambient_git_dir))
        monkeypatch.setenv("GIT_WORK_TREE", str(target))
    elif shape == "symlink_empty":
        empty = tmp_path / "empty"
        empty.mkdir()
        workspace.mkdir(parents=True)
        target.symlink_to(empty, target_is_directory=True)
    elif shape == "bare":
        subprocess.run(["git", "init", "--bare", "-q", str(target)], check=True)

    before = _workspace_snapshot(workspace)
    result, target, clones = _run_workspace_clone(workspace, monkeypatch)

    if expected == "clone":
        assert result.returncode == 0, result.stderr
        assert len(clones) == 1
        assert (target / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    elif expected == "preserve":
        assert result.returncode == 0, result.stderr
        assert clones == []
        assert _workspace_snapshot(workspace) == before
    else:
        assert result.returncode != 0
        assert "refusing to overwrite" in result.stderr
        assert clones == []
        assert _workspace_snapshot(workspace) == before


def test_workspace_prep_refuses_malformed_directory_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed directory-shaped Git checkout is refused without edits."""
    workspace = tmp_path / "workspace"
    clone_dir = workspace / "repo"
    git_dir = clone_dir / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("malformed head\n")
    (clone_dir / "keep.txt").write_text("preserve me\n")
    before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    command = k8s._render_workspace_prep_command(
        str(workspace),
        [RepoWorkspace(url="https://github.com/org/repo.git", branch=None, repo_name="repo")],
        ":",
        "host-test",
    )
    result = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before


def test_workspace_prep_refuses_concurrent_target_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer populating the destination during clone makes placement fail."""
    workspace = tmp_path / "workspace"
    target = workspace / "repo"
    monkeypatch.setenv("RACE_TARGET", str(target))
    command = k8s._render_workspace_prep_command(
        str(workspace),
        [RepoWorkspace(url="https://github.com/org/repo.git", branch=None, repo_name="repo")],
        ":",
        "host-test",
    )
    script = (
        'python3() { if [ "$1" = "-c" ]; then return 0; fi; command python3 "$@"; }\n'
        "git() {\n"
        '  if [ "$1" = "-C" ]; then return 128; fi\n'
        '  clone_dir="${@: -1}"\n'
        '  mkdir -p "$clone_dir/.git" "$RACE_TARGET/.git"\n'
        '  printf "ref: refs/heads/main\\n" > "$clone_dir/.git/HEAD"\n'
        '  printf "foreign\\n" > "$RACE_TARGET/.git/HEAD"\n'
        "}\n" + command[2]
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0
    assert (target / ".git" / "HEAD").read_text() == "foreign\n"
    assert not (target / "clone").exists()


def test_build_job_manifest_disambiguates_same_named_repos() -> None:
    """Two repos with the same last-segment name clone into owner-qualified dirs,
    not one colliding directory that would fail the concurrent clone."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        repos=[
            RepoWorkspace(url="https://github.com/org-a/api", branch=None, repo_name="api"),
            RepoWorkspace(url="https://github.com/org-b/api", branch=None, repo_name="api"),
        ],
    )
    script = _pod_spec(manifest)["initContainers"][0]["command"][2]
    assert "https://github.com/org-a/api /home/omnigent/workspace/org-a__api" in script
    assert "https://github.com/org-b/api /home/omnigent/workspace/org-b__api" in script
    assert "workspace/api " not in script  # no plain colliding dir


def test_build_job_manifest_staging_never_collides_with_sibling_destination() -> None:
    """Repo ``foo`` must not stage through ``foo.tmp`` when a sibling repo is
    literally named ``foo.tmp`` — the staging-cleanup branch would refuse the
    launch against that sibling's checkout, or remove it outright if it carried
    a root-level ownership marker."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        repos=[
            RepoWorkspace(url="https://github.com/org/foo.git", branch=None, repo_name="foo"),
            RepoWorkspace(
                url="https://github.com/org/foo.tmp.git", branch=None, repo_name="foo.tmp"
            ),
        ],
    )
    script = _pod_spec(manifest)["initContainers"][0]["command"][2]
    # foo stages through the suffixed name, not the sibling's destination.
    assert "mkdir -- /home/omnigent/workspace/foo.tmp2\n" in script
    assert (
        "replace_empty_dir /home/omnigent/workspace/foo.tmp2/clone "
        "/home/omnigent/workspace/foo " in script
    )
    # No cleanup fragment ever targets the sibling's checkout directory.
    assert "rm -rf -- /home/omnigent/workspace/foo.tmp\n" not in script
    # The sibling still clones into its own destination via its own staging.
    assert (
        "replace_empty_dir /home/omnigent/workspace/foo.tmp.tmp/clone "
        "/home/omnigent/workspace/foo.tmp " in script
    )


def test_build_job_manifest_without_repo_has_no_clone() -> None:
    """No repo → the init container only makes the workspace, no git clone."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    init = _pod_spec(manifest)["initContainers"][0]
    script = init["command"][2]
    assert "mkdir -p /home/omnigent/workspace" in script
    assert "git clone" not in script
    # No repo → no broker wiring, and the launch token is NOT exposed to the
    # workspace-less init container.
    assert "configure_clone_credentials" not in script
    assert all(e["name"] != "OMNIGENT_HOST_TOKEN" for e in init["env"])


def test_build_job_manifest_host_config_is_written_by_init_container() -> None:
    """host_config rides the init container script, after mkdir/clone, before the host."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        repos=[
            RepoWorkspace(url="https://github.com/org/repo.git", branch=None, repo_name="repo")
        ],
        host_config=_HOST_CONFIG,
    )
    spec = _pod_spec(manifest)
    script = spec["initContainers"][0]["command"][2]
    write_command = render_host_config_write_command(_HOST_CONFIG)
    assert write_command in script
    assert script.index("mkdir -p") < script.index("git clone") < script.index(write_command)
    assert write_command not in spec["containers"][0]["command"][2]


def test_build_job_manifest_forwards_config_home_to_init_container() -> None:
    """Init and host resolve injected config under the same configured directory."""
    manifest = build_job_manifest(
        **{
            **_MANIFEST_KW,
            "env_literals": {
                "OMNIGENT_CONFIG_HOME": "/home/omnigent/custom-config",
                "PLAIN_CONFIG": "host-only",
            },
        },
        host_config=_HOST_CONFIG,
    )

    spec = _pod_spec(manifest)
    init_env = spec["initContainers"][0]["env"]
    host_env = spec["containers"][0]["env"]
    assert init_env == [
        {"name": "HOME", "value": "/home/omnigent"},
        {
            "name": "OMNIGENT_CONFIG_HOME",
            "value": "/home/omnigent/custom-config",
        },
    ]
    assert {entry["name"] for entry in host_env} >= {
        "OMNIGENT_CONFIG_HOME",
        "PLAIN_CONFIG",
    }


@pytest.mark.parametrize(
    "config_home",
    ["/tmp/elsewhere", "/home/omnigent-other", "/home/omnigent/../tmp", "../etc"],
)
def test_build_job_manifest_rejects_config_home_outside_home_dir(config_home: str) -> None:
    """
    Init and host share only the HOME emptyDir, so a config dir that resolves
    outside it would make the injected config invisible to the host.
    """
    with pytest.raises(ValueError, match=r"OMNIGENT_CONFIG_HOME.*must resolve under"):
        build_job_manifest(
            **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": config_home}},
            host_config=_HOST_CONFIG,
        )


@pytest.mark.parametrize(
    "config_home",
    ["/home/omnigent", "/home/omnigent/", "/home/omnigent/cfg", "cfg", "relative/dir", ".", ""],
)
def test_build_job_manifest_accepts_config_home_at_or_under_home_dir(config_home: str) -> None:
    """A dir at or under HOME is on the shared volume — allowed."""
    manifest = build_job_manifest(
        **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": config_home}},
        host_config=_HOST_CONFIG,
    )
    assert {"name": "OMNIGENT_CONFIG_HOME", "value": config_home} in _pod_spec(manifest)[
        "initContainers"
    ][0]["env"]


def test_build_job_manifest_config_home_outside_home_dir_ok_without_host_config() -> None:
    """Without host_config the init container writes nothing, so the path is moot."""
    manifest = build_job_manifest(
        **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": "/tmp/elsewhere"}},
    )
    init_env = _pod_spec(manifest)["initContainers"][0]["env"]
    assert {"name": "OMNIGENT_CONFIG_HOME", "value": "/tmp/elsewhere"} in init_env


def test_build_job_manifest_without_host_config_has_no_config_write() -> None:
    """No host_config → the init container only preps the workspace."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    script = _pod_spec(manifest)["initContainers"][0]["command"][2]
    assert "config.yaml" not in script
    assert "python3 -c" not in script


def test_build_job_manifest_token_rides_secret_ref_not_the_spec() -> None:
    """The launch token is referenced via secretKeyRef, never written into the spec."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    host_env = _pod_spec(manifest)["containers"][0]["env"]
    token_entry = next(e for e in host_env if e["name"] == HOST_TOKEN_ENV_VAR)
    assert token_entry["valueFrom"]["secretKeyRef"] == {
        "name": "omnigent-managed-abc-1a2b3c-token",
        "key": HOST_TOKEN_ENV_VAR,
    }
    assert "value" not in token_entry
    assert {e["name"]: e.get("value") for e in host_env}[HOST_ID_ENV_VAR] == "host_abcdef"
    assert {e["name"]: e.get("value") for e in host_env}[HOST_NAME_ENV_VAR] == "managed-abcdef"
    assert _TOKEN not in json.dumps(manifest)


def test_build_token_secret_manifest_carries_token_in_stringdata() -> None:
    """The token Secret holds the raw token under the host-token key, labeled for GC."""
    secret = build_token_secret_manifest(
        secret_name="omnigent-pod-token", namespace="omnigent-sandboxes", token=_TOKEN
    )
    assert secret["stringData"] == {HOST_TOKEN_ENV_VAR: _TOKEN}
    assert secret["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "omnigent"
    assert secret["type"] == "Opaque"


def test_build_job_manifest_harness_secret_projects_into_both_containers() -> None:
    """The harness creds Secret is projected via envFrom on init (for clone) + host."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    spec = _pod_spec(manifest)
    init = spec["initContainers"][0]
    host = spec["containers"][0]
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]
    assert host["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]


def test_build_job_manifest_omits_envfrom_without_harness_secret() -> None:
    """No harness Secret → no envFrom key on either container."""
    manifest = build_job_manifest(**{**_MANIFEST_KW, "harness_secret": None})
    spec = _pod_spec(manifest)
    assert "envFrom" not in spec["initContainers"][0]
    assert "envFrom" not in spec["containers"][0]


def test_build_job_manifest_defaults_to_amd64_node_selector() -> None:
    """No node_selector → Pods keep the amd64 default placement."""
    manifest = build_job_manifest(**{**_MANIFEST_KW, "node_selector": None})
    assert _pod_spec(manifest)["nodeSelector"] == {"kubernetes.io/arch": "amd64"}


def test_build_job_manifest_node_selector_can_override_arch() -> None:
    """An operator kubernetes.io/arch entry overrides the amd64 default."""
    manifest = build_job_manifest(
        **{**_MANIFEST_KW, "node_selector": {"disktype": "ssd", "kubernetes.io/arch": "arm64"}}
    )
    selector = _pod_spec(manifest)["nodeSelector"]
    assert selector["kubernetes.io/arch"] == "arm64"
    assert selector["disktype"] == "ssd"


def test_build_job_manifest_omits_tolerations_by_default() -> None:
    """No tolerations → no tolerations key: byte-compatible with pre-tolerations manifests."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    assert "tolerations" not in _pod_spec(manifest)


def test_build_job_manifest_tolerations_land_on_the_pod_spec_verbatim() -> None:
    """Normalized toleration entries reach spec.tolerations verbatim."""
    tolerations = [
        {
            "key": "sei.io/node-role",
            "operator": "Equal",
            "value": "omnigent-sandbox",
            "effect": "NoSchedule",
        },
        {"operator": "Exists"},
    ]
    manifest = build_job_manifest(**{**_MANIFEST_KW, "tolerations": tolerations})
    assert _pod_spec(manifest)["tolerations"] == tolerations


def test_build_job_manifest_omits_runtime_class_by_default() -> None:
    """No runtime_class → no runtimeClassName key: the cluster default runtime."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    assert "runtimeClassName" not in _pod_spec(manifest)


def test_build_job_manifest_runtime_class_sets_runtime_class_name() -> None:
    """An operator runtime_class lands verbatim as spec.runtimeClassName."""
    manifest = build_job_manifest(**{**_MANIFEST_KW, "runtime_class": "kata"})
    assert _pod_spec(manifest)["runtimeClassName"] == "kata"


def test_build_job_manifest_home_emptydir_is_bounded_by_default() -> None:
    """
    The writable-HOME emptyDir carries a sizeLimit (8Gi) out of the box: an
    unbounded emptyDir lets one sandbox push its node into disk pressure, and
    the kubelet then evicts by node-wide ranking — innocent Pods first.
    """
    manifest = build_job_manifest(**_MANIFEST_KW)
    volumes = {v["name"]: v for v in _pod_spec(manifest)["volumes"]}
    assert volumes["home"] == {"name": "home", "emptyDir": {"sizeLimit": "8Gi"}}
    assert k8s._HOME_SIZE_LIMIT_DEFAULT == "8Gi"


def test_build_job_manifest_home_size_limit_override_lands_on_the_emptydir() -> None:
    """An operator home_size_limit replaces the default sizeLimit verbatim."""
    manifest = build_job_manifest(**_MANIFEST_KW, home_size_limit="20Gi")
    volumes = {v["name"]: v for v in _pod_spec(manifest)["volumes"]}
    assert volumes["home"] == {"name": "home", "emptyDir": {"sizeLimit": "20Gi"}}


def test_build_job_manifest_home_size_limit_none_renders_unbounded_emptydir() -> None:
    """An explicit None (config `home_size_limit: null`) restores the unbounded emptyDir."""
    manifest = build_job_manifest(**_MANIFEST_KW, home_size_limit=None)
    volumes = {v["name"]: v for v in _pod_spec(manifest)["volumes"]}
    assert volumes["home"] == {"name": "home", "emptyDir": {}}


def test_resolve_pod_resources_defaults_leave_ephemeral_storage_unset() -> None:
    """No built-in ephemeral-storage: an omitted field stays out of the manifest
    so a namespace LimitRange can default it."""
    resources = k8s._resolve_pod_resources(None)
    assert resources == {
        "requests": {"cpu": k8s._SANDBOX_CPU_REQUEST, "memory": k8s._SANDBOX_MEMORY_REQUEST},
        "limits": {"cpu": k8s._SANDBOX_CPU_LIMIT, "memory": k8s._SANDBOX_MEMORY_LIMIT},
    }
    assert "ephemeral-storage" not in resources["requests"]
    assert "ephemeral-storage" not in resources["limits"]


def test_resolve_pod_resources_forwards_ephemeral_storage_in_both_tiers() -> None:
    """A configured ephemeral-storage request / limit reaches the container resources."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        resources={
            "requests": {"ephemeral-storage": "2Gi"},
            "limits": {"memory": "8Gi", "ephemeral-storage": "8Gi"},
        },
    )
    host = _pod_spec(manifest)["containers"][0]
    assert host["resources"]["requests"] == {
        "cpu": k8s._SANDBOX_CPU_REQUEST,
        "memory": k8s._SANDBOX_MEMORY_REQUEST,
        "ephemeral-storage": "2Gi",
    }
    assert host["resources"]["limits"] == {
        "cpu": k8s._SANDBOX_CPU_LIMIT,
        "memory": "8Gi",
        "ephemeral-storage": "8Gi",
    }


def test_build_job_manifest_pvc_mounts_land_on_host_container_only() -> None:
    """Each pvc_mounts entry becomes a persistentVolumeClaim volume mounted on host only."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        pvc_mounts=[
            {"claim_name": "omnigent-datasets", "mount_path": "/mnt/datasets", "read_only": True},
            {"claim_name": "scratch", "mount_path": "/mnt/scratch", "read_only": False},
        ],
    )
    spec = _pod_spec(manifest)
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["home"] == _BOUNDED_HOME_VOLUME
    assert volumes["pvc-0"]["persistentVolumeClaim"] == {
        "claimName": "omnigent-datasets",
        "readOnly": True,
    }
    assert volumes["pvc-1"]["persistentVolumeClaim"] == {"claimName": "scratch"}
    host_mounts = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}
    assert host_mounts["pvc-0"] == {
        "name": "pvc-0",
        "mountPath": "/mnt/datasets",
        "readOnly": True,
    }
    assert host_mounts["pvc-1"] == {"name": "pvc-1", "mountPath": "/mnt/scratch"}
    assert spec["initContainers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/omnigent"}
    ]


def test_build_job_manifest_without_pvc_mounts_is_unchanged() -> None:
    """No pvc_mounts → the single home emptyDir, exactly as before."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    spec = _pod_spec(manifest)
    assert spec["volumes"] == [_BOUNDED_HOME_VOLUME]
    assert spec["containers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/omnigent"}
    ]


def test_build_job_manifest_secret_mounts_land_on_host_container_only() -> None:
    """Each secret_mounts entry becomes a read-only secret volume mounted on host only."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        secret_mounts=[
            {"secret_name": "git-token", "mount_path": "/mnt/secrets/git"},
            {"secret_name": "npm-token", "mount_path": "/mnt/secrets/npm"},
        ],
    )
    spec = _pod_spec(manifest)
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["home"] == _BOUNDED_HOME_VOLUME
    assert volumes["secret-0"]["secret"] == {
        "secretName": "git-token",
        "optional": False,
        "defaultMode": 0o440,
    }
    assert volumes["secret-1"]["secret"] == {
        "secretName": "npm-token",
        "optional": False,
        "defaultMode": 0o440,
    }
    host_mounts = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}
    assert host_mounts["secret-0"] == {
        "name": "secret-0",
        "mountPath": "/mnt/secrets/git",
        "readOnly": True,
    }
    assert host_mounts["secret-1"] == {
        "name": "secret-1",
        "mountPath": "/mnt/secrets/npm",
        "readOnly": True,
    }
    assert spec["initContainers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/omnigent"}
    ]


def test_build_job_manifest_pvc_and_secret_mounts_coexist() -> None:
    """PVC and Secret mounts get independent name spaces (pvc-N / secret-N) on the host."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        pvc_mounts=[{"claim_name": "datasets", "mount_path": "/mnt/datasets", "read_only": True}],
        secret_mounts=[{"secret_name": "git-token", "mount_path": "/mnt/secrets/git"}],
    )
    spec = _pod_spec(manifest)
    volume_names = {v["name"] for v in spec["volumes"]}
    assert volume_names == {"home", "pvc-0", "secret-0"}
    host_mount_names = {m["name"] for m in spec["containers"][0]["volumeMounts"]}
    assert host_mount_names == {"home", "pvc-0", "secret-0"}


def test_build_job_manifest_without_secret_mounts_is_unchanged() -> None:
    """No secret_mounts → the single home emptyDir, exactly as before."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    spec = _pod_spec(manifest)
    assert spec["volumes"] == [_BOUNDED_HOME_VOLUME]
    assert spec["containers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/omnigent"}
    ]


def test_build_job_manifest_stamps_agent_label_alongside_reserved_pair() -> None:
    """A valid agent name adds the omnigent.ai/agent classifier; reserved pair stays."""
    manifest = build_job_manifest(**_MANIFEST_KW, agent_name="research-agent")
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "omnigent",
        "omnigent.ai/role": "sandbox-host",
        "omnigent.ai/agent": "research-agent",
    }


def test_build_job_manifest_echoes_valid_agent_name_verbatim() -> None:
    """The label value equals the agent name exactly — case, dots, and underscores
    are all valid label characters, so a valid name is never rewritten."""
    manifest = build_job_manifest(**_MANIFEST_KW, agent_name="Research.Agent_v2")
    assert manifest["metadata"]["labels"]["omnigent.ai/agent"] == "Research.Agent_v2"


def test_build_job_manifest_without_agent_label_keeps_only_reserved_pair() -> None:
    """No agent → labels are exactly the reserved managed-by/role pair."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "omnigent",
        "omnigent.ai/role": "sandbox-host",
    }


def test_build_job_manifest_empty_agent_label_is_omitted() -> None:
    """An empty agent name is treated as no agent — no omnigent.ai/agent key."""
    manifest = build_job_manifest(**_MANIFEST_KW, agent_name="")
    assert "omnigent.ai/agent" not in manifest["metadata"]["labels"]


@pytest.mark.parametrize(
    "raw",
    [
        "Research Agent",  # space is not a valid label character
        "agent/v2:beta",  # '/' and ':' are not valid label characters
        "--weird__name..",  # does not start/end alphanumeric
        "a" * 64,  # exceeds the 63-char label-value limit
        "///...---",  # nothing valid survives
    ],
)
def test_build_job_manifest_omits_agent_label_needing_transformation(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A name that is not ALREADY a valid label value is omitted, never coerced."""
    with caplog.at_level(logging.WARNING):
        manifest = build_job_manifest(**_MANIFEST_KW, agent_name=raw)
    assert "omnigent.ai/agent" not in manifest["metadata"]["labels"]
    assert any(
        "stays unclassified" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    ), f"the dropped label was not warned about: {[r.getMessage() for r in caplog.records]}"


def test_build_job_manifest_is_restricted_and_least_privilege() -> None:
    """The Pod template satisfies Pod Security 'restricted' and mounts no SA token."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    spec = _pod_spec(manifest)
    assert spec["restartPolicy"] == "OnFailure"
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    host = spec["containers"][0]
    assert host["securityContext"]["allowPrivilegeEscalation"] is False
    assert host["securityContext"]["capabilities"] == {"drop": ["ALL"]}


@pytest.mark.parametrize(
    ("repos", "expect_clone", "expect_branch"),
    [
        ([], False, False),
        ([RepoWorkspace(url="https://x/y.git", branch=None, repo_name="y")], True, False),
        ([RepoWorkspace(url="https://x/y.git", branch="release-1.2", repo_name="y")], True, True),
    ],
)
def test_render_workspace_prep_command(
    repos: list[RepoWorkspace],
    expect_clone: bool,
    expect_branch: bool,
) -> None:
    """The init command always mkdir's the workspace and clones only when asked."""
    command = k8s._render_workspace_prep_command(
        "/ws", repos, "http://srv.example.com", "host_abc"
    )
    script = command[2]
    assert "mkdir -p /ws" in script
    assert ("git clone" in script) is expect_clone
    assert ("--branch release-1.2 --single-branch" in script) is expect_branch
    # The per-user broker is wired (connected-gated at runtime) only when cloning.
    assert ("configure_clone_credentials" in script) is expect_clone
    assert script.count("python3 -c") == (1 if expect_clone else 0)


def test_new_pod_name_and_token_secret_name() -> None:
    """Pod names are DNS-label-safe and the token Secret is the name + suffix."""
    name = k8s._new_pod_name("Managed-ABC_123!")
    assert name.startswith("omnigent-managed-abc-123-")
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)
    assert k8s._token_secret_name(name) == f"{name}-token"


def test_resolve_sandbox_env_rejects_reserved_and_credential_and_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env passthrough rejects reserved names, credential-looking names, and unset vars."""
    monkeypatch.setenv("PLAIN_CONFIG", "value")
    assert KubernetesSandboxLauncher(env=["PLAIN_CONFIG"])._resolve_sandbox_env() == {
        "PLAIN_CONFIG": "value"
    }
    with pytest.raises(click.ClickException, match="reserved"):
        KubernetesSandboxLauncher(env=["HOME"])._resolve_sandbox_env()
    with pytest.raises(click.ClickException, match="credential"):
        KubernetesSandboxLauncher(env=["MY_API_KEY"])._resolve_sandbox_env()
    with pytest.raises(click.ClickException, match="not set"):
        KubernetesSandboxLauncher(env=["DEFINITELY_UNSET_VAR_XYZ"])._resolve_sandbox_env()


def test_env_var_name_override_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env-var namespace override that isn't a valid RFC 1123 name fails fast."""
    monkeypatch.setenv(k8s.NAMESPACE_ENV_VAR, "Not_A_Valid_NS")
    with pytest.raises(click.ClickException, match="not a valid Kubernetes name"):
        KubernetesSandboxLauncher()._resolve_namespace()


def test_pod_ready_timeout_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no config and no env var, the hardcoded default wins."""
    monkeypatch.delenv(k8s._POD_READY_TIMEOUT_ENV_VAR, raising=False)
    assert k8s._resolve_pod_ready_timeout_s(None) == k8s._POD_READY_TIMEOUT_S


def test_pod_ready_timeout_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit config, the env var overrides the hardcoded default."""
    monkeypatch.setenv(k8s._POD_READY_TIMEOUT_ENV_VAR, "300")
    assert k8s._resolve_pod_ready_timeout_s(None) == 300


def test_pod_ready_timeout_config_wins_over_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox.kubernetes.pod_ready_timeout_s takes precedence over the env var."""
    monkeypatch.setenv(k8s._POD_READY_TIMEOUT_ENV_VAR, "300")
    assert k8s._resolve_pod_ready_timeout_s(45) == 45


def test_pod_ready_timeout_env_var_accepts_float_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """A float-looking env value is accepted, matching the E2B lifetime resolver."""
    monkeypatch.setenv(k8s._POD_READY_TIMEOUT_ENV_VAR, "120.0")
    assert k8s._resolve_pod_ready_timeout_s(None) == 120


def test_pod_ready_timeout_env_var_rejects_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed env value fails fast with a clear error instead of a raw ValueError."""
    monkeypatch.setenv(k8s._POD_READY_TIMEOUT_ENV_VAR, "not-a-number")
    with pytest.raises(click.ClickException, match="must be a number of seconds"):
        k8s._resolve_pod_ready_timeout_s(None)


# ── SDK-driven tests (fake kubernetes client) ───────────────


class _FakeApiException(Exception):
    """Stands in for ``kubernetes.client.rest.ApiException``."""

    def __init__(self, *, status: int | None = None, reason: str = "", body: str = "") -> None:
        super().__init__(reason or body or str(status))
        self.status = status
        self.reason = reason
        self.body = body


class _FakeConfigException(Exception):
    """Stands in for ``kubernetes.config.ConfigException``."""


class _FakeDeleteOptions:
    """Stands in for ``kubernetes.client.V1DeleteOptions``."""

    def __init__(self, propagation_policy=None):
        self.propagation_policy = propagation_policy


class _FakeCore:
    """Recording stand-in for ``CoreV1Api``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created_secrets: list[dict[str, object]] = []
        self.deleted_secrets: list[str] = []
        self.deleted_pods: list[str] = []
        self.delete_pod_errors: list[Exception | None] = []
        self.events: list[object] = []
        self.logs: dict[str, str] = {}
        self.read_queue: list[object] = []
        self.read_default: object = _pod(phase="Pending")
        self.create_secret_error: Exception | None = None
        self.list_pod_error: Exception | None = None
        self.last_label_selector: str | None = None
        self.pod_list_items: list[object] = []

    def create_namespaced_secret(self, namespace, body, _request_timeout=None):
        self.calls.append("create_secret")
        if self.create_secret_error is not None:
            raise self.create_secret_error
        self.created_secrets.append(body)

    def list_namespaced_pod(self, namespace, label_selector=None, _request_timeout=None):
        self.calls.append("list_pod")
        self.last_label_selector = label_selector
        if self.list_pod_error is not None:
            raise self.list_pod_error
        return SimpleNamespace(items=self.pod_list_items)

    def read_namespaced_pod(self, name, namespace, _request_timeout=None):
        self.calls.append("read_pod")
        resp = self.read_queue.pop(0) if self.read_queue else self.read_default
        if isinstance(resp, Exception):
            raise resp
        return resp

    def delete_namespaced_secret(self, name, namespace, _request_timeout=None):
        self.calls.append("delete_secret")
        self.deleted_secrets.append(name)

    def delete_namespaced_pod(self, name, namespace, _request_timeout=None):
        self.calls.append("delete_pod")
        if self.delete_pod_errors:
            err = self.delete_pod_errors.pop(0)
            if err is not None:
                raise err
        self.deleted_pods.append(name)

    def list_namespaced_event(self, namespace, field_selector=None, _request_timeout=None):
        return SimpleNamespace(items=self.events)

    def read_namespaced_pod_log(
        self, name, namespace, container=None, tail_lines=None, _request_timeout=None
    ):
        return self.logs.get(container, "")


class _FakeBatch:
    """Recording stand-in for ``BatchV1Api``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created_jobs: list[dict[str, object]] = []
        self.deleted_jobs: list[str] = []
        self.last_delete_body: object = None
        self.create_job_error: Exception | None = None
        self.delete_job_errors: list[Exception | None] = []

    def create_namespaced_job(self, namespace, body, _request_timeout=None):
        self.calls.append("create_job")
        if self.create_job_error is not None:
            raise self.create_job_error
        self.created_jobs.append(body)

    def delete_namespaced_job(self, name, namespace, body=None, _request_timeout=None):
        self.calls.append("delete_job")
        self.last_delete_body = body
        if self.delete_job_errors:
            err = self.delete_job_errors.pop(0)
            if err is not None:
                raise err
        self.deleted_jobs.append(name)


def _pod(phase=None, init_statuses=None, container_statuses=None, conditions=None):
    """Build a ``V1Pod`` stand-in (the launcher reads only ``status`` via getattr)."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name="omnigent-pod-child-xyz"),
        status=SimpleNamespace(
            phase=phase,
            init_container_statuses=init_statuses,
            container_statuses=container_statuses,
            conditions=conditions,
        ),
    )


def _terminated(exit_code, *, name, reason="Error"):
    """A container status in the terminated state."""
    return SimpleNamespace(
        name=name,
        state=SimpleNamespace(
            terminated=SimpleNamespace(exit_code=exit_code, reason=reason), waiting=None
        ),
    )


@pytest.fixture
def fake_clients(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeCore, _FakeBatch]:
    """Inject a fake ``kubernetes`` package and return the recording CoreV1Api + BatchV1Api."""
    core = _FakeCore()
    batch = _FakeBatch()

    client_mod = types.ModuleType("kubernetes.client")
    client_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    client_mod.Configuration = lambda: SimpleNamespace()  # type: ignore[attr-defined]
    client_mod.ApiClient = lambda cfg=None: SimpleNamespace(  # type: ignore[attr-defined]
        close=lambda: None
    )
    client_mod.CoreV1Api = lambda api_client=None: core  # type: ignore[attr-defined]
    client_mod.BatchV1Api = lambda api_client=None: batch  # type: ignore[attr-defined]
    client_mod.V1DeleteOptions = _FakeDeleteOptions  # type: ignore[attr-defined]
    rest_mod = types.ModuleType("kubernetes.client.rest")
    rest_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    config_mod = types.ModuleType("kubernetes.config")
    config_mod.load_incluster_config = lambda client_configuration=None: None  # type: ignore[attr-defined]
    config_mod.load_kube_config = (  # type: ignore[attr-defined]
        lambda config_file=None, client_configuration=None: None
    )
    config_mod.ConfigException = _FakeConfigException  # type: ignore[attr-defined]
    pkg = types.ModuleType("kubernetes")
    pkg.client = client_mod  # type: ignore[attr-defined]
    pkg.config = config_mod  # type: ignore[attr-defined]

    for name, mod in (
        ("kubernetes", pkg),
        ("kubernetes.client", client_mod),
        ("kubernetes.client.rest", rest_mod),
        ("kubernetes.config", config_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    # No-op the poll/backoff sleeps so the readiness/retry loops run instantly.
    monkeypatch.setattr(k8s.time, "sleep", lambda _s: None)
    return core, batch


def _launcher() -> KubernetesSandboxLauncher:
    """A launcher pinned to in-cluster config with explicit, env-free settings."""
    return KubernetesSandboxLauncher(
        in_cluster=True, namespace="omnigent-sandboxes", secret_name="omnigent-creds", env=()
    )


def _setup_pod_discovery(core: _FakeCore, pod_phase: str = "Running") -> None:
    """Set up the fake core to return a child Pod when listed by job-name label."""
    child_pod = _pod(phase=pod_phase)
    core.pod_list_items = [child_pod]
    core.read_queue = [child_pod]


def test_launch_host_creates_secret_then_job_and_returns_workspace(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """The happy path creates the token Secret BEFORE the Job and returns the workspace."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    workspace = _launcher().start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    assert workspace == "/home/omnigent/workspace"
    # Secret is created before the Job.
    all_calls = core.calls + batch.calls
    assert all_calls.index("create_secret") < all_calls.index("create_job")
    assert core.created_secrets[0]["stringData"] == {HOST_TOKEN_ENV_VAR: _TOKEN}
    assert batch.created_jobs[0]["metadata"]["name"] == "omnigent-job-1"
    # Nothing torn down on success.
    assert batch.deleted_jobs == []


def test_launch_host_threads_pvc_mounts_into_the_job(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A launcher built with pvc_mounts creates Jobs carrying the PVC volume."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=(),
        pvc_mounts=[
            {"claim_name": "omnigent-datasets", "mount_path": "/mnt/datasets", "read_only": True}
        ],
    )
    launcher.start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    pod_spec = batch.created_jobs[0]["spec"]["template"]["spec"]
    assert {
        "name": "pvc-0",
        "persistentVolumeClaim": {"claimName": "omnigent-datasets", "readOnly": True},
    } in pod_spec["volumes"]


def test_launch_host_threads_tolerations_into_the_job(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A launcher built with tolerations creates Jobs whose Pod spec carries them."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    tolerations = [
        {
            "key": "sei.io/node-role",
            "operator": "Equal",
            "value": "omnigent-sandbox",
            "effect": "NoSchedule",
        }
    ]
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=(),
        tolerations=tolerations,
    )
    launcher.start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    pod_spec = batch.created_jobs[0]["spec"]["template"]["spec"]
    assert pod_spec["tolerations"] == tolerations


@pytest.mark.parametrize(
    ("home_size_limit", "expected_empty_dir"),
    [("20Gi", {"sizeLimit": "20Gi"}), (None, {})],
)
def test_launch_host_threads_home_size_limit_into_the_job(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    home_size_limit: str | None,
    expected_empty_dir: dict[str, str],
) -> None:
    """A launcher built with home_size_limit creates Jobs whose HOME emptyDir carries it."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=(),
        home_size_limit=home_size_limit,
    )
    launcher.start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    pod_spec = batch.created_jobs[0]["spec"]["template"]["spec"]
    assert {"name": "home", "emptyDir": expected_empty_dir} in pod_spec["volumes"]


def test_launch_host_threads_secret_mounts_into_the_job(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A launcher built with secret_mounts creates Jobs carrying the secret volume."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=(),
        secret_mounts=[{"secret_name": "git-token", "mount_path": "/mnt/secrets/git"}],
    )
    launcher.start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    pod_spec = batch.created_jobs[0]["spec"]["template"]["spec"]
    assert {
        "name": "secret-0",
        "secret": {"secretName": "git-token", "optional": False, "defaultMode": 0o440},
    } in pod_spec["volumes"]


def test_launch_host_threads_agent_label_into_the_job(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """start_host stamps the resolved agent on the created Job's labels."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    _launcher().start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
        agent_name="research-agent",
    )
    labels = batch.created_jobs[0]["metadata"]["labels"]
    assert labels["omnigent.ai/agent"] == "research-agent"
    assert labels["app.kubernetes.io/managed-by"] == "omnigent"


def test_launch_host_without_agent_label_keeps_reserved_labels(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """No agent_name → the created Job carries only the reserved managed-by/role pair."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    _launcher().start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    assert "omnigent.ai/agent" not in batch.created_jobs[0]["metadata"]["labels"]


def test_launch_host_with_repo_returns_clone_dir(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """With one repo, the returned workspace is the cloned directory under the workspace."""
    core, _batch = fake_clients
    _setup_pod_discovery(core)
    workspace = _launcher().start_host(
        "omnigent-job-2",
        token=_TOKEN,
        host_id="host_2",
        host_name="managed-2",
        server_url="http://srv.example.com",
        repos=[
            RepoWorkspace(url="https://github.com/org/repo.git", branch=None, repo_name="repo")
        ],
    )
    assert workspace == "/home/omnigent/workspace/repo"


def test_launch_host_with_multiple_repos_returns_parent_workspace(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """With several repos, the returned workspace is the parent that holds them all."""
    core, _batch = fake_clients
    _setup_pod_discovery(core)
    workspace = _launcher().start_host(
        "omnigent-job-2b",
        token=_TOKEN,
        host_id="host_2b",
        host_name="managed-2b",
        server_url="http://srv.example.com",
        repos=[
            RepoWorkspace(url="https://github.com/org/a.git", branch=None, repo_name="a"),
            RepoWorkspace(url="https://github.com/org/b.git", branch=None, repo_name="b"),
        ],
    )
    assert workspace == "/home/omnigent/workspace"


def test_launch_host_cleans_up_on_create_failure(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A failed Job create reaps the already-created token Secret and raises."""
    core, batch = fake_clients
    batch.create_job_error = _FakeApiException(status=500, reason="Internal Server Error")
    with pytest.raises(click.ClickException, match="create sandbox job"):
        _launcher().start_host(
            "omnigent-job-3",
            token=_TOKEN,
            host_id="host_3",
            host_name="managed-3",
            server_url="http://srv.example.com",
        )
    assert "omnigent-job-3-token" in core.deleted_secrets
    assert "omnigent-job-3" in batch.deleted_jobs


def test_launch_host_invalid_config_home_fails_before_creating_secret(
    fake_clients: tuple[_FakeCore, _FakeBatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An out-of-HOME config dir must fail while the manifest is built — before the
    token Secret is created.
    """
    core, _batch = fake_clients
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", "/tmp/outside")
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=["OMNIGENT_CONFIG_HOME"],
    )
    with pytest.raises(ValueError, match=r"OMNIGENT_CONFIG_HOME.*must resolve under"):
        launcher.start_host(
            "omnigent-job-x",
            token=_TOKEN,
            host_id="host_x",
            host_name="managed-x",
            server_url="http://srv.example.com",
            host_config=_HOST_CONFIG,
        )
    assert "create_secret" not in core.calls
    assert core.created_secrets == []


def test_launch_host_fast_fails_on_clone_failure_with_log_tail(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """Once backoffLimit is exhausted (phase Failed), fast-fail with the log tail."""
    core, batch = fake_clients
    # Phase "Failed" means the Job exhausted its backoffLimit — terminal.
    failed_pod = _pod(
        phase="Failed",
        init_statuses=[_terminated(128, name="workspace-prep")],
    )
    core.pod_list_items = [failed_pod]
    core.read_queue = [failed_pod]
    core.logs["workspace-prep"] = "fatal: repository 'https://x/y.git' not found"
    with pytest.raises(click.ClickException) as exc:
        _launcher().start_host(
            "omnigent-job-4",
            token=_TOKEN,
            host_id="host_4",
            host_name="managed-4",
            server_url="http://srv.example.com",
            repos=[RepoWorkspace(url="https://x/y.git", branch=None, repo_name="y")],
        )
    assert "workspace prep failed (exit 128" in exc.value.message
    assert "repository 'https://x/y.git' not found" in exc.value.message
    # The orphaned Job and Secret are cleaned up on failure.
    assert "delete_job" in batch.calls
    assert core.deleted_secrets == ["omnigent-job-4-token"]


def test_launch_host_times_out_with_reason(
    fake_clients: tuple[_FakeCore, _FakeBatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Pod that never runs times out fast, surfacing the last waiting reason."""
    core, _batch = fake_clients
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    pending_pod = _pod(
        phase="Pending",
        container_statuses=[
            SimpleNamespace(
                name="host",
                state=SimpleNamespace(
                    waiting=SimpleNamespace(reason="ImagePullBackOff", message="back-off"),
                    terminated=None,
                ),
            )
        ],
    )
    core.pod_list_items = [pending_pod]
    core.read_default = pending_pod
    with pytest.raises(click.ClickException, match="did not start within"):
        _launcher().start_host(
            "omnigent-job-5",
            token=_TOKEN,
            host_id="host_5",
            host_name="managed-5",
            server_url="http://srv.example.com",
        )


@pytest.mark.parametrize(
    ("wait_state", "expected"),
    [
        ("undiscovered", "did not create a child pod within 1s"),
        ("replaced", "could not be rediscovered before the 1s deadline"),
        ("read-error", "could not be read before the 1s deadline"),
        ("pending", "did not start within 1s"),
    ],
)
def test_configured_pod_ready_timeout_bounds_entire_job_wait(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    monkeypatch: pytest.MonkeyPatch,
    wait_state: str,
    expected: str,
) -> None:
    """The configured budget bounds discovery, replacement, reads, and Pending."""
    core, _batch = fake_clients
    pod = _pod(phase="Pending")
    if wait_state != "undiscovered":
        core.pod_list_items = [pod]
    if wait_state == "replaced":
        core.read_default = _FakeApiException(status=404, reason="Not Found")
    elif wait_state == "read-error":
        core.read_default = _FakeApiException(status=500, reason="Internal Server Error")
    else:
        core.read_default = pod

    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(k8s.time, "monotonic", lambda: next(ticks))
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=(),
        pod_ready_timeout_s=1,
    )

    with pytest.raises(click.ClickException, match=expected):
        launcher._wait_for_pod_running("omnigent-sandboxes", "omnigent-job-timeout")


def test_terminate_deletes_job_and_secret(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """Terminate deletes the Job, attempts bare-Pod fallback, and deletes the Secret."""
    core, batch = fake_clients
    _launcher().terminate("omnigent-job-6")
    assert batch.deleted_jobs == ["omnigent-job-6"]
    assert batch.last_delete_body.propagation_policy == "Foreground"
    assert core.deleted_pods == ["omnigent-job-6"]
    assert core.deleted_secrets == ["omnigent-job-6-token"]


def test_resume_recycles_job_and_token_secret(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """Resume clears stale launch resources so start_host can recreate the same id."""
    core, batch = fake_clients
    launcher = _launcher()

    assert launcher.can_resume is True
    assert launcher.capabilities.resume_stopped is True

    launcher.resume("omnigent-job-resume")

    assert batch.deleted_jobs == ["omnigent-job-resume"]
    assert batch.last_delete_body.propagation_policy == "Foreground"
    assert core.deleted_pods == ["omnigent-job-resume"]
    assert core.deleted_secrets == ["omnigent-job-resume-token"]


def test_terminate_is_idempotent_on_404(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A Job 404 still deletes the bare Pod fallback and the Secret."""
    core, batch = fake_clients
    batch.delete_job_errors = [_FakeApiException(status=404, reason="Not Found")]
    _launcher().terminate("omnigent-job-7")  # must not raise
    assert core.deleted_pods == ["omnigent-job-7"]
    assert core.deleted_secrets == ["omnigent-job-7-token"]


def test_terminate_retries_transient_then_gives_up_best_effort(
    fake_clients: tuple[_FakeCore, _FakeBatch], capsys: pytest.CaptureFixture[str]
) -> None:
    """A persistent transient delete error is retried, then warned (not raised)."""
    from urllib3.exceptions import HTTPError

    core, batch = fake_clients
    batch.delete_job_errors = [HTTPError("timeout")] * k8s._DELETE_MAX_ATTEMPTS
    _launcher().terminate("omnigent-job-8")  # best-effort: must not raise
    assert "could not delete Kubernetes job 'omnigent-job-8'" in capsys.readouterr().err
    # The Secret delete still runs after the Job gives up.
    assert core.deleted_secrets == ["omnigent-job-8-token"]


def test_terminate_still_deletes_secret_on_job_403(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A 403 on Job delete still cleans up the Secret (no leak)."""
    core, batch = fake_clients
    batch.delete_job_errors = [_FakeApiException(status=403, reason="Forbidden")]
    with pytest.raises(click.ClickException, match="Forbidden"):
        _launcher().terminate("omnigent-job-9")
    # Secret must still be deleted even though Job delete raised.
    assert core.deleted_secrets == ["omnigent-job-9-token"]


def test_find_job_pod_raises_on_403(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 on pods:list surfaces immediately, not as a misleading 90s timeout."""
    core, _batch = fake_clients
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    core.list_pod_error = _FakeApiException(status=403, reason="Forbidden")
    core.read_default = _pod(phase="Running")
    with pytest.raises(click.ClickException, match="list sandbox pods"):
        _launcher().start_host(
            "omnigent-job-rbac",
            token=_TOKEN,
            host_id="host_rbac",
            host_name="managed-rbac",
            server_url="http://srv.example.com",
        )


def test_find_job_pod_sends_correct_label_selector(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """The launcher queries for the child Pod using the correct job-name label."""
    core, _batch = fake_clients
    _setup_pod_discovery(core)
    _launcher().start_host(
        "omnigent-job-sel",
        token=_TOKEN,
        host_id="host_sel",
        host_name="managed-sel",
        server_url="http://srv.example.com",
    )
    assert core.last_label_selector == "job-name=omnigent-job-sel"


def test_wait_rediscovers_pod_on_404(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A 404 on read (Pod replaced) triggers re-discovery, not a terminal failure."""
    core, _batch = fake_clients
    replacement_pod = _pod(phase="Running")
    replacement_pod.metadata = SimpleNamespace(
        name="omnigent-job-repl-abc", deletion_timestamp=None
    )
    # First discovery returns original, which 404s on read.
    # Second discovery returns the replacement, which is Running.
    original_pod = _pod(phase="Pending")
    original_pod.metadata = SimpleNamespace(name="omnigent-job-repl-xyz", deletion_timestamp=None)
    core.pod_list_items = [original_pod]
    core.read_queue = [
        _FakeApiException(status=404, reason="Not Found"),
    ]

    # After the 404, re-discovery should find the replacement.
    def list_pod_side_effect(namespace, label_selector=None, _request_timeout=None):
        core.calls.append("list_pod")
        core.last_label_selector = label_selector
        # Second call returns the replacement.
        core.pod_list_items = [replacement_pod]
        return SimpleNamespace(items=core.pod_list_items)

    core.list_namespaced_pod = list_pod_side_effect
    core.read_default = replacement_pod
    workspace = _launcher().start_host(
        "omnigent-job-repl",
        token=_TOKEN,
        host_id="host_repl",
        host_name="managed-repl",
        server_url="http://srv.example.com",
    )
    assert workspace is not None


def test_crashloopbackoff_is_terminal(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A host in CrashLoopBackOff is detected even though the Pod is Running."""
    core, _batch = fake_clients
    crashloop_pod = _pod(
        phase="Running",
        container_statuses=[
            SimpleNamespace(
                name="host",
                restart_count=3,
                state=SimpleNamespace(
                    waiting=SimpleNamespace(reason="CrashLoopBackOff", message="back-off"),
                    terminated=None,
                ),
            )
        ],
    )
    core.pod_list_items = [crashloop_pod]
    core.read_queue = [crashloop_pod]
    with pytest.raises(click.ClickException, match="crash-looping"):
        _launcher().start_host(
            "omnigent-job-crash",
            token=_TOKEN,
            host_id="host_crash",
            host_name="managed-crash",
            server_url="http://srv.example.com",
        )


def _crashloop_waiting(name, restart_count=2):
    """A container status parked in CrashLoopBackOff (kubelet backing off)."""
    return SimpleNamespace(
        name=name,
        restart_count=restart_count,
        state=SimpleNamespace(
            waiting=SimpleNamespace(
                reason="CrashLoopBackOff",
                message=f"back-off 20s restarting failed container={name}",
            ),
            terminated=None,
        ),
        last_state=SimpleNamespace(terminated=SimpleNamespace(exit_code=128, reason="Error")),
    )


def test_init_crashloopbackoff_pending_is_terminal(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash-looping init container fails the launch fast, not at the deadline.

    The Pod sits in ``Pending`` while the kubelet restarts the workspace-prep
    init container (e.g. its git clone keeps failing); the start wait must
    detect the crash loop instead of polling out the pod-ready budget.
    """
    core, _batch = fake_clients
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    crashloop_pod = _pod(
        phase="Pending",
        init_statuses=[_crashloop_waiting("workspace-prep")],
    )
    core.pod_list_items = [crashloop_pod]
    core.read_default = crashloop_pod
    with pytest.raises(click.ClickException) as excinfo:
        _launcher().start_host(
            "omnigent-job-init-crash",
            token=_TOKEN,
            host_id="host_init_crash",
            host_name="managed-init-crash",
            server_url="http://srv.example.com",
        )
    message = str(excinfo.value)
    assert "init container 'workspace-prep' is crash-looping" in message
    assert "did not start within" not in message


def test_init_crashloopbackoff_failure_carries_init_log_tail(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash-loop failure carries the init container's own log tail."""
    core, _batch = fake_clients
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    clone_error = "fatal: could not resolve host: git.example.invalid"
    core.logs["workspace-prep"] = clone_error + "\n"
    crashloop_pod = _pod(
        phase="Pending",
        init_statuses=[_crashloop_waiting("workspace-prep")],
    )
    core.pod_list_items = [crashloop_pod]
    core.read_default = crashloop_pod
    with pytest.raises(click.ClickException) as excinfo:
        _launcher().start_host(
            "omnigent-job-init-log",
            token=_TOKEN,
            host_id="host_init_log",
            host_name="managed-init-log",
            server_url="http://srv.example.com",
        )
    assert clone_error in str(excinfo.value)


def test_init_failure_pending_is_not_terminal(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under OnFailure, a Pending init failure is NOT terminal (kubelet retries)."""
    core, _batch = fake_clients
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    # Init container failed but Pod is still Pending — kubelet will retry.
    pending_pod = _pod(
        phase="Pending",
        init_statuses=[_terminated(128, name="workspace-prep")],
    )
    core.pod_list_items = [pending_pod]
    core.read_default = pending_pod
    # Should time out, NOT fast-fail with "workspace prep failed".
    with pytest.raises(click.ClickException, match="did not start within"):
        _launcher().start_host(
            "omnigent-job-retry",
            token=_TOKEN,
            host_id="host_retry",
            host_name="managed-retry",
            server_url="http://srv.example.com",
        )


def test_provision_reserves_pod_name_and_no_exec_transport() -> None:
    """provision reserves a Pod name (no Pod created); no exec transport."""
    launcher = _launcher()
    name = launcher.provision("managed-abc")
    assert name.startswith("omnigent-managed-abc-")
    assert not hasattr(launcher, "run")
    assert launcher.capabilities.cli_bootstrap is False
    assert launcher.capabilities.classifies_runner_by_agent is True
