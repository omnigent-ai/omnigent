"""Real local Git verifies seed lifecycle; broker and remote transport stay offline."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.experimental.workspace_profiles import runtime, seed
from omnigent.experimental.workspace_profiles.profiles import manifest_sha, parse_manifest

pytestmark = pytest.mark.posix_only


def git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.defpath,
        "HOME": str(repo),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Seed Test",
        "GIT_AUTHOR_EMAIL": "seed@example.test",
        "GIT_COMMITTER_NAME": "Seed Test",
        "GIT_COMMITTER_EMAIL": "seed@example.test",
    }
    result = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@dataclass
class Lab:
    root: Path
    remotes: dict[str, Path] = field(default_factory=dict)
    calls: list[tuple[list[str], Path, dict[str, str]]] = field(default_factory=list)
    preparations: list[list[str]] = field(default_factory=list)

    def remote(self, name: str = "Repo") -> tuple[str, Path]:
        path = self.root / name
        path.mkdir()
        git(path, "init")
        (path / "file.txt").write_text("seed version\n")
        git(path, "add", ".")
        git(path, "commit", "-m", "initial")
        url = f"https://github.com/Example/{name}.git"
        self.remotes[url.lower()] = path
        return url, path

    def seed(self, names: tuple[str, ...] = ("Repo",)) -> tuple[Path, str]:
        repositories = [(self.remote(name)[0], "main") for name in names]
        output = self.root / "image-seed"
        manifest = seed.build_seed(repositories, output, "builder-test-token")
        return output, manifest_sha(manifest)

    def prepare(self, output: Path, digest: str, name: str = "home") -> Path:
        home = self.root / name
        home.mkdir()
        runtime.prepare_workspace(home, output, digest)
        return home

    def activate(self, home: Path, output: Path, digest: str, **kwargs: Any) -> None:
        runtime.activate_workspace(
            home=home,
            seed_directory=output,
            expected_sha=digest,
            server_url="https://server.example.test",
            host_id=kwargs.get("host_id", "host-1"),
            host_token="host-test-token",
            prepare_command=["normal-workspace-prep"],
        )


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    fixture = Lab(tmp_path)
    original_run = runtime.IsolatedGit.run

    def local_remote(self: runtime.IsolatedGit, args: Any, **kwargs: Any) -> bytes:
        fixture.calls.append((list(args), kwargs.get("cwd", self.scratch), dict(self.env)))
        redirected = (
            [str(fixture.remotes.get(arg, arg)) for arg in args]
            if args[0] in {"clone", "fetch", "ls-remote"}
            else args
        )
        return original_run(self, redirected, **kwargs)

    monkeypatch.setattr(runtime.IsolatedGit, "run", local_remote)
    monkeypatch.setattr(
        runtime,
        "_broker_credential",
        lambda *_: runtime.BrokerCredential("alice", "owner-test-token"),
    )
    monkeypatch.setattr(
        runtime, "_execute_prepare", lambda args: fixture.preparations.append(list(args))
    )
    return fixture


def test_seed_artifact_has_only_bundles_and_manifest_without_credentials(lab: Lab) -> None:
    output, digest = lab.seed()
    manifest = runtime.load_manifest(output, digest)
    assert manifest.repos[0].directory == "Repo"
    assert manifest.repos[0].url == "https://github.com/example/repo.git"
    assert {p.name for p in output.iterdir()} == {"manifest.json", "repo-0.bundle"}
    for path in output.iterdir():
        assert b"builder-test-token" not in path.read_bytes()
    for args, _, env in lab.calls:
        assert "builder-test-token" not in " ".join(args)
        assert "GIT_TOKEN" not in env


def test_prepared_homes_have_independent_checkouts(lab: Lab) -> None:
    output, digest = lab.seed()
    first = lab.prepare(output, digest, "first")
    second = lab.prepare(output, digest, "second")
    first_file = first / "workspace/Repo/file.txt"
    second_file = second / "workspace/Repo/file.txt"
    first_file.write_text("private edits")
    assert second_file.read_text() == "seed version\n"
    assert first_file.stat().st_ino != second_file.stat().st_ino
    assert (
        git(first / "workspace/Repo", "remote", "get-url", "origin")
        == "https://github.com/example/repo.git"
    )


def test_ready_bootstrap_runs_only_after_seed_copy(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.host import warm_bootstrap

    output, digest = lab.seed()
    home = lab.root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime, "SEED_DIRECTORY", output)
    entered = []

    def ready() -> int:
        assert (home / "workspace/Repo/file.txt").read_text() == "seed version\n"
        entered.append(True)
        return 0

    monkeypatch.setattr(warm_bootstrap, "prepare", ready)
    assert runtime.main(["prepare", "--manifest-sha", digest]) == 0
    assert entered == [True]
    (output / "repo-0.bundle").write_bytes(b"corrupt")
    assert runtime.main(["prepare", "--manifest-sha", digest]) == 1
    assert entered == [True]


def test_activation_rechecks_remote_even_for_existing_git_checkout(lab: Lab) -> None:
    output, digest = lab.seed()
    home = lab.prepare(output, digest)
    lab.calls.clear()
    lab.activate(home, output, digest)
    assert any(args[0] == "ls-remote" for args, _, _ in lab.calls)
    assert lab.preparations == [["normal-workspace-prep"]]


@pytest.mark.parametrize("reason", ["denied", "unlinked", "timeout"])
def test_failed_owner_check_never_runs_preparation_on_existing_checkout(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    output, digest = lab.seed()
    home = lab.prepare(output, digest)
    monkeypatch.setenv("GIT_TOKEN", "shared-token-must-not-be-used")

    def fail(*_: object) -> runtime.BrokerCredential:
        raise runtime.WorkspaceProfileError(reason)

    monkeypatch.setattr(runtime, "_broker_credential", fail)
    before = git(home / "workspace/Repo", "rev-parse", "HEAD")
    with pytest.raises(runtime.WorkspaceProfileError):
        lab.activate(home, output, digest)
    assert not lab.preparations
    assert git(home / "workspace/Repo", "rev-parse", "HEAD") == before


def test_all_repository_access_is_checked_before_any_workspace_mutation(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, digest = lab.seed(("First", "Second"))
    home = lab.prepare(output, digest)
    checkout = home / "workspace/First"
    (checkout / "file.txt").write_text("untouched if another repository is denied\n")
    remote = lab.remotes["https://github.com/example/first.git"]
    (remote / "file.txt").write_text("changed remote\n")
    git(remote, "commit", "-am", "update")
    original = runtime._remote_head

    def check(auth: runtime.IsolatedGit, repo: Any) -> str:
        if repo.directory == "Second":
            raise runtime.WorkspaceProfileError("denied")
        return original(auth, repo)

    monkeypatch.setattr(runtime, "_remote_head", check)
    lab.calls.clear()
    with pytest.raises(runtime.WorkspaceProfileError):
        lab.activate(home, output, digest)
    assert (checkout / "file.txt").read_text().startswith("untouched")
    assert not any(args[0] in {"fetch", "checkout"} for args, _, _ in lab.calls)
    assert not lab.preparations


def test_first_activation_refreshes_stale_branch_using_owner_only(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, digest = lab.seed()
    home = lab.prepare(output, digest)
    remote = next(iter(lab.remotes.values()))
    (remote / "file.txt").write_text("fresh remote content\n")
    git(remote, "commit", "-am", "new head")
    new_head = git(remote, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_TOKEN", "shared-token")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("SSH_AUTH_SOCK", "untrusted-agent")
    lab.calls.clear()
    lab.activate(home, output, digest)
    checkout = home / "workspace/Repo"
    assert git(checkout, "rev-parse", "HEAD") == new_head
    assert git(checkout, "rev-parse", "origin/main") == new_head
    assert git(checkout, "rev-list", "--left-right", "--count", "HEAD...@{upstream}") == "0\t0"
    assert (checkout / "file.txt").read_text() == "fresh remote content\n"
    network = [
        (args, cwd, env)
        for args, cwd, env in lab.calls
        if any(str(arg).startswith("https://github.com/") for arg in args)
    ]
    assert network
    for _, cwd, env in network:
        assert not cwd.is_relative_to(home)
        assert env[runtime._GIT_TOKEN_ENV] == "owner-test-token"
        assert not {"GIT_TOKEN", "GIT_CONFIG_COUNT", "SSH_AUTH_SOCK"} & env.keys()
    local = [env for _, cwd, env in lab.calls if cwd == checkout]
    assert local and all(runtime._GIT_TOKEN_ENV not in env for env in local)


def test_wake_preserves_branch_commits_and_dirty_files_but_rechecks_access(lab: Lab) -> None:
    output, digest = lab.seed()
    home = lab.prepare(output, digest)
    lab.activate(home, output, digest)
    checkout = home / "workspace/Repo"
    git(checkout, "checkout", "-b", "user-work")
    (checkout / "committed.txt").write_text("own commit")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "user commit")
    own_commit = git(checkout, "rev-parse", "HEAD")
    (checkout / "file.txt").write_text("dirty user edits")
    remote = next(iter(lab.remotes.values()))
    (remote / "file.txt").write_text("new upstream")
    git(remote, "commit", "-am", "upstream moved")
    lab.calls.clear()
    runtime.prepare_workspace(home, output, digest)
    lab.activate(home, output, digest)
    assert git(checkout, "rev-parse", "HEAD") == own_commit
    assert git(checkout, "branch", "--show-current") == "user-work"
    assert (checkout / "file.txt").read_text() == "dirty user edits"
    assert [args[0] for args, _, _ in lab.calls] == ["ls-remote"]


def test_wake_denial_retains_files_and_does_not_run_preparation(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, digest = lab.seed()
    home = lab.prepare(output, digest)
    lab.activate(home, output, digest)
    marker = home / "workspace/Repo/user-file"
    marker.write_text("retain me")
    lab.preparations.clear()

    def deny(*_: object) -> None:
        raise runtime.WorkspaceProfileError("revoked")

    monkeypatch.setattr(runtime, "_authorize_repository", deny)
    with pytest.raises(runtime.WorkspaceProfileError):
        lab.activate(home, output, digest)
    assert marker.read_text() == "retain me"
    assert not lab.preparations


@pytest.mark.parametrize("initialized", [False, True])
def test_deleted_original_branch_only_blocks_first_activation(lab: Lab, initialized: bool) -> None:
    output, digest = lab.seed()
    home = lab.prepare(output, digest)
    if initialized:
        lab.activate(home, output, digest)
    checkout = home / "workspace/Repo"
    original_head = git(checkout, "rev-parse", "HEAD")
    (checkout / "file.txt").write_text("retained user work")
    remote = next(iter(lab.remotes.values()))
    git(remote, "checkout", "-b", "merged")
    git(remote, "branch", "-D", "main")
    lab.preparations.clear()
    lab.calls.clear()
    if initialized:
        lab.activate(home, output, digest)
        assert lab.preparations == [["normal-workspace-prep"]]
    else:
        with pytest.raises(runtime.WorkspaceProfileError):
            lab.activate(home, output, digest)
        assert not lab.preparations
    assert git(checkout, "rev-parse", "HEAD") == original_head
    assert (checkout / "file.txt").read_text() == "retained user work"
    assert [args[0] for args, _, _ in lab.calls] == ["ls-remote"]


@pytest.mark.parametrize("changed", ["owner", "host"])
def test_initialized_workspace_cannot_change_owner_or_host(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    output, digest = lab.seed()
    home = lab.prepare(output, digest)
    lab.activate(home, output, digest)
    lab.preparations.clear()
    if changed == "owner":
        monkeypatch.setattr(
            runtime,
            "_broker_credential",
            lambda *_: runtime.BrokerCredential("bob", "other-test-token"),
        )
    with pytest.raises(runtime.WorkspaceProfileError, match="different owner or host"):
        lab.activate(home, output, digest, host_id="host-2" if changed == "host" else "host-1")
    assert not lab.preparations


@pytest.mark.parametrize(
    "damage", ["manifest", "digest", "bundle", "bundle-symlink", "directory-traversal"]
)
def test_invalid_seed_is_rejected_before_workspace_creation(lab: Lab, damage: str) -> None:
    output, digest = lab.seed()
    if damage == "manifest":
        (output / "manifest.json").write_text("not json")
    elif damage == "digest":
        digest = "0" * 64
    elif damage == "bundle":
        (output / "repo-0.bundle").write_bytes(b"wrong bundle")
    elif damage == "bundle-symlink":
        bundle = output / "repo-0.bundle"
        real = output / "real.bundle"
        bundle.rename(real)
        bundle.symlink_to(real)
    else:
        raw = json.loads((output / "manifest.json").read_text())
        raw["repos"][0]["directory"] = "../outside"
        (output / "manifest.json").write_text(json.dumps(raw))
    home = lab.root / "home"
    home.mkdir()
    with pytest.raises(runtime.WorkspaceProfileError):
        runtime.prepare_workspace(home, output, digest)
    assert not (home / "workspace").exists()


@pytest.mark.parametrize("target", ["workspace", "marker", "lock"])
def test_control_state_symlinks_are_rejected(lab: Lab, target: str) -> None:
    output, digest = lab.seed()
    home = lab.prepare(output, digest)
    if target == "workspace":
        workspace = home / "workspace"
        retained = home / "retained"
        workspace.rename(retained)
        workspace.symlink_to(retained, target_is_directory=True)
    else:
        path = home / (
            "workspace/" + runtime._MARKER
            if target == "marker"
            else ".omnigent-workspace-profile.lock"
        )
        other = home / "retained-file"
        path.rename(other)
        path.symlink_to(other)
    with pytest.raises((runtime.WorkspaceProfileError, OSError)):
        runtime.prepare_workspace(home, output, digest)
    assert not lab.preparations


@pytest.mark.parametrize("content", ["submodule", "lfs-attributes", "lfs-pointer"])
def test_builder_rejects_unsupported_content_without_publishing(lab: Lab, content: str) -> None:
    url, remote = lab.remote()
    if content == "submodule":
        (remote / ".gitmodules").write_text('[submodule "private"]\npath = private\n')
    elif content == "lfs-attributes":
        (remote / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    else:
        (remote / "data.bin").write_text(
            "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 100\n"
        )
    git(remote, "add", ".")
    git(remote, "commit", "-m", "unsupported")
    output = lab.root / "rejected-seed"
    with pytest.raises(runtime.WorkspaceProfileError, match=r"submodules|Git LFS"):
        seed.build_seed([(url, "main")], output, "builder-test-token")
    assert not output.exists()


@pytest.mark.parametrize(
    "payload", [{"connected": False}, {"connected": True, "owner": "alice"}, ["bad-json-shape"]]
)
def test_broker_requires_connected_owner_token(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    client = httpx.Client
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs))
    monkeypatch.setenv("GIT_TOKEN", "shared-token")
    with pytest.raises(runtime.WorkspaceProfileError):
        runtime._broker_credential("https://server.example.test", "host-1", "host-test-token")


def test_builder_supports_relative_output_and_preserves_original_directory_case(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = lab.remote("AriseLM")
    monkeypatch.chdir(lab.root)
    manifest = seed.build_seed([(url, "main")], Path("seed-output"), "builder-test-token")
    assert manifest.repos[0].directory == "AriseLM"
    assert runtime.load_manifest(lab.root / "seed-output", manifest_sha(manifest)) == manifest


def test_manifest_digest_is_independent_of_json_whitespace(lab: Lab) -> None:
    output, digest = lab.seed()
    path = output / "manifest.json"
    raw = json.loads(path.read_text())
    path.write_text(json.dumps(raw, indent=4))
    assert runtime.load_manifest(output, digest) == parse_manifest(raw)
