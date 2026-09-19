"""Prepare isolated workspace seeds and authorize their owner before host startup."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from omnigent.experimental.workspace_profiles.profiles import (
    SeedManifest,
    SeedRepository,
    manifest_sha,
    parse_manifest,
)
from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER

SEED_DIRECTORY = Path("/opt/omnigent-workspace-seed")
_MARKER = ".omnigent-workspace-profile.json"
_MAX_JSON_BYTES = 1024 * 1024
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_TOKEN_ENV = "OMNIGENT_WORKSPACE_GIT_TOKEN"


class WorkspaceProfileError(Exception):
    """An error safe to expose without subprocess output or credentials."""


@dataclass(frozen=True, repr=False)
class BrokerCredential:
    owner: str
    token: str


def _regular_file(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise WorkspaceProfileError("Workspace profile files must be regular files.")


def _directory(path: Path) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise WorkspaceProfileError("Workspace profile directories must not be symlinks.")


def _read_json(path: Path) -> object:
    _regular_file(path)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise WorkspaceProfileError("Workspace profile JSON exceeds its size limit.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise WorkspaceProfileError("Invalid workspace profile JSON.") from None


def file_sha256(path: Path) -> str:
    """Hash a regular seed file without loading a large bundle into memory."""
    _regular_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(seed_directory: Path, expected_sha: str) -> SeedManifest:
    """Verify the pinned manifest and every credential-free bundle before use."""
    if not _HEX64.fullmatch(expected_sha):
        raise WorkspaceProfileError("Invalid workspace manifest digest.")
    try:
        _directory(seed_directory)
        manifest = parse_manifest(_read_json(seed_directory / "manifest.json"))
        if manifest_sha(manifest) != expected_sha:
            raise WorkspaceProfileError("Workspace manifest digest does not match its profile.")
        for repo in manifest.repos:
            if repo.directory == _MARKER:
                raise WorkspaceProfileError("Repository directory conflicts with workspace state.")
            if file_sha256(seed_directory / repo.bundle) != repo.bundle_sha256:
                raise WorkspaceProfileError("Workspace bundle digest does not match its manifest.")
        return manifest
    except (OSError, ValueError):
        raise WorkspaceProfileError("Invalid or incomplete workspace seed.") from None


class IsolatedGit:
    """Git running outside ambient credential/configuration and shell startup state."""

    def __init__(self, scratch: Path, token: str | None = None) -> None:
        self.scratch = scratch
        self.env = {
            "PATH": os.defpath,
            "HOME": str(scratch),
            "XDG_CONFIG_HOME": str(scratch / "xdg"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "LANG": "C",
            "LC_ALL": "C",
        }
        if token is not None:
            askpass = scratch / "askpass"
            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
                f"  *Password*) printf '%s\\n' \"${_GIT_TOKEN_ENV}\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
            )
            askpass.chmod(0o700)
            self.env["GIT_ASKPASS"] = str(askpass)
            self.env[_GIT_TOKEN_ENV] = token
        else:
            self.env["GIT_ASKPASS"] = "/usr/bin/false"

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        allowed_codes: tuple[int, ...] = (0,),
    ) -> bytes:
        """Execute trusted argv; never include Git stderr or credentials in errors."""
        command = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            "http.extraHeader=",
            "-c",
            "http.followRedirects=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "init.templateDir=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "protocol.file.allow=always",
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=cwd or self.scratch,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise WorkspaceProfileError("Workspace Git operation failed.") from None
        if result.returncode not in allowed_codes:
            raise WorkspaceProfileError("Workspace repository access or Git operation failed.")
        return result.stdout


@contextlib.contextmanager
def isolated_git(token: str | None = None) -> Iterator[IsolatedGit]:
    with tempfile.TemporaryDirectory(prefix="omnigent-profile-git-") as scratch:
        yield IsolatedGit(Path(scratch), token)


def reject_unsupported_content(git: IsolatedGit, checkout: Path, commit: str) -> None:
    """Reject submodules and Git LFS instead of silently omitting private content."""
    entries = git.run(["ls-tree", "-rz", "--full-tree", commit], cwd=checkout).split(b"\0")
    for entry in filter(None, entries):
        metadata, _, path = entry.partition(b"\t")
        name = path.rsplit(b"/", 1)[-1]
        if metadata.startswith(b"160000 ") or name in {b".gitmodules", b".lfsconfig"}:
            raise WorkspaceProfileError("Workspace seeds do not support submodules or Git LFS.")
        if name == b".gitattributes":
            attributes = git.run(["show", f"{commit}:{os.fsdecode(path)}"], cwd=checkout)
            if re.search(rb"filter\s*=\s*lfs(?:\s|$)", attributes):
                raise WorkspaceProfileError("Workspace seeds do not support Git LFS.")
    pointers = git.run(
        ["grep", "-I", "-l", "-e", "^version https://git-lfs.github.com/spec/v1$", commit, "--"],
        cwd=checkout,
        allowed_codes=(0, 1),
    )
    if pointers:
        raise WorkspaceProfileError("Workspace seeds do not support Git LFS.")


def _write_marker(workspace: Path, value: dict[str, object]) -> None:
    marker = workspace / _MARKER
    if marker.exists() or marker.is_symlink():
        _regular_file(marker)
    descriptor, temporary = tempfile.mkstemp(prefix=".profile-state-", dir=workspace)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_marker(workspace: Path, expected_sha: str) -> dict[str, object]:
    _directory(workspace)
    value = _read_json(workspace / _MARKER)
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("manifest_sha") != expected_sha
        or type(value.get("initialized")) is not bool
    ):
        raise WorkspaceProfileError("Workspace state does not match its original seed profile.")
    if value["initialized"] and (
        not isinstance(value.get("host_id"), str)
        or not value["host_id"]
        or not isinstance(value.get("owner"), str)
        or not value["owner"]
    ):
        raise WorkspaceProfileError("Initialized workspace identity is missing.")
    return value


@contextlib.contextmanager
def _workspace_lock(home: Path) -> Iterator[None]:
    import fcntl

    _directory(home)
    lock_path = home / ".omnigent-workspace-profile.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkspaceProfileError("Invalid workspace lock file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def prepare_workspace(home: Path, seed_directory: Path, expected_sha: str) -> None:
    """Atomically seed a new HOME; retained workspaces are never overwritten."""
    manifest = load_manifest(seed_directory, expected_sha)
    workspace = home / "workspace"
    with _workspace_lock(home):
        if workspace.exists() or workspace.is_symlink():
            _directory(workspace)
            if any(workspace.iterdir()):
                _read_marker(workspace, expected_sha)
                return
        staging = Path(tempfile.mkdtemp(prefix=".omnigent-workspace-seed-", dir=home))
        try:
            with isolated_git() as git:
                for repo in manifest.repos:
                    checkout = staging / repo.directory
                    git.run(
                        [
                            "clone",
                            "--no-checkout",
                            "--single-branch",
                            "--branch",
                            repo.branch,
                            "--",
                            str(seed_directory / repo.bundle),
                            str(checkout),
                        ]
                    )
                    head = git.run(["rev-parse", "HEAD"], cwd=checkout).decode().strip()
                    if head != repo.commit:
                        raise WorkspaceProfileError(
                            "Workspace bundle contains an unexpected commit."
                        )
                    reject_unsupported_content(git, checkout, repo.commit)
                    git.run(["checkout", "--force", "-B", repo.branch, repo.commit], cwd=checkout)
                    git.run(["remote", "set-url", "origin", repo.url], cwd=checkout)
            _write_marker(
                staging, {"version": 1, "manifest_sha": expected_sha, "initialized": False}
            )
            os.rename(staging, workspace)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _broker_credential(server_url: str, host_id: str, host_token: str) -> BrokerCredential:
    if not host_id or not host_token:
        raise WorkspaceProfileError("Workspace activation requires a managed-host identity.")
    parsed = urlsplit(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise WorkspaceProfileError("Invalid credential broker URL.")
    url = f"{server_url.rstrip('/')}/v1/hosts/{quote(host_id, safe='')}/credentials/github"
    try:
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            response = client.get(url, headers={MANAGED_HOST_TOKEN_HEADER: host_token})
        if response.status_code != 200:
            raise WorkspaceProfileError("Owner GitHub credential lookup failed.")
        value = response.json()
    except (httpx.HTTPError, ValueError):
        raise WorkspaceProfileError("Owner GitHub credential lookup failed.") from None
    if (
        not isinstance(value, dict)
        or value.get("connected") is not True
        or not isinstance(value.get("token"), str)
        or not value["token"].strip()
        or not isinstance(value.get("owner"), str)
        or not value["owner"]
    ):
        raise WorkspaceProfileError(
            "Connect the owner's GitHub account before using a workspace seed."
        )
    return BrokerCredential(owner=value["owner"], token=value["token"])


def _remote_head(git: IsolatedGit, repo: SeedRepository) -> str:
    ref = f"refs/heads/{repo.branch}"
    lines = (
        git.run(["ls-remote", "--exit-code", "--refs", "--", repo.url, ref]).decode().splitlines()
    )
    matches = [line.split("\t")[0] for line in lines if line.endswith(f"\t{ref}")]
    if len(matches) != 1 or not _HEX40.fullmatch(matches[0]):
        raise WorkspaceProfileError("The requested repository branch is unavailable.")
    return matches[0]


def _authorize_repository(git: IsolatedGit, repo: SeedRepository) -> None:
    # Retained workspaces can outlive their original remote branch.
    git.run(["ls-remote", "--refs", "--", repo.url])


def _updated_bundle_checkout(
    git: IsolatedGit, repo: SeedRepository, seed_directory: Path, commit: str
) -> Path:
    checkout = Path(tempfile.mkdtemp(prefix="update-", dir=git.scratch))
    git.run(["clone", "--bare", "--", str(seed_directory / repo.bundle), str(checkout)])
    git.run(
        [
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            repo.url,
            f"refs/heads/{repo.branch}",
        ],
        cwd=checkout,
    )
    fetched = git.run(["rev-parse", "FETCH_HEAD"], cwd=checkout).decode().strip()
    if fetched != commit:
        raise WorkspaceProfileError(
            "Repository branch moved during activation; retry the session."
        )
    reject_unsupported_content(git, checkout, commit)
    return checkout


def _execute_prepare(command: Sequence[str]) -> None:
    try:
        result = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        )
    except (OSError, subprocess.SubprocessError):
        raise WorkspaceProfileError("Workspace preparation failed.") from None
    if result.returncode:
        raise WorkspaceProfileError("Workspace preparation failed.")


def activate_workspace(
    *,
    home: Path,
    seed_directory: Path,
    expected_sha: str,
    server_url: str,
    host_id: str,
    host_token: str,
    prepare_command: Sequence[str],
) -> None:
    """Authorize all seed content before startup; refresh only a never-assigned workspace."""
    if (
        not prepare_command
        or not prepare_command[0]
        or any(not isinstance(arg, str) or "\0" in arg for arg in prepare_command)
    ):
        raise WorkspaceProfileError("Invalid workspace preparation command.")
    manifest = load_manifest(seed_directory, expected_sha)
    workspace = home / "workspace"
    with _workspace_lock(home):
        marker = _read_marker(workspace, expected_sha)
        credential = _broker_credential(server_url, host_id, host_token)
        if marker["initialized"] and (
            marker["host_id"] != host_id or marker["owner"] != credential.owner
        ):
            raise WorkspaceProfileError("Workspace is assigned to a different owner or host.")
        with isolated_git(credential.token) as authenticated:
            if marker["initialized"]:
                for repo in manifest.repos:
                    _authorize_repository(authenticated, repo)
            else:
                # Authenticate every repository before fetching or changing any checkout.
                heads = [(repo, _remote_head(authenticated, repo)) for repo in manifest.repos]
                updated = {
                    repo.directory: _updated_bundle_checkout(
                        authenticated, repo, seed_directory, head
                    )
                    for repo, head in heads
                    if head != repo.commit
                }
                with isolated_git() as local:
                    for repo, head in heads:
                        checkout = workspace / repo.directory
                        _directory(checkout)
                        _directory(checkout / ".git")
                        if repo.directory in updated:
                            local.run(
                                [
                                    "fetch",
                                    "--no-tags",
                                    "--no-recurse-submodules",
                                    "--",
                                    str(updated[repo.directory]),
                                    head,
                                ],
                                cwd=checkout,
                            )
                        local.run(
                            ["update-ref", f"refs/remotes/origin/{repo.branch}", head],
                            cwd=checkout,
                        )
                        local.run(["checkout", "--force", "-B", repo.branch, head], cwd=checkout)
                _write_marker(
                    workspace,
                    {
                        "version": 1,
                        "manifest_sha": expected_sha,
                        "initialized": True,
                        "host_id": host_id,
                        "owner": credential.owner,
                    },
                )
        _execute_prepare(prepare_command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("prepare").add_argument("--manifest-sha", required=True)
    commands.add_parser("activate").add_argument("payload")
    args = parser.parse_args(argv)
    try:
        home = Path(os.environ["HOME"])
        if not home.is_absolute():
            raise WorkspaceProfileError("Workspace HOME must be absolute.")
        if args.mode == "prepare":
            prepare_workspace(home, SEED_DIRECTORY, args.manifest_sha)
            from omnigent.host.warm_bootstrap import prepare

            return prepare()
        payload = json.loads(args.payload)
        if not isinstance(payload, dict) or set(payload) != {
            "manifest_sha",
            "prepare_command",
            "server_url",
        }:
            raise WorkspaceProfileError("Invalid workspace activation payload.")
        if not isinstance(payload["manifest_sha"], str) or not isinstance(
            payload["server_url"], str
        ):
            raise WorkspaceProfileError("Invalid workspace activation payload.")
        if not isinstance(payload["prepare_command"], list):
            raise WorkspaceProfileError("Invalid workspace activation payload.")
        activate_workspace(
            home=home,
            seed_directory=SEED_DIRECTORY,
            expected_sha=payload["manifest_sha"],
            server_url=payload["server_url"],
            host_id=os.environ.get(HOST_ID_ENV_VAR, ""),
            host_token=os.environ.get(HOST_TOKEN_ENV_VAR, ""),
            prepare_command=payload["prepare_command"],
        )
        return 0
    except WorkspaceProfileError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, KeyError):
        print("Workspace seed initialization failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
