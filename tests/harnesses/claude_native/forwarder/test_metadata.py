"""Metadata tests for Claude-native forwarding."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    BtwOverlay,
    ClaudeTranscriptItem,
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
