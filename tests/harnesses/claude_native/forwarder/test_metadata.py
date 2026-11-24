"""Metadata tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    BtwOverlay,
    ClaudeTranscriptItem,
    record_hook_event,
    write_tmux_target,
)
from omnigent.util.reasoning_effort import CLAUDE_EFFORTS, EFFORT_CLEAR_VALUES
from tests.harnesses.claude_native.forwarder._support import (
    _CapturedRequest,
)


@pytest.mark.asyncio
async def test_forward_model_from_status_posts_the_status_model_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The statusLine's model posts VERBATIM — the harness's own spelling,
    never collapsed to a picker alias — and dedupes on repeat polls.

    A family collapse here is how a routed Opus 4.9 rendered as the
    ``opus`` row holding 4.8; the verbatim report is what makes the web's
    exact-match highlight truthful for every generation and provider
    spelling.
    """
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge_dir: {"model": "databricks-claude-opus-4-9", "context_window_size": 200000},
    )
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    dedupe = forwarder._ForwardDedupeState()
    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await forwarder._forward_model_from_status(
            client, session_id="conv_abc", bridge_dir=tmp_path, dedupe=dedupe
        )
        await forwarder._forward_model_from_status(
            client, session_id="conv_abc", bridge_dir=tmp_path, dedupe=dedupe
        )

    model_posts = [r for r in requests if r["type"] == "external_model_change"]
    assert model_posts == [
        {"type": "external_model_change", "data": {"model": "databricks-claude-opus-4-9"}}
    ]
    assert dedupe.posted_model == "databricks-claude-opus-4-9"


@pytest.mark.asyncio
async def test_model_reports_keep_generation_and_context_marker(tmp_path: Path) -> None:
    """
    Reports preserve the generation and the ``[1m]`` marker byte-for-byte.

    Two same-family models of different generations (a routed 4.9 beside a
    pinned 4.8) and a 1M-context variant must each post as themselves —
    any normalization would let the record claim a model the pane is not
    on.
    """
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    dedupe = forwarder._ForwardDedupeState()
    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for model in (
            "<synthetic>",
            "databricks-claude-opus-4-8",
            "<synthetic>",
            "databricks-claude-opus-4-9",
            "databricks-claude-opus-4-9[1m]",
            " <synthetic> ",
        ):
            await forwarder._post_model_change_if_new(
                client, session_id="conv_abc", dedupe=dedupe, model=model
            )

    assert [r["data"]["model"] for r in requests] == [
        "databricks-claude-opus-4-8",
        "databricks-claude-opus-4-9",
        "databricks-claude-opus-4-9[1m]",
    ]
    assert dedupe.observed_model == "databricks-claude-opus-4-9[1m]"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_model", [None, "system.ai.claude-opus-4-8[1m]"])
async def test_forwarder_reports_the_launch_model_then_a_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_model: str | None
) -> None:
    """
    EVERY observation posts, verbatim: the first is the launch report.

    The first assistant entry names the model the session spawned on —
    posting it is what seeds ``reported_model`` so surfaces show the
    pane's truth within seconds of launch — and a later assistant entry
    on a different model posts that new model, byte-for-byte.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    status = {"model": status_model}
    monkeypatch.setattr(forwarder, "read_claude_context_state", lambda _: status)

    def _assistant(uuid: str, model: str, text: str) -> str:
        """
        Build one assistant JSONL line carrying ``message.model``.

        :param uuid: Transcript entry uuid, e.g. ``"a1"``.
        :param model: Concrete model id, e.g. ``"claude-opus-4-8"``.
        :param text: Assistant text content.
        :returns: A JSON-encoded transcript line.
        """
        return json.dumps(
            {
                "type": "assistant",
                "uuid": uuid,
                "message": {
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": text}],
                },
            }
        )

    transcript_path.write_text(_assistant("a1", "claude-opus-4-8", "hi") + "\n", encoding="utf-8")
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker()
    dedupe = forwarder._ForwardDedupeState()

    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Accept every forwarder POST and record its payload.

        :param request: Outbound HTTP request from the forwarder.
        :returns: 202 for every event.
        """
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        # The first observation IS the launch report — posted verbatim.
        launch_posts = [r for r in requests if r["type"] == "external_model_change"]
        expected_model = status_model or "claude-opus-4-8"
        assert [p["data"] for p in launch_posts] == [{"model": expected_model}]
        assert dedupe.posted_model == expected_model

        # User switches model inside the terminal.
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(_assistant("a2", "claude-sonnet-5", "switched") + "\n")
        status["model"] = "claude-sonnet-5" if status_model else None
        requests.clear()
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    model_posts = [r for r in requests if r["type"] == "external_model_change"]
    assert len(model_posts) == 1
    assert model_posts[0]["data"] == {"model": "claude-sonnet-5"}
    assert dedupe.posted_model == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_forwarder_mirrors_tui_rename_on_first_observation(tmp_path: Path) -> None:
    """
    A ``/rename`` posts ``external_session_title`` on the FIRST observation.

    Unlike the model mirror there is no spawn default to protect: a
    ``custom-title`` record exists only because the operator renamed the
    session, so it is a real change worth posting immediately.

    The second phase rewinds the byte cursor so the same ``custom-title``
    record is read again — the restart / rewind path the dedupe exists
    for. A steady-state poll reads only past its cursor and would never
    re-see the record, so rewinding is what actually exercises the guard.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "custom-title", "customTitle": "auth-refactor", "sessionId": "s1"})
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker()
    dedupe = forwarder._ForwardDedupeState()

    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Accept every forwarder POST and record its payload.

        :param request: Outbound HTTP request from the forwarder.
        :returns: 202 for every event.
        """
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        title_posts = [r for r in requests if r["type"] == "external_session_title"]
        assert len(title_posts) == 1
        assert title_posts[0]["data"] == {"title": "auth-refactor"}
        assert dedupe.posted_title == "auth-refactor"

        # Rewind to the top of the file so the rename record is re-read,
        # as a restart / cursor rewind would. The dedupe must swallow it.
        requests.clear()
        rewound = forwarder.TranscriptForwardState(
            transcript_path=transcript_path,
            line_cursor=0,
            byte_offset=0,
            cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
        )
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=rewound,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    assert [r for r in requests if r["type"] == "external_session_title"] == []


@pytest.mark.asyncio
async def test_forwarder_retries_title_post_after_transient_failure(tmp_path: Path) -> None:
    """
    A failed ``external_session_title`` POST is retried on a later poll.

    ``observed_title`` is sticky across polls, so a poll whose incremental
    window carries no ``custom-title`` record still reconciles the observed
    title against the last POSTed one — the rename is not lost once the
    original poll's window is gone.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "custom-title", "customTitle": "auth-refactor", "sessionId": "s1"})
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker()
    dedupe = forwarder._ForwardDedupeState()

    fail_titles = True
    title_posts: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Fail title posts while ``fail_titles`` is set; accept everything else.

        :param request: Outbound HTTP request from the forwarder.
        :returns: 500 for the first title post, else 202.
        """
        payload = json.loads(request.content.decode("utf-8"))
        if payload["type"] == "external_session_title":
            title_posts.append(payload)
            if fail_titles:
                return httpx.Response(500, json={})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        # The POST failed, so the baseline stays behind the observation.
        assert len(title_posts) == 1
        assert dedupe.observed_title == "auth-refactor"
        assert dedupe.posted_title is None

        # Next poll: no new rename in the window, but the retry still fires.
        fail_titles = False
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}}
                )
                + "\n"
            )
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    assert len(title_posts) == 2
    assert dedupe.posted_title == "auth-refactor"


@pytest.mark.asyncio
async def test_forwarder_retries_model_post_after_transient_failure(tmp_path: Path) -> None:
    """
    A failed ``external_model_change`` POST is retried on a later poll —
    not lost once the switch poll's transcript window is gone.

    ``observed_model`` is sticky across polls, so even a poll whose
    incremental window carries no fresh ``message.model`` (e.g. a plain
    user turn) reconciles the observed alias against the last POSTed one
    and re-attempts the drop. Guards the self-healing contract of the
    model mirror against a single transient Omnigent error.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"

    def _assistant(uuid: str, model: str) -> str:
        """Build an assistant JSONL line carrying ``message.model``."""
        return json.dumps(
            {
                "type": "assistant",
                "uuid": uuid,
                "message": {
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "x"}],
                },
            }
        )

    def _user(uuid: str) -> str:
        """Build a user JSONL line (no ``message.model``)."""
        return json.dumps(
            {"type": "user", "uuid": uuid, "message": {"role": "user", "content": "thanks"}}
        )

    transcript_path.write_text(_assistant("a1", "claude-opus-4-8") + "\n", encoding="utf-8")
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker()
    dedupe = forwarder._ForwardDedupeState()

    model_posts: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Fail the FIRST external_model_change POST (503); accept the rest."""
        body = json.loads(request.content.decode("utf-8"))
        if body["type"] == "external_model_change":
            model_posts.append(body["data"])
            if len(model_posts) == 1:
                return httpx.Response(503, json={"error": "transient"})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def _poll() -> None:
            nonlocal state
            state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=retry_tracker,
                dedupe=dedupe,
            )

        # Poll 1: the launch report is attempted and fails transiently.
        await _poll()
        assert model_posts == [{"model": "claude-opus-4-8"}]
        assert dedupe.posted_model is None  # NOT advanced — POST failed
        assert dedupe.observed_model == "claude-opus-4-8"  # but remembered

        # Poll 2: a plain user turn (no message.model) still retries the drop.
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(_user("u1") + "\n")
        await _poll()
        assert model_posts == [{"model": "claude-opus-4-8"}, {"model": "claude-opus-4-8"}]
        assert dedupe.posted_model == "claude-opus-4-8"  # now committed

        # Poll 3: a TUI switch posts the new model verbatim.
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(_assistant("a2", "claude-sonnet-5") + "\n")
        await _poll()
        assert model_posts[-1] == {"model": "claude-sonnet-5"}
        assert dedupe.posted_model == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_relay_permission_mode_mirrors_switch_and_dedupes() -> None:
    """
    Both the launch mode and a later shift+tab reach the session label.

    Claude Code emits no event on a mode change, so the pane footer is
    mirrored. The launch mode is posted as well: a manual-mode session has no
    mode label and no launch flag, so skipping it would leave the web picker
    with nothing to render. An unchanged footer stays quiet, and a footerless
    (``None``) read must not post a reversal.
    """
    dedupe = forwarder._ForwardDedupeState()
    posts: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Accept every POST and record its payload."""
        posts.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def _relay(mode: str | None) -> None:
            """Run one permission-mode relay pass for the given pane mode."""
            await forwarder._relay_permission_mode(
                client,
                session_id="conv_abc",
                mode=mode,
                dedupe=dedupe,
            )

        # The launch mode is posted, so the picker has a mode to show.
        await _relay("default")
        assert [p["type"] for p in posts] == ["external_permission_mode_change"]
        assert posts[0]["data"] == {"permission_mode": "default", "initial_observation": True}
        assert dedupe.posted_permission_mode == "default"

        # Unchanged footer is a no-op, not a repeat POST.
        await _relay("default")
        assert len(posts) == 1

        # The user presses shift+tab into auto mode.
        await _relay("auto")
        assert posts[-1]["data"] == {"permission_mode": "auto", "initial_observation": False}
        assert dedupe.posted_permission_mode == "auto"

        # Still auto — the switch isn't re-posted every poll.
        await _relay("auto")
        assert len(posts) == 2

        # A footerless pane reads as unknown and must not post a reversal.
        await _relay(None)
        assert len(posts) == 2
        assert dedupe.posted_permission_mode == "auto"


@pytest.mark.asyncio
async def test_relay_permission_mode_posts_manual_launch_mode() -> None:
    """
    A manual-mode launch publishes a mode, keeping the picker reachable.

    Manual is the default, and launching into it writes no
    ``--permission-mode`` arg and no mode label, leaving the pane footer as the
    only source. The first relay must post it: with no mode stored the web
    picker hides itself, and manual becomes a state no one can switch out of.
    """
    dedupe = forwarder._ForwardDedupeState()
    posts: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Accept the POST and record its payload."""
        posts.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await forwarder._relay_permission_mode(
            client,
            session_id="conv_abc",
            mode="default",
            dedupe=dedupe,
        )

    assert posts == [
        {
            "type": "external_permission_mode_change",
            "data": {"permission_mode": "default", "initial_observation": True},
        }
    ]
    assert dedupe.posted_permission_mode == "default"


@pytest.mark.asyncio
async def test_relay_permission_mode_retries_after_transient_failure() -> None:
    """
    A failed mode POST is retried, so the switch isn't silently dropped.

    The pane is the source of truth but the poll window moves on; if a 503
    advanced the baseline, the web picker would stay stale until the user
    switched modes again.
    """
    dedupe = forwarder._ForwardDedupeState()
    posts: list[dict[str, Any]] = []
    fail_modes = {"plan"}

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Fail the first ``plan`` POST with 503; accept everything else."""
        payload = json.loads(request.content.decode("utf-8"))
        posts.append(payload)
        mode = payload["data"]["permission_mode"]
        if mode in fail_modes:
            fail_modes.discard(mode)
            return httpx.Response(503, json={})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def _relay(mode: str | None) -> None:
            """Run one permission-mode relay pass for the given pane mode."""
            await forwarder._relay_permission_mode(
                client,
                session_id="conv_abc",
                mode=mode,
                dedupe=dedupe,
            )

        await _relay("default")  # the launch mode lands
        assert dedupe.posted_permission_mode == "default"

        await _relay("plan")
        assert len(posts) == 2
        assert dedupe.posted_permission_mode == "default"  # NOT advanced — POST failed

        await _relay("plan")
        assert [p["data"] for p in posts] == [
            {"permission_mode": "default", "initial_observation": True},
            {"permission_mode": "plan", "initial_observation": False},
            {"permission_mode": "plan", "initial_observation": False},
        ]
        assert dedupe.posted_permission_mode == "plan"  # now committed


@contextlib.asynccontextmanager
async def _relay_against_rejecting_server(
    reject: set[str],
    *,
    status: int = 400,
) -> AsyncIterator[
    tuple[
        list[dict[str, Any]],
        forwarder._ForwardDedupeState,
        Callable[[str | None], Awaitable[None]],
    ]
]:
    """Relay pane modes to a server that rejects the modes in ``reject`` with *status*."""
    dedupe = forwarder._ForwardDedupeState()
    posts: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Reject the modes in ``reject`` with *status*; accept the rest."""
        payload = json.loads(request.content.decode("utf-8"))
        posts.append(payload)
        if payload["data"]["permission_mode"] in reject:
            return httpx.Response(status, json={"error": {"code": "invalid_input"}})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def _relay(mode: str | None) -> None:
            """Run one permission-mode relay pass for the given pane mode."""
            await forwarder._relay_permission_mode(
                client,
                session_id="conv_abc",
                mode=mode,
                dedupe=dedupe,
            )

        yield posts, dedupe, _relay


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422])
async def test_relay_permission_mode_stops_reposting_a_mode_the_server_rejects(
    status: int,
) -> None:
    """
    An older server's 400 for a newer footer value is not retried every poll.

    A runner that reads ``dontAsk`` can report to a server whose read-back
    set predates it, which rejects the report as invalid input. Unlike a 503
    that will clear, re-posting would fail on every pane poll for the life
    of the session, so the relay pauses before trying again. A changed footer
    is still reported right away as the switch it is.
    """
    async with _relay_against_rejecting_server({"dontAsk"}, status=status) as (
        posts,
        dedupe,
        relay,
    ):
        await relay("dontAsk")
        assert len(posts) == 1
        assert dedupe.posted_permission_mode is None
        assert dedupe.rejected_permission_mode == "dontAsk"

        # The same footer on later polls is not re-posted during the pause.
        await relay("dontAsk")
        await relay("dontAsk")
        assert len(posts) == 1

        # Leaving don't-ask is reported as the observed switch it is.
        await relay("default")
        assert posts[-1]["data"] == {"permission_mode": "default", "initial_observation": False}
        assert dedupe.posted_permission_mode == "default"
        assert dedupe.rejected_permission_mode is None


@pytest.mark.asyncio
async def test_relay_permission_mode_retries_a_rejected_mode_after_the_pause() -> None:
    """
    A rejected mode is offered again once the pause passes, so an upgrade lands.

    The server can be upgraded under a live runner; with the footer unchanged,
    only a later retry can bring the read-back up to date.
    """
    reject = {"dontAsk"}
    async with _relay_against_rejecting_server(reject) as (posts, dedupe, relay):
        await relay("dontAsk")
        assert len(posts) == 1
        # The pause is the documented interval, measured from the rejection.
        assert dedupe.rejected_permission_mode_retry_at - time.monotonic() == pytest.approx(
            forwarder._REJECTED_PERMISSION_MODE_RETRY_S, abs=5.0
        )

        # Suppressed while the deadline is still ahead, however close.
        await relay("dontAsk")
        dedupe.rejected_permission_mode_retry_at = time.monotonic() + 60.0
        await relay("dontAsk")
        assert len(posts) == 1

        # Still rejected after the pause: one more attempt, then a new pause.
        dedupe.rejected_permission_mode_retry_at = 0.0
        await relay("dontAsk")
        assert len(posts) == 2
        assert dedupe.rejected_permission_mode == "dontAsk"
        await relay("dontAsk")
        assert len(posts) == 2

        # The server now knows the mode: the next attempt lands as the launch mode.
        reject.clear()
        dedupe.rejected_permission_mode_retry_at = 0.0
        await relay("dontAsk")
        assert posts[-1]["data"] == {"permission_mode": "dontAsk", "initial_observation": True}
        assert dedupe.posted_permission_mode == "dontAsk"
        assert dedupe.rejected_permission_mode is None


@pytest.mark.parametrize(
    ("modes", "statuses", "expected"),
    [
        pytest.param(
            [None, "auto", None, "auto", "auto"],
            [503, 202],
            [("auto", True), ("auto", True)],
            id="startup-retry-remains-passive",
        ),
        pytest.param(
            ["default", "auto", "auto"],
            [503, 503, 202],
            [("default", True), ("auto", False), ("auto", False)],
            id="switch-before-first-successful-post",
        ),
        pytest.param(
            ["default", "auto", None, "default", "default"],
            [202, 503, 503, 202],
            [("default", True), ("auto", False), ("default", False), ("default", False)],
            id="switch-back-to-posted-mode-is-still-a-selection",
        ),
    ],
)
@pytest.mark.asyncio
async def test_relay_permission_mode_provenance_survives_delivery_failures(
    modes: list[str | None],
    statuses: list[int],
    expected: list[tuple[str, bool]],
) -> None:
    """Unreadable panes and failed POSTs must not erase an observed selection."""
    dedupe = forwarder._ForwardDedupeState()
    posts: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content)["data"])
        return httpx.Response(statuses[len(posts) - 1], json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        for mode in modes:
            await forwarder._relay_permission_mode(
                client, session_id="conv_abc", mode=mode, dedupe=dedupe
            )

    assert posts == [
        {"permission_mode": mode, "initial_observation": initial} for mode, initial in expected
    ]
    assert dedupe.posted_permission_mode == expected[-1][0]


def _slash_command_item(*, name: str, arguments: str) -> ClaudeTranscriptItem:
    """
    Build a ``slash_command`` transcript item as the bridge emits it.

    :param name: Command name with the leading ``/`` already stripped,
        e.g. ``"effort"``.
    :param arguments: Verbatim ``<command-args>`` text, e.g. ``"max"``.
    :returns: A ``slash_command`` item shaped like
        :func:`_user_transcript_items_from_entry` produces.
    """
    return ClaudeTranscriptItem(
        source_id="rec01:0:slash_command",
        item_type="slash_command",
        data={"agent": "claude", "kind": "command", "name": name, "arguments": arguments},
        response_id="resp_1",
    )


async def _run_effort_sync(
    item: ClaudeTranscriptItem,
    *,
    status: int = 200,
) -> list[_CapturedRequest]:
    """
    Drive ``_maybe_sync_effort_from_slash_command`` against a mock AP.

    :param item: Transcript item to feed the helper.
    :param status: HTTP status the mock PATCH endpoint returns, e.g.
        ``503`` to exercise the best-effort swallow path.
    :returns: Every request the helper issued, in order.
    """
    captured: list[_CapturedRequest] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and return a canned PATCH response."""
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        captured.append(_CapturedRequest(method=request.method, path=request.url.path, body=body))
        return httpx.Response(status, json={"id": "conv_x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._maybe_sync_effort_from_slash_command(
            client, session_id="conv_x", item=item
        )
    return captured


@pytest.mark.parametrize("level", sorted(CLAUDE_EFFORTS))
async def test_in_pane_effort_set_patches_session_silently(level: str) -> None:
    """``/effort <level>`` in the pane PATCHes reasoning_effort with silent=True."""
    captured = await _run_effort_sync(_slash_command_item(name="effort", arguments=level))

    assert captured == [
        _CapturedRequest(
            method="PATCH",
            path="/v1/sessions/conv_x",
            body={"reasoning_effort": level, "silent": True},
        )
    ], f"expected one silent reasoning_effort={level} PATCH, got {captured!r}"


@pytest.mark.parametrize("alias", sorted(EFFORT_CLEAR_VALUES))
async def test_in_pane_effort_clear_patches_clear_alias(alias: str) -> None:
    """``/effort default`` (and off/reset) forwards the clear alias verbatim."""
    captured = await _run_effort_sync(_slash_command_item(name="effort", arguments=alias))

    assert captured == [
        _CapturedRequest(
            method="PATCH",
            path="/v1/sessions/conv_x",
            body={"reasoning_effort": alias, "silent": True},
        )
    ], f"expected one silent clear PATCH for alias={alias}, got {captured!r}"


def _message_item() -> ClaudeTranscriptItem:
    """A plain user-message item (not a slash command)."""
    return ClaudeTranscriptItem(
        source_id="rec01:0:message",
        item_type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        response_id="resp_1",
    )


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(_slash_command_item(name="effort", arguments="turbo"), id="unknown-level"),
        pytest.param(_slash_command_item(name="effort", arguments=""), id="no-arg-show"),
        pytest.param(_slash_command_item(name="model", arguments="opus"), id="non-effort-command"),
        pytest.param(_message_item(), id="plain-message"),
    ],
)
async def test_effort_sync_skips_non_effort_changes(item: ClaudeTranscriptItem) -> None:
    """Only a recognized ``/effort`` set/clear PATCHes; everything else no-ops."""
    captured = await _run_effort_sync(item)

    assert captured == [], f"expected no PATCH for this item, got {captured!r}"


async def test_effort_sync_swallows_patch_failure() -> None:
    """A failed PATCH is best-effort — attempted, logged, never raised."""
    captured = await _run_effort_sync(
        _slash_command_item(name="effort", arguments="max"), status=503
    )

    # Attempted exactly once and the 503 swallowed (no exception escaped the await).
    assert len(captured) == 1
    assert captured[0].method == "PATCH"


# ── /btw side-chat overlay relay ───────────────────────────────────


def _btw_recording_client_calls() -> tuple[list[dict[str, Any]], httpx.MockTransport]:
    """
    Build a MockTransport that records POST bodies for /btw relay tests.

    :returns: The shared ``calls`` list and the transport to hand an
        ``httpx.AsyncClient``.
    """
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record each POST body and return a benign success."""
        calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"queued": False, "item_id": "item_x"})

    return calls, httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_relay_btw_overlay_relays_after_stability_then_dedupes() -> None:
    """
    A settled /btw overlay relays ONE transient side-chat event.

    The first read only records the exchange as pending (torn-capture
    guard); the second (same exchange) posts the transient
    ``external_btw_sidechat`` event; the third is deduped and posts nothing.
    """
    overlay = BtwOverlay(question="/btw is this ok?", answer="Yes, all good.", truncated=False)
    dedupe = forwarder._ForwardDedupeState()
    calls, transport = _btw_recording_client_calls()

    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        for _ in range(3):
            await forwarder._relay_btw_overlay(
                client,
                session_id="conv1",
                overlay=overlay,
                dedupe=dedupe,
            )

    assert len(calls) == 1
    (body,) = calls
    assert body["type"] == "external_btw_sidechat"
    assert body["data"]["question"] == "/btw is this ok?"
    assert body["data"]["answer"] == "Yes, all good."
    assert body["data"]["truncated"] is False


@pytest.mark.asyncio
async def test_relay_btw_overlay_relays_truncated_flag() -> None:
    """A clipped overlay relays the visible answer with truncated=True."""
    overlay = BtwOverlay(question="/btw big", answer="line1\nline2", truncated=True)
    dedupe = forwarder._ForwardDedupeState()
    calls, transport = _btw_recording_client_calls()

    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        for _ in range(2):
            await forwarder._relay_btw_overlay(
                client,
                session_id="conv1",
                overlay=overlay,
                dedupe=dedupe,
            )

    assert len(calls) == 1
    assert calls[0]["data"]["answer"] == "line1\nline2"
    assert calls[0]["data"]["truncated"] is True


@pytest.mark.asyncio
async def test_relay_btw_overlay_no_overlay_is_noop() -> None:
    """No visible overlay posts nothing and clears any pending key."""
    dedupe = forwarder._ForwardDedupeState()
    dedupe.btw_pending_key = "stale"
    calls, transport = _btw_recording_client_calls()

    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._relay_btw_overlay(
            client,
            session_id="conv1",
            overlay=None,
            dedupe=dedupe,
        )

    assert calls == []
    assert dedupe.btw_pending_key is None


@pytest.mark.asyncio
async def test_forward_pane_signals_captures_once_and_relays_both(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    One throttled capture feeds both the permission-mode and /btw relays.

    Back-to-back polls inside one throttle window capture the pane exactly
    once (the shared-capture win), and the single snapshot's mode + settled
    overlay both reach the wire (the overlay after its two-read stability
    guard).
    """
    from omnigent.harnesses.claude_native.bridge import PaneSignals

    overlay = BtwOverlay(question="/btw ok?", answer="Yes.", truncated=False)
    reads = 0

    def _fake_signals(_bridge_dir: Path) -> PaneSignals:
        """Serve one snapshot carrying both signals, counting captures."""
        nonlocal reads
        reads += 1
        return PaneSignals(permission_mode="auto", btw_overlay=overlay)

    monkeypatch.setattr(forwarder, "read_pane_signals", _fake_signals)
    monkeypatch.setattr(forwarder, "_PANE_POLL_INTERVAL_S", 0.0)
    dedupe = forwarder._ForwardDedupeState()
    calls, transport = _btw_recording_client_calls()

    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        for _ in range(3):
            await forwarder._forward_pane_signals(
                client,
                session_id="conv1",
                bridge_dir=tmp_path,
                dedupe=dedupe,
            )

    types = [c["type"] for c in calls]
    assert types.count("external_permission_mode_change") == 1
    assert types.count("external_btw_sidechat") == 1
    assert dedupe.posted_permission_mode == "auto"


@pytest.mark.asyncio
async def test_forward_pane_signals_throttles_capture(tmp_path: Path) -> None:
    """Back-to-back polls inside one throttle window capture the pane once."""
    from omnigent.harnesses.claude_native.bridge import PaneSignals

    reads = 0

    def _fake_signals(_bridge_dir: Path) -> PaneSignals:
        """Count captures; the signals themselves are irrelevant here."""
        nonlocal reads
        reads += 1
        return PaneSignals()

    with patch.object(forwarder, "read_pane_signals", _fake_signals):
        dedupe = forwarder._ForwardDedupeState()
        transport = httpx.MockTransport(lambda _req: httpx.Response(202, json={}))
        async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
            for _ in range(5):
                await forwarder._forward_pane_signals(
                    client,
                    session_id="conv1",
                    bridge_dir=tmp_path,
                    dedupe=dedupe,
                )

    assert reads == 1
    assert dedupe.pane_next_read > 0.0


_BUDGET_RETRY_ERROR = (
    '403 Budget "example-budget" (00000000-0000-0000-0000-000000000000) has reached'
    " its limit of $0.1. To continue, contact an admin to increase the budget."
)


def _claude_pane(status_row: str) -> str:
    """Render a Claude Code pane whose row above the input box is ``status_row``."""
    return (
        "❯ say hi\n"
        "\n"
        f"{status_row}\n"
        "────────────────────────────────────────\n"
        "❯ \n"
        "────────────────────────────────────────\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt\n"
    )


def _fake_tmux_pane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    failed_send_keys: int = 0,
    on_send_keys: Callable[[], None] | None = None,
) -> tuple[Path, list[str], list[list[str]]]:
    """
    Advertise a tmux pane whose capture returns the last entry of ``panes``.

    :param failed_send_keys: Number of initial ``send-keys`` calls that fail.
    :param on_send_keys: Observe each attempted keystroke before its result.
    :returns: The bridge directory, the mutable ``panes`` list and the
        recorded successful ``send-keys`` commands.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )
    panes: list[str] = []
    sent_keys: list[list[str]] = []
    send_keys_attempts = 0

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        """Serve the current pane and record keystrokes."""
        nonlocal send_keys_attempts
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=panes[-1], stderr="")
        if "send-keys" in cmd:
            if on_send_keys is not None:
                on_send_keys()
            send_keys_attempts += 1
            if send_keys_attempts <= failed_send_keys:
                return SimpleNamespace(returncode=1, stdout="", stderr="no client")
            sent_keys.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(forwarder, "_PANE_POLL_INTERVAL_S", 0.0)
    return bridge_dir, panes, sent_keys


def _failed_status_posts(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the ``failed`` session-status payloads among recorded POST bodies."""
    return [
        call["data"]
        for call in calls
        if call["type"] == "external_session_status" and call["data"]["status"] == "failed"
    ]


@pytest.mark.parametrize(
    ("rejection", "status"),
    [
        pytest.param(_BUDGET_RETRY_ERROR, 403, id="budget-403"),
        pytest.param(
            '401 {"type":"error","error":{"type":"authentication_error"}}', 401, id="auth-401"
        ),
    ],
)
@pytest.mark.asyncio
async def test_forward_pane_signals_fails_and_interrupts_a_rejected_api_retry(
    rejection: str,
    status: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A 401/403 retry row fails the turn with its error, then sends one Escape.

    Claude Code retries a gateway budget rejection ten times while the web
    shows only a working spinner. The row stays visible through its backoff,
    so repeat polls must not post or interrupt again. A rejection is reported
    afresh once the row leaves the pane, or for a newer prompt submitted
    before any poll saw the pane without one. A rejection before any output
    has no response id, so only the prompt tells the turns apart.
    """
    bridge_dir, panes, sent_keys = _fake_tmux_pane(monkeypatch, tmp_path)
    calls, transport = _btw_recording_client_calls()
    dedupe = forwarder._ForwardDedupeState()
    hook_state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)

    async def _submit_prompt() -> None:
        nonlocal hook_state
        record_hook_event(bridge_dir, {"hook_event_name": "UserPromptSubmit"})
        hook_state = await forwarder._forward_available_status_events(
            client=client,
            session_id="conv1",
            bridge_dir=bridge_dir,
            state=hook_state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
            task_subjects={},
            task_statuses={},
            task_order=[],
        )

    async def _poll(pane: str) -> None:
        panes.append(pane)
        await forwarder._forward_pane_signals(
            client, session_id="conv1", bridge_dir=bridge_dir, dedupe=dedupe
        )

    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await _submit_prompt()
        await _poll(_claude_pane(f"✻ {rejection} · Retrying in 2s · attempt 3/10"))
        await _poll(_claude_pane(f"✻ {rejection} · Retrying in 1s · attempt 3/10"))

        (failed,) = _failed_status_posts(calls)
        assert "response_id" not in failed
        assert failed["failure_detail"].startswith(f"API Error: {rejection}")
        assert "attempt 3 of 10" in failed["failure_detail"]
        assert failed["failure_context"]["http_status"] == status
        assert failed["failure_context"]["failure_source"] == "claude_api_retry"
        assert len(sent_keys) == 1
        assert sent_keys[0][-1] == "Escape"

        await _poll(_claude_pane("✻ Billowing…"))
        await _poll(_claude_pane(f"✻ {rejection} · Retrying in 3s · attempt 3/10"))
        assert len(_failed_status_posts(calls)) == 2
        assert len(sent_keys) == 2

        await _submit_prompt()
        await _poll(_claude_pane("✻ API error · Retrying in 1s · attempt 2/10"))
        await _poll(_claude_pane(f"✻ {rejection} · Retrying in 2s · attempt 3/10"))
        await _poll(_claude_pane(f"✻ {rejection} · Retrying in 1s · attempt 3/10"))

    failure_ids = [post["failure_context"]["failure_id"] for post in _failed_status_posts(calls)]
    assert len(failure_ids) == 3
    assert failure_ids[0] == failure_ids[1] != failure_ids[2]
    assert len(sent_keys) == 3


@pytest.mark.parametrize(
    "status_row",
    [
        pytest.param("✻ 429 Too many requests · Retrying in 4s · attempt 3/10", id="rate-limit"),
        pytest.param("✻ 500 Internal server error · Retrying in 4s · attempt 3/10", id="500"),
        pytest.param("✻ 503 Service unavailable · Retrying in 4s · attempt 3/10", id="503"),
        pytest.param("✻ Waiting for API response · Retrying in 4s · attempt 1/10", id="no-status"),
    ],
)
@pytest.mark.asyncio
async def test_forward_pane_signals_leaves_other_api_retries_running(
    status_row: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Retries that can still succeed, like a rate limit, are left to Claude Code."""
    bridge_dir, panes, sent_keys = _fake_tmux_pane(monkeypatch, tmp_path)
    panes.append(_claude_pane(status_row))
    calls, transport = _btw_recording_client_calls()

    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._forward_pane_signals(
            client,
            session_id="conv1",
            bridge_dir=bridge_dir,
            dedupe=forwarder._ForwardDedupeState(),
            response_id="resp_1",
        )

    assert _failed_status_posts(calls) == []
    assert sent_keys == []


@pytest.mark.parametrize(
    "scenario",
    [
        "unchanged",
        "stopped",
        "followup",
        "identical-retry",
        "compacted",
        "compact-during-report",
        "restarted",
        "restarted-followup",
        "pending-followup",
        "relay-followup",
    ],
)
@pytest.mark.asyncio
async def test_forward_pane_signals_interrupts_a_rejected_api_retry_only_after_reporting_it(
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Report before Escape, recover delivery failures, and protect replacement turns."""
    recover_failures = scenario == "unchanged"
    suspended_operation = "relay" if scenario == "relay-followup" else "report"
    should_interrupt = scenario in {
        "unchanged",
        "compacted",
        "compact-during-report",
        "restarted",
        "pending-followup",
    }
    timeline: list[str] = []
    bridge_dir, panes, sent_keys = _fake_tmux_pane(
        monkeypatch,
        tmp_path,
        failed_send_keys=int(recover_failures),
        on_send_keys=lambda: timeline.append("escape"),
    )
    panes.append(_claude_pane(f"✻ {_BUDGET_RETRY_ERROR} · Retrying in 2s · attempt 3/10"))
    calls: list[dict[str, Any]] = []
    operation_started = asyncio.Event()
    finish_operation = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        """Suspend the successful report; inject delivery failure only in its recovery case."""
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        is_failure = bool(_failed_status_posts([body]))
        if is_failure:
            if recover_failures and len(_failed_status_posts(calls)) == 1:
                timeline.append("report_failed")
                return httpx.Response(503, json={})
        if is_failure or (
            scenario == "relay-followup" and body["type"] == "external_permission_mode_change"
        ):
            operation = "report" if is_failure else "relay"
            timeline.append(f"{operation}_started")
            operation_started.set()
            await finish_operation.wait()
            timeline.append(f"{operation}_completed")
        return httpx.Response(200, json={})

    dedupe = forwarder._ForwardDedupeState()
    response_id = "resp_1"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:

        async def _forward_hooks(state: forwarder.HookForwardState) -> None:
            await forwarder._forward_available_status_events(
                client=client,
                session_id="conv1",
                bridge_dir=bridge_dir,
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
                task_subjects={},
                task_statuses={},
                task_order=[],
            )

        record_hook_event(
            bridge_dir, {"hook_event_name": "UserPromptSubmit", "session_id": "parent-session"}
        )
        await _forward_hooks(forwarder.HookForwardState(event_cursor=0, byte_offset=0))
        if scenario == "compacted":
            record_hook_event(
                bridge_dir,
                {
                    "hook_event_name": "SessionStart",
                    "source": "compact",
                    "session_id": "parent-session",
                },
            )
        if scenario in {"restarted", "restarted-followup"}:
            dedupe = forwarder._ForwardDedupeState()
            restored = forwarder._read_hook_state(bridge_dir)
            assert restored is not None and restored.event_cursor == 1
            await _forward_hooks(restored)
            assert dedupe.prompt_submit_cursor is None

        async def _poll() -> None:
            await forwarder._forward_pane_signals(
                client,
                session_id="conv1",
                bridge_dir=bridge_dir,
                dedupe=dedupe,
                response_id=response_id,
            )

        if scenario == "pending-followup":
            record_hook_event(
                bridge_dir,
                {"hook_event_name": "UserPromptSubmit", "session_id": "parent-session"},
            )
            finish_operation.set()
            await _poll()
            assert _failed_status_posts(calls) == []
            assert sent_keys == []
            assert dedupe.prompt_submit_cursor == 1
            finish_operation.clear()
            restored = forwarder._read_hook_state(bridge_dir)
            assert restored is not None
            await _forward_hooks(restored)
            assert dedupe.prompt_submit_cursor == 2
            response_id = "resp_2"

        if recover_failures:
            await _poll()
            assert len(_failed_status_posts(calls)) == 1
            assert sent_keys == []
        recovery_timeline = ["report_failed"] if recover_failures else []

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_poll())
            try:
                await asyncio.wait_for(operation_started.wait(), timeout=5.0)
                assert timeline == [*recovery_timeline, f"{suspended_operation}_started"]
                if scenario in {"stopped", "followup", "relay-followup"}:
                    panes.append(_claude_pane("" if scenario == "stopped" else "✻ Billowing…"))
                if scenario in {
                    "followup",
                    "identical-retry",
                    "restarted-followup",
                    "relay-followup",
                }:
                    record_hook_event(
                        bridge_dir,
                        {"hook_event_name": "UserPromptSubmit", "session_id": "parent-session"},
                    )
                    assert dedupe.prompt_submit_cursor == (
                        None if scenario == "restarted-followup" else 1
                    )
                if scenario == "compact-during-report":
                    record_hook_event(
                        bridge_dir,
                        {
                            "hook_event_name": "SessionStart",
                            "source": "compact",
                            "session_id": "parent-session",
                        },
                    )
            finally:
                finish_operation.set()
        report_count = 0 if scenario == "relay-followup" else 2 if recover_failures else 1
        assert len(_failed_status_posts(calls)) == report_count
        assert len(sent_keys) == int(should_interrupt and not recover_failures)

        await _poll()

    assert [post["response_id"] for post in _failed_status_posts(calls)] == [
        response_id
    ] * report_count
    escape_count = (2 if recover_failures else 1) if should_interrupt else 0
    assert timeline == [
        *recovery_timeline,
        f"{suspended_operation}_started",
        f"{suspended_operation}_completed",
        *(["escape"] * escape_count),
    ]
    assert len(sent_keys) == int(should_interrupt)
