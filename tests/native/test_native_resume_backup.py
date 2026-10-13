"""Cold resume keeps the first local transcript it replaces.

A Claude or Codex cold resume rewrites the local transcript from the session's
Omnigent items. For an imported session those items can be a trimmed copy, so
the rewrite used to lose the rest of the user's original history. Before
replacing an existing file the resume now hard-links it to
``<name>.jsonl.omnigent-backup``, once: later resumes replace only the live
file. These tests drive both resume paths against a mock Omnigent server, and
check that the import listing never treats a backup as a session.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest

from omnigent.harnesses.claude_native import main as claude_main
from omnigent.harnesses.codex_native import main as codex_main
from omnigent.native import _native_resume_backup as backup_module
from omnigent.native._native_resume_backup import (
    keep_original_resume_transcript,
    resume_backup_path,
)
from omnigent.session_import import local as local_module

_CLAUDE_ID = "a1b2c3d4-1234-5678-9abc-def012345670"
_CODEX_ID = "0199a5c0-1234-7abc-8def-0123456789ab"
_SESSION = "conv_backup"
_BACKUP_LOGGER = backup_module.__name__


def _items(*texts: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"msg_{n}",
            "response_id": f"resp_{n}",
            "type": "message",
            "role": "user" if n % 2 == 0 else "assistant",
            "content": [{"type": "input_text" if n % 2 == 0 else "output_text", "text": text}],
        }
        for n, text in enumerate(texts)
    ]


def _server(items: list[dict[str, Any]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == f"/v1/sessions/{_SESSION}/items", (
            request.url
        )
        return httpx.Response(200, json={"data": items, "has_more": False})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


def _texts(path: Path) -> list[str]:
    """Every text block in a written transcript, in order."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("text", "content"):
                if isinstance(value.get(key), str):
                    found.append(value[key])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for line in path.read_text(encoding="utf-8").splitlines():
        walk(json.loads(line))
    return found


class TestClaudeResumeBackup:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.workspace = self.root / "repo"
        self.workspace.mkdir()

        projects = self.root / ".claude" / "projects"
        monkeypatch.setattr(claude_main, "_CLAUDE_PROJECTS_DIR", projects)
        self.target = (
            projects
            / claude_main._sanitize_claude_project_name(str(self.workspace))
            / f"{_CLAUDE_ID}.jsonl"
        )
        self.backup = self.target.with_name(f"{_CLAUDE_ID}.jsonl.omnigent-backup")

    def write_original(self) -> bytes:
        self.target.parent.mkdir(parents=True)
        lines = [
            {
                "type": "user",
                "sessionId": _CLAUDE_ID,
                "message": {"role": "user", "content": f"original {n}"},
            }
            for n in range(3)
        ]
        data = "".join(json.dumps(line) + "\n" for line in lines).encode()
        self.target.write_bytes(data)
        return data

    @pytest.mark.asyncio
    async def resume(self, items: list[dict[str, Any]]) -> Path | None:
        async with _server(items) as client:
            return await claude_main._ensure_local_claude_resume_transcript(
                client,
                session_id=_SESSION,
                external_session_id=_CLAUDE_ID,
                workspace=self.workspace,
                bridge_dir=self.root / "bridge",
            )

    @pytest.mark.asyncio
    async def test_existing_transcript_is_kept_and_the_live_file_rebuilt(self) -> None:
        original = self.write_original()
        inode = self.target.stat().st_ino
        written = await self.resume(_items("kept 0", "kept 1"))
        assert written == self.target
        assert self.backup.read_bytes() == original
        # Linked, not copied: the backup is the original file itself.
        assert self.backup.stat().st_ino == inode
        assert _texts(self.target) == ["kept 0", "kept 1"]
        assert sorted(p.name for p in self.target.parent.iterdir()) == sorted(
            [self.target.name, self.backup.name]
        )

    @pytest.mark.asyncio
    async def test_a_later_resume_keeps_the_first_original(self) -> None:
        original = self.write_original()
        await self.resume(_items("first resume"))
        await self.resume(_items("second resume", "reply"))
        assert self.backup.read_bytes() == original
        assert _texts(self.target) == ["second resume", "reply"]

    @pytest.mark.asyncio
    async def test_no_existing_transcript_no_backup(self) -> None:
        written = await self.resume(_items("fresh machine"))
        assert written == self.target
        assert _texts(self.target) == ["fresh machine"]
        assert not self.backup.exists()

    @pytest.mark.asyncio
    async def test_unavailable_history_uses_the_local_file_without_a_backup(self) -> None:
        original = self.write_original()
        unavailable = claude_main._ResumeHistoryUnavailableError("history unavailable")
        with mock.patch.object(
            claude_main,
            "_fetch_all_session_items_for_claude_resume",
            mock.AsyncMock(side_effect=unavailable),
        ):
            written = await self.resume([])
        assert written == self.target
        assert self.target.read_bytes() == original
        assert not self.backup.exists()

    @pytest.mark.asyncio
    async def test_empty_history_writes_nothing_and_keeps_no_backup(self) -> None:
        original = self.write_original()
        assert await self.resume([]) is None
        assert self.target.read_bytes() == original
        assert not self.backup.exists()

    @pytest.mark.asyncio
    async def test_a_failed_backup_does_not_fail_the_resume(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        self.write_original()
        with mock.patch.object(
            backup_module.os, "link", side_effect=PermissionError("read-only directory")
        ):
            written = await self.resume(_items("still resumes"))
        assert written == self.target
        assert _texts(self.target) == ["still resumes"]
        assert not self.backup.exists()
        assert any(
            "resuming without a backup" in record.message
            for record in caplog.records
            if record.name == _BACKUP_LOGGER
        )


class TestCodexResumeBackup:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.codex_home = self.root / "bridge" / "codex-home"
        self.target = (
            self.codex_home
            / "sessions"
            / "2026"
            / "10"
            / "01"
            / f"rollout-2026-10-01T00-00-00-{_CODEX_ID}.jsonl"
        )
        self.backup = self.target.with_name(self.target.name + ".omnigent-backup")

    def write_original(self) -> bytes:
        self.target.parent.mkdir(parents=True)
        records = [
            {
                "type": "session_meta",
                "payload": {"id": _CODEX_ID, "cwd": str(self.workspace), "source": "cli"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "original"}],
                },
            },
        ]
        data = "".join(json.dumps(record) + "\n" for record in records).encode()
        self.target.write_bytes(data)
        return data

    @pytest.mark.asyncio
    async def resume(self, items: list[dict[str, Any]]) -> Path:
        async with _server(items) as client:
            return await codex_main._ensure_local_codex_resume_rollout(
                client,
                session_id=_SESSION,
                external_session_id=_CODEX_ID,
                codex_home=self.codex_home,
                workspace=self.workspace,
                model_provider="omnigent_databricks",
                codex_path=None,
            )

    def rollouts(self) -> list[str]:
        return sorted(p.name for p in (self.codex_home / "sessions").rglob("*") if p.is_file())

    @pytest.mark.asyncio
    async def test_existing_rollout_is_kept_and_the_live_file_rebuilt(self) -> None:
        original = self.write_original()
        inode = self.target.stat().st_ino
        written = await self.resume(_items("kept 0", "kept 1"))
        # The newest existing rollout is reused, so it is the one kept.
        assert written == self.target
        assert self.backup.read_bytes() == original
        assert self.backup.stat().st_ino == inode
        assert "kept 0" in _texts(self.target) and "original" not in _texts(self.target)
        assert self.rollouts() == sorted([self.target.name, self.backup.name])

    @pytest.mark.asyncio
    async def test_a_later_resume_keeps_the_first_original(self) -> None:
        original = self.write_original()
        await self.resume(_items("first resume"))
        written = await self.resume(_items("second resume"))
        assert written == self.target
        assert self.backup.read_bytes() == original
        assert "second resume" in _texts(self.target)
        assert self.rollouts() == sorted([self.target.name, self.backup.name])

    @pytest.mark.asyncio
    async def test_no_existing_rollout_no_backup(self) -> None:
        written = await self.resume(_items("fresh machine"))
        assert "fresh machine" in _texts(written)
        assert self.rollouts() == [written.name]

    @pytest.mark.asyncio
    async def test_unavailable_history_uses_the_local_rollout_without_a_backup(self) -> None:
        original = self.write_original()
        unavailable = codex_main._CodexResumeHistoryUnavailableError("history unavailable")
        with mock.patch.object(
            codex_main,
            "_fetch_all_session_items_for_codex_resume",
            mock.AsyncMock(side_effect=unavailable),
        ):
            written = await self.resume([])
        assert written == self.target
        assert self.target.read_bytes() == original
        assert not self.backup.exists()

    @pytest.mark.asyncio
    async def test_empty_history_still_rewrites_and_keeps_the_original(self) -> None:
        # Unlike Claude, Codex always writes (session_meta alone resumes), so
        # an empty server history replaces the file and the original is kept.
        original = self.write_original()
        await self.resume([])
        assert self.backup.read_bytes() == original
        assert "original" not in _texts(self.target)

    @pytest.mark.asyncio
    async def test_a_failed_backup_does_not_fail_the_resume(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        self.write_original()
        with mock.patch.object(
            backup_module.os, "link", side_effect=OSError(18, "Invalid cross-device link")
        ):
            written = await self.resume(_items("still resumes"))
        assert written == self.target
        assert "still resumes" in _texts(self.target)
        assert not self.backup.exists()
        assert any(
            "resuming without a backup" in record.message
            for record in caplog.records
            if record.name == _BACKUP_LOGGER
        )


class TestKeepOriginalHelper:
    def test_backup_name_keeps_jsonl_before_the_suffix(self, tmp_path: Path) -> None:
        assert resume_backup_path(tmp_path / "x.jsonl") == tmp_path / "x.jsonl.omnigent-backup"

    def test_an_existing_backup_is_never_replaced(self, tmp_path: Path) -> None:
        target = tmp_path / "s.jsonl"
        target.write_text("second\n")
        resume_backup_path(target).write_text("first\n")
        assert keep_original_resume_transcript(target) is None
        assert resume_backup_path(target).read_text() == "first\n"

    def test_concurrent_resumes_keep_one_original(self, tmp_path: Path) -> None:
        target = tmp_path / "s.jsonl"
        target.write_text("original\n")
        barrier = threading.Barrier(8)
        results: list[Path | None] = []

        def resume(n: int) -> None:
            tmp = tmp_path / f"s.jsonl.{n}.tmp"
            tmp.write_text(f"rebuild {n}\n")
            barrier.wait()
            results.append(keep_original_resume_transcript(target))
            os.replace(tmp, target)

        threads = [threading.Thread(target=resume, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(result is not None for result in results) == 1
        assert resume_backup_path(target).read_text() == "original\n"
        assert target.read_text().startswith("rebuild ")


class TestImportListingIgnoresBackups:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("CODEX_HOME", str(self.home / ".codex"))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: self.home))

    def test_claude_backups_are_not_sessions(self) -> None:
        project = self.home / ".claude" / "projects" / "-repo"
        project.mkdir(parents=True)
        record = {"type": "user", "cwd": "/repo", "message": {"role": "user", "content": "hi"}}
        (project / f"{_CLAUDE_ID}.jsonl").write_text(json.dumps(record) + "\n")
        (project / f"{_CLAUDE_ID}.jsonl.omnigent-backup").write_text(json.dumps(record) + "\n")
        # A backup whose live file is gone is not listed either.
        (project / "b2b2b2b2-1234-5678-9abc-def012345670.jsonl.omnigent-backup").write_text(
            json.dumps(record) + "\n"
        )
        assert local_module.list_recent_local_session_ids("claude", limit=10) == (_CLAUDE_ID,)

    def test_codex_backups_are_not_sessions(self) -> None:
        day = self.home / ".codex" / "sessions" / "2026" / "10" / "01"
        day.mkdir(parents=True)
        meta = {
            "type": "session_meta",
            "payload": {"id": _CODEX_ID, "cwd": "/repo", "source": "cli"},
        }
        name = f"rollout-2026-10-01T00-00-00-{_CODEX_ID}.jsonl"
        (day / name).write_text(json.dumps(meta) + "\n")
        (day / f"{name}.omnigent-backup").write_text(json.dumps(meta) + "\n")
        other = "0199a5c0-1234-7abc-8def-0123456789ac"
        (day / f"rollout-2026-10-01T00-00-00-{other}.jsonl.omnigent-backup").write_text(
            json.dumps(meta) + "\n"
        )
        assert local_module.list_recent_local_session_ids("codex", limit=10) == (_CODEX_ID,)
        # The resume-side lookup that picks the rollout to rebuild skips it too.
        assert codex_main._find_codex_rollout(self.home / ".codex", _CODEX_ID) == day / name
