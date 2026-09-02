from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from omnigent_company_brain.locking import process_file_lock

CommandRunner = Callable[[list[str], Path, Mapping[str, str]], subprocess.CompletedProcess[str]]


def _default_runner(
    command: list[str],
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=dict(env),
        check=False,
        capture_output=True,
        text=True,
    )


def _redact_error(value: str) -> str:
    redacted = value
    for marker in ("postgresql://", "postgres://", "Bearer ", "gbrain_"):
        if marker in redacted:
            redacted = redacted.split(marker, 1)[0] + "[redacted]"
    return redacted.strip()[:500]


def _parse_json_output(value: str, *, fallback: Any) -> Any:
    stripped = value.strip()
    if stripped:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    lines = value.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            try:
                return json.loads("\n".join(lines[index:]))
            except json.JSONDecodeError:
                continue
    for line in reversed(lines):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return fallback


@dataclass(frozen=True, slots=True)
class GbrainSyncReceipt:
    source_id: str
    commit_sha: str
    replayed: bool
    result: dict[str, Any]


class GbrainSyncRunner:
    def __init__(
        self,
        *,
        state_dir: Path,
        executable: str = "gbrain",
        no_embedding: bool = False,
        command_runner: CommandRunner = _default_runner,
    ) -> None:
        self._state_dir = state_dir.resolve()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._executable = executable
        self._no_embedding = no_embedding
        self._command_runner = command_runner

    @property
    def _env(self) -> dict[str, str]:
        return {**os.environ, "GBRAIN_HOME": str(self._state_dir)}

    def _run(self, command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        result = self._command_runner(command, cwd, self._env)
        if result.returncode != 0:
            detail = _redact_error(result.stderr or result.stdout or "gbrain command failed")
            raise RuntimeError(detail)
        return result

    @staticmethod
    def _git(
        repo_path: Path, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_path,
            check=check,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary.write_text(value, encoding="utf-8", newline="\n")
        temporary.replace(path)

    def _materialize_source_mirror(
        self,
        repo_path: Path,
        source_commit_sha: str,
    ) -> tuple[Path, str, dict[str, Any], tuple[str, ...]]:
        customer_manifest_text = self._git(
            repo_path,
            "show",
            f"{source_commit_sha}:.company-brain/documents.json",
        ).stdout
        customer_manifest = json.loads(customer_manifest_text)
        if not isinstance(customer_manifest, dict) or not isinstance(
            customer_manifest.get("documents"), dict
        ):
            raise ValueError("invalid committed company brain document manifest")
        active = customer_manifest.get("documents") or {}
        mirror_path = self._state_dir / "company-shared-source"
        mirror_path.mkdir(parents=True, exist_ok=True)
        if not (mirror_path / ".git").exists():
            self._git(mirror_path, "init", "-b", "main")

        mirror_state_path = self._state_dir / "company-brain-mirror-state.json"
        if mirror_state_path.exists():
            previous = json.loads(mirror_state_path.read_text(encoding="utf-8"))
        else:
            previous = {"active": {}, "tombstones": {}}
        previous_active = previous.get("active") or {}
        tombstones = dict(previous.get("tombstones") or {})

        for relative_path in active:
            destination = mirror_path / relative_path
            committed_content = self._git(
                repo_path,
                "show",
                f"{source_commit_sha}:{relative_path}",
            ).stdout
            self._write(destination, committed_content)
            tombstones.pop(relative_path, None)

        for relative_path in set(previous_active) - set(active):
            metadata = previous_active[relative_path]
            tombstones[relative_path] = metadata
            title = str(metadata.get("title") or "Deleted source page")
            source_url = str(metadata.get("canonical_source_url") or "")
            tombstone = (
                "---\n"
                f"title: {json.dumps(f'Deleted: {title}', ensure_ascii=False)}\n"
                'type: "source"\n'
                'visibility: "private"\n'
                'company_brain_visibility: "org-shared"\n'
                "company_brain_deleted: true\n"
                f"source_url: {json.dumps(source_url, ensure_ascii=False)}\n"
                f"source_commit: {json.dumps(source_commit_sha)}\n"
                "---\n\n"
                f"# Deleted: {title}\n\n"
                "This source was deleted upstream. Its last published content remains "
                "available in the customer-owned Git history.\n"
            )
            self._write(mirror_path / relative_path, tombstone)

        source_metadata = {
            "schema_version": 1,
            "source_commit_sha": source_commit_sha,
        }
        self._write(
            mirror_path / ".company-brain" / "source.json",
            json.dumps(source_metadata, indent=2, sort_keys=True) + "\n",
        )
        self._git(mirror_path, "add", "--all")
        staged = self._git(mirror_path, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 1:
            self._git(
                mirror_path,
                "-c",
                "user.name=wulo-work company brain",
                "-c",
                "user.email=company-brain@wulo.work",
                "commit",
                "-m",
                f"company-brain: materialize {source_commit_sha}",
            )
        elif staged.returncode != 0:
            raise RuntimeError("git could not inspect the gbrain source mirror")
        mirror_commit_sha = self._git(mirror_path, "rev-parse", "HEAD").stdout.strip()
        next_state = {
            "schema_version": 1,
            "source_commit_sha": source_commit_sha,
            "active": active,
            "tombstones": tombstones,
        }
        tombstone_slugs = tuple(sorted(path.removesuffix(".md") for path in tombstones))
        return mirror_path, mirror_commit_sha, next_state, tombstone_slugs

    def initialize(self) -> None:
        config_path = self._state_dir / ".gbrain" / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            database_url = os.environ.get("GBRAIN_DATABASE_URL", "").strip()
            if database_url and (
                config.get("engine") != "postgres"
                or config.get("database_url") != database_url
                or config.get("embedding_disabled") is not self._no_embedding
            ):
                raise ValueError("persisted gbrain engine configuration differs from runtime")
            return
        database_url = os.environ.get("GBRAIN_DATABASE_URL", "").strip()
        if database_url:
            if not database_url.startswith(("postgres://", "postgresql://")):
                raise ValueError("GBRAIN_DATABASE_URL must use PostgreSQL")
            command = [
                self._executable,
                "init",
                "--url",
                database_url,
                "--non-interactive",
                "--json",
            ]
            if self._no_embedding:
                command.append("--no-embedding")
        else:
            command = [
                self._executable,
                "init",
                "--pglite",
                "--path",
                str(self._state_dir / "brain.pglite"),
                "--non-interactive",
                "--no-embedding",
                "--json",
            ]
        self._run(command, self._state_dir)

    def _ensure_source(self, repo_path: Path, source_id: str) -> None:
        listed = self._run(
            [self._executable, "sources", "list", "--json"],
            repo_path,
        )
        payload = _parse_json_output(listed.stdout, fallback=[])
        sources = payload.get("sources", payload) if isinstance(payload, dict) else payload
        for source in sources:
            if str(source.get("id")) != source_id:
                continue
            registered_path = source.get("local_path")
            if registered_path and Path(str(registered_path)).resolve() != repo_path.resolve():
                raise RuntimeError("gbrain source is registered to a different local path")
            return
        self._run(
            [
                self._executable,
                "sources",
                "add",
                source_id,
                "--path",
                str(repo_path),
                "--name",
                "Company shared knowledge",
            ],
            repo_path,
        )

    def _source_health(self, repo_path: Path, source_id: str) -> dict[str, Any]:
        result = self._run(
            [self._executable, "sources", "status", "--json"],
            repo_path,
        )
        payload = _parse_json_output(result.stdout, fallback={})
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(sources, list):
            raise RuntimeError("gbrain source status returned an invalid payload")
        source = next(
            (
                item
                for item in sources
                if isinstance(item, dict) and item.get("source_id") == source_id
            ),
            None,
        )
        if source is None:
            raise RuntimeError("gbrain source status omitted the synced source")
        lag_seconds = source.get("lag_seconds")
        queue_depth = source.get("queue_depth")
        fresh = (
            source.get("last_sync_at") is not None
            and isinstance(lag_seconds, int | float)
            and lag_seconds <= 300
            and queue_depth == 0
            and source.get("sync_running") is False
        )
        if not fresh:
            raise RuntimeError("gbrain source is not fresh after sync")
        return {
            "source_id": source_id,
            "fresh": True,
            "last_sync_at": source.get("last_sync_at"),
            "lag_seconds": lag_seconds,
            "total_pages": source.get("total_pages"),
            "total_chunks": source.get("total_chunks"),
            "embedded_chunks": source.get("embedded_chunks"),
            "embed_coverage_pct": source.get("embed_coverage_pct"),
            "failed_jobs_24h": source.get("failed_jobs_24h"),
            "queue_depth": queue_depth,
            "sync_running": False,
        }

    def sync(self, repo_path: Path, *, source_id: str, commit_sha: str) -> GbrainSyncReceipt:
        with process_file_lock(self._state_dir / "company-brain-sync.lock"):
            return self._sync_locked(repo_path, source_id=source_id, commit_sha=commit_sha)

    def _sync_locked(
        self,
        repo_path: Path,
        *,
        source_id: str,
        commit_sha: str,
    ) -> GbrainSyncReceipt:
        repo_path = repo_path.resolve()
        actual_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if actual_head != commit_sha:
            raise ValueError("gbrain sync commit must equal the brain repo HEAD")
        state_path = self._state_dir / "company-brain-sync-state.json"
        if state_path.exists():
            loaded_state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(loaded_state, dict) or not isinstance(
                loaded_state.get("completed"), dict
            ):
                raise ValueError("invalid company brain sync state")
            state = cast(dict[str, Any], loaded_state)
        else:
            state = {"schema_version": 1, "completed": {}}
        completed = cast(dict[str, dict[str, Any]], state["completed"])
        key = f"{source_id}:{commit_sha}"
        if key in completed:
            return GbrainSyncReceipt(
                source_id=source_id,
                commit_sha=commit_sha,
                replayed=True,
                result=completed[key],
            )

        mirror_path, mirror_commit_sha, mirror_state, tombstone_slugs = (
            self._materialize_source_mirror(repo_path, commit_sha)
        )
        self.initialize()
        self._ensure_source(mirror_path, source_id)
        command = [
            self._executable,
            "sync",
            "--source",
            source_id,
            "--repo",
            str(mirror_path),
            "--no-pull",
            "--yes",
            "--json",
        ]
        if self._no_embedding:
            command.append("--no-embed")
        result = self._run(command, mirror_path)
        payload = _parse_json_output(result.stdout, fallback={})
        for slug in tombstone_slugs:
            deleted = self._command_runner(
                [
                    self._executable,
                    "delete",
                    slug,
                    "--source-id",
                    source_id,
                ],
                mirror_path,
                self._env,
            )
            if deleted.returncode != 0 and "not found" not in (
                f"{deleted.stdout}\n{deleted.stderr}".lower()
            ):
                raise RuntimeError(_redact_error(deleted.stderr or deleted.stdout))
        source_health = self._source_health(mirror_path, source_id)
        payload = {
            "gbrain": payload,
            "mirror_commit_sha": mirror_commit_sha,
            "soft_deleted": list(tombstone_slugs),
            "source_commit_sha": commit_sha,
            "source_health": source_health,
        }
        mirror_state_path = self._state_dir / "company-brain-mirror-state.json"
        mirror_state_temporary = mirror_state_path.with_suffix(".tmp")
        mirror_state_temporary.write_text(
            json.dumps(mirror_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        mirror_state_temporary.replace(mirror_state_path)
        completed[key] = payload
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(state_path)
        return GbrainSyncReceipt(
            source_id=source_id,
            commit_sha=commit_sha,
            replayed=False,
            result=payload,
        )
