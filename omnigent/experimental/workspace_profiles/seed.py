"""Build credential-free repository bundles for an experimental workspace image."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from omnigent.experimental.workspace_profiles.profiles import (
    SeedManifest,
    canonical_github_url,
    manifest_sha,
    parse_manifest,
    valid_branch,
)
from omnigent.experimental.workspace_profiles.runtime import (
    WorkspaceProfileError,
    file_sha256,
    isolated_git,
    reject_unsupported_content,
)
from omnigent.onboarding.sandboxes.types import RepoWorkspace, clone_dir_names

BUILDER_TOKEN_ENV = "OMNIGENT_WORKSPACE_SEED_GITHUB_TOKEN"


def build_seed(repositories: Sequence[tuple[str, str]], output: Path, token: str) -> SeedManifest:
    """Clone explicit branches into disposable state and publish only bundles and metadata."""
    if not token.strip():
        raise WorkspaceProfileError(
            "The workspace seed builder requires a GitHub token environment variable."
        )
    if not repositories or any(not valid_branch(branch) for _, branch in repositories):
        raise WorkspaceProfileError("Workspace seeds require explicit valid repository branches.")
    repos = []
    for url, branch in repositories:
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name.lower().endswith(".git"):
            name = name[:-4]
        repos.append(RepoWorkspace(canonical_github_url(url), branch, name))
    if len({(repo.url, repo.branch) for repo in repos}) != len(repos):
        raise WorkspaceProfileError("Workspace seed repositories must be unique.")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise WorkspaceProfileError("Workspace seed output must be a new directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".workspace-seed-build-", dir=output.parent))
    try:
        entries: list[dict[str, object]] = []
        with isolated_git(token) as git:
            for index, (repo, directory) in enumerate(
                zip(repos, clone_dir_names(repos), strict=True)
            ):
                checkout = git.scratch / f"repo-{index}.git"
                branch = str(repo.branch)
                git.run(
                    [
                        "clone",
                        "--bare",
                        "--single-branch",
                        "--no-tags",
                        "--branch",
                        branch,
                        "--",
                        repo.url,
                        str(checkout),
                    ]
                )
                commit = (
                    git.run(["rev-parse", f"refs/heads/{branch}"], cwd=checkout).decode().strip()
                )
                reject_unsupported_content(git, checkout, commit)
                bundle = staging / f"repo-{index}.bundle"
                git.run(["bundle", "create", str(bundle), f"refs/heads/{branch}"], cwd=checkout)
                entries.append(
                    {
                        "url": repo.url,
                        "branch": branch,
                        "directory": directory,
                        "commit": commit,
                        "bundle": bundle.name,
                        "bundle_sha256": file_sha256(bundle),
                    }
                )
        manifest = parse_manifest({"version": 1, "repos": entries})
        (staging / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        )
        os.rename(staging, output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", action="append", required=True, help="GitHub clone URL#explicit-branch"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        repositories: list[tuple[str, str]] = []
        for value in args.repo:
            url, separator, branch = value.partition("#")
            if not separator:
                raise WorkspaceProfileError("Each --repo requires an explicit #branch.")
            repositories.append((url, branch))
        manifest = build_seed(repositories, args.output, os.environ.get(BUILDER_TOKEN_ENV, ""))
        print(manifest_sha(manifest))
        return 0
    except WorkspaceProfileError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError):
        print("Workspace seed build failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
