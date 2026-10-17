"""Binding and observation fences for delayed runner-offline decisions."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import StatementError

from omnigent.db.compression import encode
from omnigent.db.db_models import (
    SqlConversationLabel,
    SqlConversationMetadata,
    current_workspace_id,
    workspace_scope,
)
from omnigent.entities import MessageData, NewConversationItem, ResourceEventData
from omnigent.entities.session_observation import RunnerOfflineDecision, SessionObservation
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture(scope="module")
def stored_byte_boundaries() -> tuple[str, str, str]:
    import random
    import string

    source = "".join(
        random.Random(417).choices(
            string.ascii_letters + string.digits + "!#$%&()*+,-./:;<=>?@[]^_{|}~", k=25000
        )
    )
    fitting, oversized = source[:19904], source[:19905]
    fitting_frame, oversized_frame = encode(fitting), encode(oversized)
    assert fitting_frame is not None and len(fitting_frame) == 16_383
    assert oversized_frame is not None and len(oversized_frame) == 16_384
    return fitting, oversized, source


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def store(request: pytest.FixtureRequest) -> SqlAlchemyConversationStore:
    return request.getfixturevalue(request.param)


def _decision(observation: SessionObservation) -> RunnerOfflineDecision:
    return RunnerOfflineDecision(
        observation=observation,
        episode_id="offline-episode",
        error_labels={
            "omnigent.last_task_error_code": "runner_disconnected",
            "omnigent.last_task_error_message": "Synthetic runner exit",
            "omnigent.last_task_error_runner_id": observation.runner_id or "",
        },
        activity_turn_id=observation.response_id,
        scheduled_run_id="captured-run",
    )


@pytest.mark.parametrize(
    "field",
    [
        "live_status_response_id",
        "live_status_error_labels",
        "runner_offline_decision",
        "terminal_startup_claim",
        "live_status_observation_id",
        "live_status_error_key",
    ],
)
@pytest.mark.parametrize("write_path", ["orm", "core"])
def test_observation_columns_bound_final_stored_bytes(
    store: SqlAlchemyConversationStore,
    stored_byte_boundaries: tuple[str, str, str],
    field: str,
    write_path: str,
) -> None:
    fitting, oversized, _ = stored_byte_boundaries
    is_identifier = field in {"live_status_observation_id", "live_status_error_key"}
    if is_identifier:
        fitting, oversized = "🎉" * 64, "🎉" * 65
    conv = store.create_conversation(runner_id="runner-a")
    key = (current_workspace_id(), conv.id)
    column = getattr(SqlConversationMetadata, field)

    def write(value: str) -> None:
        with store._session_immediate("test_observation_payload_bound") as session:
            if write_path == "orm":
                meta = session.get(SqlConversationMetadata, key)
                assert meta is not None
                setattr(meta, field, value)
                meta.live_status_revision += 1
            else:
                session.execute(
                    update(SqlConversationMetadata)
                    .where(
                        SqlConversationMetadata.workspace_id == key[0],
                        SqlConversationMetadata.id == key[1],
                    )
                    .values(
                        {
                            field: value,
                            "live_status_revision": SqlConversationMetadata.live_status_revision
                            + 1,
                        }
                    )
                )

    write(fitting)
    with store._session("test_read_stored_observation_payload") as session:
        meta = session.get(SqlConversationMetadata, key)
        assert meta is not None and getattr(meta, field) == fitting
        revision = meta.live_status_revision
        assert session.scalar(
            select(func.length(column)).where(
                SqlConversationMetadata.workspace_id == key[0],
                SqlConversationMetadata.id == key[1],
            )
        ) == (64 if is_identifier else 16_383)
    with pytest.raises(StatementError, match=r"stored bytes|64 characters"):
        write(oversized)
    with store._session("test_read_retained_observation_payload") as session:
        meta = session.get(SqlConversationMetadata, key)
        assert meta is not None and getattr(meta, field) == fitting
        assert meta.live_status_revision == revision


@pytest.mark.parametrize(
    "field", ["response", "labels", "decision", "claim", "observation_id", "error_key"]
)
def test_rejected_observation_payload_preserves_its_committed_owner(
    store: SqlAlchemyConversationStore,
    stored_byte_boundaries: tuple[str, str, str],
    field: str,
) -> None:
    fitting, _, oversized = stored_byte_boundaries
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(
        conv.id, "running", response_id=fitting if field == "claim" else "turn-a"
    )
    assert original is not None
    owner = (
        original
        if field == "claim"
        else store.begin_terminal_startup(conv.id, original, "startup-a")
    )
    assert owner is not None
    before = store.get_conversation(conv.id)
    with pytest.raises(StatementError, match=r"stored bytes|64 characters"):
        if field == "response":
            store.record_session_observation(
                conv.id,
                "running",
                response_id=oversized,
                expected_observation=owner,
                starts_new_turn=True,
            )
        elif field == "labels":
            store.record_session_observation(
                conv.id,
                "failed",
                response_id="turn-a",
                expected_observation=owner,
                error_labels={"omnigent.last_task_error_message": oversized},
            )
        elif field == "decision":
            store.record_runner_offline_decision(
                conv.id,
                _decision(owner).model_copy(update={"error_labels": {"message": oversized}}),
            )
        elif field == "observation_id":
            store.record_session_observation(
                conv.id, "waiting", expected_observation=owner, observation_id="🎉" * 65
            )
        elif field == "error_key":
            store.record_session_observation(
                conv.id, "waiting", expected_observation=owner, error_key="🎉" * 65
            )
        else:
            store.begin_terminal_startup(conv.id, owner, "startup-too-large")
    after = store.get_conversation(conv.id)
    assert after is not None and before is not None
    assert after.session_observation == owner
    assert after.terminal_pending == before.terminal_pending
    assert after.runner_offline_decision == before.runner_offline_decision
    assert after.live_status_error_labels == before.live_status_error_labels
    if field != "claim":
        assert store.finish_terminal_startup(conv.id, owner, "ready-a") is not None


def test_binding_epoch_changes_only_when_the_runner_binding_changes(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation()
    assert conv.runner_binding_epoch == 0
    assert store.set_runner_id(conv.id, "runner-a")
    assert not store.set_runner_id(conv.id, "runner-a")
    assert store.get_conversation(conv.id).runner_binding_epoch == 1
    assert store.replace_runner_id(conv.id, "runner-a").runner_binding_epoch == 1
    assert store.replace_runner_id(conv.id, "runner-b").runner_binding_epoch == 2
    assert store.replace_runner_id(conv.id, "runner-a").runner_binding_epoch == 3
    unchanged = store.replace_runner_id(conv.id, "runner-b", expected_runner_id="runner-old")
    assert unchanged.runner_id == "runner-a" and unchanged.runner_binding_epoch == 3
    assert store.clear_runner_id(conv.id).runner_binding_epoch == 4
    assert store.clear_runner_id(conv.id).runner_binding_epoch == 4
    assert store.set_runner_id(conv.id, "runner-a")
    assert store.clear_host_binding(conv.id).runner_binding_epoch == 6


def test_recovery_rebinding_requires_the_exact_captured_observation(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    captured = store.record_session_observation(conv.id, "running", response_id="old-turn")
    assert captured is not None
    current = store.record_session_observation(conv.id, "running", response_id="new-turn")
    assert current is not None
    unchanged = store.replace_runner_id(conv.id, "runner-parent", expected_observation=captured)
    assert unchanged.runner_id == "runner-a" and unchanged.session_observation == current
    store.replace_runner_id(conv.id, "runner-b")
    store.replace_runner_id(conv.id, "runner-a")
    current = store.get_session_observation(conv.id)
    unchanged = store.replace_runner_id(conv.id, "runner-parent", expected_observation=captured)
    assert unchanged.runner_id == "runner-a" and unchanged.session_observation == current
    moved = store.replace_runner_id(conv.id, "runner-parent", expected_observation=current)
    assert moved.runner_id == "runner-parent"
    assert moved.runner_binding_epoch == current.binding_epoch + 1


@pytest.mark.parametrize("binding", ["replace", "clear_then_set"])
@pytest.mark.parametrize("cause", ["legacy", "legacy_owned", "canonical"])
def test_binding_preserves_only_unowned_legacy_recovery_cause(
    store: SqlAlchemyConversationStore, binding: str, cause: str
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    error = {
        "omnigent.last_task_error_code": "runner_disconnected",
        "omnigent.last_task_error_message": "Disconnected",
    }
    if cause == "legacy_owned":
        error["omnigent.last_task_error_runner_id"] = "runner-a"
    store.set_labels(conv.id, {"custom.label": "keep", **error})
    if cause == "canonical":
        store.record_session_observation(conv.id, "failed", error_labels=error)
    else:
        store.set_session_live_status(conv.id, "failed")
    if binding == "replace":
        rebound = store.replace_runner_id(conv.id, "runner-b")
    else:
        store.clear_runner_id(conv.id)
        assert store.set_runner_id(conv.id, "runner-b")
        rebound = store.get_conversation(conv.id)
    assert rebound is not None and rebound.live_status == "failed"
    assert rebound.labels == (
        {"custom.label": "keep", **error} if cause == "legacy" else {"custom.label": "keep"}
    )
    store.record_session_observation(conv.id, "running", response_id="new-turn")
    assert store.get_conversation(conv.id).labels == {"custom.label": "keep"}


@pytest.mark.parametrize("terminal", ["idle", "failed"])
def test_an_old_response_cannot_close_a_newer_busy_response(
    store: SqlAlchemyConversationStore,
    terminal: str,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    current = store.record_session_observation(conv.id, "running", response_id="new-turn")
    assert current is not None
    assert (
        store.record_session_observation(
            conv.id,
            terminal,
            response_id="old-turn",
            expected_binding_epoch=current.binding_epoch,
            expected_revision=current.revision,
        )
        is None
    )
    assert store.get_session_observation(conv.id) == current


@pytest.mark.parametrize("superseded", [False, True])
def test_setup_failure_can_start_its_own_response_only_at_the_exact_captured_revision(
    store: SqlAlchemyConversationStore, superseded: bool
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    captured = store.record_session_observation(conv.id, "running", response_id="prior-turn")
    assert captured is not None
    if superseded:
        store.record_session_observation(conv.id, "running", response_id="newer-turn")
    accepted = store.record_session_observation(
        conv.id,
        "failed",
        response_id="setup-turn",
        error_key="setup-failure",
        expected_binding_epoch=captured.binding_epoch,
        expected_revision=captured.revision,
        starts_new_turn=True,
    )
    if superseded:
        assert accepted is None
        assert store.get_session_observation(conv.id).response_id == "newer-turn"
    else:
        assert accepted is not None and accepted.response_id == "setup-turn"
    with pytest.raises(ValueError, match="exact captured observation"):
        store.record_session_observation(conv.id, "failed", starts_new_turn=True)


def test_startup_reserves_a_new_revision_and_preserves_the_last_normal_result(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    labels = {"omnigent.last_task_error_code": "actual-error"}
    original = store.record_session_observation(
        conv.id, "failed", response_id="old-turn", error_key="actual-error", error_labels=labels
    )
    assert original is not None
    startup = store.begin_terminal_startup(conv.id, original, "startup-b")
    assert startup == original.model_copy(
        update={"revision": original.revision + 1, "observation_id": "startup-b"}
    )
    assert store.get_conversation(conv.id).labels == labels
    assert store.get_conversation(conv.id).terminal_pending is True
    assert store.begin_terminal_startup(conv.id, original, "startup-b") == startup
    assert store.begin_terminal_startup(conv.id, startup, "startup-b") is None
    newer = store.begin_terminal_startup(conv.id, startup, "startup-c")
    assert newer is not None and newer.revision == startup.revision + 1
    assert store.begin_terminal_startup(conv.id, original, "startup-b") is None
    assert store.get_session_observation(conv.id) == newer
    completed = store.finish_terminal_startup(conv.id, newer, "ready-c")
    assert completed == newer.model_copy(
        update={"revision": newer.revision + 1, "observation_id": "ready-c"}
    )
    assert store.get_conversation(conv.id).terminal_pending is False
    assert store.finish_terminal_startup(conv.id, newer, "ready-c") == completed
    pending = store.begin_terminal_startup(conv.id, completed, "startup-d")
    assert pending is not None and store.get_conversation(conv.id).terminal_pending is True
    assert store.finish_terminal_startup(conv.id, newer, "ready-c") is None
    assert store.get_session_observation(conv.id) == pending


def test_startup_recovery_invalidates_the_derived_interruption_without_retagging_it(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(conv.id, "running", response_id="old-turn")
    assert original is not None
    decision = _decision(original)
    assert store.record_runner_offline_decision(conv.id, decision) is not None
    startup = store.begin_terminal_startup(conv.id, original, "startup-b")
    assert startup is not None and startup.status == "running"
    current = store.get_conversation(conv.id)
    assert current.runner_offline_decision is None and current.labels == {}
    assert decision.observation == original and decision.episode_id == "offline-episode"


@pytest.mark.parametrize("previous_response", [None, "turn-a"])
def test_prior_turn_completion_keeps_the_exact_startup_claim(
    store: SqlAlchemyConversationStore, previous_response: str | None
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    previous = store.record_session_observation(conv.id, "running", response_id=previous_response)
    assert previous is not None
    startup = store.begin_terminal_startup(conv.id, previous, "startup-a")
    assert startup is not None
    idle = store.record_session_observation(
        conv.id,
        "idle",
        response_id="turn-a",
        observation_id="completed-a",
        expected_observation=startup,
        expected_binding_epoch=startup.binding_epoch,
        expected_revision=startup.revision,
    )
    assert idle is not None and idle.status == "idle" and idle.response_id == "turn-a"
    assert store.get_conversation(conv.id).terminal_pending is True
    assert store.begin_terminal_startup(conv.id, previous, "startup-a") == startup
    assert (
        store.record_session_observation(
            conv.id,
            "idle",
            response_id="turn-a",
            observation_id="completed-a",
            expected_observation=startup,
            expected_binding_epoch=startup.binding_epoch,
            expected_revision=startup.revision,
        )
        == idle
    )
    completed = store.finish_terminal_startup(conv.id, startup, "ready-a")
    assert completed == idle.model_copy(
        update={"revision": idle.revision + 1, "observation_id": "ready-a"}
    )
    assert store.finish_terminal_startup(conv.id, startup, "ready-a") == completed
    assert store.get_conversation(conv.id).terminal_pending is False


@pytest.mark.parametrize(
    "replacement",
    ["running", "waiting", "failed", "new-idle", "startup", "binding-aba", "offline"],
)
def test_prior_completion_cannot_keep_a_superseded_startup_claim(
    store: SqlAlchemyConversationStore, replacement: str
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    previous = store.record_session_observation(conv.id, "running", response_id="turn-a")
    assert previous is not None
    startup = store.begin_terminal_startup(conv.id, previous, "startup-a")
    assert startup is not None
    idle = store.record_session_observation(
        conv.id, "idle", response_id="turn-a", expected_observation=startup
    )
    assert idle is not None
    if replacement == "startup":
        assert store.begin_terminal_startup(conv.id, idle, "startup-b") is not None
    elif replacement == "binding-aba":
        store.replace_runner_id(conv.id, "runner-b")
        store.replace_runner_id(conv.id, "runner-a")
    elif replacement == "offline":
        assert store.record_runner_offline_decision(conv.id, _decision(idle)) is not None
    else:
        status = "idle" if replacement == "new-idle" else replacement
        assert (
            store.record_session_observation(
                conv.id,
                status,
                response_id="turn-b",
                expected_observation=idle,
                starts_new_turn=True,
                error_key="actual-error" if replacement == "failed" else None,
                error_labels={"omnigent.last_task_error_code": "actual-error"}
                if replacement == "failed"
                else None,
            )
            is not None
        )
    before = store.get_conversation(conv.id)
    assert before is not None
    assert store.finish_terminal_startup(conv.id, startup, "ready-a") is None
    after = store.get_conversation(conv.id)
    assert after is not None and after.session_observation == before.session_observation
    assert after.terminal_pending == before.terminal_pending
    assert after.labels == before.labels
    assert after.runner_offline_decision == before.runner_offline_decision


@pytest.mark.parametrize("status", ["running", "waiting", "failed"])
def test_duplicate_busy_or_failure_edge_invalidates_the_startup_claim(
    store: SqlAlchemyConversationStore, status: str
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(conv.id, status, response_id="turn-a")
    assert original is not None
    startup = store.begin_terminal_startup(conv.id, original, "startup-a")
    assert startup is not None
    busy = store.record_session_observation(
        conv.id, status, response_id="turn-a", expected_observation=startup
    )
    assert busy is not None and busy.revision > startup.revision
    assert store.get_conversation(conv.id).terminal_pending is False
    assert store.finish_terminal_startup(conv.id, startup, "ready-a") is None


@pytest.mark.parametrize("replacement", ["unowned-idle", "different-idle", "real-failure"])
def test_idle_or_failure_without_the_protected_lineage_cannot_complete_startup(
    store: SqlAlchemyConversationStore, replacement: str
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(conv.id, "running", response_id="turn-a")
    assert original is not None
    startup = store.begin_terminal_startup(conv.id, original, "startup-a")
    assert startup is not None
    idle = store.record_session_observation(
        conv.id, "idle", response_id="turn-a", expected_observation=startup
    )
    assert idle is not None
    failed = replacement == "real-failure"
    current = store.record_session_observation(
        conv.id,
        "failed" if failed else "idle",
        response_id="turn-b" if replacement == "different-idle" else "turn-a",
        expected_observation=None if replacement == "unowned-idle" else idle,
        error_key="actual-error" if failed else None,
        error_labels={"omnigent.last_task_error_code": "actual-error"} if failed else None,
    )
    assert current is not None and current.revision > idle.revision
    assert not store.get_conversation(conv.id).terminal_pending
    assert store.finish_terminal_startup(conv.id, startup, "ready-a") is None
    assert store.get_session_observation(conv.id) == current
    if failed:
        assert store.get_conversation(conv.id).labels == {
            "omnigent.last_task_error_code": "actual-error"
        }


def test_late_idle_cannot_cross_a_new_startup_reservation(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(conv.id, "running", response_id="turn-a")
    assert original is not None
    startup_a = store.begin_terminal_startup(conv.id, original, "startup-a")
    assert startup_a is not None
    idle_a = store.record_session_observation(
        conv.id, "idle", response_id="turn-a", expected_observation=startup_a
    )
    assert idle_a is not None
    startup_b = store.begin_terminal_startup(conv.id, idle_a, "startup-b")
    assert startup_b is not None
    assert (
        store.record_session_observation(
            conv.id,
            "idle",
            response_id="turn-a",
            observation_id="old-idle",
            expected_observation=idle_a,
        )
        is None
    )
    assert store.finish_terminal_startup(conv.id, startup_a, "ready-a") is None
    assert store.get_session_observation(conv.id) == startup_b
    completed_b = store.finish_terminal_startup(conv.id, startup_b, "ready-b")
    assert completed_b is not None and completed_b.response_id == "turn-a"


@pytest.mark.parametrize("same_observation_id", [False, True])
def test_completed_startup_receipt_cannot_survive_new_work_reusing_the_turn_id(
    store: SqlAlchemyConversationStore, same_observation_id: bool
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(conv.id, "idle", response_id="turn-a")
    assert original is not None
    startup = store.begin_terminal_startup(conv.id, original, "startup-a")
    assert startup is not None
    completed = store.finish_terminal_startup(conv.id, startup, "ready-a")
    assert completed is not None
    newer = store.record_session_observation(
        conv.id,
        "idle",
        response_id="turn-a",
        observation_id="ready-a" if same_observation_id else "new-work",
        expected_observation=completed,
        starts_new_turn=True,
    )
    assert newer is not None and newer.revision == completed.revision + 1
    assert store.finish_terminal_startup(conv.id, startup, "ready-a") is None
    assert store.get_session_observation(conv.id) == newer


@pytest.mark.parametrize("claim_payload", [None, "{}"])
def test_legacy_or_malformed_pending_row_has_no_implicit_startup_claim(
    store: SqlAlchemyConversationStore, claim_payload: str | None
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    original = store.record_session_observation(conv.id, "running", response_id="turn-a")
    assert original is not None
    startup = store.begin_terminal_startup(conv.id, original, "startup-a")
    assert startup is not None
    with store._session_immediate("simulate_legacy_terminal_startup") as session:
        meta = session.get(SqlConversationMetadata, (current_workspace_id(), conv.id))
        assert meta is not None
        meta.terminal_startup_claim = claim_payload
    assert store.finish_terminal_startup(conv.id, startup, "ready-a") is None
    assert store.get_session_observation(conv.id) == startup
    assert store.get_conversation(conv.id).terminal_pending


def test_startup_retry_removes_only_offline_overlay_and_keeps_the_real_failure_payload(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    error = {
        "omnigent.last_task_error_code": "actual-error",
        "omnigent.last_task_error_message": "Actual source failure",
    }
    failed = store.record_session_observation(
        conv.id, "failed", response_id="turn-a", error_key="actual-error", error_labels=error
    )
    assert failed is not None
    startup_a = store.begin_terminal_startup(conv.id, failed, "startup-a")
    assert startup_a is not None
    assert store.record_runner_offline_decision(conv.id, _decision(startup_a)) is not None
    assert (
        store.get_conversation(conv.id).labels["omnigent.last_task_error_code"]
        == "runner_disconnected"
    )
    startup_b = store.begin_terminal_startup(conv.id, startup_a, "startup-b")
    assert startup_b is not None and startup_b.error_key == failed.error_key
    current = store.get_conversation(conv.id)
    assert current is not None and current.runner_offline_decision is None
    assert current.live_status == "failed" and current.labels == error
    assert current.live_status_error_labels == error and current.terminal_pending is True


def test_observation_revisions_are_ordered_and_duplicate_busy_edges_are_stable(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    running = store.record_session_observation(
        conv.id, "running", response_id="turn-one", observation_id="observed-running"
    )
    duplicate = store.record_session_observation(
        conv.id, "running", response_id="turn-one", observation_id="duplicate-busy"
    )
    assert running is not None and running.revision == 1
    assert duplicate == running
    failed = store.record_session_observation(
        conv.id, "failed", response_id="turn-one", error_key="native-error"
    )
    assert failed is not None and failed.revision == 2
    assert (
        store.record_session_observation(
            conv.id, "failed", response_id="turn-one", error_key="native-error"
        )
        == failed
    )
    idle = store.record_session_observation(conv.id, "idle")
    next_turn = store.record_session_observation(conv.id, "running", response_id="turn-two")
    newer_turn = store.record_session_observation(conv.id, "running", response_id="turn-three")
    assert idle is not None and idle.revision == 3
    assert next_turn is not None and next_turn.revision == 4
    assert newer_turn is not None and newer_turn.revision == 5


@pytest.mark.parametrize("terminal", ["idle", "failed"])
@pytest.mark.parametrize("busy", ["running", "waiting"])
def test_a_new_no_id_busy_turn_does_not_reopen_a_terminal_response(
    store: SqlAlchemyConversationStore, terminal: str, busy: str
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    store.record_session_observation(conv.id, "running", response_id="old-turn")
    ended = store.record_session_observation(conv.id, terminal, response_id="old-turn")
    current = store.record_session_observation(conv.id, busy)
    assert ended is not None and current is not None
    assert current.response_id is None and current.revision == ended.revision + 1


def test_ordered_promotion_accepts_only_the_exact_already_queued_observation(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    saved = store.record_session_observation(conv.id, "idle", observation_id="old-idle")
    assert saved is not None
    queued = store.record_session_observation(
        conv.id, "running", response_id="turn", observation_id="queued-running"
    )
    promoted = store.record_session_observation(
        conv.id,
        "running",
        response_id="turn",
        observation_id="queued-running",
        expected_observation=saved,
        expected_binding_epoch=saved.binding_epoch,
        expected_revision=saved.revision,
    )
    assert promoted == queued
    assert (
        store.record_session_observation(
            conv.id,
            "running",
            response_id="turn",
            observation_id="different-running",
            expected_observation=saved,
            expected_binding_epoch=saved.binding_epoch,
            expected_revision=saved.revision,
        )
        is None
    )
    assert store.get_session_observation(conv.id) == queued


def test_owned_decision_is_metadata_only_idempotent_and_inert_after_aba_rebind(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    observation = store.record_session_observation(conv.id, "running", response_id="old-turn")
    assert observation is not None
    decision = _decision(observation)
    recorded = store.record_runner_offline_decision(conv.id, decision)
    repeated = store.record_runner_offline_decision(conv.id, decision)
    assert recorded is not None and recorded.created
    assert repeated is not None and not repeated.created and repeated.decision == decision
    failed = store.get_conversation(conv.id)
    assert failed is not None and failed.live_status == "failed"
    assert failed.runner_offline_decision == decision
    assert failed.labels["omnigent.last_task_error_code"] == "runner_disconnected"
    assert failed.updated_at == conv.updated_at
    assert store.get_session_observation(conv.id) == observation
    with store._conv_session("inspect_offline_label_persistence") as session:
        assert session.scalar(select(func.count()).select_from(SqlConversationLabel)) == 0
    replacement = store.replace_runner_id(conv.id, "runner-b")
    assert replacement.runner_offline_decision is None
    assert "omnigent.last_task_error_code" not in replacement.labels
    restored = store.replace_runner_id(conv.id, "runner-a")
    assert restored.runner_binding_epoch == observation.binding_epoch + 2
    assert store.record_runner_offline_decision(conv.id, decision) is None
    assert store.get_conversation(conv.id).runner_offline_decision is None


def test_normal_error_projection_clears_only_managed_keys_and_preserves_legacy_history(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    historical = {
        "custom.label": "keep",
        "omnigent.last_task_error_code": "legacy-error",
        "omnigent.last_task_error_message": "legacy detail",
    }
    store.set_labels(conv.id, historical)
    assert store.get_conversation(conv.id).labels == historical
    current_labels = {
        "omnigent.last_task_error_code": "provider-error",
        "omnigent.last_task_error_message": "current provider failure",
    }
    failed = store.record_session_observation(
        conv.id,
        "failed",
        response_id="turn",
        error_key="current-error",
        error_labels=current_labels,
    )
    assert failed is not None
    assert store.get_conversation(conv.id).labels == {"custom.label": "keep", **current_labels}
    assert store.settle_stopped_session_observation(conv.id, "runner-a") is None
    assert store.settle_orphaned_session_observation(conv.id, 9999999999) is None
    assert store.record_session_observation(conv.id, "failed") == failed
    assert store.get_conversation(conv.id).labels == {"custom.label": "keep", **current_labels}
    store.record_session_observation(conv.id, "running", response_id="new-turn")
    assert store.get_conversation(conv.id).labels == {"custom.label": "keep"}
    store.replace_runner_id(conv.id, "runner-b")
    assert store.get_conversation(conv.id).labels == {"custom.label": "keep"}
    with store._conv_session("inspect_legacy_error_history") as session:
        persisted = session.scalars(select(SqlConversationLabel)).all()
    assert {row.key: row.value for row in persisted} == historical


def test_duplicate_busy_observation_cannot_recover_an_owned_interruption(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    busy = store.record_session_observation(
        conv.id, "running", response_id="turn", observation_id="original-busy"
    )
    assert busy is not None
    decision = _decision(busy)
    assert store.record_runner_offline_decision(conv.id, decision) is not None
    for token in ("original-busy", "duplicate-busy"):
        assert store.record_session_observation(conv.id, "running", observation_id=token) is None
    assert store.get_conversation(conv.id).runner_offline_decision == decision
    assert store.get_session_observation(conv.id) == busy
    recovered = store.record_session_observation(conv.id, "idle")
    assert recovered is not None and recovered.revision > busy.revision
    assert store.get_conversation(conv.id).runner_offline_decision is None


def test_owned_child_activity_read_projection_preserves_pagination_and_legacy_items(
    store: SqlAlchemyConversationStore,
) -> None:
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, runner_id="runner-a")
    observed = store.record_session_observation(child.id, "running", response_id="old-turn")
    assert observed is not None
    decision = _decision(observed)
    assert store.record_runner_offline_decision(child.id, decision) is not None
    markers = store.append(
        parent.id,
        [
            NewConversationItem(
                type="resource_event",
                response_id="old-turn",
                data=ResourceEventData(
                    event_type="session.subagent.returned",
                    resource_id=child.id,
                    resource_type="session",
                    resource={
                        "status": "failed",
                        "session_observation": observed.model_dump(),
                        "runner_offline_episode_id": decision.episode_id,
                    },
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="message-turn",
                data=MessageData(role="user", content=[{"type": "text", "text": "next message"}]),
            ),
            NewConversationItem(
                type="resource_event",
                response_id="marker-turn",
                data=ResourceEventData(
                    event_type="session.subagent.returned",
                    resource_id=child.id,
                    resource_type="session",
                    resource={"status": "failed"},
                ),
            ),
        ],
    )
    assert store.get_item(parent.id, markers[0].id) is not None
    assert len(store.list_items(parent.id).data) == 3
    store.replace_runner_id(child.id, "runner-b")
    first = store.list_items(parent.id, limit=1)
    assert first.data == [] and first.has_more
    assert first.first_id == first.last_id == markers[0].id
    following = store.list_items(parent.id, after=first.last_id)
    assert [item.id for item in following.data] == [item.id for item in markers[1:]]
    assert not following.has_more
    assert store.get_item(parent.id, markers[0].id) is None
    with workspace_scope(424242):
        assert store.get_item(parent.id, markers[0].id) is None
        assert store.list_items(parent.id).data == []


def test_activity_metadata_outage_preserves_ordinary_history(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = store.create_conversation()
    items = store.append(
        parent.id,
        [
            NewConversationItem(
                type="message",
                response_id="message-turn",
                data=MessageData(role="user", content=[{"type": "text", "text": "keep me"}]),
            ),
            NewConversationItem(
                type="resource_event",
                response_id="marker-turn",
                data=ResourceEventData(
                    event_type="session.subagent.returned",
                    resource_id="a" * 32,
                    resource_type="session",
                    resource={
                        "session_observation": SessionObservation(
                            runner_id="runner-a"
                        ).model_dump(),
                        "runner_offline_episode_id": "old-episode",
                    },
                ),
            ),
        ],
    )

    def unavailable(_operation: str):
        raise RuntimeError("synthetic metadata outage")

    monkeypatch.setattr(store, "_session", unavailable)
    visible = store.list_items(parent.id)
    assert [item.id for item in visible.data] == [items[0].id]
    assert visible.last_id == items[1].id


@pytest.mark.parametrize(
    ("status", "response_id", "error_key"),
    [
        ("idle", None, None),
        ("failed", "old-turn", "new-native-error"),
        ("running", "new-turn", None),
        ("waiting", "old-turn", None),
    ],
)
def test_new_observation_invalidates_an_old_failure_before_or_after_commit(
    store: SqlAlchemyConversationStore,
    status: str,
    response_id: str | None,
    error_key: str | None,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    observation = store.record_session_observation(conv.id, "running", response_id="old-turn")
    assert observation is not None
    decision = _decision(observation)
    assert store.record_runner_offline_decision(conv.id, decision) is not None
    newer = store.record_session_observation(
        conv.id, status, response_id=response_id, error_key=error_key
    )
    assert newer is not None and newer.revision > observation.revision
    assert store.record_runner_offline_decision(conv.id, decision) is None
    refreshed = store.get_conversation(conv.id)
    assert refreshed is not None and refreshed.live_status == status
    assert refreshed.runner_offline_decision is None
    assert "omnigent.last_task_error_code" not in refreshed.labels


def test_observation_and_failure_operations_are_workspace_scoped(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    observed = store.record_session_observation(conv.id, "running", response_id="turn")
    assert observed is not None
    with workspace_scope(424242):
        assert store.get_session_observation(conv.id) is None
        assert store.begin_terminal_startup(conv.id, observed, "other-startup") is None
        assert store.finish_terminal_startup(conv.id, observed, "other-ready") is None
        assert store.record_session_observation(conv.id, "idle") is None
        assert store.record_runner_offline_decision(conv.id, _decision(observed)) is None
    assert store.get_session_observation(conv.id) == observed
