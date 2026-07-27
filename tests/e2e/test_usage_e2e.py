"""E2E happy-path test for the ``GET /v1/usage`` report (powers ``omni usage``).

Pure HTTP — boots the live server and calls the route directly without
starting an LLM turn. Like the comments e2e, the server always runs with
``permission_store`` active, so the test creates a *real* session via
``POST /v1/sessions`` and sends ``X-Forwarded-Email`` so the server can
resolve the caller and scope the report to their sessions.

Usage::

    pytest tests/e2e/test_usage_e2e.py -v
"""

from __future__ import annotations

import io
import json
import tarfile

import httpx
import yaml

_OWNER_EMAIL = "usage-owner@e2e.test"
_AGENT_NAME = "e2e-usage-test"


def _build_minimal_agent_bundle() -> bytes:
    """Build a minimal agent bundle as an in-memory tar.gz for session create."""
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": _AGENT_NAME,
            "executor": {"type": "omnigent", "config": {"harness": "openai-agents"}},
            "llm": {"model": _AGENT_NAME, "connection": {"api_key": "test-key"}},
        }
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config)
        tf.addfile(info, io.BytesIO(config))
    return buf.getvalue()


def _create_session(client: httpx.Client, *, email: str) -> str:
    """Create a real session as *email* (granted LEVEL_OWNER) and return its id."""
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_minimal_agent_bundle(), "application/gzip")},
        headers={"X-Forwarded-Email": email},
    )
    assert resp.status_code == 201, f"Session creation failed: {resp.status_code} {resp.text}"
    return resp.json()["session_id"]


def test_usage_report_happy_path(http_client: httpx.Client) -> None:
    """The report is well-formed, windows are monotonic, and it lists the caller's session."""
    session_id = _create_session(http_client, email=_OWNER_EMAIL)

    resp = http_client.get("/v1/usage", headers={"X-Forwarded-Email": _OWNER_EMAIL})
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    report = resp.json()

    assert report["object"] == "usage_report"
    windows = [
        report["cost_today"],
        report["cost_last_7d"],
        report["cost_last_30d"],
        report["total_cost_usd"],
    ]
    assert all(isinstance(v, (int, float)) for v in windows)
    # Windows nest (today ⊆ 7d ⊆ 30d ⊆ all-time), so each is <= the next.
    assert windows == sorted(windows)

    by_id = {s["id"]: s for s in report["sessions"]}
    assert session_id in by_id, "created session missing from the usage report"
    # No turn ran, so the fresh session is priced at zero with no per-model cost.
    assert by_id[session_id]["cost_usd"] == 0.0
    assert by_id[session_id]["models"] == {}
