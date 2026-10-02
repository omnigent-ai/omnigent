"""
Tests for the codex-native forwarder's model-change sync-back
(:mod:`omnigent.harnesses.codex_native.forwarder`).

For codex-native, ``config.toml``'s ``model`` key is the cost-policy source
of truth (it is what an in-TUI ``/model`` writes). At subscription and at
each ``turn/started`` the forwarder reads it (``_refresh_model_from_config``,
which delegates to the shared ``read_codex_config_model`` in the bridge
module) onto ``_CodexForwarderState.model`` and mirrors it to the Omnigent server
as an ``external_model_change`` event (→ persisted ``conv.model_override``)
so the cost-budget policy resolves the selected model. The startup/spawn
model IS mirrored (so Omnigent learns the session's model even when unchanged);
only an already-mirrored value is not re-posted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    codex_home_for_bridge_dir,
)
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


def _state(model: str | None, posted_model: str | None) -> fwd._CodexForwarderState:
    """
    Build a forwarder state with the given current + last-mirrored model.

    :param model: Current Codex model, e.g. ``"gpt-5.4"`` or ``None``.
    :param posted_model: Last-mirrored model baseline, e.g. ``"gpt-5.5"``.
    :returns: A ``_CodexForwarderState`` for the sync-back helper.
    """
    state = fwd._CodexForwarderState()
    state.model = model
    state.posted_model = posted_model
    return state


@pytest.mark.asyncio
async def test_sync_model_change_posts_on_change() -> None:
    """A model differing from the baseline posts external_model_change.

    The in-TUI ``/model`` switch (gpt-5.5 → gpt-5.4) must mirror to Omnigent as
    an ``external_model_change`` and advance the baseline so it isn't
    re-posted. A missing post here is exactly the bug a user hit: the
    terminal model changed but the cost policy kept seeing gpt-5.5.
    """
    client = _RecordingClient()
    state = _state(model="gpt-5.4", posted_model="gpt-5.5")

    await fwd._sync_model_change(client, session_id="conv_x", forwarder_state=state)

    # Exactly one mirror post, carrying the new raw codex model id.
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {"type": "external_model_change", "data": {"model": "gpt-5.4"}},
        )
    ]
    # Baseline advanced → the same model won't re-post on the next update.
    assert state.posted_model == "gpt-5.4"


@pytest.mark.asyncio
async def test_sync_model_change_no_post_when_unchanged() -> None:
    """Model equal to the baseline (seeded spawn default) does not post.

    Prevents the spawn/startup model from being echoed back to Omnigent as a
    spurious "change" (which would also fire on every settings update).
    """
    client = _RecordingClient()
    state = _state(model="gpt-5.5", posted_model="gpt-5.5")

    await fwd._sync_model_change(client, session_id="conv_x", forwarder_state=state)

    assert client.posts == []


@pytest.mark.asyncio
async def test_sync_model_change_no_post_when_model_unknown() -> None:
    """No model observed yet (``None``) → nothing to mirror."""
    client = _RecordingClient()
    state = _state(model=None, posted_model="gpt-5.5")

    await fwd._sync_model_change(client, session_id="conv_x", forwarder_state=state)

    assert client.posts == []


def _write_codex_config(bridge_dir: Path, body: str) -> Path:
    """
    Write a ``config.toml`` into the session's per-session ``CODEX_HOME``.

    :param bridge_dir: The bridge dir whose ``codex-home/config.toml`` is
        written (the path the model reader reads).
    :param body: Raw TOML body, e.g. ``'model = "gpt-5.4"\\n'``.
    :returns: The written ``config.toml`` path.
    """
    home = codex_home_for_bridge_dir(bridge_dir)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(body)
    return path


def test_refresh_model_from_config_updates_state(tmp_path: Path) -> None:
    """``config.toml``'s model lands on the forwarder state for mirroring.

    This is the exact path the subscription and ``turn/started`` handlers use
    to learn the user's ``/model`` selection: read config.toml (via the
    shared ``read_codex_config_model``) → set ``forwarder_state.model`` →
    ``_sync_model_change`` mirrors it to AP. The config.toml parsing itself
    is covered in ``tests/harnesses/codex_native/test_codex_native_bridge.py``; this asserts the
    forwarder wires the read into its state.
    """
    _write_codex_config(tmp_path, 'model = "gpt-5.4"\n')
    state = _state(model="gpt-5.5", posted_model="gpt-5.5")

    fwd._refresh_model_from_config(tmp_path, state)

    # The selected model (gpt-5.4) replaces the prior value, ready to mirror.
    assert state.model == "gpt-5.4"


def test_refresh_prefers_pushed_settings_model_over_stale_config(tmp_path: Path) -> None:
    """An unchanged config.toml must not roll back a live thread-settings model.

    Regression for the routed-model reversion: routing switched the running
    thread via ``thread/settings/update`` (notified as
    ``thread/settings/updated``), but config.toml still held the pinned
    launch model; the next ``turn/started`` re-read the stale file and
    mirrored the default back over ``model_override`` — reverting the routed
    model one turn after it applied.
    """
    _write_codex_config(tmp_path, 'model = "databricks-gpt-5-5"\n')
    state = fwd._CodexForwarderState()
    # Subscription-time read adopts the pinned launch model (baseline).
    fwd._refresh_model_from_config(tmp_path, state)
    assert state.model == "databricks-gpt-5-5"
    # Omnigent pushes a routed model thread-level; the live notification wins.
    state.note_thread_settings_updated({"threadSettings": {"model": "databricks-gpt-5-6-luna"}})

    # turn/started re-read: config.toml is UNCHANGED — the pushed model holds.
    fwd._refresh_model_from_config(tmp_path, state)

    assert state.model == "databricks-gpt-5-6-luna"


def test_refresh_adopts_changed_config_over_settings_model(tmp_path: Path) -> None:
    """A config.toml that changed since the last read wins over settings.

    An in-TUI ``/model`` (or the executor's mirror write) rewrites the file —
    that is the freshest signal and must not be masked by an older
    ``thread/settings/updated`` value.
    """
    _write_codex_config(tmp_path, 'model = "databricks-gpt-5-5"\n')
    state = fwd._CodexForwarderState()
    fwd._refresh_model_from_config(tmp_path, state)
    state.note_thread_settings_updated({"threadSettings": {"model": "databricks-gpt-5-6-luna"}})
    # The user picks a third model in the TUI: /model rewrites config.toml.
    _write_codex_config(tmp_path, 'model = "gpt-5.6-sol"\n')

    fwd._refresh_model_from_config(tmp_path, state)

    assert state.model == "gpt-5.6-sol"


def test_refresh_launch_race_ends_on_routed_model(tmp_path: Path) -> None:
    """Launch-race scenario: the pinned default ends up on the routed model.

    The terminal launch pins the default into config.toml before first-turn
    routing runs. The executor then pushes the routed model thread-level AND
    mirrors it into config.toml (``write_codex_config_model``); the next
    ``turn/started`` re-read must adopt the routed model — with or without
    the mirror write having succeeded.
    """
    from omnigent.harnesses.codex_native.bridge import write_codex_config_model

    _write_codex_config(tmp_path, 'model = "databricks-gpt-5-5"\n')
    state = fwd._CodexForwarderState()
    fwd._refresh_model_from_config(tmp_path, state)
    # First routed turn: settings push (notification) + executor mirror write.
    state.note_thread_settings_updated({"threadSettings": {"model": "databricks-gpt-5-6-luna"}})
    assert write_codex_config_model(tmp_path, "databricks-gpt-5-6-luna") is True

    fwd._refresh_model_from_config(tmp_path, state)

    assert state.model == "databricks-gpt-5-6-luna"
    # Later turns stay on the routed model (no reversion churn).
    fwd._refresh_model_from_config(tmp_path, state)
    assert state.model == "databricks-gpt-5-6-luna"


def test_refresh_effort_from_config_adopts_changed_value(tmp_path: Path) -> None:
    """``config.toml``'s effort lands on the forwarder state for mirroring.

    This is the path the ``turn/started`` handler uses to learn an in-TUI
    ``/model`` effort change (which rewrites config.toml with no
    notification): read config.toml (via the shared ``read_codex_config_effort``)
    → set ``forwarder_state.effort`` → ``_sync_reasoning_effort_change``
    mirrors it so the chat composer's effort control updates.
    """
    _write_codex_config(tmp_path, 'model = "gpt-5.5"\nmodel_reasoning_effort = "medium"\n')
    state = fwd._CodexForwarderState()
    fwd._refresh_effort_from_config(tmp_path, state)
    assert state.effort == "medium"

    # The user picks a new effort in the TUI: /model rewrites config.toml.
    _write_codex_config(tmp_path, 'model = "gpt-5.5"\nmodel_reasoning_effort = "high"\n')

    fwd._refresh_effort_from_config(tmp_path, state)

    assert state.effort == "high"
    assert state.last_config_effort == "high"


def test_refresh_effort_prefers_pushed_settings_effort_over_stale_config(
    tmp_path: Path,
) -> None:
    """An unchanged config.toml must not roll back a live thread-settings effort.

    An Omnigent-initiated effort change lands thread-level (notified as
    ``thread/settings/updated``) without rewriting config.toml; the next
    ``turn/started`` re-read of the unchanged file must keep the pushed
    effort rather than reverting it one turn after it applied.
    """
    _write_codex_config(tmp_path, 'model_reasoning_effort = "medium"\n')
    state = fwd._CodexForwarderState()
    # Subscription/turn-time read adopts the pinned launch effort (baseline).
    fwd._refresh_effort_from_config(tmp_path, state)
    assert state.effort == "medium"
    # Omnigent pushes a new effort thread-level; the live notification wins.
    state.note_thread_settings_updated({"threadSettings": {"effort": "low"}})

    # turn/started re-read: config.toml is UNCHANGED — the pushed effort holds.
    fwd._refresh_effort_from_config(tmp_path, state)

    assert state.effort == "low"


def test_refresh_effort_noop_when_config_has_no_effort(tmp_path: Path) -> None:
    """A config.toml without an effort key preserves the prior value.

    Absence (or an unreadable file) is not a signal to clear or invent an
    effort — the forwarder keeps whatever it last learned.
    """
    _write_codex_config(tmp_path, 'model = "gpt-5.5"\n')
    state = fwd._CodexForwarderState()
    state.effort = "medium"

    fwd._refresh_effort_from_config(tmp_path, state)

    assert state.effort == "medium"
    assert state.last_config_effort is None


def test_note_resume_response_records_model_without_seeding_baseline() -> None:
    """The startup/resume model is recorded but the baseline stays unset.

    Omnigent must learn the session's ACTUAL model — including the spawn default —
    because the cost gate resolves ``conv.model_override or spec.llm.model``
    and for codex the spawn model is frequently NOT ``spec.llm.model``. So
    ``note_resume_response`` records ``model`` but leaves ``posted_model``
    ``None``, so the next ``_sync_model_change`` mirrors the real model. If
    this re-seeded the baseline, an unchanged cheap session would never post
    ``external_model_change`` and the gate would wrongly DENY it.
    """
    state = fwd._CodexForwarderState()

    state.note_resume_response({"result": {"model": "gpt-5.4-mini"}})

    assert state.model == "gpt-5.4-mini"
    # Baseline NOT seeded → the spawn model will be mirrored on the next sync.
    assert state.posted_model is None


@pytest.mark.asyncio
async def test_sync_after_resume_posts_spawn_model() -> None:
    """End-to-end: an unchanged spawn model is mirrored to AP.

    This is the regression for the wrongly-blocked cheap session: codex
    spawned on gpt-5.4-mini, the model never "changed", yet Omnigent must still
    receive it as ``model_override`` so the cost gate sees a cheap model
    instead of falling back to the spec model and DENYing.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    state.note_resume_response({"result": {"model": "gpt-5.4-mini"}})

    await fwd._sync_model_change(client, session_id="conv_x", forwarder_state=state)

    # The spawn model is mirrored (not suppressed as "unchanged").
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {"type": "external_model_change", "data": {"model": "gpt-5.4-mini"}},
        )
    ]
    assert state.posted_model == "gpt-5.4-mini"


def test_thread_settings_updated_records_effort_and_collaboration_mode() -> None:
    """
    ``thread/settings/updated`` records Codex's live thinking settings.

    App-server sends the public ``ThreadSettings`` shape with ``effort`` and
    ``collaborationMode``. If this parser regresses, the later sync helpers have
    no state to mirror, so Omnigent would keep stale ``reasoning_effort`` and
    mode metadata even though Codex changed them.
    """
    state = fwd._CodexForwarderState()

    state.note_thread_settings_updated(
        {
            "threadSettings": {
                "model": "gpt-5.4-codex",
                "effort": "medium",
                "collaborationMode": {
                    "mode": "plan",
                    "settings": {
                        "model": "gpt-5.4-codex",
                        "reasoning_effort": "medium",
                        "developer_instructions": None,
                    },
                },
            }
        }
    )

    assert state.model == "gpt-5.4-codex"
    assert state.effort == "medium"
    assert state.collaboration_mode == "plan"


def test_thread_settings_updated_records_approval_preset() -> None:
    """
    ``thread/settings/updated`` resolves the live ``/permissions`` preset.

    A TUI approval change arrives here; the forwarder must map the approval
    fields to a preset value so the sync helper can mirror it to the web
    read-back label. If this regresses, a TUI-side switch never reaches the UI.
    """
    state = fwd._CodexForwarderState()

    state.note_thread_settings_updated(
        {
            "threadSettings": {
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "sandboxPolicy": {"type": "dangerFullAccess"},
                "activePermissionProfile": {"id": ":danger-full-access", "extends": None},
            }
        }
    )

    assert state.approval_preset == "full-access"


@pytest.mark.asyncio
async def test_sync_codex_approval_mode_change_posts_preset_and_dedupes() -> None:
    """
    Codex ``/permissions`` changes mirror the runtime preset to Omnigent once.

    The post must carry ``approval_mode`` so the server stamps the read-back
    label + publishes; a second sync with the same preset must not re-post.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(approval_preset="read-only")

    await fwd._sync_codex_approval_mode_change(client, session_id="conv_x", forwarder_state=state)
    await fwd._sync_codex_approval_mode_change(client, session_id="conv_x", forwarder_state=state)

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_codex_approval_mode_change",
                "data": {"approval_mode": "read-only"},
            },
        )
    ]
    assert state.posted_approval_preset == "read-only"


@pytest.mark.asyncio
async def test_sync_reasoning_effort_change_posts_and_dedupes() -> None:
    """
    Codex effort changes mirror to Omnigent exactly once per observed value.

    The first sync must POST ``external_reasoning_effort_change`` so the server
    persists ``conversation.reasoning_effort``. The second sync with the same
    value must not re-post; otherwise every repeated settings notification would
    churn the session stream.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(effort="medium")

    await fwd._sync_reasoning_effort_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )
    await fwd._sync_reasoning_effort_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )

    # One post proves the new effort reached AP; no second post proves the
    # dedupe baseline advanced after a successful mirror.
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_reasoning_effort_change",
                "data": {"reasoning_effort": "medium"},
            },
        )
    ]
    assert state.posted_effort == "medium"
    assert state.posted_effort_known is True


@pytest.mark.asyncio
async def test_sync_reasoning_effort_change_posts_clear() -> None:
    """
    Codex clearing effort mirrors JSON null to Omnigent.

    ``None`` is a meaningful observed value (model/default effort), so the
    forwarder must still post it after a prior explicit effort. If this returned
    early on falsey ``None``, Omnigent would keep a stale explicit effort.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(
        effort=None,
        posted_effort="high",
        posted_effort_known=True,
    )

    await fwd._sync_reasoning_effort_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_reasoning_effort_change",
                "data": {"reasoning_effort": None},
            },
        )
    ]
    assert state.posted_effort is None
    assert state.posted_effort_known is True


@pytest.mark.asyncio
async def test_sync_codex_collaboration_mode_change_posts_and_dedupes() -> None:
    """
    Codex collaboration mode changes mirror to Omnigent labels once.

    The ``mode`` value is the durable "Plan vs Default" signal we can get from
    app-server. Missing this POST would leave the session snapshot without the
    current Codex mode.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(collaboration_mode="plan")

    await fwd._sync_codex_collaboration_mode_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )
    await fwd._sync_codex_collaboration_mode_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_codex_collaboration_mode_change",
                "data": {"mode": "plan"},
            },
        )
    ]
    assert state.posted_collaboration_mode == "plan"


@pytest.mark.asyncio
async def test_sync_codex_approval_mode_change_posts_and_dedupes() -> None:
    """Codex ``/permissions`` changes mirror to terminal_launch_args once."""
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    state.note_thread_settings_updated(
        {
            "threadSettings": {
                "approvalPolicy": "never",
                "approvalsReviewer": "auto_review",
                "sandboxPolicy": {"type": "danger-full-access"},
                "activePermissionProfile": {"id": "dev", "extends": ":workspace"},
            }
        }
    )

    await fwd._sync_codex_approval_mode_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )
    await fwd._sync_codex_approval_mode_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_codex_approval_mode_change",
                "data": {
                    "terminal_launch_args": [
                        "-c",
                        'default_permissions="dev"',
                        "-c",
                        'approval_policy="never"',
                        "-c",
                        'approvals_reviewer="auto_review"',
                    ],
                    # Same event now also carries the runtime preset (danger sandbox
                    # → full-access) for the web read-back label.
                    "approval_mode": "full-access",
                },
            },
        )
    ]
    assert state.posted_terminal_launch_args == [
        "-c",
        'default_permissions="dev"',
        "-c",
        'approval_policy="never"',
        "-c",
        'approvals_reviewer="auto_review"',
    ]
    assert state.posted_approval_preset == "full-access"


def test_codex_permission_settings_fall_back_to_legacy_policy_args() -> None:
    """Legacy settings without an active profile keep approval and sandbox."""
    assert fwd._codex_terminal_launch_args_from_settings(
        {
            "approvalPolicy": "on-failure",
            "approvalsReviewer": "user",
            "sandboxPolicy": {"type": "workspace-write"},
        }
    ) == [
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "on-failure",
        "-c",
        'approvals_reviewer="user"',
    ]
