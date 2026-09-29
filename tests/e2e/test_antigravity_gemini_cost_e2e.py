"""Live-server regression: antigravity-native Gemini usage is priced. The reader posted
agy's tiered display name as the usage ``model``, which no catalog id matched, leaving a
null cost; agy cannot sign in from CI, so the real reader is driven directly."""

from __future__ import annotations

import asyncio
import io
import json
import signal
import subprocess
import tarfile
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import yaml

from omnigent.harnesses.antigravity_native import reader as agy_reader
from tests._helpers.live_server import HarnessCredentials, start_live_server
from tests._helpers.model_catalog import offline_catalog_isolated, seed_offline_catalog

pytestmark = offline_catalog_isolated

_OWNER_EMAIL = "antigravity-cost-owner@e2e.test"
_AGENT_NAME = "e2e-antigravity-gemini-cost"

# The priced catalog id, and agy's tiered display name the reader resolves the
# enum to.
_CATALOG_MODEL_ID = "gemini-3.8-flash"
_DISPLAY_MODEL_NAME = "Gemini 3.8 Flash (Medium)"
_AGY_MODEL_ENUM = "MODEL_PLACEHOLDER_M20"
_GEMINI_CATALOG_ENTRY: dict[str, Any] = {
    "mode": "chat",
    "context_window": {"max_input": 1_000_000, "max_output": 65_536},
    "capabilities": {"function_calling": True},
    "pricing": {
        "input_per_million_tokens": 0.30,
        "output_per_million_tokens": 2.50,
        "cache_read_per_million_tokens": 0.075,
    },
}


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


@pytest.fixture(scope="module")
def gemini_catalog_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A bare live server (no runner) pricing ``gemini-3.8-flash`` from an offline catalog."""
    root = tmp_path_factory.mktemp("agy_gemini_cost")
    server_env = seed_offline_catalog(
        root / "cache", "gemini", {_CATALOG_MODEL_ID: _GEMINI_CATALOG_ENTRY}
    )
    proc, base_url = start_live_server(
        creds=HarnessCredentials(harness="openai-agents", profile=None, llm_api_key="test-key"),
        db_path=root / "e2e.db",
        artifact_dir=root / "artifacts",
        log_path=root / "server.log",
        extra_env=server_env,
    )
    try:
        yield base_url
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _create_session(client: httpx.Client) -> str:
    """Create a real session owned by ``_OWNER_EMAIL`` and return its id."""
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_minimal_agent_bundle(), "application/gzip")},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    return resp.json()["session_id"]


def _post_native_usage(client: httpx.Client, session_id: str, *, model: str) -> None:
    """POST an ``external_session_usage`` frame directly (cumulative tokens, no cost)."""
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "model": model,
                "cumulative_input_tokens": 40_000,
                "cumulative_output_tokens": 5_000,
                "cumulative_cache_read_input_tokens": 10_000,
            },
        },
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    assert resp.status_code == 202, resp.text


def _emit_usage_via_reader(base_url: str, session_id: str) -> None:
    """Drive the real reader to post the usage event a live agy turn would emit."""
    state = agy_reader._ReaderState(
        seen=set(),
        interacted=set(),
        model_catalog={
            "models": {
                _AGY_MODEL_ENUM: {
                    "model": _AGY_MODEL_ENUM,
                    "displayName": _DISPLAY_MODEL_NAME,
                }
            }
        },
    )
    step: dict[str, Any] = {
        "type": agy_reader._TYPE_PLANNER_RESPONSE,
        "status": agy_reader._STATUS_DONE,
        "stepIndex": 2,
        "metadata": {
            "modelUsage": {
                "inputTokens": "40000",
                "outputTokens": "5000",
                "cacheReadTokens": "10000",
                "model": _AGY_MODEL_ENUM,
            }
        },
    }

    async def _drive() -> None:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Forwarded-Email": _OWNER_EMAIL},
            timeout=30,
        ) as async_client:
            await agy_reader._maybe_emit_session_usage(
                step, client=async_client, session_id=session_id, state=state
            )

    asyncio.run(_drive())


def _read_session_usage(client: httpx.Client, session_id: str) -> dict[str, Any]:
    """Read the session usage projection over the public REST surface."""
    resp = client.get(
        f"/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        headers={"X-Forwarded-Email": _OWNER_EMAIL},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["usage_included"] is True
    return body


def _recorded_tokens(usage_by_model: dict[str, Any]) -> int:
    """Total tokens recorded across every per-model bucket."""
    return sum(int(bucket.get("total_tokens", 0)) for bucket in usage_by_model.values())


def test_antigravity_gemini_display_name_usage_is_priced(
    gemini_catalog_server: str,
) -> None:
    """Usage the reader records under the tiered label prices like the catalog id."""
    with httpx.Client(base_url=gemini_catalog_server, timeout=30) as client:
        # Control: the catalog id prices, so a null below is the label, not a
        # missing catalog.
        control_id = _create_session(client)
        _post_native_usage(client, control_id, model=_CATALOG_MODEL_ID)
        control = _read_session_usage(client, control_id)
        assert control["total_cost_usd"] is not None, (
            "catalog precondition failed: even the catalog id "
            f"{_CATALOG_MODEL_ID!r} was not priced -- the offline catalog did not load"
        )
        assert control["total_cost_usd"] > 0

        # The same tokens, recorded by the reader under agy's tiered label.
        bug_id = _create_session(client)
        _emit_usage_via_reader(gemini_catalog_server, bug_id)
        bug = _read_session_usage(client, bug_id)
        buckets: dict[str, Any] = bug["usage_by_model"] or {}

        assert _recorded_tokens(buckets) > 0, "token usage was not recorded for the Gemini turn"
        assert bug["total_cost_usd"] == pytest.approx(control["total_cost_usd"]), (
            f"antigravity-native Gemini usage recorded under {_DISPLAY_MODEL_NAME!r} reports "
            f"tokens but total_cost_usd={bug['total_cost_usd']!r}; the same usage under the "
            f"catalog id {_CATALOG_MODEL_ID!r} prices to {control['total_cost_usd']}"
        )
        assert [bucket.get("total_cost_usd") for bucket in buckets.values()] == pytest.approx(
            [control["total_cost_usd"]]
        ), f"per-model usage buckets were not priced: {buckets!r}"
