"""CLI read-fault handling: classified messages, no crash reports or raw exception text."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner, Result

from omnigent.cli import cli
from omnigent.session_import import local as local_module
from omnigent.session_import.local import OMNIGENT_SESSION_ID_HINT

_BASE = "http://localhost:6767"
_CODEX_IDS = [f"0199a5c0-0000-7abc-8def-00000000000{n}" for n in range(1, 7)]


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


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A HOME with a Codex transcript."""
    env = {
        "HOME": str(tmp_path),
        "CODEX_HOME": str(tmp_path / ".codex"),
    }
    monkeypatch.setattr("os.environ", {**os.environ, **env})
    return tmp_path


def codex(home: Path, session_id: str, lines: list[bytes]) -> Path:
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
    return path


def _run(args: list[str]) -> Result:
    """Run ``omnigent import`` with mocked server."""

    def _post(url: str, *, json: dict[str, Any], **_kwargs: Any) -> httpx.Response:
        body = {"session_id": "conv_1", "status": "imported", "item_count": len(json["items"])}
        return httpx.Response(201, json=body, request=httpx.Request("POST", url))

    with patch("omnigent.cli._resolve_attach_server", return_value=_BASE):
        with patch("httpx.post", side_effect=_post):
            return CliRunner().invoke(
                cli,
                ["import", *args],
                env={"HOME": os.environ.get("HOME", ""), "OMNIGENT_CONFIG_HOME": ""},
            )


def _assert_clean_exit(result: Result) -> None:
    assert result.exit_code == 1, result.output
    # A ClickException exit, not an escaped exception.
    assert isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output


class TestCliReadFaults:
    """CLI read faults are classified and printed cleanly."""

    def test_single_session_load_fault_is_a_classified_error(self, home: Path) -> None:
        """A single session load fault is printed as a classified error."""
        sid = _CODEX_IDS[0]
        with patch.object(
            local_module, "load_codex_session", side_effect=RuntimeError("/secret/path boom")
        ):
            result = _run(["--harness", "codex", "--session", sid])
        _assert_clean_exit(result)
        assert (
            f"Couldn't read Codex session {sid}: its transcript couldn't be parsed (RuntimeError)."
            in result.output
        )
        assert "/secret/path" not in result.output

    def test_single_session_decode_fault_names_utf8(self, home: Path) -> None:
        """A single session UTF-8 decode fault names UTF-8."""
        sid = _CODEX_IDS[0]
        fault = UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")
        with patch.object(local_module, "load_codex_session", side_effect=fault):
            result = _run(["--harness", "codex", "--session", sid])
        _assert_clean_exit(result)
        assert (
            f"Couldn't read Codex session {sid}: its transcript isn't valid UTF-8."
            in result.output
        )

    def test_batch_keeps_its_summary_when_one_session_faults(self, home: Path) -> None:
        """A batch with one fault keeps its summary line."""
        good, bad = _CODEX_IDS[0], _CODEX_IDS[1]
        codex(home, good, _codex_lines(good))
        codex(home, bad, _codex_lines(bad))
        real = local_module.load_codex_session

        def _load(session_id: str, **kwargs: Any) -> Any:
            if session_id == bad:
                raise KeyError("payload")
            return real(session_id, **kwargs)

        with patch.object(local_module, "load_codex_session", side_effect=_load):
            result = _run(["--harness", "codex", "--last", "5"])
        _assert_clean_exit(result)
        expected = (
            f"Failed {bad}: Couldn't read Codex session {bad}: "
            "its transcript couldn't be parsed (KeyError)."
        )
        assert expected in result.output
        assert "Imported: 1\n" in result.output
        assert "Failed: 1\n" in result.output

    def test_listing_fault_is_a_classified_error(self, home: Path) -> None:
        """A listing fault is printed as a classified error."""
        with patch.object(
            local_module, "_recent_local_sessions_with_recency", side_effect=RuntimeError("x")
        ):
            result = _run(["--harness", "codex", "--last", "5"])
        _assert_clean_exit(result)
        assert "Couldn't list local Codex sessions (RuntimeError)." in result.output

    def test_all_harnesses_warns_about_a_skipped_harness(self, home: Path) -> None:
        """A skipped harness is warned about, but the batch continues."""
        sid = _CODEX_IDS[0]
        codex(home, sid, _codex_lines(sid))
        real = local_module._recent_local_sessions_with_recency

        def _listing(source: str, *, limit: int) -> list[tuple[str, float]]:
            if source == "qwen":
                raise RuntimeError("broken")
            return real(source, limit=limit)  # type: ignore[arg-type]

        with patch.object(local_module, "_recent_local_sessions_with_recency", _listing):
            result = _run(["--harness", "all", "--last", "5"])
        assert result.exit_code == 0, result.output
        assert (
            "Warning: couldn't list local Qwen Code sessions (RuntimeError); they were skipped."
            in result.output
        )
        assert "Imported: 1\n" in result.output

    def test_omnigent_session_id_gets_a_hint(self, home: Path) -> None:
        """An Omnigent-shaped session id gets a hint."""
        for omnigent_id in ("0123456789abcdef0123456789abcdef", "1050142556855171"):
            with patch.object(local_module, "list_recent_local_session_ids", return_value=[]):
                result = _run(["--harness", "codex", "--session", omnigent_id])
            _assert_clean_exit(result)
            assert (
                f"Codex session {omnigent_id!r} was not found. {OMNIGENT_SESSION_ID_HINT}"
                in result.output
            )
        assert OMNIGENT_SESSION_ID_HINT == (
            "That looks like an Omnigent session id; import takes the harness's own session id."
        )

    def test_harness_session_id_gets_no_hint(self, home: Path) -> None:
        """A harness session id doesn't get the Omnigent hint."""
        result = _run(["--harness", "codex", "--session", _CODEX_IDS[0]])
        _assert_clean_exit(result)
        assert "was not found" in result.output
        assert "Omnigent session id" not in result.output
