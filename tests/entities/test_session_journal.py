"""Validation of journal entries before they are stored."""

from __future__ import annotations

import pytest

from omnigent.entities.session_journal import (
    NOTE_BODY_MAX_BYTES,
    PLAN_MAX_RAW_BYTES,
    PLAN_MAX_STEPS,
    PLAN_NAME_MAX_CHARS,
    InvalidJournalEntryError,
    JournalEntryTooLargeError,
    normalize_note,
    normalize_payload,
    normalize_plan,
    normalize_status,
    normalize_step_id,
)


def _step(step_id: str, *depends_on: str, **fields: object) -> dict[str, object]:
    return {"id": step_id, "name": step_id.title(), "depends_on": list(depends_on), **fields}


@pytest.mark.parametrize("step", ["propose", "opt-n1", "a", "x_2", "9lives", "a" * 64])
def test_step_ids_accept_short_lowercase_ids(step: str) -> None:
    assert normalize_step_id(f" {step} ") == step


@pytest.mark.parametrize("step", ["", "Propose", "-lead", "has space", "a" * 65, "ünï"])
def test_step_ids_reject_other_forms(step: str) -> None:
    with pytest.raises(InvalidJournalEntryError, match="step must be a short ID"):
        normalize_step_id(step)
    with pytest.raises(InvalidJournalEntryError, match="step must be a string"):
        normalize_step_id(7)


def test_status_text_is_required_except_for_started() -> None:
    assert normalize_status("started") == {"status": "started", "text": None}
    assert normalize_status("started", text="  ") == {"status": "started", "text": None}
    assert normalize_status("done", text="PR  #13102\nmerged") == {
        "status": "done",
        "text": "PR #13102 merged",
    }
    assert normalize_status("needs_input", text="Approve the deploy to prod?") == {
        "status": "needs_input",
        "text": "Approve the deploy to prod?",
    }
    for status in ("done", "needs_input", "failed"):
        with pytest.raises(InvalidJournalEntryError, match=f"text is required for a {status}"):
            normalize_status(status)


def test_status_rejects_unknown_values_long_text_and_extra_fields() -> None:
    # A step the agent can't get past waits for the user, so there's no "blocked".
    for status in ("running", "blocked"):
        with pytest.raises(InvalidJournalEntryError, match="status must be one of"):
            normalize_status(status, text="x")
    with pytest.raises(InvalidJournalEntryError, match="at most 80 characters"):
        normalize_status("done", text="x" * 81)
    with pytest.raises(InvalidJournalEntryError, match="unknown status fields: emoji"):
        normalize_payload("status", {"status": "done", "text": "ok", "emoji": "✅"})


def test_note_is_trimmed_and_capped_in_bytes() -> None:
    assert normalize_note("\n# Summary\nbody\n\n") == {"body": "# Summary\nbody"}
    assert normalize_note("é" * (NOTE_BODY_MAX_BYTES // 2))["body"]
    with pytest.raises(JournalEntryTooLargeError, match="over the 4096-byte limit"):
        normalize_note("é" * (NOTE_BODY_MAX_BYTES // 2 + 1))
    with pytest.raises(InvalidJournalEntryError, match="must not be empty"):
        normalize_note(" \n ")


def test_plan_keeps_its_name_and_graph_and_defaults_dependencies() -> None:
    payload = normalize_plan(
        [_step("baseline"), {"id": "profile", "name": "Profile", "depends_on": ["baseline"]}],
        name="  Cut GET /orders\n p95 by 20%  ",
    )
    assert payload == {
        "name": "Cut GET /orders p95 by 20%",
        "source": "agent",
        "steps": [
            {"id": "baseline", "name": "Baseline", "goal": None, "depends_on": []},
            {"id": "profile", "name": "Profile", "goal": None, "depends_on": ["baseline"]},
        ],
    }
    only = normalize_plan([{"id": "only", "name": "Only"}], name="Only")
    assert only["steps"][0]["depends_on"] == []


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (None, "a plan needs a name"),
        (" \n ", "name must not be empty"),
        (
            "n" * (PLAN_NAME_MAX_CHARS + 1),
            f"name must be at most {PLAN_NAME_MAX_CHARS} characters",
        ),
    ],
)
def test_plan_needs_a_one_line_name(name: object, message: str) -> None:
    with pytest.raises(InvalidJournalEntryError, match=message):
        normalize_plan([_step("a")], name=name)
    with pytest.raises(InvalidJournalEntryError, match=message):
        normalize_payload(
            "plan", {"steps": [_step("a")], **({} if name is None else {"name": name})}
        )


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([], "at least one step"),
        ([_step("a"), _step("a")], "step IDs must be unique: a"),
        ([_step("a", "missing")], "depends on steps not in the plan: missing"),
        ([_step("a", "b"), _step("b", "a"), _step("c")], "form a cycle among: a, b"),
        ([_step("a", "a")], "form a cycle among: a"),
        ([_step("a"), _step("b", "a", "a")], "lists a dependency twice"),
        ([_step("a", execution={"agent_id": "x"})], "unknown fields: execution"),
        ([{"id": "a", "name": "  "}], r"steps\[0\].name must not be empty"),
        ([_step("a", goal="g" * 301)], "at most 300 characters"),
        ([_step(f"s{index}") for index in range(PLAN_MAX_STEPS + 1)], "at most 100 steps"),
        ("a", "steps must be a list"),
    ],
)
def test_plan_rejects_invalid_graphs(steps: object, message: str) -> None:
    with pytest.raises(InvalidJournalEntryError, match=message):
        normalize_plan(steps, name="The plan")


def test_plan_raw_size_is_capped() -> None:
    steps = [_step(f"s{index}", goal="g" * 300, name="n" * 80) for index in range(PLAN_MAX_STEPS)]
    with pytest.raises(
        JournalEntryTooLargeError, match=f"over the {PLAN_MAX_RAW_BYTES}-byte limit"
    ):
        normalize_plan(steps, name="The plan")


def test_payloads_only_for_kinds_with_a_writer() -> None:
    with pytest.raises(InvalidJournalEntryError, match="unknown plan fields: version"):
        normalize_payload("plan", {"name": "A", "steps": [_step("a")], "version": 7})
    with pytest.raises(InvalidJournalEntryError, match="dispatch entries can't be written yet"):
        normalize_payload("dispatch", {})
    with pytest.raises(InvalidJournalEntryError, match="kind must be one of"):
        normalize_payload("objective", {})
