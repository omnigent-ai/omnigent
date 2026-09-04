"""Credential-free checks for the opt-in native compaction canary helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e._native_compaction_resume_canary import (
    ArtifactQuarantine,
    canary_cases,
    claude_compact_record,
    codex_compact_record,
    configured_session_count,
)
from tests.e2e._native_resume_helpers import _CONV_ID_RE


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_canary_cases_default_to_three_and_cover_both_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_NATIVE_CANARY_COUNT", raising=False)
    assert canary_cases("TEST_NATIVE_CANARY_COUNT") == [
        (0, "local-artifact"),
        (1, "server-reconstruction"),
        (2, "local-artifact"),
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http://127.0.0.1:56850/c/d0dcbb91ba9249b6b4d180c2a065e73e",
            "d0dcbb91ba9249b6b4d180c2a065e73e",
        ),
        (
            "http://127.0.0.1:56850/c/conv_25cf39e3b0ea4d0c8721277215",
            "conv_25cf39e3b0ea4d0c8721277215",
        ),
    ],
)
def test_conversation_id_parser_accepts_current_and_legacy_ids(url: str, expected: str) -> None:
    match = _CONV_ID_RE.search(url)
    assert match is not None
    assert match.group(1) == expected


@pytest.mark.parametrize("value", ["0", "2", "not-an-int"])
def test_configured_session_count_rejects_invalid_or_too_small_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TEST_NATIVE_CANARY_COUNT", value)
    with pytest.raises(ValueError, match="TEST_NATIVE_CANARY_COUNT"):
        configured_session_count("TEST_NATIVE_CANARY_COUNT")


def test_native_compaction_parsers_require_marker_retention(tmp_path: Path) -> None:
    marker = "OMNI-MARKER-ABC"
    claude_path = tmp_path / "claude.jsonl"
    _write_jsonl(
        claude_path,
        [
            {"type": "user", "message": {"content": marker}},
            {
                "type": "user",
                "isCompactSummary": True,
                "message": {"content": f"Retain {marker}"},
            },
        ],
    )
    assert claude_compact_record(claude_path, marker) is not None
    assert claude_compact_record(claude_path, "MISSING") is None

    codex_path = tmp_path / "rollout.jsonl"
    replacement = [{"type": "message", "role": "user", "content": marker}]
    _write_jsonl(
        codex_path,
        [{"type": "compacted", "payload": {"replacement_history": replacement}}],
    )
    record = codex_compact_record(codex_path, marker)
    assert record is not None
    payload = record["payload"]
    assert isinstance(payload, dict)
    assert payload["replacement_history"] == replacement


def test_artifact_quarantine_restores_original_and_preserves_replacement(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "native" / "session.jsonl"
    artifact.parent.mkdir()
    artifact.write_text("original\n")
    quarantine = ArtifactQuarantine(tmp_path / "quarantine")

    quarantine.hide([artifact])
    assert not artifact.exists()
    artifact.write_text("reconstructed\n")
    quarantine.restore()

    assert artifact.read_text() == "original\n"
    reconstructed = list((quarantine.root / "reconstructed").iterdir())
    assert len(reconstructed) == 1
    assert reconstructed[0].read_text() == "reconstructed\n"
