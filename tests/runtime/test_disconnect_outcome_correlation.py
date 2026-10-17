from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from omnigent.entities.session_observation import RunnerOfflineDecision, SessionObservation
from omnigent.runtime import disconnect_outcome, inflight_text, session_stream


@dataclass
class Scenario:
    session_id: str
    failure: RunnerOfflineDecision
    recovered: SessionObservation
    rows: list[dict[str, Any]]
    observed: list[dict[str, Any]]

    def arm(self) -> None:
        disconnect_outcome.note_disconnect_failure_cleared(
            self.session_id,
            failure_code="runner_disconnected",
            runner_id=self.failure.observation.runner_id,
            decision=self.failure,
            recovered_observation=self.recovered,
        )

    def publish(self, owner: SessionObservation, event: dict[str, Any]) -> None:
        assert session_stream.learn_observation(self.session_id, owner)
        session_stream.publish(
            self.session_id, {**event, "session_observation": owner.model_dump()}
        )

    def outcomes(self) -> list[str]:
        return [
            r["attributes"]["outcome"]
            for r in self.rows
            if r["event_name"] == "session_turn_failure_outcome"
        ]


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Scenario:
    disconnect_outcome.reset_for_tests()
    session_stream.reset_observation_floors()
    inflight_text.reset_for_tests()
    rows: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []

    class Logger:
        def info(self, *_args: Any, **kwargs: Any) -> None:
            rows.append(kwargs["extra"])

    monkeypatch.setattr(disconnect_outcome, "_logger", Logger())
    observer = disconnect_outcome.observe_event

    def observe(session_id: str, event: dict[str, Any]) -> None:
        observed.append(event)
        observer(session_id, event)

    monkeypatch.setattr(disconnect_outcome, "observe_event", observe)
    lost = SessionObservation(
        runner_id="runner-A",
        binding_epoch=2,
        revision=7,
        observation_id="owned-A",
        status="running",
        response_id="turn-A",
    )
    recovered = lost.model_copy(
        update={
            "revision": 8,
            "observation_id": "recovered-A",
            "status": "idle",
            "response_id": None,
        }
    )
    failure = RunnerOfflineDecision(
        observation=lost,
        episode_id="failure-episode-A",
        error_labels={"omnigent.last_task_error_code": "runner_disconnected"},
        activity_turn_id="turn-A",
    )
    value = Scenario("controlled-" + request.node.name, failure, recovered, rows, observed)
    yield value
    disconnect_outcome.reset_for_tests()
    session_stream.reset_observation_floors()
    inflight_text.reset_for_tests()


def running(
    s: Scenario,
    *,
    response_id: str = "turn-A",
    runner_id: str = "runner-A",
    binding_epoch: int = 2,
) -> SessionObservation:
    return s.recovered.model_copy(
        update={
            "revision": 9,
            "observation_id": "accepted-" + response_id,
            "status": "running",
            "response_id": response_id,
            "runner_id": runner_id,
            "binding_epoch": binding_epoch,
        }
    )


@pytest.mark.parametrize(
    "kind", ["other-turn", "other-binding", "error-item", "user-item", "empty-delta"]
)
def test_accepted_non_resume_cannot_reclassify_lost_A(scenario: Scenario, kind: str) -> None:
    s = scenario
    original_failure = s.failure.model_dump_json()
    owner = running(s)
    event: dict[str, Any] = {
        "type": "response.output_text.delta",
        "delta": "progress",
        "response_id": "turn-A",
    }
    if kind == "other-turn":
        owner = running(s, response_id="turn-B")
        event["response_id"] = "turn-B"
    elif kind == "other-binding":
        owner = running(s, runner_id="runner-B", binding_epoch=3)
    elif kind == "error-item":
        event = {
            "type": "response.output_item.done",
            "item": {
                "type": "error",
                "response_id": "turn-A",
                "code": "native_turn_error",
                "message": "synthetic failure",
            },
        }
    elif kind == "user-item":
        event = {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "user",
                "response_id": "turn-A",
                "content": [{"type": "input_text", "text": "new input"}],
            },
        }
    elif kind == "empty-delta":
        event["delta"] = ""
    s.arm()
    s.publish(owner, event)
    assert len(s.observed) == 1, "event must pass the current ownership and replay gates"
    assert s.failure.model_dump_json() == original_failure
    assert "turn_resumed" not in s.outcomes()


@pytest.mark.parametrize("is_meta", [False, True], ids=["real-user", "framework-context"])
def test_native_user_mirror_is_not_new_turn_evidence(scenario: Scenario, is_meta: bool) -> None:
    s = scenario
    owner = running(s)
    s.arm()
    s.publish(
        owner,
        {
            "type": "session.input.consumed",
            "data": {
                "item_id": "input-B",
                "type": "message",
                "data": {
                    "role": "user",
                    "is_meta": is_meta,
                    "content": [{"type": "input_text", "text": "steering"}],
                },
            },
        },
    )
    s.publish(
        owner,
        {"type": "response.output_text.delta", "response_id": "turn-A", "delta": "later output"},
    )
    assert len(s.observed) == 2
    assert s.outcomes() == ["turn_resumed"]


def test_stale_owned_A_after_B_is_rejected_before_accounting(scenario: Scenario) -> None:
    s = scenario
    s.arm()
    assert session_stream.learn_observation(s.session_id, running(s, response_id="turn-B"))
    event = {
        "type": "response.output_text.delta",
        "response_id": "turn-A",
        "delta": "stale",
        "session_observation": s.failure.observation.model_dump(),
    }
    assert session_stream.publish(s.session_id, event) == 0
    assert s.observed == []
    assert s.outcomes() == []


def test_duplicate_native_replay_is_rejected_before_accounting(scenario: Scenario) -> None:
    s = scenario
    owner = running(s)
    event = {
        "type": "response.output_text.delta",
        "response_id": "turn-A",
        "message_id": "native-A",
        "index": 1,
        "delta": "old preview",
    }
    assert inflight_text.record_publish(s.session_id, event) is not None
    s.arm()
    s.publish(owner, event)
    assert s.observed == []
    assert s.outcomes() == []


def test_matching_owned_A_progress_still_records_resume(scenario: Scenario) -> None:
    s = scenario
    s.arm()
    s.publish(
        running(s),
        {
            "type": "response.output_text.delta",
            "response_id": "turn-A",
            "delta": "genuine progress",
        },
    )
    assert len(s.observed) == 1
    assert s.outcomes() == ["turn_resumed"]


def test_explicit_user_turn_prevents_reclassifying_A(scenario: Scenario) -> None:
    s = scenario
    s.arm()
    disconnect_outcome.note_user_turn(s.session_id)
    s.publish(
        running(s),
        {
            "type": "response.output_text.delta",
            "response_id": "turn-A",
            "delta": "after user action",
        },
    )
    assert s.outcomes() == ["superseded_by_user_turn"]


@pytest.mark.parametrize(
    "missing",
    [
        "decision",
        "turn",
        "episode",
        "owner-id",
        "recovered-id",
        "same-revision",
        "other-binding",
        "other-recovered-turn",
        "genuine-task-error",
    ],
)
def test_incomplete_failure_identity_cannot_arm_resume_tracking(
    scenario: Scenario, missing: str
) -> None:
    s = scenario
    decision = s.failure
    recovered = s.recovered
    if missing == "decision":
        decision = None
    elif missing == "turn":
        decision = decision.model_copy(
            update={"observation": decision.observation.model_copy(update={"response_id": None})}
        )
    elif missing == "episode":
        decision = decision.model_copy(update={"episode_id": ""})
    elif missing == "owner-id":
        decision = decision.model_copy(
            update={
                "observation": decision.observation.model_copy(update={"observation_id": None})
            }
        )
    elif missing == "recovered-id":
        recovered = recovered.model_copy(update={"observation_id": None})
    elif missing == "same-revision":
        recovered = recovered.model_copy(update={"revision": decision.observation.revision})
    elif missing == "other-binding":
        recovered = recovered.model_copy(update={"binding_epoch": 3})
    elif missing == "other-recovered-turn":
        recovered = recovered.model_copy(update={"response_id": "turn-B"})
    elif missing == "genuine-task-error":
        decision = decision.model_copy(
            update={"error_labels": {"omnigent.last_task_error_code": "native_turn_error"}}
        )
    disconnect_outcome.note_disconnect_failure_cleared(
        s.session_id,
        failure_code="runner_disconnected",
        decision=decision,
        recovered_observation=recovered,
    )
    s.publish(
        running(s),
        {"type": "response.output_text.delta", "response_id": "turn-A", "delta": "later output"},
    )
    assert s.outcomes() == []
    [cleared] = [row for row in s.rows if row["event_name"] == "session_turn_failure_cleared"]
    assert cleared["attributes"]["outcome_tracking"] is False


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.output_text.delta", "response_id": "turn-A", "delta": "unowned"},
        {
            "type": "response.output_text.delta",
            "response_id": "turn-A",
            "delta": "offline",
            "runner_offline_episode_id": "another-failure",
        },
        {"type": "response.output_text.delta", "response_id": "turn-B", "delta": "mismatched"},
    ],
)
def test_unknown_or_inconsistent_progress_retains_failure(
    scenario: Scenario, event: dict[str, Any]
) -> None:
    s = scenario
    s.arm()
    if event["delta"] != "unowned":
        event = {**event, "session_observation": running(s).model_dump()}
    disconnect_outcome.observe_event(s.session_id, event)
    assert s.outcomes() == []


def test_paused_A_observation_cannot_remove_replacement_episode(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = scenario
    s.arm()
    later_owner = running(s).model_copy(
        update={"revision": 10, "observation_id": "new-failure-owner"}
    )
    later_decision = s.failure.model_copy(
        update={"observation": later_owner, "episode_id": "failure-episode-B"}
    )
    later_recovered = later_owner.model_copy(
        update={
            "revision": 11,
            "observation_id": "new-recovery",
            "status": "idle",
            "response_id": None,
        }
    )
    original = disconnect_outcome._matches_observation

    def replace_after_capture(watch: Any, event: dict[str, Any]) -> bool:
        matched = original(watch, event)
        disconnect_outcome.note_disconnect_failure_cleared(
            s.session_id,
            failure_code="runner_disconnected",
            decision=later_decision,
            recovered_observation=later_recovered,
        )
        return matched

    monkeypatch.setattr(disconnect_outcome, "_matches_observation", replace_after_capture)
    s.publish(
        running(s),
        {
            "type": "response.output_text.delta",
            "response_id": "turn-A",
            "delta": "older accepted progress",
        },
    )
    assert s.outcomes() == []
    assert disconnect_outcome._watches[s.session_id].decision == later_decision
    monkeypatch.setattr(disconnect_outcome, "_matches_observation", original)
    current = later_owner.model_copy(update={"revision": 12, "observation_id": "current-progress"})
    s.publish(
        current,
        {
            "type": "response.output_text.delta",
            "response_id": "turn-A",
            "delta": "current progress",
        },
    )
    [outcome] = [row for row in s.rows if row["event_name"] == "session_turn_failure_outcome"]
    assert outcome["attributes"]["runner_offline_episode_id"] == "failure-episode-B"
    assert outcome["turn_id"] == "turn-A"


def test_watch_captures_failure_labels_before_the_caller_changes_them(scenario: Scenario) -> None:
    s = scenario
    s.arm()
    s.failure.error_labels["omnigent.last_task_error_code"] = "native_turn_error"
    s.publish(
        running(s),
        {
            "type": "response.output_text.delta",
            "response_id": "turn-A",
            "delta": "actual continuation",
        },
    )
    [outcome] = [row for row in s.rows if row["event_name"] == "session_turn_failure_outcome"]
    assert outcome["attributes"]["failure_code"] == "runner_disconnected"
    assert outcome["attributes"]["runner_offline_episode_id"] == "failure-episode-A"


def test_unidentified_later_clear_breaks_the_old_episode_lineage(scenario: Scenario) -> None:
    s = scenario
    s.arm()
    unknown_recovery = s.recovered.model_copy(
        update={"revision": 11, "observation_id": "unidentified-recovery"}
    )
    disconnect_outcome.note_disconnect_failure_cleared(
        s.session_id,
        failure_code="runner_disconnected",
        decision=None,
        recovered_observation=unknown_recovery,
    )
    later = running(s).model_copy(update={"revision": 12, "observation_id": "later-A-progress"})
    s.publish(
        later,
        {"type": "response.output_text.delta", "response_id": "turn-A", "delta": "later progress"},
    )
    assert len(s.observed) == 1
    assert s.outcomes() == []


@pytest.mark.parametrize(
    ("observed_status", "wire_status"),
    [("idle", "running"), ("failed", "running"), ("running", "waiting"), ("waiting", "running")],
)
def test_contradictory_status_is_not_owned_progress(
    scenario: Scenario, observed_status: str, wire_status: str
) -> None:
    s = scenario
    s.arm()
    owner = running(s).model_copy(update={"status": observed_status})
    s.publish(owner, {"type": "session.status", "status": wire_status, "response_id": "turn-A"})
    assert len(s.observed) == 1
    assert s.outcomes() == []


def test_owned_content_completion_does_not_require_a_busy_status(scenario: Scenario) -> None:
    s = scenario
    s.arm()
    owner = running(s).model_copy(update={"status": "idle"})
    s.publish(
        owner,
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "response_id": "turn-A",
                "content": [{"type": "output_text", "text": "completed output"}],
            },
        },
    )
    assert len(s.observed) == 1
    assert s.outcomes() == ["turn_resumed"]


def test_unidentified_clear_cannot_remove_a_concurrent_known_episode(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    s = scenario
    s.arm()
    later_owner = running(s, response_id="turn-B").model_copy(
        update={"revision": 10, "observation_id": "later-owner"}
    )
    later_decision = s.failure.model_copy(
        update={"observation": later_owner, "episode_id": "failure-episode-B"}
    )
    later_recovered = later_owner.model_copy(
        update={
            "revision": 11,
            "observation_id": "later-recovery",
            "status": "idle",
            "response_id": None,
        }
    )
    entered, release = threading.Event(), threading.Event()
    original = disconnect_outcome._captured_watch

    def pause_unknown(code: str, runner_id: str | None, decision: Any, recovered: Any) -> Any:
        if decision is None:
            entered.set()
            assert release.wait(10)
        return original(code, runner_id, decision, recovered)

    monkeypatch.setattr(disconnect_outcome, "_captured_watch", pause_unknown)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            disconnect_outcome.note_disconnect_failure_cleared,
            s.session_id,
            failure_code="runner_disconnected",
            decision=None,
            recovered_observation=later_recovered,
        )
        try:
            assert entered.wait(10)
            disconnect_outcome.note_disconnect_failure_cleared(
                s.session_id,
                failure_code="runner_disconnected",
                decision=later_decision,
                recovered_observation=later_recovered,
            )
        finally:
            release.set()
        pending.result(timeout=10)
    assert disconnect_outcome._watches[s.session_id].decision == later_decision
    current = later_owner.model_copy(update={"revision": 12, "observation_id": "current-B"})
    s.publish(
        current,
        {
            "type": "response.output_text.delta",
            "response_id": "turn-B",
            "delta": "current B progress",
        },
    )
    [outcome] = [row for row in s.rows if row["event_name"] == "session_turn_failure_outcome"]
    assert outcome["attributes"]["runner_offline_episode_id"] == "failure-episode-B"


@pytest.mark.parametrize("resolve_newer", [False, True])
def test_paused_valid_registration_cannot_replace_newer_episode(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, resolve_newer: bool
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    s = scenario
    later_owner = running(s, response_id="turn-B").model_copy(
        update={"revision": 10, "observation_id": "later-owner"}
    )
    later_decision = s.failure.model_copy(
        update={"observation": later_owner, "episode_id": "failure-episode-B"}
    )
    later_recovered = later_owner.model_copy(
        update={
            "revision": 11,
            "observation_id": "later-recovery",
            "status": "idle",
            "response_id": None,
        }
    )
    entered, release = threading.Event(), threading.Event()
    original = disconnect_outcome._captured_watch

    def pause_A(code: str, runner_id: str | None, decision: Any, recovered: Any) -> Any:
        if decision is not None and decision.episode_id == "failure-episode-A":
            entered.set()
            assert release.wait(10)
        return original(code, runner_id, decision, recovered)

    monkeypatch.setattr(disconnect_outcome, "_captured_watch", pause_A)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(s.arm)
        try:
            assert entered.wait(10)
            disconnect_outcome.note_disconnect_failure_cleared(
                s.session_id,
                failure_code="runner_disconnected",
                decision=later_decision,
                recovered_observation=later_recovered,
            )
            assert session_stream.learn_observation(s.session_id, later_recovered)
            if resolve_newer:
                disconnect_outcome.note_user_turn(s.session_id)
        finally:
            release.set()
        pending.result(timeout=10)
    if resolve_newer:
        assert s.session_id not in disconnect_outcome._watches
    else:
        assert disconnect_outcome._watches[s.session_id].decision == later_decision
    older_clear = next(
        row
        for row in s.rows
        if row["event_name"] == "session_turn_failure_cleared"
        and row["attributes"]["runner_offline_episode_id"] == "failure-episode-A"
    )
    assert older_clear["attributes"]["outcome_tracking"] is False
    if resolve_newer:
        assert s.outcomes() == ["superseded_by_user_turn"]
        return
    current = later_owner.model_copy(update={"revision": 12, "observation_id": "current-B"})
    s.publish(
        current,
        {
            "type": "response.output_text.delta",
            "response_id": "turn-B",
            "delta": "current B progress",
        },
    )
    [outcome] = [row for row in s.rows if row["event_name"] == "session_turn_failure_outcome"]
    assert outcome["attributes"]["runner_offline_episode_id"] == "failure-episode-B"


def test_user_turn_fences_a_registration_that_has_not_installed_a_watch(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = scenario
    original = disconnect_outcome._captured_watch

    def start_user_turn(code: str, runner_id: str | None, decision: Any, recovered: Any) -> Any:
        disconnect_outcome.note_user_turn(s.session_id)
        return original(code, runner_id, decision, recovered)

    monkeypatch.setattr(disconnect_outcome, "_captured_watch", start_user_turn)
    s.arm()
    assert s.session_id not in disconnect_outcome._watches
    assert s.outcomes() == []


def test_already_newer_registration_rejects_an_older_valid_clear(scenario: Scenario) -> None:
    s = scenario
    later_owner = running(s, response_id="turn-B").model_copy(
        update={"revision": 10, "observation_id": "later-owner"}
    )
    later_decision = s.failure.model_copy(
        update={"observation": later_owner, "episode_id": "failure-episode-B"}
    )
    later_recovered = later_owner.model_copy(
        update={
            "revision": 11,
            "observation_id": "later-recovery",
            "status": "idle",
            "response_id": None,
        }
    )
    disconnect_outcome.note_disconnect_failure_cleared(
        s.session_id,
        failure_code="runner_disconnected",
        decision=later_decision,
        recovered_observation=later_recovered,
    )
    s.arm()
    assert disconnect_outcome._watches[s.session_id].decision == later_decision
    older_clear = next(
        row
        for row in s.rows
        if row["event_name"] == "session_turn_failure_cleared"
        and row["attributes"]["runner_offline_episode_id"] == "failure-episode-A"
    )
    assert older_clear["attributes"]["outcome_tracking"] is False
