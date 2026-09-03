from __future__ import annotations

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.e2e_ui import playwright_evidence
from tests.e2e_ui.playwright_evidence import ContextMetadata, redact_text, redact_url


def test_browser_metadata_redacts_secrets_paths_queries_and_inline_bodies(
    tmp_path: Path,
) -> None:
    assert (
        redact_url("https://user:password@example.test/v1/items?token=secret#fragment")
        == "https://example.test/v1/items"
    )
    assert redact_url("data:text/plain,secret-body") == "data:[redacted]"
    redacted = redact_text(
        "Bearer abc.def.ghi api_key=super-secret "
        "https://example.test/path?token=query /Users/alice/private/file.txt"
    )
    assert "abc.def.ghi" not in redacted
    assert "super-secret" not in redacted
    assert "token=query" not in redacted
    assert "/Users/alice" not in redacted

    metadata = ContextMetadata("tests/test_contract.py::test_it", "direct-sync", tmp_path)
    metadata.add(
        "request",
        method="GET",
        resource_type="fetch",
        url=redact_url("https://example.test/v1/items?token=secret"),
    )
    metadata.write()
    payload = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert all("headers" not in event and "body" not in event for event in payload["events"])
    assert "token=secret" not in serialized


def test_url_segments_and_parametrized_nodeids_are_digest_redacted(tmp_path: Path) -> None:
    token = "sk-" + "A" * 48
    redacted_url = redact_url(f"https://example.test/sessions/{token}/messages")
    assert token not in redacted_url
    assert "<redacted-" in redacted_url

    metadata = ContextMetadata(
        f"tests/test_secret.py::test_case[token={token}]",
        "direct-sync",
        tmp_path,
    )
    metadata.write()
    payload = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert token not in payload["nodeid"]
    assert len(payload["nodeid_sha256"]) == 64


def test_concurrent_browser_launches_allocate_unique_evidence_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(playwright_evidence.VERIFY_RUN_DIR_ENV, str(tmp_path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(
            pool.map(
                lambda _index: playwright_evidence._evidence_context_dir(
                    "test_same_node",
                    1,
                ),
                range(32),
            )
        )

    assert all(path is not None and path.is_dir() for path in paths)
    assert len({path.name for path in paths if path is not None}) == 32


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return output.getvalue()


@pytest.mark.parametrize(
    ("limits", "payload", "message"),
    [
        (
            {"MAX_TRACE_ARCHIVE_BYTES": 1},
            _zip_bytes({"trace.trace": b"x"}),
            "top-level size limit",
        ),
        (
            {"MAX_TRACE_MEMBER_BYTES": 8},
            _zip_bytes({"trace.trace": b"x" * 9}),
            "member exceeds limit",
        ),
        (
            {"MAX_TRACE_MEMBERS": 2},
            _zip_bytes({"a": b"1", "b": b"2", "c": b"3"}),
            "member count exceeds limit",
        ),
        (
            {"MAX_TRACE_EXPANDED_BYTES": 5},
            _zip_bytes({"a": b"123", "b": b"456"}),
            "expanded bytes exceed limit",
        ),
        (
            {"MAX_TRACE_ARCHIVE_DEPTH": 1},
            _zip_bytes({"nested.zip": _zip_bytes({"trace.trace": b"nested"})}),
            "nesting exceeds limit",
        ),
    ],
)
def test_trace_sanitization_rejects_bounded_archive_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: dict[str, int],
    payload: bytes,
    message: str,
) -> None:
    trace = tmp_path / "trace.zip"
    trace.write_bytes(payload)
    for name, value in limits.items():
        monkeypatch.setattr(playwright_evidence, name, value)

    with pytest.raises(ValueError, match=message):
        playwright_evidence._sanitize_trace_archive(trace)

    assert trace.read_bytes() == payload
    assert not list(tmp_path.glob(".trace.zip.*.tmp"))
