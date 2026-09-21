"""Live-server coverage for usage reports and deferred session usage reads.

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
from typing import Any

import httpx
import pytest
import yaml

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder

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


def _create_child_session(client: httpx.Client, parent_id: str, name: str) -> str:
    """Create a real sub-agent row through the native event-ingestion route."""
    response = client.post(
        f"/v1/sessions/{parent_id}/events",
        json={
            "type": "external_acp_subagent_start",
            "data": {"subagent_id": name, "title": name},
        },
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    assert response.status_code == 202, response.text
    return response.json()["child_session_id"]


def _post_native_usage(
    client: httpx.Client, session_id: str, *, model: str, cost: float | None
) -> None:
    """Persist a native usage frame without starting an LLM turn."""
    data: dict[str, Any] = {
        "model": model,
        "cumulative_input_tokens": 100,
        "cumulative_output_tokens": 20,
        "cumulative_cache_read_input_tokens": 25,
    }
    if cost is not None:
        data["cumulative_cost_usd"] = cost
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_usage", "data": data},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    assert response.status_code == 202, response.text


def _model_usage(cost: float | None, *, sessions: int = 1) -> dict[str, int | float | None]:
    """Expected aggregation of the token counts posted by ``_post_native_usage``."""
    return {
        "input_tokens": 75 * sessions,
        "output_tokens": 20 * sessions,
        "total_tokens": 120 * sessions,
        "cache_read_input_tokens": 25 * sessions,
        "cache_creation_input_tokens": None,
        "total_cost_usd": cost,
    }


def _assert_session_usage_responses(
    client: httpx.Client,
    session_id: str,
    *,
    cost: float | None,
    by_model: dict[str, dict[str, int | float | None]],
) -> None:
    """Default reads retain usage; opt-outs omit it without changing stored spend."""
    path = f"/v1/sessions/{session_id}"
    headers = {"X-Forwarded-Email": _OWNER_EMAIL}
    for method in ("GET", "PATCH"):
        body = {"title": "Usage projection verified"} if method == "PATCH" else None
        params = {"include_usage": "false"}
        if method == "GET":
            params.update({"include_items": "false", "include_liveness": "false"})
        slim = client.request(method, path, params=params, headers=headers, json=body)
        assert slim.status_code == 200, slim.text
        assert slim.json()["id"] == session_id
        assert slim.json()["usage_included"] is False
        assert slim.json()["total_cost_usd"] is None
        assert slim.json()["usage_by_model"] is None
        if method == "PATCH":
            assert slim.json()["title"] == "Usage projection verified"

        full = client.request(method, path, headers=headers, json=body)
        assert full.status_code == 200, full.text
        assert full.json()["usage_included"] is True
        assert full.json()["total_cost_usd"] == cost
        assert full.json()["usage_by_model"] == by_model

    usage = client.get(f"{path}/usage", headers=headers)
    assert usage.status_code == 200, usage.text
    assert usage.json() == {"id": session_id, "total_cost_usd": cost, "usage_by_model": by_model}
    assert usage.headers["Cache-Control"] == "no-store"


@pytest.mark.compat_smoke
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


@pytest.mark.compat_smoke
def test_codex_effective_context_window_reaches_session_snapshot(
    http_client: httpx.Client,
) -> None:
    """A Codex usage frame persists its effective window through the live server."""
    session_id = _create_session(http_client, email=_OWNER_EMAIL)
    usage_data = codex_native_forwarder._session_usage_data_from_params(
        {
            "tokenUsage": {
                "modelContextWindow": 258_400,
                "total": {
                    "inputTokens": 206_533,
                    "outputTokens": 1_000,
                    "contextWindow": 1_050_000,
                },
                "last": {"inputTokens": 206_533},
            },
        }
    )
    assert usage_data is not None

    resp = http_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_usage", "data": usage_data},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    assert resp.status_code == 202, resp.text

    for params in (
        {},
        {"include_items": "false", "include_liveness": "false", "include_usage": "false"},
    ):
        snapshot = http_client.get(
            f"/v1/sessions/{session_id}",
            params=params,
            headers={"X-Forwarded-Email": _OWNER_EMAIL},
        )
        snapshot.raise_for_status()
        assert snapshot.json()["last_total_tokens"] == 206_533
        assert snapshot.json()["context_window"] == 258_400


@pytest.mark.min_server_version("0.15.0")
def test_session_usage_reads_preserve_archived_subtree_totals(http_client: httpx.Client) -> None:
    """Stored native usage reaches every read surface, including archived descendants."""
    parent_id = _create_session(http_client, email=_OWNER_EMAIL)
    child_id = _create_child_session(http_client, parent_id, "usage-child")
    grandchild_id = _create_child_session(http_client, child_id, "usage-grandchild")
    sibling_id = _create_child_session(http_client, parent_id, "usage-sibling")
    for session_id, model, cost in (
        (parent_id, "usage-model-a", 1.0),
        (child_id, "usage-model-a", 2.5),
        (grandchild_id, "usage-model-b", 0.25),
        (sibling_id, "usage-model-b", 4.0),
    ):
        _post_native_usage(http_client, session_id, model=model, cost=cost)

    archived = http_client.patch(
        f"/v1/sessions/{grandchild_id}",
        params={"include_usage": "false"},
        json={"archived": True},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    assert archived.json()["usage_included"] is False

    _assert_session_usage_responses(
        http_client,
        parent_id,
        cost=7.75,
        by_model={
            "usage-model-a": _model_usage(3.5, sessions=2),
            "usage-model-b": _model_usage(4.25, sessions=2),
        },
    )
    _assert_session_usage_responses(
        http_client,
        child_id,
        cost=2.75,
        by_model={"usage-model-a": _model_usage(2.5), "usage-model-b": _model_usage(0.25)},
    )


@pytest.mark.parametrize("cost", [None, 0.0], ids=["unpriced", "priced-zero"])
@pytest.mark.min_server_version("0.15.0")
def test_session_usage_reads_distinguish_unpriced_tokens_from_zero(
    http_client: httpx.Client, cost: float | None
) -> None:
    """Unpriced token counts remain unknown, while an explicit zero stays priced."""
    session_id = _create_session(http_client, email=_OWNER_EMAIL)
    model = "e2e-unpriced-usage-model"
    _post_native_usage(http_client, session_id, model=model, cost=cost)
    _assert_session_usage_responses(
        http_client, session_id, cost=cost, by_model={model: _model_usage(cost)}
    )
