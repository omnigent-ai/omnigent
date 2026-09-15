"""A snapshot refresh must not empty the composer's slash-command menu.

The web chat loads a session with ``?refresh_state=true`` so a reload re-reads
live capabilities instead of serving stale caches. Runner-derived skills were
*dropped* on that path, and the snapshot's skill read is deliberately
non-blocking — it kicks a background fetch and returns what it has. So the
refresh emptied the cache and then answered its own request with ``[]``, and
the menu the response feeds had nothing in it but the built-ins.

The native model catalog in the same invalidator already avoids this by
marking stale and serving the previous value; these tests hold skills to the
same contract.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from omnigent.server.routes._sessions.common import (
    _runner_skills_cache,
    _runner_skills_failed,
    _runner_skills_inflight,
    _runner_skills_stale,
)
from omnigent.server.routes._sessions.helpers import (
    _invalidate_runner_backed_snapshot_state,
    _load_runner_skills,
)
from omnigent.server.routes._sessions.orchestration import (
    _fetch_runner_skills,
    _runner_skills_status,
)
from omnigent.server.schemas import SkillSummary

SESSION = "conv_refresh_probe"


class _StubRunner:
    """Minimal stand-in for the runner's httpx client.

    Only ``get`` is exercised; the loader treats anything non-200 or
    unparseable as a miss, which is what the failure cases below need.
    """

    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self._status = status
        self.calls = 0

    async def get(self, url: str, timeout: float = 5.0) -> object:
        self.calls += 1
        payload, status = self._payload, self._status

        class _Resp:
            status_code = status

            def json(self) -> object:
                return payload

        return _Resp()


@pytest.fixture(autouse=True)
def _clean_caches() -> object:
    """Keep this module's writes out of the process-wide caches."""
    for store in (_runner_skills_cache, _runner_skills_inflight):
        store.pop(SESSION, None)
    _runner_skills_stale.discard(SESSION)
    _runner_skills_failed.discard(SESSION)
    yield
    for store in (_runner_skills_cache, _runner_skills_inflight):
        store.pop(SESSION, None)
    _runner_skills_stale.discard(SESSION)
    _runner_skills_failed.discard(SESSION)


def _seed(*names: str) -> None:
    """Put *names* in the runner-skills cache as a warm entry."""
    _runner_skills_cache[SESSION] = [SkillSummary(name=n, description=f"{n} desc") for n in names]


def test_a_browser_refresh_keeps_the_skills_it_asked_to_refresh() -> None:
    """The reload's own response still carries the menu.

    ``cancel_inflight=False`` is the browser-refresh call. Dropping the entry
    here is what emptied the menu: the snapshot being built for this very
    request reads the cache immediately afterwards.
    """
    _seed("code-review", "figma:figma-use")

    _invalidate_runner_backed_snapshot_state(
        SESSION, cancel_inflight=False, drop_model_options=True
    )

    assert [s.name for s in _runner_skills_cache[SESSION]] == ["code-review", "figma:figma-use"]
    assert SESSION in _runner_skills_stale


def test_runner_teardown_still_drops_them() -> None:
    """Skills belong to the runner that went away.

    ``cancel_inflight=True`` is the disconnect path, where serving the old
    list would advertise commands nothing can resolve.
    """
    _seed("code-review")

    _invalidate_runner_backed_snapshot_state(
        SESSION, cancel_inflight=True, drop_model_options=True
    )

    assert SESSION not in _runner_skills_cache
    assert SESSION not in _runner_skills_stale


async def test_a_real_refill_clears_the_stale_mark() -> None:
    """Otherwise every later snapshot re-fetches forever.

    Driven through ``_load_runner_skills`` rather than by writing the cache
    here: a test that performs the production write itself cannot notice the
    production code dropping it.
    """
    _seed("code-review")
    _invalidate_runner_backed_snapshot_state(
        SESSION, cancel_inflight=False, drop_model_options=True
    )
    assert SESSION in _runner_skills_stale

    runner = _StubRunner({"skills": [{"name": "code-review", "description": "fresh"}]})
    await _load_runner_skills(cast(Any, runner), SESSION)

    assert SESSION not in _runner_skills_stale
    assert [s.description for s in _runner_skills_cache[SESSION]] == ["fresh"]


async def test_a_failed_refill_keeps_serving_and_keeps_retrying() -> None:
    """A runner that cannot answer must not empty the menu.

    The mark stays set so a later poll tries again — the same ceiling a cold
    cache has always had — while the previous list keeps serving.
    """
    _seed("code-review")
    _invalidate_runner_backed_snapshot_state(
        SESSION, cancel_inflight=False, drop_model_options=True
    )

    await _load_runner_skills(cast(Any, _StubRunner({"skills": []}, status=503)), SESSION)

    assert SESSION in _runner_skills_stale
    assert [s.name for s in _runner_skills_cache[SESSION]] == ["code-review"]


async def test_the_snapshot_serves_stale_skills_and_starts_one_refetch() -> None:
    """The read half: a refreshing snapshot answers with the list it has.

    This is the behaviour the browser depends on — the response to the
    request that asked for the refresh is the one that fills the menu — and
    it must also register exactly one in-flight fetch rather than firing per
    poll.
    """
    _seed("code-review", "figma:figma-use")
    _invalidate_runner_backed_snapshot_state(
        SESSION, cancel_inflight=False, drop_model_options=True
    )
    runner = _StubRunner({"skills": [{"name": "code-review", "description": "fresh"}]})

    served = await _fetch_runner_skills(cast(Any, runner), SESSION)

    assert [s.name for s in served] == ["code-review", "figma:figma-use"]
    inflight = _runner_skills_inflight.get(SESSION)
    assert inflight is not None
    await inflight
    assert SESSION not in _runner_skills_stale


async def test_a_session_with_no_cached_skills_is_not_marked() -> None:
    """Nothing to serve means nothing to remember.

    Every cold session a browser opens would otherwise leave an id in the
    stale set for the life of the process.
    """
    _invalidate_runner_backed_snapshot_state(
        SESSION, cancel_inflight=False, drop_model_options=True
    )

    assert SESSION not in _runner_skills_stale


@pytest.mark.parametrize("skills", [[], [{"name": "review", "description": "Review code"}]])
async def test_loading_settles_even_when_no_skills_are_found(skills: list[dict[str, str]]) -> None:
    runner = cast(Any, _StubRunner({"skills": skills}))
    assert await _fetch_runner_skills(runner, SESSION) == []
    assert _runner_skills_status(runner, SESSION) == "loading"

    await _runner_skills_inflight[SESSION]

    assert _runner_skills_status(runner, SESSION) == "ready"
    assert [s.model_dump() for s in await _fetch_runner_skills(runner, SESSION)] == skills
    assert runner.calls == 1


@pytest.mark.parametrize("payload", [{}, {"skills": None}, {"skills": {}}, {"skills": [{}]}])
async def test_failed_discovery_notifies_once_and_can_recover(
    payload: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes import sessions

    published: list[dict[str, object]] = []

    class RecordingStream:
        @staticmethod
        def publish(session_id: str, event: dict[str, object]) -> None:
            published.append(event)

    monkeypatch.setattr(sessions, "session_stream", RecordingStream)
    runner = cast(Any, _StubRunner(payload))
    await _load_runner_skills(runner, SESSION)
    assert _runner_skills_status(runner, SESSION) == "error"
    assert [event["type"] for event in published] == ["session.skills"]

    # Reading the snapshot after the failure event must not create a nudge loop.
    await _fetch_runner_skills(runner, SESSION)
    await _runner_skills_inflight[SESSION]
    assert len(published) == 1

    await _load_runner_skills(cast(Any, _StubRunner({"skills": []})), SESSION)
    assert _runner_skills_status(runner, SESSION) == "ready"
    assert len(published) == 2


async def test_transport_failure_stops_loading_and_refresh_clears_error() -> None:
    class UnreachableRunner:
        async def get(self, *args: object, **kwargs: object) -> None:
            raise httpx.ReadTimeout("runner timed out")

    runner = cast(Any, UnreachableRunner())
    await _load_runner_skills(runner, SESSION)
    assert _runner_skills_status(runner, SESSION) == "error"

    _invalidate_runner_backed_snapshot_state(
        SESSION,
        cancel_inflight=False,
        drop_model_options=False,
    )
    assert _runner_skills_status(runner, SESSION) == "loading"
    assert _runner_skills_status(None, SESSION) == "unavailable"
