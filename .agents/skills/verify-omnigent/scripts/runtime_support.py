#!/usr/bin/env python3
"""Strict environments, repository guards, and managed process trees."""

from __future__ import annotations

import argparse
import atexit
import configparser
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

SCHEMA_VERSION = 1
LOCKFILES = (
    "uv.lock",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
)
PROTECTED_DEPENDENCY_PATHS = (
    "node_modules/.modules.yaml",
    "web/node_modules/.bin/prettier",
    "web/node_modules/.bin/oxlint",
    "web/node_modules/.bin/tsc",
    "web/node_modules/.bin/vite",
    "web/node_modules/.bin/vitest",
    "web/electron/node_modules/.bin/electron-builder",
    ".venv/bin/python",
    ".venv/bin/omnigent",
    ".venv/pyvenv.cfg",
)
PROTECTED_DEPENDENCY_ROOTS = (
    ".venv",
    "node_modules",
    "web/node_modules",
    "web/electron/node_modules",
)
PROTECTED_BUILD_PATHS = (
    "web/dist",
    "web/electron/dist",
)
INTERNAL_ENV_KEYS = {
    "OMNIGENT_VERIFY_PARTIAL_COMPARISON",
    "OMNIGENT_VERIFY_ONLY_STEP_INDICES",
    "OMNIGENT_VERIFY_CHANGED_FILES_JSON",
    "OMNIGENT_VERIFY_DISABLE_BASELINE",
    "OMNIGENT_VERIFY_INTERNAL_REQUEST",
    "OMNIGENT_VERIFY_CONTROL_SNAPSHOT",
    "OMNIGENT_VERIFY_DEEP_DEPENDENCY_SNAPSHOT",
}
_BASE_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
_EXTRA_ENV_KEYS = {
    "CSC_IDENTITY_AUTO_DISCOVERY",
    "OMNIGENT_VERIFY_ADAPTER",
    "OMNIGENT_VERIFY_BASE_REF",
    "OMNIGENT_VERIFY_CLEANUP_MARKER",
    "OMNIGENT_VERIFY_DATABRICKS_PROFILE",
    "OMNIGENT_VERIFY_EVIDENCE_DIR",
    "OMNIGENT_VERIFY_HARNESS",
    "OMNIGENT_VERIFY_OSS_REF",
    "OMNIGENT_VERIFY_REPO_ROOT",
    "OMNIGENT_VERIFY_RUN_DIR",
    "OMNIGENT_WEB_UI_DIST",
    "OMNIGENT_VERIFY_SKILL_ROOT",
    "OMNIGENT_VERIFY_STEP_TIMEOUT_SECONDS",
    "PORT",
    "PYTHONPATH",
    "PYTHONSAFEPATH",
    "UNIVERSE_ROOT",
}
MANAGED_FD_ENV = "OMNIGENT_VERIFY_MANAGED_FD"
CREDENTIAL_MODE_ENV = "OMNIGENT_VERIFY_CREDENTIAL_MODE"


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    with temporary.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def portable_argv(
    argv: list[str] | tuple[str, ...],
    *,
    repo_root: Path,
    run_dir: Path,
    skill_root: Path | None = None,
    home: Path | None = None,
) -> list[str]:
    replacements = [
        (os.fspath(run_dir.resolve()), "<run-dir>"),
        (os.fspath(repo_root.resolve()), "<repo-root>"),
    ]
    if skill_root is not None:
        replacements.extend(
            {
                (os.fspath(skill_root.absolute()), "<skill-root>"),
                (os.fspath(skill_root.resolve()), "<skill-root>"),
            }
        )
    raw_home = os.environ.get("HOME")
    effective_home = home or (Path(raw_home).expanduser() if raw_home else None)
    if effective_home is not None:
        replacements.append((os.fspath(effective_home.resolve()), "<home>"))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    portable = []
    for value in argv:
        for root, token in replacements:
            value = value.replace(root, token)
        portable.append(value)
    return portable


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ("git", *args),
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def _git_optional_bytes(repo_root: Path, *args: str) -> bytes | None:
    result = subprocess.run(
        ("git", *args),
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _path_identity(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing", "mode": None, "sha256": None}
    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        digest = hashlib.sha256(os.fsencode(target))
        try:
            resolved = path.resolve(strict=True)
            if resolved.is_file():
                digest.update(b"\0")
                with resolved.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
        except OSError as exc:
            raise RuntimeError(
                f"dependency symlink {path} could not be resolved ({type(exc).__name__})"
            ) from exc
        kind = "symlink"
        sha256 = digest.hexdigest()
    elif path.is_file():
        kind = "file"
        sha256 = _file_sha256(path)
    elif path.is_dir():
        kind = "directory"
        sha256 = hashlib.sha256(b"").hexdigest()
    else:
        kind = "other"
        sha256 = hashlib.sha256(b"").hexdigest()
    return {
        "kind": kind,
        "mode": mode,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "sha256": sha256,
    }


def _tree_identity(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"kind": "missing", "entries": {}}
    if not path.is_dir():
        return {"kind": "not-directory", "entries": {".": _path_identity(path)}}
    entries: dict[str, object] = {}
    for child in sorted(path.rglob("*")):
        entries[child.relative_to(path).as_posix()] = _path_identity(child)
    return {"kind": "directory", "entries": entries}


def _tree_metadata_identity(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"kind": "missing", "sha256": None, "entries": 0}
    digest = hashlib.sha256()
    count = 0
    pending = [path]
    while pending:
        current = pending.pop()
        metadata = current.lstat()
        relative = current.relative_to(path)
        kind = "link" if current.is_symlink() else "dir" if current.is_dir() else "file"
        target = os.readlink(current) if current.is_symlink() else ""
        record = (
            os.fsencode(relative)
            + b"\0"
            + kind.encode()
            + b"\0"
            + str(metadata.st_mode).encode()
            + b"\0"
            + str(metadata.st_size).encode()
            + b"\0"
            + str(metadata.st_mtime_ns).encode()
            + b"\0"
            + str(metadata.st_ctime_ns).encode()
            + b"\0"
            + os.fsencode(target)
            + b"\0"
        )
        digest.update(record)
        count += 1
        if kind == "dir":
            pending.extend(sorted(current.iterdir(), reverse=True))
    return {"kind": "tree", "sha256": digest.hexdigest(), "entries": count}


def _browser_executables(root: Path) -> tuple[Path, ...]:
    patterns = (
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
        "chromium-*/chrome-win*/chrome.exe",
        "chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
    )
    return tuple(
        sorted(
            path
            for pattern in patterns
            for path in root.glob(pattern)
            if path.is_file() and os.access(path, os.X_OK)
        )
    )


def verified_playwright_browsers_path(
    source: dict[str, str] | None = None,
) -> Path | None:
    """Resolve an existing browser cache without exposing the caller's HOME."""
    effective_source = dict(os.environ) if source is None else source
    candidates: list[Path] = []
    configured = effective_source.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        candidates.append(Path(configured).expanduser())
    home = effective_source.get("HOME")
    if home:
        home_path = Path(home)
        candidates.extend(
            (
                home_path / "Library" / "Caches" / "ms-playwright",
                home_path / ".cache" / "ms-playwright",
            )
        )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and _browser_executables(resolved):
            return resolved
    return None


def _pnpm_cache_destination() -> Path | None:
    override = os.environ.get("OMNIGENT_VERIFY_PNPM_CACHE_ROOT")
    if override:
        return Path(override)
    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return None
    return account_home / ".cache" / "verify-omnigent" / "pnpm"


@contextlib.contextmanager
def _pnpm_cache_lock(cache_root: Path, *, exclusive: bool) -> Iterator[None]:
    parent = cache_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / ".pnpm.verify-omnigent.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def pinned_pnpm_version(repo_root: Path) -> str:
    package = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
    declared = package.get("packageManager") if isinstance(package, dict) else None
    if (
        not isinstance(declared, str)
        or not declared.startswith("pnpm@")
        or not declared.removeprefix("pnpm@")
        or any(character.isspace() for character in declared)
    ):
        raise RuntimeError("package.json must declare an exact packageManager pnpm version")
    version = declared.removeprefix("pnpm@")
    if any(character in version for character in "*^~<>=|"):
        raise RuntimeError("package.json packageManager pnpm version must be exact")
    return version


def _tree_content_identity(root: Path) -> str:
    digest = hashlib.sha256()
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        digest.update(relative.encode("utf-8"))
        digest.update(f"\0{metadata.st_mode:o}\0{metadata.st_size}\0".encode())
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def _validated_pnpm_cache(cache_root: Path) -> tuple[Path, dict[str, object]] | None:
    try:
        resolved = cache_root.resolve(strict=True)
        root_stat = resolved.stat()
        seal_path = resolved / "omnigent-prepared-pnpm.json"
        seal_stat = seal_path.stat()
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        if (
            root_stat.st_uid != os.getuid()
            or seal_stat.st_uid != os.getuid()
            or root_stat.st_mode & 0o022
            or seal_stat.st_mode & 0o022
            or not isinstance(seal, dict)
            or seal.get("schema_version") != 1
            or seal.get("package_manager") != "pnpm"
            or not isinstance(seal.get("version"), str)
        ):
            return None
        version = seal["version"]
        runtime = resolved / version
        executable = runtime / "bin" / "pnpm"
        if (
            not runtime.is_dir()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
            or seal.get("tree") != _tree_metadata_identity(runtime)
            or seal.get("content_sha256") != _tree_content_identity(runtime)
        ):
            return None
        return resolved, seal
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def verified_pnpm_tools_root() -> Path | None:
    destination = _pnpm_cache_destination()
    if destination is None:
        return None
    with _pnpm_cache_lock(destination, exclusive=False):
        validated = _validated_pnpm_cache(destination)
        return validated[0] if validated is not None else None


def _local_pnpm_candidates(version: str) -> tuple[Path, ...]:
    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return ()
    candidates = (
        account_home / ".cache" / "node" / "corepack" / "v1" / "pnpm" / version,
        account_home / "Library" / "Caches" / "node" / "corepack" / "v1" / "pnpm" / version,
        account_home / "Library" / "pnpm" / ".tools" / "pnpm" / version / "node_modules" / "pnpm",
        account_home
        / ".local"
        / "share"
        / "pnpm"
        / ".tools"
        / "pnpm"
        / version
        / "node_modules"
        / "pnpm",
    )
    return tuple(candidates)


def _valid_local_pnpm_package(candidate: Path, version: str) -> bool:
    try:
        resolved = candidate.resolve(strict=True)
        metadata = json.loads((resolved / "package.json").read_text(encoding="utf-8"))
        executable = resolved / "bin" / "pnpm.mjs"
        stat = resolved.stat()
        return (
            resolved.is_dir()
            and stat.st_uid == os.getuid()
            and not stat.st_mode & 0o022
            and isinstance(metadata, dict)
            and metadata.get("name") == "pnpm"
            and metadata.get("version") == version
            and executable.is_file()
        )
    except (OSError, json.JSONDecodeError):
        return False


def prepare_pnpm_cache(
    repo_root: Path,
    staging: Path,
    *,
    candidates: tuple[Path, ...] | None = None,
    allow_download: bool = False,
) -> dict[str, object]:
    version = pinned_pnpm_version(repo_root)
    available = candidates if candidates is not None else _local_pnpm_candidates(version)
    package = next(
        (
            candidate.resolve()
            for candidate in available
            if _valid_local_pnpm_package(candidate, version)
        ),
        None,
    )
    if package is None and allow_download:
        subprocess.run(
            ("corepack", "prepare", f"pnpm@{version}"),
            cwd=repo_root,
            check=True,
        )
        package = next(
            (
                candidate.resolve()
                for candidate in _local_pnpm_candidates(version)
                if _valid_local_pnpm_package(candidate, version)
            ),
            None,
        )
    if package is None:
        raise RuntimeError(
            f"pnpm {version} is not in a supported local cache; "
            "Corepack preparation did not produce it"
        )
    runtime = staging / version
    copied_package = runtime / "package"
    runtime.mkdir(parents=True)
    shutil.copytree(package, copied_package, symlinks=False)
    binary = runtime / "bin" / "pnpm"
    binary.parent.mkdir()
    binary.write_text(
        '#!/bin/sh\nexec node "$(dirname "$0")/../package/bin/pnpm.mjs" "$@"\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    for path in (runtime, *runtime.rglob("*")):
        with contextlib.suppress(OSError):
            path.chmod(path.stat().st_mode & ~0o022)
    actual = subprocess.run(
        (os.fspath(binary), "--version"),
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual != version:
        raise RuntimeError(f"prepared pnpm reported {actual!r}, expected {version!r}")
    seal: dict[str, object] = {
        "schema_version": 1,
        "package_manager": "pnpm",
        "version": version,
        "tree": _tree_metadata_identity(runtime),
        "content_sha256": _tree_content_identity(runtime),
    }
    atomic_json(staging / "omnigent-prepared-pnpm.json", seal)
    return seal


def publish_pnpm_cache(staging: Path, destination: Path) -> None:
    with _pnpm_cache_lock(destination, exclusive=True):
        try:
            staging = staging.resolve(strict=True)
            parent = destination.parent.resolve(strict=True)
            staging.relative_to(parent)
        except (OSError, ValueError) as exc:
            raise RuntimeError("pnpm staging cache is outside its publication root") from exc
        if _validated_pnpm_cache(staging) is None:
            raise RuntimeError("pnpm staging cache failed authentication")
        previous = destination.resolve(strict=True) if destination.is_symlink() else None
        if destination.exists() and not destination.is_symlink():
            raise RuntimeError("refusing a non-atomic pnpm cache destination")
        pointer = parent / f".pnpm.pointer.{secrets.token_hex(8)}"
        pointer.symlink_to(staging.name, target_is_directory=True)
        published = False
        try:
            os.replace(pointer, destination)
            published = True
            if destination.resolve(strict=True) != staging:
                raise RuntimeError("published pnpm cache pointer does not resolve to staging")
        except Exception:
            pointer.unlink(missing_ok=True)
            if published:
                if previous is None:
                    destination.unlink(missing_ok=True)
                else:
                    rollback = parent / f".pnpm.rollback.{secrets.token_hex(8)}"
                    rollback.symlink_to(previous.name, target_is_directory=True)
                    os.replace(rollback, destination)
            raise
        if (
            previous is not None
            and previous != staging
            and previous.parent == parent
            and previous.name.startswith("pnpm.version.")
        ):
            with contextlib.suppress(OSError):
                shutil.rmtree(previous)


def _stage_pnpm_tools(repo_root: Path, destination: Path) -> Path | None:
    cache_root = _pnpm_cache_destination()
    if cache_root is None:
        return None
    expected = pinned_pnpm_version(repo_root)
    with _pnpm_cache_lock(cache_root, exclusive=False):
        validated = _validated_pnpm_cache(cache_root)
        if validated is None or validated[1].get("version") != expected:
            return None
        source = validated[0] / expected
        target = destination / expected
        if target.exists():
            if _tree_content_identity(target) != validated[1].get("content_sha256"):
                raise RuntimeError("existing staged pnpm runtime failed tree authentication")
            return destination
        shutil.copytree(source, target, symlinks=False)
        if _tree_content_identity(target) != validated[1].get("content_sha256"):
            shutil.rmtree(destination, ignore_errors=True)
            raise RuntimeError("staged pnpm runtime failed tree authentication")
    return destination


def playwright_browser_snapshot(
    source: dict[str, str] | None = None,
) -> dict[str, object] | None:
    root = verified_playwright_browsers_path(source)
    if root is None:
        return None
    return {
        "root": os.fspath(root),
        "installations": sorted(path.name for path in root.iterdir()),
        "markers": {
            marker.relative_to(root).as_posix(): _path_identity(marker)
            for installation in sorted(root.iterdir())
            if installation.is_dir()
            for name in ("INSTALLATION_COMPLETE", "DEPENDENCIES_VALIDATED")
            if (marker := installation / name).is_file()
        },
        "executables": {
            path.relative_to(root).as_posix(): _path_identity(path)
            for path in _browser_executables(root)
        },
    }


def _configured_remote_hooks(repo_root: Path) -> list[tuple[str, str]]:
    config_path = repo_root / ".pre-commit-config.yaml"
    if not config_path.is_file():
        raise RuntimeError("Repository has no .pre-commit-config.yaml.")
    config = config_path.read_text(encoding="utf-8")
    hooks: list[tuple[str, str]] = []
    current_repo: str | None = None
    for line in config.splitlines():
        repo_match = re.match(r"^\s*-\s+repo:\s*([^\s#]+)", line)
        if repo_match:
            current_repo = repo_match.group(1)
            continue
        rev_match = re.match(r"^\s+rev:\s*([^\s#]+)", line)
        if rev_match and current_repo is not None and current_repo not in {"local", "meta"}:
            hooks.append((current_repo, rev_match.group(1)))
            current_repo = None
    return hooks


def _pre_commit_cache_roots(source: dict[str, str]) -> list[Path]:
    roots = []
    if source.get("PRE_COMMIT_HOME"):
        roots.append(Path(source["PRE_COMMIT_HOME"]))
    if source.get("HOME"):
        home = Path(source["HOME"])
        roots.extend(
            (
                home / ".cache/verify-omnigent/pre-commit",
                home / ".cache/pre-commit",
                home / "Library/Caches/pre-commit",
            )
        )
    return list(dict.fromkeys(roots))


def seal_pre_commit_cache(repo_root: Path, cache_root: Path) -> dict[str, object]:
    configured = _configured_remote_hooks(repo_root)
    entries = []
    database = cache_root / "db.db"
    if configured and not database.is_file():
        raise RuntimeError("Prepared pre-commit cache has no db.db.")
    with sqlite3.connect(database) as connection:
        for repo, rev in configured:
            row = connection.execute(
                "SELECT path FROM repos WHERE repo = ? AND ref = ?",
                (repo, rev),
            ).fetchone()
            if not row or not isinstance(row[0], str):
                raise RuntimeError(f"Prepared cache lacks {repo}@{rev}.")
            path = Path(row[0]).resolve(strict=True)
            path.relative_to(cache_root.resolve(strict=True))
            entries.append(
                {
                    "repo": repo,
                    "rev": rev,
                    "path": str(path),
                    "tree": _tree_metadata_identity(path),
                }
            )
    seal = {
        "schema_version": 1,
        "config_sha256": hashlib.sha256(
            (repo_root / ".pre-commit-config.yaml").read_bytes()
        ).hexdigest(),
        "hooks": entries,
    }
    atomic_json(cache_root / "omnigent-prepared-hooks.json", seal)
    return seal


@contextlib.contextmanager
def _hook_cache_lock(cache_root: Path, *, exclusive: bool) -> Iterator[None]:
    """Lease a published cache, or serialize its publication and collection."""
    parent = cache_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / ".pre-commit.verify-omnigent.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def publish_pre_commit_cache(repo_root: Path, staging: Path, destination: Path) -> None:
    """Atomically publish a fully sealed version directory through a cache pointer."""
    with _hook_cache_lock(destination, exclusive=True):
        staging = staging.resolve(strict=True)
        parent = destination.parent.resolve(strict=True)
        staging.relative_to(parent)
        seal_path = staging / "omnigent-prepared-hooks.json"
        if not seal_path.is_file():
            raise RuntimeError("Staging cache is not sealed.")
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        expected_config = hashlib.sha256(
            (repo_root / ".pre-commit-config.yaml").read_bytes()
        ).hexdigest()
        if (
            not isinstance(seal, dict)
            or seal.get("schema_version") != 1
            or seal.get("config_sha256") != expected_config
        ):
            raise RuntimeError("Staging cache seal does not match the repository config.")
        for repo, rev in _configured_remote_hooks(repo_root):
            with sqlite3.connect(staging / "db.db") as connection:
                row = connection.execute(
                    "SELECT path FROM repos WHERE repo = ? AND ref = ?",
                    (repo, rev),
                ).fetchone()
            if (
                not row
                or not isinstance(row[0], str)
                or not _authenticated_pre_commit_entry(
                    repo_root,
                    staging,
                    repo,
                    rev,
                    Path(row[0]).resolve(strict=True),
                )
            ):
                raise RuntimeError(f"Staging cache failed authentication for {repo}@{rev}.")
        previous_target: Path | None = None
        if destination.is_symlink():
            previous_target = destination.resolve(strict=True)
        elif destination.exists():
            raise RuntimeError("Refusing non-atomic migration of a legacy hook-cache directory.")
        pointer = parent / f".pre-commit.pointer.{secrets.token_hex(8)}"
        pointer.symlink_to(staging.name, target_is_directory=True)
        published = False
        try:
            os.replace(pointer, destination)
            published = True
            if destination.resolve(strict=True) != staging:
                raise RuntimeError("Published cache pointer does not resolve to staging.")
        except Exception:
            pointer.unlink(missing_ok=True)
            if published and previous_target is not None:
                rollback = parent / f".pre-commit.rollback.{secrets.token_hex(8)}"
                rollback.symlink_to(previous_target.name, target_is_directory=True)
                os.replace(rollback, destination)
            raise
        if (
            previous_target is not None
            and previous_target != staging
            and previous_target.parent == parent
            and previous_target.name.startswith("pre-commit.version.")
        ):
            # A stale version is safer than rolling the pointer back or dangling it.
            with contextlib.suppress(OSError):
                shutil.rmtree(previous_target)


def _authenticated_pre_commit_entry(
    repo_root: Path,
    cache_root: Path,
    repo: str,
    rev: str,
    candidate: Path,
) -> bool:
    try:
        seal = json.loads(
            (cache_root / "omnigent-prepared-hooks.json").read_text(encoding="utf-8")
        )
        expected_config = hashlib.sha256(
            (repo_root / ".pre-commit-config.yaml").read_bytes()
        ).hexdigest()
        if (
            not isinstance(seal, dict)
            or seal.get("schema_version") != 1
            or seal.get("config_sha256") != expected_config
        ):
            return False
        entry = next(
            (
                item
                for item in seal.get("hooks", [])
                if isinstance(item, dict)
                and item.get("repo") == repo
                and item.get("rev") == rev
                and item.get("path") == str(candidate)
            ),
            None,
        )
        return isinstance(entry, dict) and entry.get("tree") == _tree_metadata_identity(candidate)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _stage_configured_pre_commit_cache(
    repo_root: Path,
    destination: Path,
    source: dict[str, str],
) -> None:
    configured = _configured_remote_hooks(repo_root)
    if not configured:
        return
    roots = _pre_commit_cache_roots(source)
    with contextlib.ExitStack() as leases:
        for cache_root in roots:
            leases.enter_context(_hook_cache_lock(cache_root, exclusive=False))
        selected: list[tuple[str, str, Path]] = []
        for repo, rev in configured:
            match: Path | None = None
            for cache_root in roots:
                database = cache_root / "db.db"
                if not database.is_file():
                    continue
                resolved_cache = cache_root.resolve(strict=True)
                cache_stat = resolved_cache.stat()
                database_stat = database.stat()
                if (
                    cache_stat.st_uid != os.getuid()
                    or database_stat.st_uid != os.getuid()
                    or cache_stat.st_mode & 0o022
                    or database_stat.st_mode & 0o022
                ):
                    continue
                with sqlite3.connect(database) as connection:
                    row = connection.execute(
                        "SELECT path FROM repos WHERE repo = ? AND ref = ?",
                        (repo, rev),
                    ).fetchone()
                if row and isinstance(row[0], str):
                    candidate = Path(row[0]).resolve(strict=True)
                    candidate.relative_to(resolved_cache)
                    if candidate.is_dir() and _authenticated_pre_commit_entry(
                        repo_root,
                        resolved_cache,
                        repo,
                        rev,
                        candidate,
                    ):
                        match = candidate
                        break
            if match is None:
                raise RuntimeError(
                    f"Configured pre-commit hook {repo}@{rev} is not prepared "
                    "in a trusted user cache."
                )
            selected.append((repo, rev, match))
        destination.mkdir(parents=True, exist_ok=True)
        database = destination / "db.db"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS repos "
                "(repo TEXT NOT NULL, ref TEXT NOT NULL, path TEXT NOT NULL, "
                "PRIMARY KEY (repo, ref))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS configs (path TEXT NOT NULL PRIMARY KEY)"
            )
            for index, (repo, rev, source_path) in enumerate(selected):
                staged = destination / f"repo-{index}"
                shutil.copytree(source_path, staged, symlinks=False)
                source_bytes = os.fsencode(source_path)
                staged_bytes = os.fsencode(staged)
                for path in staged.rglob("*"):
                    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
                        continue
                    payload = path.read_bytes()
                    if b"\0" not in payload and source_bytes in payload:
                        path.write_bytes(payload.replace(source_bytes, staged_bytes))
                connection.execute(
                    "INSERT INTO repos(repo, ref, path) VALUES (?, ?, ?)",
                    (repo, rev, str(staged)),
                )


def repository_snapshot(
    repo_root: Path,
    *,
    deep_dependencies: bool = False,
) -> dict[str, object]:
    repo_root = repo_root.resolve(strict=True)
    status = _git_bytes(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    listed = _git_bytes(
        repo_root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    paths = [os.fsdecode(raw) for raw in listed.split(b"\0") if raw]
    entries: dict[str, object] = {}
    for relative in paths:
        value = Path(relative)
        if value.is_absolute() or ".." in value.parts:
            raise RuntimeError(f"git returned unsafe path {relative!r}")
        path = repo_root / value
        try:
            if path.is_symlink():
                try:
                    path.resolve(strict=True).relative_to(repo_root)
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        f"tracked or visible symlink {relative!r} escapes the checkout"
                    ) from exc
            entries[relative] = _path_identity(path)
        except OSError as exc:
            raise RuntimeError(
                f"repository path {relative!r} could not be hashed ({type(exc).__name__})"
            ) from exc
    lockfiles: dict[str, object] = {}
    for relative in LOCKFILES:
        path = repo_root / relative
        lockfiles[relative] = _path_identity(path) if path.exists() else None
    protected: dict[str, object] = {}
    for relative in PROTECTED_DEPENDENCY_PATHS:
        path = repo_root / relative
        protected[relative] = _path_identity(path) if path.exists() else None
    protected_roots = {
        relative: _path_identity(repo_root / relative) if (repo_root / relative).exists() else None
        for relative in PROTECTED_DEPENDENCY_ROOTS
    }
    protected_trees = (
        {
            relative: _tree_metadata_identity(repo_root / relative)
            for relative in PROTECTED_DEPENDENCY_ROOTS
        }
        if deep_dependencies
        else None
    )
    refs = _git_bytes(
        repo_root,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00",
    )
    head = _git_optional_bytes(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    index_entries = _git_bytes(repo_root, "ls-files", "--stage", "-z")
    index_diff = _git_bytes(repo_root, "diff", "--cached", "--binary", "--full-index", "--")
    index_identity = {
        "entries_sha256": hashlib.sha256(index_entries).hexdigest(),
        "diff_sha256": hashlib.sha256(index_diff).hexdigest(),
    }
    head_path = Path(os.fsdecode(_git_bytes(repo_root, "rev-parse", "--git-path", "HEAD")).strip())
    if not head_path.is_absolute():
        head_path = repo_root / head_path
    head_file_identity = _path_identity(head_path)
    head_file_identity.pop("mtime_ns", None)
    symbolic_head = _git_optional_bytes(repo_root, "symbolic-ref", "-q", "HEAD")
    build_outputs = {
        relative: _tree_identity(repo_root / relative) for relative in PROTECTED_BUILD_PATHS
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "entries": entries,
        "lockfiles": lockfiles,
        "protected_dependencies": protected,
        "protected_dependency_roots": protected_roots,
        "protected_dependency_trees": protected_trees,
        "protected_build_outputs": build_outputs,
        "git_identity": {
            "refs_sha256": hashlib.sha256(refs).hexdigest(),
            "head_sha256": hashlib.sha256(head).hexdigest() if head is not None else None,
            "head_file": head_file_identity,
            "symbolic_head": (
                os.fsdecode(symbolic_head).strip() if symbolic_head is not None else None
            ),
            "index": index_identity,
        },
    }


def compare_snapshots(before: dict[str, object], after: dict[str, object]) -> list[str]:
    if (
        before.get("schema_version") != SCHEMA_VERSION
        or after.get("schema_version") != SCHEMA_VERSION
    ):
        return ["repository snapshot schema is invalid"]
    changes: list[str] = []
    if before.get("status_sha256") != after.get("status_sha256"):
        changes.append("git status changed")
    before_entries = before.get("entries")
    after_entries = after.get("entries")
    if not isinstance(before_entries, dict) or not isinstance(after_entries, dict):
        return [*changes, "repository file snapshot is unreadable"]
    changed_paths = sorted(
        path
        for path in set(before_entries) | set(after_entries)
        if before_entries.get(path) != after_entries.get(path)
    )
    if changed_paths:
        preview = ", ".join(repr(path) for path in changed_paths[:20])
        suffix = "" if len(changed_paths) <= 20 else f" (+{len(changed_paths) - 20} more)"
        changes.append(f"repository bytes changed: {preview}{suffix}")
    before_locks = before.get("lockfiles")
    after_locks = after.get("lockfiles")
    if not isinstance(before_locks, dict) or not isinstance(after_locks, dict):
        changes.append("lockfile snapshot is unreadable")
    else:
        changed_locks = [
            path for path in LOCKFILES if before_locks.get(path) != after_locks.get(path)
        ]
        if changed_locks:
            changes.append("lockfiles changed: " + ", ".join(changed_locks))
    before_dependencies = before.get("protected_dependencies")
    after_dependencies = after.get("protected_dependencies")
    if not isinstance(before_dependencies, dict) or not isinstance(after_dependencies, dict):
        changes.append("dependency metadata snapshot is unreadable")
    else:
        changed_dependencies = [
            path
            for path in PROTECTED_DEPENDENCY_PATHS
            if before_dependencies.get(path) != after_dependencies.get(path)
        ]
        if changed_dependencies:
            changes.append("dependency metadata changed: " + ", ".join(changed_dependencies))
    before_roots = before.get("protected_dependency_roots")
    after_roots = after.get("protected_dependency_roots")
    if not isinstance(before_roots, dict) or not isinstance(after_roots, dict):
        changes.append("dependency root snapshot is unreadable")
    else:
        changed_roots = [
            path
            for path in PROTECTED_DEPENDENCY_ROOTS
            if before_roots.get(path) != after_roots.get(path)
        ]
        if changed_roots:
            changes.append("dependency roots changed: " + ", ".join(changed_roots))
    before_trees = before.get("protected_dependency_trees")
    after_trees = after.get("protected_dependency_trees")
    if before_trees is None and after_trees is None:
        pass
    elif not isinstance(before_trees, dict) or not isinstance(after_trees, dict):
        changes.append("dependency tree snapshot is unreadable")
    else:
        changed_trees = [
            path
            for path in PROTECTED_DEPENDENCY_ROOTS
            if before_trees.get(path) != after_trees.get(path)
        ]
        if changed_trees:
            changes.append("dependency trees changed: " + ", ".join(changed_trees))
    before_builds = before.get("protected_build_outputs")
    after_builds = after.get("protected_build_outputs")
    if not isinstance(before_builds, dict) or not isinstance(after_builds, dict):
        changes.append("build output snapshot is unreadable")
    else:
        changed_builds = [
            path
            for path in PROTECTED_BUILD_PATHS
            if before_builds.get(path) != after_builds.get(path)
        ]
        if changed_builds:
            changes.append("build outputs changed: " + ", ".join(changed_builds))
    if before.get("git_identity") != after.get("git_identity"):
        changes.append("Git refs, HEAD, or index changed")
    return changes


def strict_environment(
    run_dir: Path,
    *,
    source: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
    credentialed: bool = False,
    pre_commit: bool = False,
) -> dict[str, str]:
    _ = pre_commit
    effective_source = dict(os.environ) if source is None else source
    group_root, environment_root = _strict_environment_paths(run_dir)
    home = environment_root / "home"
    reusing_environment = (
        environment_root.is_dir()
        and effective_source.get("HOME") is not None
        and Path(effective_source["HOME"]).resolve() == home.resolve()
    )
    if environment_root.exists() and not reusing_environment:
        shutil.rmtree(environment_root)
    if environment_root.exists() and not reusing_environment:
        raise RuntimeError(f"Sensitive runtime state survived cleanup: {environment_root}")
    group_root.mkdir(parents=True, exist_ok=True)
    atexit.register(cleanup_strict_environment, run_dir)
    cache = environment_root / "cache"
    config = environment_root / "config"
    data = environment_root / "data"
    temp = environment_root / "tmp"
    for path in (home, cache, config, data, temp):
        path.mkdir(parents=True, exist_ok=True)
    env = {key: effective_source[key] for key in _BASE_ENV_KEYS if key in effective_source}
    if effective_source.get(MANAGED_FD_ENV):
        env[MANAGED_FD_ENV] = effective_source[MANAGED_FD_ENV]
    browser_root = verified_playwright_browsers_path(effective_source)
    repo_root = Path(
        (extra or {}).get(
            "OMNIGENT_VERIFY_REPO_ROOT",
            effective_source.get("OMNIGENT_VERIFY_REPO_ROOT", str(Path.cwd())),
        )
    )
    pnpm_tools_root = (
        _stage_pnpm_tools(repo_root, cache / "pnpm-tools")
        if (repo_root / "package.json").is_file()
        else None
    )
    pre_commit_home = cache / "pre-commit"
    pre_commit_home.mkdir(parents=True, exist_ok=True)
    if pre_commit and not (pre_commit_home / "db.db").is_file():
        repo_root = Path(
            (extra or {}).get(
                "OMNIGENT_VERIFY_REPO_ROOT",
                effective_source.get("OMNIGENT_VERIFY_REPO_ROOT", str(Path.cwd())),
            )
        )
        _stage_configured_pre_commit_cache(repo_root, pre_commit_home, effective_source)
    env.update(
        {
            "CI": "true",
            "HOME": str(home),
            "XDG_CACHE_HOME": str(cache),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(data),
            "TMPDIR": str(temp),
            "PRE_COMMIT_HOME": str(pre_commit_home),
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "NO_COLOR": "1",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_OFFLINE": "true",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PNPM_CONFIG_CONFIRM_MODULES_PURGE": "false",
            "PNPM_CONFIG_FROZEN_LOCKFILE": "true",
            "PNPM_CONFIG_OFFLINE": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_FROZEN": "1",
            "UV_NO_SYNC": "1",
            "UV_OFFLINE": "1",
        }
    )
    if browser_root is not None:
        env["PLAYWRIGHT_BROWSERS_PATH"] = os.fspath(browser_root)
    if pnpm_tools_root is not None:
        env["OMNIGENT_VERIFY_TRUSTED_PNPM_TOOLS_ROOT"] = os.fspath(pnpm_tools_root)
        version = pinned_pnpm_version(repo_root)
        env["PATH"] = os.pathsep.join(
            (
                os.fspath(pnpm_tools_root / version / "bin"),
                env.get("PATH", ""),
            )
        ).rstrip(os.pathsep)
    if credentialed:
        selected_profile = (extra or {}).get(
            "OMNIGENT_VERIFY_DATABRICKS_PROFILE"
        ) or effective_source.get("OMNIGENT_VERIFY_DATABRICKS_PROFILE")
        if selected_profile:
            source_config = Path(
                effective_source.get(
                    "DATABRICKS_CONFIG_FILE",
                    str(Path(effective_source.get("HOME", "")) / ".databrickscfg"),
                )
            )
            parser = configparser.RawConfigParser()
            if not source_config.is_file() or not parser.read(source_config):
                raise RuntimeError(
                    f"Selected Databricks profile {selected_profile!r} has no readable config."
                )
            if not parser.has_section(selected_profile):
                raise RuntimeError(
                    f"Selected Databricks profile {selected_profile!r} does not exist."
                )
            staged = configparser.RawConfigParser()
            staged.add_section(selected_profile)
            for key, value in parser.items(selected_profile, raw=True):
                staged.set(selected_profile, key, value)
            staged_path = home / ".databrickscfg"
            if source_config.resolve() != staged_path.resolve():
                with staged_path.open("w", encoding="utf-8") as handle:
                    staged.write(handle)
                staged_path.chmod(0o600)
            env["DATABRICKS_CONFIG_FILE"] = str(staged_path)
            env["DATABRICKS_CONFIG_PROFILE"] = selected_profile
            env[CREDENTIAL_MODE_ENV] = "databricks-profile"
        else:
            harness = (extra or {}).get("OMNIGENT_VERIFY_HARNESS") or effective_source.get(
                "OMNIGENT_VERIFY_HARNESS"
            )
            if effective_source.get("OPENAI_API_KEY") and effective_source.get("OPENAI_BASE_URL"):
                env.update(
                    {
                        "OPENAI_API_KEY": effective_source["OPENAI_API_KEY"],
                        "OPENAI_BASE_URL": effective_source["OPENAI_BASE_URL"],
                        CREDENTIAL_MODE_ENV: "openai-environment",
                    }
                )
            elif effective_source.get("DATABRICKS_HOST") and effective_source.get(
                "DATABRICKS_TOKEN"
            ):
                env.update(
                    {
                        "DATABRICKS_HOST": effective_source["DATABRICKS_HOST"],
                        "DATABRICKS_TOKEN": effective_source["DATABRICKS_TOKEN"],
                        CREDENTIAL_MODE_ENV: "databricks-environment",
                    }
                )
            elif (
                harness and harness.startswith("cursor") and effective_source.get("CURSOR_API_KEY")
            ):
                env.update(
                    {
                        "CURSOR_API_KEY": effective_source["CURSOR_API_KEY"],
                        CREDENTIAL_MODE_ENV: "cursor-api-key",
                    }
                )
    for key, value in (extra or {}).items():
        if key not in _EXTRA_ENV_KEYS:
            raise RuntimeError(f"environment override {key!r} is not allowlisted")
        env[key] = value
    return env


def _strict_environment_paths(run_dir: Path) -> tuple[Path, Path]:
    resolved = run_dir.resolve()
    manifest_ancestors = [
        ancestor
        for ancestor in (resolved, *resolved.parents)
        if (ancestor / "manifest.json").is_file()
    ]
    verification_root = manifest_ancestors[-1] if manifest_ancestors else resolved
    group_key = hashlib.sha256(os.fsencode(verification_root)).hexdigest()[:24]
    run_key = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:24]
    group_root = Path(tempfile.gettempdir()) / f"omnigent-verify-runtime.{group_key}"
    return group_root, group_root / run_key


def cleanup_strict_environment(run_dir: Path, *, all_runs: bool = False) -> None:
    group_root, environment_root = _strict_environment_paths(run_dir)
    target = group_root if all_runs else environment_root
    if target.exists():
        shutil.rmtree(target)
    if target.exists():
        raise RuntimeError(f"Sensitive runtime state survived cleanup: {target}")
    with contextlib.suppress(OSError):
        group_root.rmdir()


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_group(pgid: int, requested: int = signal.SIGTERM) -> None:
    for signum, grace in ((requested, 1.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        if not _group_exists(pgid):
            return
        try:
            os.killpg(pgid, signum)
        except PermissionError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pgid, signum)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while _group_exists(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)


def _process_table() -> dict[int, tuple[int, int]]:
    result = subprocess.run(
        ("ps", "-axo", "pid=,ppid=,pgid="),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    table: dict[int, tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            pid, ppid, pgid = (int(value) for value in fields)
        except ValueError:
            continue
        table[pid] = (ppid, pgid)
    return table


def _refresh_descendants(root_pid: int, known: set[int]) -> dict[int, tuple[int, int]]:
    table = _process_table()
    parents = {root_pid, *known}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _pgid) in table.items():
            if pid not in known and ppid in parents:
                known.add(pid)
                parents.add(pid)
                changed = True
    return table


def _marker_holders(marker: Path) -> set[int]:
    holders: set[int] = set()
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        marker_stat = marker.stat()
        for process_dir in Path("/proc").iterdir():
            if not process_dir.name.isdigit():
                continue
            for descriptor in (process_dir / "fd").glob("*"):
                try:
                    target_stat = descriptor.stat()
                except OSError:
                    continue
                if (
                    target_stat.st_dev == marker_stat.st_dev
                    and target_stat.st_ino == marker_stat.st_ino
                ):
                    holders.add(int(process_dir.name))
                    break
    elif sys.platform == "darwin":
        result = subprocess.run(
            ("/usr/sbin/lsof", "-F", "p", "--", str(marker)),
            check=False,
            capture_output=True,
            text=True,
        )
        holders.update(
            int(line[1:])
            for line in result.stdout.splitlines()
            if line.startswith("p") and line[1:].isdigit()
        )
    holders.discard(os.getpid())
    return holders


def terminate_process_tree(
    root_pid: int,
    descendants: set[int],
    requested: int = signal.SIGTERM,
    marker: Path | None = None,
) -> None:
    own_pgid = os.getpgrp()
    for signum, grace in ((requested, 1.0), (signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
        table = _refresh_descendants(root_pid, descendants)
        if marker is not None:
            descendants.update(_marker_holders(marker))
        alive = {pid for pid in descendants if pid in table}
        groups = {
            pgid
            for pid in {root_pid, *alive}
            if (entry := table.get(pid)) is not None
            and (pgid := entry[1]) > 0
            and pgid != own_pgid
        }
        for pgid in groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signum)
        for pid in {root_pid, *alive}:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signum)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            table = _refresh_descendants(root_pid, descendants)
            if marker is not None:
                descendants.update(_marker_holders(marker))
            if root_pid not in table and not any(pid in table for pid in descendants):
                return
            time.sleep(0.05)


def managed_run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    timeout: float | None,
    result_path: Path,
    console_fd: int | None = None,
) -> int:
    if not argv:
        raise RuntimeError("managed command argv is empty")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    received_signal: int | None = None

    def receive_signal(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum

    previous = {
        signum: signal.signal(signum, receive_signal)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    started = time.monotonic()
    timed_out = False
    process: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
    descendants: set[int] = set()
    marker_path = result_path.with_name(f".process-tree.{secrets.token_hex(12)}")
    marker_descriptor = os.open(marker_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with log_path.open("wb") as log:
            child_env = dict(env)
            child_env[MANAGED_FD_ENV] = str(marker_descriptor)
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(marker_descriptor,),
            )
            assert process.stdout is not None
            process_stdout = process.stdout

            def copy_output() -> None:
                try:
                    while chunk := process_stdout.read(64 * 1024):
                        log.write(chunk)
                        log.flush()
                        with contextlib.suppress(OSError):
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                        if console_fd is not None:
                            with contextlib.suppress(OSError):
                                os.write(console_fd, chunk)
                except (OSError, ValueError):
                    return

            reader = threading.Thread(target=copy_output, daemon=True)
            reader.start()
            while True:
                _refresh_descendants(process.pid, descendants)
                if process.poll() is not None:
                    break
                if received_signal is not None:
                    terminate_process_tree(
                        process.pid,
                        descendants,
                        received_signal,
                        marker_path,
                    )
                    break
                if timeout is not None and time.monotonic() - started >= timeout:
                    timed_out = True
                    terminate_process_tree(process.pid, descendants, marker=marker_path)
                    break
                time.sleep(0.05)
            exit_status = process.wait(timeout=1)
            table = _refresh_descendants(process.pid, descendants)
            descendants.update(_marker_holders(marker_path))
            if _group_exists(process.pid) or any(pid in table for pid in descendants):
                terminate_process_tree(process.pid, descendants, marker=marker_path)
            process.stdout.close()
            reader.join(timeout=1)
    finally:
        os.close(marker_descriptor)
        marker_path.unlink(missing_ok=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if timed_out:
        exit_status = 124
    elif received_signal is not None:
        exit_status = 128 + received_signal
    atomic_json(
        result_path,
        {
            "schema_version": SCHEMA_VERSION,
            "exit_status": exit_status,
            "signal": signal.Signals(received_signal).name.removeprefix("SIG")
            if received_signal
            else None,
            "timed_out": timed_out,
        },
    )
    return exit_status


def inherited_managed_fds(source: dict[str, str] | None = None) -> tuple[int, ...]:
    """Return the validated verifier marker descriptor inherited by this process."""
    effective_source = dict(os.environ) if source is None else source
    raw = effective_source.get(MANAGED_FD_ENV)
    if raw is None:
        return ()
    try:
        descriptor = int(raw)
        os.fstat(descriptor)
    except (OSError, ValueError):
        return ()
    return (descriptor,)


def _parse_extra(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator:
            raise RuntimeError(f"invalid environment override {item!r}")
        result[key] = value
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--repo-root", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--deep-dependencies", action="store_true")
    check = subparsers.add_parser("check")
    check.add_argument("--repo-root", type=Path, required=True)
    check.add_argument("--before", type=Path, required=True)
    check.add_argument("--deep-dependencies", action="store_true")
    seal = subparsers.add_parser("seal-pre-commit-cache")
    seal.add_argument("--repo-root", type=Path, required=True)
    seal.add_argument("--cache-root", type=Path, required=True)
    publish = subparsers.add_parser("publish-pre-commit-cache")
    publish.add_argument("--repo-root", type=Path, required=True)
    publish.add_argument("--staging", type=Path, required=True)
    publish.add_argument("--destination", type=Path, required=True)
    prepare_pnpm = subparsers.add_parser("prepare-pnpm-cache")
    prepare_pnpm.add_argument("--repo-root", type=Path, required=True)
    prepare_pnpm.add_argument("--staging", type=Path, required=True)
    prepare_pnpm.add_argument("--allow-download", action="store_true")
    publish_pnpm = subparsers.add_parser("publish-pnpm-cache")
    publish_pnpm.add_argument("--staging", type=Path, required=True)
    publish_pnpm.add_argument("--destination", type=Path, required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--cwd", type=Path, required=True)
    run.add_argument("--log", type=Path, required=True)
    run.add_argument("--result", type=Path, required=True)
    run.add_argument("--timeout", type=float)
    run.add_argument("--credentialed", action="store_true")
    run.add_argument("--console-fd", type=int)
    run.add_argument("--set-env", action="append", default=[])
    run.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.action == "snapshot":
        atomic_json(
            args.output,
            repository_snapshot(
                args.repo_root,
                deep_dependencies=args.deep_dependencies,
            ),
        )
        return
    if args.action == "check":
        before = json.loads(args.before.read_text(encoding="utf-8"))
        changes = compare_snapshots(
            before,
            repository_snapshot(
                args.repo_root,
                deep_dependencies=args.deep_dependencies,
            ),
        )
        for change in changes:
            print(f"verification blocker: {change}", file=sys.stderr)
        raise SystemExit(1 if changes else 0)
    if args.action == "seal-pre-commit-cache":
        seal = seal_pre_commit_cache(args.repo_root, args.cache_root)
        print(json.dumps(seal, sort_keys=True))
        return
    if args.action == "publish-pre-commit-cache":
        publish_pre_commit_cache(args.repo_root, args.staging, args.destination)
        return
    if args.action == "prepare-pnpm-cache":
        seal = prepare_pnpm_cache(
            args.repo_root,
            args.staging,
            allow_download=args.allow_download,
        )
        print(json.dumps(seal, sort_keys=True))
        return
    if args.action == "publish-pnpm-cache":
        publish_pnpm_cache(args.staging, args.destination)
        return
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    env = strict_environment(
        args.run_dir,
        extra=_parse_extra(args.set_env),
        credentialed=args.credentialed,
        pre_commit=any("pre-commit" in value or value == "quality-gates" for value in argv),
    )
    manifest_path = args.run_dir / "manifest.json"
    manifest_before = (
        manifest_path.read_bytes()
        if manifest_path.is_file() and not manifest_path.is_symlink()
        else None
    )
    status = managed_run(
        argv,
        cwd=args.cwd,
        env=env,
        log_path=args.log,
        timeout=args.timeout,
        result_path=args.result,
        console_fd=args.console_fd,
    )
    if manifest_before is not None:
        try:
            manifest_after = manifest_path.read_bytes()
        except OSError:
            manifest_after = None
        if manifest_after != manifest_before or manifest_path.is_symlink():
            atomic_bytes(manifest_path, manifest_before)
            print(
                "verification blocker: verifier manifest was modified by a managed child",
                file=sys.stderr,
            )
            result = json.loads(args.result.read_text(encoding="utf-8"))
            result["control_metadata_tampered"] = True
            result["exit_status"] = 125
            atomic_json(args.result, result)
            status = 125
    try:
        cleanup_strict_environment(args.run_dir, all_runs=True)
    except (OSError, RuntimeError) as exc:
        print(
            f"verification blocker: sensitive runtime cleanup failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        result = json.loads(args.result.read_text(encoding="utf-8"))
        result["runtime_cleanup"] = {"status": "failed"}
        result["exit_status"] = 125
        atomic_json(args.result, result)
        status = 125
    raise SystemExit(status)


if __name__ == "__main__":
    main()
