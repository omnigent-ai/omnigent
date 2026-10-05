"""Local-session read faults are contained and explained.

Gaps fixed: non-UTF-8 bytes no longer hide sessions; invalid UTF-8 mid-file
costs that record only, not the session; list/dict role/type skip that record;
read budgets cap Qwen, Pi, Kiro and Kimi.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from omnigent.entities import ErrorData, MessageData
from omnigent.session_import import local as local_module
from omnigent.session_import.local import (
    list_recent_local_session_ids,
    list_recent_sessions_across_harnesses,
    load_local_session,
)
from omnigent.session_import.models import SessionImportNotFoundError

_CODEX_IDS = [f"0199a5c0-0000-7abc-8def-00000000000{n}" for n in range(1, 7)]
_LATIN1_LINE = (
    b'{"type":"response_item","payload":{"type":"message","role":"user",'
    b'"content":[{"type":"input_text","text":"caf\xe9"}]}}'
)


def _rec(kind: str, payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {"timestamp": "2026-10-03T00:00:00.000Z", "type": kind, "payload": payload}
    ).encode()


def _codex_lines(session_id: str, prompts: int = 1) -> list[bytes]:
    lines = [_rec("session_meta", {"id": session_id, "cwd": "/repo", "source": "cli"})]
    for n in range(prompts):
        lines.append(
            _rec(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"p{n}"}],
                },
            )
        )
        lines.append(
            _rec(
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"a{n}"}],
                },
            )
        )
    return lines


def _texts(session: Any) -> list[str]:
    return [
        block["text"]
        for item in session.items
        if isinstance(item.data, MessageData)
        for block in item.data.content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A HOME with harness transcript directories."""
    env = {
        "HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": "",
        "CODEX_HOME": str(tmp_path / ".codex"),
        "QWEN_HOME": str(tmp_path / ".qwen"),
        "PI_CODING_AGENT_DIR": str(tmp_path / ".pi" / "agent"),
        "KIMI_CODE_HOME": str(tmp_path / ".kimi-code"),
    }
    monkeypatch.setattr("os.environ", {**os.environ, **env})
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


def codex(home: Path, session_id: str, lines: list[bytes], *, mtime: float = 1_000) -> Path:
    path = (
        home
        / ".codex"
        / "sessions"
        / "2026"
        / "10"
        / "03"
        / f"rollout-2026-10-03T00-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n".join(lines) + b"\n")
    os.utime(path, (mtime, mtime))
    return path


def jsonl(home: Path, path: Path, records: list[dict[str, Any]], *, raw: list[bytes] = ()) -> Path:
    full_path = home / path if not path.is_absolute() else path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(json.dumps(r).encode() + b"\n" for r in records)
    full_path.write_bytes(body + b"".join(line + b"\n" for line in raw))
    return full_path


def qwen(home: Path, session_id: str, turns: int, *, raw: list[bytes] = ()) -> Path:
    records: list[dict[str, Any]] = []
    parent = None
    for n in range(turns):
        for role, kind in (("user", "user"), ("model", "assistant")):
            uid = f"{kind}-{n}"
            records.append(
                {
                    "type": kind,
                    "uuid": uid,
                    "parentUuid": parent,
                    "cwd": "/w",
                    "message": {"role": role, "parts": [{"text": f"{kind} {n}"}]},
                }
            )
            parent = uid
    return jsonl(
        home, Path(".qwen") / "projects" / "p" / "chats" / f"{session_id}.jsonl", records, raw=raw
    )


def pi(home: Path, session_id: str, turns: int, *, extra: list[dict[str, Any]] = ()) -> Path:
    records: list[dict[str, Any]] = [
        {"type": "session", "version": 3, "id": session_id, "cwd": "/w"}
    ]
    parent = None
    for n in range(turns):
        eid = f"e{n}"
        records.append(
            {
                "type": "message",
                "id": eid,
                "parentId": parent,
                "message": {"role": "user", "content": [{"type": "text", "text": f"pi {n}"}]},
            }
        )
        parent = eid
    for record in extra:
        records.append({**record, "parentId": parent})
        parent = record["id"]
    return jsonl(home, Path(".pi") / "agent" / "sessions" / "p" / f"{session_id}.jsonl", records)


def kiro(home: Path, session_id: str, turns: int) -> Path:
    root = home / ".kiro" / "sessions" / "cli"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{session_id}.json").write_text(json.dumps({"cwd": "/w"}))
    records = [
        {
            "kind": "Prompt",
            "data": {"message_id": f"m{n}", "content": [{"kind": "text", "data": f"kiro {n}"}]},
        }
        for n in range(turns)
    ]
    return jsonl(home, root / f"{session_id}.jsonl", records)


def kimi(home: Path, session_id: str, turns: int) -> Path:
    path = home / ".kimi-code" / "sessions" / "ws" / session_id / "agents" / "main" / "wire.jsonl"
    records = [
        {"type": "turn.prompt", "input": [{"type": "text", "text": f"kimi {n}"}]}
        for n in range(turns)
    ]
    return jsonl(home, path, records)


class TestCodexListingUtf8:
    """Non-UTF-8 bytes no longer hide Codex sessions."""

    def test_codex_listing_keeps_every_session(self, home: Path) -> None:
        """A non-UTF-8 byte past the first line keeps all sessions in the list."""
        for n, sid in enumerate(_CODEX_IDS[:5]):
            codex(home, sid, _codex_lines(sid), mtime=1_000 + n)
        bad = _CODEX_IDS[5]
        lines = _codex_lines(bad)
        codex(home, bad, [lines[0], _LATIN1_LINE, *lines[1:]], mtime=2_000)
        listed = list_recent_local_session_ids("codex", limit=50)
        assert listed[0] == bad
        assert sorted(listed) == sorted(_CODEX_IDS)
        across = list_recent_sessions_across_harnesses(limit=50)
        assert sorted(sid for source, sid in across if source == "codex") == sorted(_CODEX_IDS)

    def test_failing_harness_is_reported_not_dropped_silently(self, home: Path) -> None:
        """A harness that breaks is reported in skipped_harnesses, not hidden."""
        sid = _CODEX_IDS[0]
        codex(home, sid, _codex_lines(sid))
        real = local_module._recent_local_sessions_with_recency

        def _listing(source: str, *, limit: int) -> list[tuple[str, float]]:
            if source == "pi":
                raise RuntimeError("index exploded")
            if source == "kimi":
                raise SessionImportNotFoundError("not installed")
            return real(source, limit=limit)  # type: ignore[arg-type]

        with mock.patch.object(local_module, "_recent_local_sessions_with_recency", _listing):
            targets = list_recent_sessions_across_harnesses(limit=10)
        assert targets == [("codex", sid)]
        # A harness that isn't there isn't a failure; a broken reader is.
        assert targets.skipped_harnesses == (("pi", "RuntimeError"),)


class TestTolerantDecoding:
    """Invalid UTF-8 mid-file costs at most that record, not the session."""

    def test_codex_bad_byte_mid_file_imports_the_rest(self, home: Path) -> None:
        """A non-UTF-8 byte mid-file is replaced; the rest imports."""
        sid = _CODEX_IDS[0]
        lines = _codex_lines(sid, prompts=2)
        codex(home, sid, [*lines[:3], _LATIN1_LINE, *lines[3:]])
        session = load_local_session("codex", sid)
        assert _texts(session) == ["p0", "a0", "caf�", "p1", "a1"]

    def test_codex_bad_byte_in_a_budgeted_read(self, home: Path) -> None:
        """With a small budget, bad UTF-8 gets replaced and read stops."""
        sid = _CODEX_IDS[0]
        lines = _codex_lines(sid, prompts=2)
        path = codex(home, sid, [*lines[:3], _LATIN1_LINE, *lines[3:]])
        with mock.patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size - 1):
            session = load_local_session("codex", sid)
        assert _texts(session)[:3] == ["p0", "a0", "caf�"]

    def test_claude_bad_byte_mid_file_imports_the_rest(self, home: Path) -> None:
        """Claude transcript: bad UTF-8 mid-file is replaced."""
        sid = "a1b2c3d4-1234-5678-9abc-def012345670"
        good = [
            {"type": "user", "cwd": "/w", "message": {"role": "user", "content": "first"}},
            {"type": "user", "cwd": "/w", "message": {"role": "user", "content": "second"}},
        ]
        bad = b'{"type":"custom-title","customTitle":"caf\xe9"}'
        path = home / ".claude" / "projects" / "-w" / f"{sid}.jsonl"
        jsonl(home, path, good[:1])
        with path.open("ab") as handle:
            handle.write(bad + b"\n" + json.dumps(good[1]).encode() + b"\n")
        session = load_local_session("claude", sid)
        assert _texts(session) == ["first", "second"]
        # The title scan reads the bad line too, replaced instead of raising.
        assert session.native_title == "caf�"

    def test_qwen_bad_byte_mid_file_imports_the_rest(self, home: Path) -> None:
        """Qwen: bad UTF-8 mid-file is replaced."""
        qwen(home, "q1", 2, raw=[b'{"type":"note","text":"caf\xe9"}'])
        session = load_local_session("qwen", "q1")
        assert _texts(session) == ["user 0", "assistant 0", "user 1", "assistant 1"]


class TestMalformedField:
    """List/dict role/type skips that record only."""

    def test_codex_list_role_and_dict_type_skip_one_record(self, home: Path) -> None:
        """Codex: malformed role/type fields skip only that record."""
        sid = _CODEX_IDS[0]
        lines = _codex_lines(sid, prompts=1)
        broken = [
            _rec(
                "response_item",
                {"type": "message", "role": [], "content": [{"type": "input_text", "text": "x"}]},
            ),
            _rec(
                "response_item",
                {"type": {"a": 1}, "name": "shell", "arguments": "{}", "call_id": "c"},
            ),
            _rec(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": ["input_image"]}, {"type": "input_text", "text": "kept"}],
                },
            ),
        ]
        codex(home, sid, [*lines, *broken])
        session = load_local_session("codex", sid)
        assert _texts(session) == ["p0", "a0", "kept"]

    def test_pi_list_role_skips_one_record(self, home: Path) -> None:
        """Pi: malformed role field skips only that record."""
        pi(
            home,
            "pi-1",
            2,
            extra=[
                {
                    "type": "message",
                    "id": "bad",
                    "message": {"role": [], "content": [{"type": "text", "text": "x"}]},
                }
            ],
        )
        session = load_local_session("pi", "pi-1")
        assert _texts(session) == ["pi 0", "pi 1"]


class TestReadBudget:
    """Qwen, Pi, Kiro and Kimi stop at the read budget with the notice."""

    def _assert_budgeted(
        self, home: Path, source: str, session_id: str, path: Path, prefix: str
    ) -> None:
        budget = path.stat().st_size // 4
        with mock.patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", budget):
            session = load_local_session(source, session_id)  # type: ignore[arg-type]
        assert session.later_history_omitted is True
        notice = session.items[-1].data
        assert isinstance(notice, ErrorData)
        assert notice.message.endswith("later history was left out.")
        texts = _texts(session)
        assert texts[0] == f"{prefix} 0"
        # Roughly a quarter of the file, never all of it.
        assert 0 < len(texts) < 100
        # Unbounded read of the same file for comparison.
        assert len(_texts(load_local_session(source, session_id))) >= 100  # type: ignore[arg-type]

    def test_qwen(self, home: Path) -> None:
        path = qwen(home, "q1", 100)
        self._assert_budgeted(home, "qwen", "q1", path, "user")

    def test_pi(self, home: Path) -> None:
        path = pi(home, "pi-1", 200)
        self._assert_budgeted(home, "pi", "pi-1", path, "pi")

    def test_kiro(self, home: Path) -> None:
        path = kiro(home, "k1", 200)
        self._assert_budgeted(home, "kiro", "k1", path, "kiro")

    def test_kimi(self, home: Path) -> None:
        path = kimi(home, "session_1", 200)
        self._assert_budgeted(home, "kimi", "session_1", path, "kimi")

    def test_within_budget_reads_unchanged(self, home: Path) -> None:
        kimi(home, "session_1", 3)
        session = load_local_session("kimi", "session_1")
        assert session.later_history_omitted is False
        assert _texts(session) == ["kimi 0", "kimi 1", "kimi 2"]

    def test_budget_stop_keeps_room_for_the_notice_under_the_item_cap(self, home: Path) -> None:
        path = kiro(home, "k1", 50)
        with (
            mock.patch.object(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size // 2),
            mock.patch.object(local_module, "IMPORT_MAX_ITEMS", 5),
        ):
            session = load_local_session("kiro", "k1")
        assert len(session.items) == 5
        notice = session.items[-1].data
        assert isinstance(notice, ErrorData)
        assert notice.message.endswith("later history was left out.")
