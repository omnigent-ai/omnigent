"""Pinned Agency resources and explicit, independent repository maintenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

import yaml

UPSTREAM = "https://github.com/msitarzewski/agency-agents.git"
VERSION = 1
# User-facing roles remain stable when an upstream title differs.
ROLES = {
    "frontend-developer": ("Frontend Developer", "Frontend Developer", "frontend-developer"),
    "backend-developer": ("Backend Developer", "Backend Architect", "backend-architect"),
    "software-architect": ("Software Architect", "Software Architect", "software-architect"),
    "mobile-app-developer": ("Mobile App Developer", "Mobile App Builder", "mobile-app-builder"),
    "technical-writer": ("Technical Writer", "Technical Writer", "technical-writer"),
    "code-reviewer": ("Code Reviewer", "Code Reviewer", "code-reviewer"),
    "database-optimizer": ("Database Optimizer", "Database Optimizer", "database-optimizer"),
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise ValueError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def safe_path(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_./-]+", value):
        raise ValueError(f"Unsafe path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in (".", "..", "") for p in value.split("/")):
        raise ValueError(f"Unsafe path: {value!r}")
    return value


def plain(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError("Metadata must be a nonempty string of at most 1000 characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Control characters in metadata")
    return value


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys rather than silently taking the last value."""


def unique_mapping(loader: UniqueLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("Invalid or duplicate metadata key")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def metadata(data: bytes) -> dict:
    text = data.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("Missing YAML frontmatter")
    header, body = text[4:].split("\n---\n", 1)
    meta = yaml.load(header, Loader=UniqueLoader)
    if not isinstance(meta, dict) or not {"name", "description"} <= meta.keys():
        raise ValueError("Metadata requires name and description")
    if set(meta) - {"name", "description", "color", "emoji", "vibe"}:
        raise ValueError("Unknown metadata keys; review converter before importing")
    for value in meta.values():
        plain(value)
    if not body.strip():
        raise ValueError("Empty persona body")
    return meta


def source(repo: Path, sha: str, path: str) -> bytes:
    safe_path(path)
    entry = git(repo, "ls-tree", sha, "--", path)
    if not entry.startswith("100644 blob ") or entry.split("\t", 1)[-1] != path:
        raise ValueError(f"Missing, renamed, deleted or non-regular source: {path} at {sha}")
    return subprocess.check_output(["git", "-C", str(repo), "show", f"{sha}:{path}"])


def require_sha(sha: str) -> None:
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Use a full lowercase 40-character commit SHA")


def pin(repo: Path, sha: str) -> dict:
    require_sha(sha)
    if git(repo, "rev-parse", f"{sha}^{{commit}}") != sha:
        raise ValueError("Pin must identify a commit")
    entries = []
    for ident, (alias, name, slug) in sorted(ROLES.items()):
        path = f"engineering/engineering-{slug}.md"
        data = source(repo, sha, path)
        meta = metadata(data)
        if meta["name"] != name:
            raise ValueError(f"Upstream persona renamed: {path}: expected {name!r}")
        entries.append(
            {
                "id": ident,
                "alias": alias,
                "name": name,
                "path": path,
                "description": meta["description"],
                "sha256": digest(data),
            }
        )
    return {
        "schema_version": VERSION,
        "converter_version": VERSION,
        "upstream_url": UPSTREAM,
        "upstream_sha": sha,
        "personas": entries,
        "license_sha256": digest(source(repo, sha, "LICENSE")),
    }


def validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "converter_version",
        "upstream_url",
        "upstream_sha",
        "personas",
        "license_sha256",
    }:
        raise ValueError("Invalid manifest fields")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != VERSION
        or type(manifest["converter_version"]) is not int
        or manifest["converter_version"] != VERSION
        or manifest["upstream_url"] != UPSTREAM
    ):
        raise ValueError("Unsupported manifest version or upstream URL")
    require_sha(manifest["upstream_sha"])
    if not isinstance(manifest["personas"], list):
        raise ValueError("personas must be a list")
    seen = set()
    hashes = [manifest["license_sha256"]]
    for entry in manifest["personas"]:
        if not isinstance(entry, dict) or set(entry) != {
            "id",
            "alias",
            "name",
            "path",
            "description",
            "sha256",
        }:
            raise ValueError("Invalid persona fields")
        ident = plain(entry["id"])
        if ident in seen or ident not in ROLES:
            raise ValueError(f"Duplicate or unknown persona ID: {ident}")
        seen.add(ident)
        alias, name, slug = ROLES[ident]
        if (entry["alias"], entry["name"], safe_path(entry["path"])) != (
            alias,
            name,
            f"engineering/engineering-{slug}.md",
        ):
            raise ValueError(f"Unexpected mapping for {ident}")
        plain(entry["description"])
        hashes.append(entry["sha256"])
    if seen != ROLES.keys():
        raise ValueError("Manifest must contain exactly the seven selected personas")
    for value in hashes:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid SHA256")


def catalog(manifest: dict) -> bytes:
    return encoded(
        [
            {
                **{key: entry[key] for key in ("id", "alias", "name", "description")},
                "resource": f"personas/{entry['id']}.md",
            }
            for entry in sorted(manifest["personas"], key=lambda p: p["id"])
        ]
    )


def build(repo: Path, manifest: dict, output: Path) -> None:
    validate_manifest(manifest)
    files = {
        "manifest.json": encoded(manifest),
        "catalog.json": catalog(manifest),
        "LICENSE": source(repo, manifest["upstream_sha"], "LICENSE"),
    }
    if digest(files["LICENSE"]) != manifest["license_sha256"]:
        raise ValueError("License hash mismatch")
    for entry in manifest["personas"]:
        data = source(repo, manifest["upstream_sha"], entry["path"])
        meta = metadata(data)
        if (
            digest(data) != entry["sha256"]
            or meta["name"] != entry["name"]
            or meta["description"] != entry["description"]
        ):
            raise ValueError(f"Source hash/metadata mismatch: {entry['path']}")
        files[f"personas/{entry['id']}.md"] = data
    files["integrity.json"] = encoded({name: digest(data) for name, data in files.items()})
    # New directories only: a build never overwrites an active bundle or user files.
    output.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def validate(bundle: Path) -> dict:
    if bundle.is_symlink():
        raise ValueError("Bundle cannot be a symlink")
    paths = list(bundle.rglob("*"))
    if any(p.is_symlink() for p in paths):
        raise ValueError("Bundle cannot contain symlinks")
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    validate_manifest(manifest)
    expected = {"manifest.json", "catalog.json", "LICENSE", "integrity.json"} | {
        f"personas/{entry['id']}.md" for entry in manifest["personas"]
    }
    if {p.relative_to(bundle).as_posix() for p in paths if p.is_file()} != expected:
        raise ValueError("Unexpected or missing bundle files")
    integrity = json.loads((bundle / "integrity.json").read_bytes())
    actual = {name: digest((bundle / name).read_bytes()) for name in expected - {"integrity.json"}}
    if integrity != actual:
        raise ValueError("Artifact integrity mismatch")
    if (bundle / "catalog.json").read_bytes() != catalog(manifest):
        raise ValueError("Catalog mismatch")
    if digest((bundle / "LICENSE").read_bytes()) != manifest["license_sha256"]:
        raise ValueError("License hash mismatch")
    for entry in manifest["personas"]:
        data = (bundle / f"personas/{entry['id']}.md").read_bytes()
        meta = metadata(data)
        if (
            digest(data) != entry["sha256"]
            or meta["name"] != entry["name"]
            or meta["description"] != entry["description"]
        ):
            raise ValueError(f"Persona mismatch: {entry['id']}")
    return manifest


def promote(candidate: Path, active: Path, backup: Path) -> None:
    validate(candidate)
    validate(active)
    locations = [p.resolve() for p in (candidate, active, backup)]
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(locations)
        for b in locations[i + 1 :]
    ):
        raise ValueError("Promotion directories must be separate")
    if backup.exists() or backup.is_symlink():
        raise ValueError("Backup already exists")
    # Keep the previous version intact; restore it if the second rename fails.
    active.rename(backup)
    try:
        candidate.rename(active)
    except OSError:
        backup.rename(active)
        raise


def repo_update(repo: Path, update: bool = False) -> dict:
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    status = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    head = git(repo, "rev-parse", "HEAD")
    remote = git(repo, "config", "--get", f"branch.{branch}.remote")
    upstream = git(repo, "rev-parse", "--symbolic-full-name", "@{upstream}")
    if remote == "." or remote.startswith("-") or not upstream.startswith("refs/remotes/"):
        raise ValueError("A remote tracking branch is required")
    if update and status:
        raise ValueError(f"Refusing update: tracked/untracked changes:\n{status}")
    git(repo, "fetch", "--no-tags", "--", remote)
    candidate = git(repo, "rev-parse", "@{upstream}")
    ahead, behind = map(
        int, git(repo, "rev-list", "--left-right", "--count", f"{head}...{candidate}").split()
    )
    result = {
        "repo": str(repo),
        "branch": branch,
        "status": status,
        "head": head,
        "candidate": candidate,
        "ahead": ahead,
        "behind": behind,
        "updated": False,
    }
    if update:
        if ahead:
            raise ValueError("Refusing update: branch is ahead or diverged")
        if (
            git(repo, "status", "--porcelain=v1", "--untracked-files=all")
            or git(repo, "rev-parse", "HEAD") != head
            or git(repo, "symbolic-ref", "--quiet", "--short", "HEAD") != branch
        ):
            raise ValueError("Repository changed during fetch; inspect and retry")
        git(
            repo,
            "-c",
            "merge.autostash=false",
            "merge",
            "--ff-only",
            "--no-autostash",
            "--no-overwrite-ignore",
            candidate,
        )
        result["updated"] = head != candidate
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("pin")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("build")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("validate")
    p.add_argument("bundle", type=Path)
    p = commands.add_parser("promote")
    for name in ("candidate", "active", "backup"):
        p.add_argument(f"--{name}", type=Path, required=True)
    for name in ("check", "update"):
        p = commands.add_parser(name)
        p.add_argument("repo", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "pin":
            data = encoded(pin(args.source, args.sha))
            with args.output.open("xb") as stream:
                stream.write(data)
        elif args.command == "build":
            build(args.source, json.loads(args.manifest.read_bytes()), args.output)
        elif args.command == "validate":
            print(json.dumps(validate(args.bundle), indent=2))
        elif args.command == "promote":
            promote(args.candidate, args.active, args.backup)
        else:
            print(json.dumps(repo_update(args.repo, args.command == "update"), indent=2))
    except (ValueError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
