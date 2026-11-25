"""Ticket history summaries: caching by last event, rate limits and the routes' gates."""

from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server import ticket_summary
from omnigent.server.ticket_summary import TicketSummarizer, render_event, render_history
from tests.server.routes.test_projects_crud import project_agents as project_agents
from tests.server.routes.test_projects_crud import project_app as project_app
from tests.server.routes.test_projects_crud import project_client as project_client


class FakeLlm:
    _model = "judge-small"

    def __init__(self, text: str = "**What was asked**\nShip it.") -> None:
        self.calls: list[dict] = []
        self.text = text

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        part = SimpleNamespace(text=self.text)
        return SimpleNamespace(output=[SimpleNamespace(content=[part])])


def _event(position: int, text: str = "hi", session_id: str | None = None) -> dict:
    return {
        "position": position,
        "kind": "comment",
        "payload": {"text": text},
        "session_id": session_id,
        "user_id": None if session_id else "alice@example.com",
    }


async def test_summary_is_cached_until_new_activity():
    llm = FakeLlm()
    engine = TicketSummarizer(llm)
    events = [_event(0, "start", "s1"), _event(1)]
    first, cached = await engine.summarize("t", events, {"s1": "Writer"}, user_key="a")
    assert not cached and first.through_position == 1 and first.model == "judge-small"
    prompt = llm.calls[0]["input"][0]["content"][0]["text"]
    assert "[0] Writer commented: start" in prompt and "alice@example.com" in prompt
    again, cached = await engine.summarize("t", events, {}, user_key="b")
    assert cached and again is first and len(llm.calls) == 1
    assert engine.cached("t", 2) is None and engine.latest("t") is first
    _, cached = await engine.summarize("t", [*events, _event(2)], {}, user_key="a")
    assert not cached and len(llm.calls) == 2


async def test_rate_limit_is_per_person_and_cache_hits_are_free(monkeypatch):
    monkeypatch.setattr(ticket_summary, "_RATE_LIMIT", 1)
    engine = TicketSummarizer(FakeLlm())
    await engine.summarize("t1", [_event(0)], {}, user_key="a")
    await engine.summarize("t1", [_event(0)], {}, user_key="a")
    with pytest.raises(OmnigentError) as limited:
        await engine.summarize("t2", [_event(0)], {}, user_key="a")
    assert limited.value.code == ErrorCode.CONFLICT
    await engine.summarize("t2", [_event(0)], {}, user_key="b")


async def test_evicting_a_summary_drops_its_lock(monkeypatch):
    monkeypatch.setattr(ticket_summary, "_CACHE_SIZE", 1)
    engine = TicketSummarizer(FakeLlm())
    await engine.summarize("t1", [_event(0)], {}, user_key="a")
    await engine.summarize("t2", [_event(0)], {}, user_key="a")
    assert engine.latest("t1") is None and set(engine._locks) == {"t2"}


async def test_unavailable_and_failing_model():
    with pytest.raises(OmnigentError) as missing:
        await TicketSummarizer(None).summarize("t", [_event(0)], {}, user_key="a")
    assert missing.value.code == ErrorCode.RUNNER_UNAVAILABLE

    class Broken(FakeLlm):
        async def create(self, **kwargs):
            raise RuntimeError("boom")

    engine = TicketSummarizer(Broken())
    with pytest.raises(OmnigentError) as failed:
        await engine.summarize("t", [_event(0)], {}, user_key="a")
    assert failed.value.code == ErrorCode.INTERNAL_ERROR and engine.latest("t") is None


def test_render_keeps_the_start_and_newest_events(monkeypatch):
    monkeypatch.setattr(ticket_summary, "_HISTORY_MAX_CHARS", 200)
    lines = [f"[{index}] someone commented: {'x' * 20}" for index in range(30)]
    text = render_history(lines)
    assert (
        text.startswith("[0]") and text.endswith(lines[-1]) and "earlier events left out" in text
    )
    moved = render_event(
        {"position": 3, "kind": "updated", "payload": {"stage": {"name": "Review"}}}, {}
    )
    assert moved == "[3] someone moved it to Review"


async def test_routes_summarize_privately_and_refuse_agents(project_client, project_app):
    project = (await project_client.post("/v1/projects", json={"name": "Shipping"})).json()
    base = f"/v1/projects/{project['id']}/tickets"
    ticket = (await project_client.post(base, json={"title": "Ship"})).json()
    path = f"{base}/{ticket['id']}/summary"
    project_app.state.ticket_summarizer = TicketSummarizer(None)
    off = (await project_client.get(path)).json()
    assert off == {
        "object": "project.ticket.summary",
        "available": False,
        "summary": None,
        "stale": False,
    }
    assert (await project_client.post(path)).status_code == 503

    llm = FakeLlm()
    project_app.state.ticket_summarizer = TicketSummarizer(llm)
    made = (await project_client.post(path)).json()
    assert made["summary"]["text"].startswith("**What was asked**")
    assert made["summary"]["cached"] is False
    assert (await project_client.post(path)).json()["summary"]["cached"] is True
    assert "Ship" in llm.calls[0]["input"][0]["content"][0]["text"]
    # Nothing was posted to the ticket.
    events = (await project_client.get(f"{base}/{ticket['id']}/events")).json()["data"]
    assert [event["kind"] for event in events] == ["created"]
    assert (await project_client.get(path)).json()["stale"] is False
    await project_client.post(f"{base}/{ticket['id']}/comments", json={"text": "More"})
    assert (await project_client.get(path)).json()["stale"] is True

    agent = {ORCHESTRATION_SOURCE_HEADER: "session"}
    assert (await project_client.post(path, headers=agent)).status_code == 403
    assert (await project_client.get(path, headers=agent)).status_code == 403
