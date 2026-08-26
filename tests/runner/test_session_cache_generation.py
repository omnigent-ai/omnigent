"""Regression: spec-fill generation guard prevents ABA repopulation.

When a spec fill is parked (awaiting the spec resolver) across a
``delete_session`` + same-id recreate, it must NOT write its stale result
into the new session's cache.  The fix increments (never pops)
``_session_cache_generations`` on every delete so that a fill that captured
generation N before the delete is rejected by the N+1 current value.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient

_AGENT_ID = "ag_test_generation"


class _AgentSnapshotServerClient(NullServerClient):
    """Server client whose session GET returns a valid agent_id."""

    def __init__(self, session_id: str, agent_id: str) -> None:
        self._session_id = session_id
        self._agent_id = agent_id

    class _SessionResp:
        status_code = 200

        def __init__(self, agent_id: str) -> None:
            self._payload = {"agent_id": agent_id, "created_at": 0}
            self.text = json.dumps(self._payload)

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            pass

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith(self._session_id):
            return self._SessionResp(self._agent_id)
        return self._Response()


@pytest.mark.asyncio
async def test_spec_fill_parked_across_delete_discards(tmp_path: Path) -> None:
    """A spec fill that starts before delete must not repopulate the cache.

    Sequence:
    1. Terminal launch triggers ``_resolve_session_spec_entry`` → spec
       resolver parks (generation captured = 0).
    2. ``DELETE /v1/sessions/{id}`` increments generation to 1.
    3. Spec resolver unparks → generation check: 1 ≠ 0 → discard.
    4. Second terminal launch triggers a fresh fill → resolver called again.

    If the generation guard were absent or broken (pop+reset to 0), the stale
    fill from step 1 would write its result and step 4 would NOT invoke the
    resolver a second time.
    """
    session_id = "conv_test_generation_aba"

    resolver_started = asyncio.Event()
    resolver_gate = asyncio.Event()
    resolve_count = 0

    async def _pausable_resolver(agent_id: str, sid: str | None = None) -> AgentSpec:
        nonlocal resolve_count
        resolve_count += 1
        resolver_started.set()
        await resolver_gate.wait()
        return AgentSpec(
            spec_version=1,
            name=agent_id,
            executor=ExecutorSpec(type="omnigent"),
        )

    app = create_runner_app(
        server_client=_AgentSnapshotServerClient(session_id, _AGENT_ID),  # type: ignore[arg-type]
        spec_resolver=_pausable_resolver,
    )

    async with _runner_client(app) as client:
        # Phase 1: start a terminal launch that parks in the spec resolver.
        launch_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "bash", "session_key": "main"},
            )
        )
        await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

        # Phase 2: delete the session — increments the generation counter.
        del_resp = await client.delete(f"/v1/sessions/{session_id}")
        assert del_resp.status_code == 200

        # Phase 3: release the resolver — the fill completes and must discard
        # its result because the generation no longer matches.
        resolver_gate.set()
        await asyncio.wait_for(launch_task, timeout=5.0)

        count_after_stale_fill = resolve_count
        assert count_after_stale_fill >= 1, "resolver should have been called at least once"

        # Phase 4: trigger a second spec fill for the same session id.
        # If the stale fill had populated the cache, the resolver would NOT
        # be called again.  A correctly guarded fill leaves the cache empty,
        # so the next fill calls the resolver a second time.
        resolver_started.clear()
        resolver_gate.clear()

        launch_task2 = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "bash", "session_key": "main"},
            )
        )

        # Wait for the resolver to start again (proves a fresh fill began).
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)
        except TimeoutError:
            pytest.fail(
                "spec resolver was not called a second time — stale fill "
                "from before deletion may have repopulated the cache (ABA)"
            )

        resolver_gate.set()
        await asyncio.wait_for(launch_task2, timeout=5.0)

        assert resolve_count > count_after_stale_fill, (
            "resolver was not called after delete: stale fill may have "
            "populated the spec cache across the session boundary"
        )
