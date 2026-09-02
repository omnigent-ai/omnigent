from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from omnigent_company_brain.adapters.common import AdaptedDocument
from omnigent_company_brain.locking import process_file_lock
from omnigent_company_brain.models import BrainDocumentV1, sha256_bytes, sha256_text

_SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "refresh_token",
    "set-cookie",
}
_NORMALIZED_SECRET_KEYS = {key.replace("-", "_") for key in _SECRET_KEYS}


class PublicationConflictError(RuntimeError):
    pass


class SecretInRawPayloadError(ValueError):
    pass


class RawObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class PublicationResult:
    commit_sha: str
    committed: bool
    fetched_count: int
    changed_count: int
    deleted_count: int
    skipped_count: int


def initialize_brain_repo(repo_path: Path, repo_url: str | None = None) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    if (repo_path / ".git").exists():
        valid = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_path,
            check=False,
            capture_output=True,
            text=True,
        )
        valid_root = (
            Path(valid.stdout.strip()).resolve()
            if valid.returncode == 0 and valid.stdout.strip()
            else None
        )
        if valid_root != repo_path.resolve():
            raise ValueError("brain repository is not a valid Git repository")
        if repo_url:
            current = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            if current.returncode == 0 and current.stdout.strip() != repo_url:
                raise ValueError("brain repository origin differs from configured repo_url")
            if current.returncode != 0:
                subprocess.run(
                    ["git", "remote", "add", "origin", repo_url],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                    text=True,
                )
        return
    if repo_url:
        if any(repo_path.iterdir()):
            raise ValueError("brain repository path is not empty")
        repo_path.rmdir()
        subprocess.run(
            ["git", "clone", "--origin", "origin", repo_url, str(repo_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _NORMALIZED_SECRET_KEYS or _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(child) for child in value)
    return False


def _validate_raw_payload(raw_json: str) -> None:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("adapter raw payload must be valid JSON") from exc
    if _contains_secret_key(payload):
        raise SecretInRawPayloadError("raw payload contains a secret-shaped field")


def _validate_raw_object_key(key: str, provider: str) -> None:
    path = PurePosixPath(key)
    if (
        path.is_absolute()
        or "\\" in key
        or ".." in path.parts
        or path.parts[:3] != ("company-brain", "raw", provider)
        or not path.suffix
    ):
        raise ValueError("binary raw object key is outside the provider artifact namespace")


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _restore_publication_paths(repo_path: Path) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode == 0:
        subprocess.run(
            [
                "git",
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                ".company-brain",
                ".raw",
                "sources",
            ],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            [
                "git",
                "rm",
                "-rf",
                "--cached",
                "--ignore-unmatch",
                "--",
                ".company-brain",
                ".raw",
                "sources",
            ],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
    subprocess.run(
        ["git", "clean", "-fd", "--", ".company-brain", ".raw", "sources"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


@contextmanager
def _publication_transaction(repo_path: Path) -> Iterator[None]:
    with process_file_lock(repo_path / ".git" / "company-brain.lock"):
        marker = repo_path / ".git" / "company-brain-publish-running"
        if marker.exists():
            _restore_publication_paths(repo_path)
            marker.unlink()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if status:
            raise PublicationConflictError("brain repo has uncommitted changes")
        _atomic_write(marker, f"{os.getpid()}\n")
        try:
            yield
        except BaseException:
            _restore_publication_paths(repo_path)
            raise
        finally:
            marker.unlink(missing_ok=True)


class GitBrainPublisher:
    def __init__(
        self,
        repo_path: Path,
        *,
        push: bool = False,
        repo_url: str | None = None,
        raw_object_store: RawObjectStore | None = None,
    ) -> None:
        if repo_url:
            parsed_repo_url = urlsplit(repo_url)
            if parsed_repo_url.scheme in {"http", "https"} and (
                parsed_repo_url.username or parsed_repo_url.password
            ):
                raise ValueError("brain repository URL must not contain credentials")
        self._repo_path = repo_path.resolve()
        self._push = push
        self._repo_url = repo_url
        self._raw_object_store = raw_object_store

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self._repo_path,
            check=check,
            capture_output=True,
            text=True,
        )

    def _head(self) -> str:
        result = self._git("rev-parse", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _push_head(self) -> None:
        branch = self._git("branch", "--show-current").stdout.strip() or "main"
        self._git("push", "origin", f"HEAD:{branch}")

    def publish(
        self,
        documents: Iterable[AdaptedDocument],
        *,
        sync_run_id: str,
        complete_fetch: bool,
        selection_id: str | None = None,
    ) -> PublicationResult:
        initialize_brain_repo(self._repo_path, self._repo_url)
        records = tuple(documents)
        if not records:
            raise ValueError("publication requires at least one document or tombstone")
        validated: list[AdaptedDocument] = []
        seen_paths: set[str] = set()
        for item in records:
            document = BrainDocumentV1.model_validate(item.document.model_dump())
            if document.stable_path in seen_paths:
                raise ValueError(f"duplicate document path: {document.stable_path}")
            seen_paths.add(document.stable_path)
            _validate_raw_payload(item.raw_json)
            if item.raw_bytes is None:
                if item.raw_object_key is not None:
                    raise ValueError("JSON provenance cannot declare a binary object key")
                if sha256_text(item.raw_json) != document.raw_sha256:
                    raise ValueError("raw_sha256 does not match JSON provenance")
            else:
                if item.raw_object_key is None:
                    raise ValueError("binary provenance requires an object key")
                _validate_raw_object_key(item.raw_object_key, document.provider)
                if sha256_bytes(item.raw_bytes) != document.raw_sha256:
                    raise ValueError("raw_sha256 does not match binary provenance")
                if self._raw_object_store is None:
                    raise ValueError("binary provenance requires an artifact store")
            validated.append(item)
        if not complete_fetch and any(
            item.document.deletion_state == "deleted" for item in validated
        ):
            raise ValueError("partial fetches cannot publish deletions")

        providers = {item.document.provider for item in validated}
        connections = {item.document.connection_id for item in validated}
        if len(providers) != 1 or len(connections) != 1:
            raise ValueError("one publication batch must contain one provider connection")
        provider = next(iter(providers))
        connection_id = next(iter(connections))

        with _publication_transaction(self._repo_path):
            changed_count = 0
            deleted_count = 0
            skipped_count = 0
            manifest_path = self._repo_path / ".company-brain" / "documents.json"
            if manifest_path.exists():
                loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(loaded_manifest, dict) or not isinstance(
                    loaded_manifest.get("documents"), dict
                ):
                    raise ValueError("invalid company brain document manifest")
                manifest: dict[str, Any] = loaded_manifest
            else:
                manifest = {"schema_version": 1, "documents": {}}
            manifest_documents = manifest["documents"]

            for item in validated:
                document = item.document
                target = self._repo_path / document.stable_path
                raw_target = self._repo_path / document.raw_object_reference
                if item.raw_bytes is not None and item.raw_object_key is not None:
                    if self._raw_object_store is None:
                        raise ValueError("binary provenance requires an artifact store")
                    self._raw_object_store.put(item.raw_object_key, item.raw_bytes)
                _atomic_write(raw_target, item.raw_json)
                if document.deletion_state == "deleted":
                    if target.exists():
                        target.unlink()
                        deleted_count += 1
                    else:
                        skipped_count += 1
                    manifest_documents.pop(document.stable_path, None)
                    continue
                existing = target.read_text(encoding="utf-8") if target.exists() else None
                if existing == document.markdown:
                    skipped_count += 1
                else:
                    _atomic_write(target, document.markdown)
                    changed_count += 1
                manifest_documents[document.stable_path] = {
                    "canonical_source_url": document.canonical_source_url,
                    "connection_id": document.connection_id,
                    "content_sha256": document.content_sha256,
                    "external_resource_id": document.external_resource_id,
                    "provider": document.provider,
                    "raw_sha256": document.raw_sha256,
                    "selection_id": selection_id,
                    "source_modified_at": document.source_modified_at.isoformat(),
                    "title": document.title,
                }

            manifest_text = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            _atomic_write(manifest_path, manifest_text)
            self._git("add", "--all", "--", ".company-brain", ".raw", "sources")
            staged = self._git("diff", "--cached", "--quiet", check=False)
            if staged.returncode == 0:
                if self._push:
                    self._push_head()
                return PublicationResult(
                    commit_sha=self._head(),
                    committed=False,
                    fetched_count=len(validated),
                    changed_count=0,
                    deleted_count=0,
                    skipped_count=len(validated),
                )
            if staged.returncode != 1:
                raise RuntimeError("git could not inspect the staged publication")

            message = (
                f"company-brain: sync {provider}\n\n"
                f"sync-run: {sync_run_id}\n"
                f"connection: {connection_id}\n"
                f"fetched: {len(validated)}\n"
                f"changed: {changed_count}\n"
                f"deleted: {deleted_count}\n"
                f"skipped: {skipped_count}"
            )
            self._git(
                "-c",
                "user.name=wulo-work company brain",
                "-c",
                "user.email=company-brain@wulo.work",
                "commit",
                "-m",
                message,
            )
            commit_sha = self._head()
            if self._push:
                self._push_head()
            return PublicationResult(
                commit_sha=commit_sha,
                committed=True,
                fetched_count=len(validated),
                changed_count=changed_count,
                deleted_count=deleted_count,
                skipped_count=skipped_count,
            )
