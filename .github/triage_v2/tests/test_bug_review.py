from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.classification import (
    MAX_BUG_REVIEW_CHARACTERS,
    PromptClassifier,
    _parse_json_object,
)
from issue_prioritization.comments import build_triage_comment, preserve_needs_info_deadline
from issue_prioritization.config import ScoringConfig
from issue_prioritization.event import prioritize_issue, write_event_artifacts
from issue_prioritization.github import GitHubClient, GitHubMutationSink
from issue_prioritization.labels import LabelManifest
from issue_prioritization.pipeline import PipelineMode

BODY = "Open a session. Reconnect Wi-Fi. The transcript stays blank."
SOURCE_ONLY = "Found only by reading source. Nobody executed this sequence."
NOW = datetime(2026, 9, 18, tzinfo=UTC)


def response(decision="actionable"):
    return {
        "type": "Bug",
        "impact": "medium",
        "area_keys": [],
        "reasoning": "Session transcript may be unavailable.",
        "evidence_kind": "direct_steps" if decision == "actionable" else "code_analysis",
        "information_status": "sufficient" if decision == "actionable" else "needs_info",
        "missing_information": [] if decision == "actionable" else ["observed_behavior"],
        "bug_review": {
            "actionability": decision,
            "reason": "Assessment of the reported failure.",
            "readability": "clear" if decision == "actionable" else "not_assessed",
            "source_only_quote": SOURCE_ONLY if decision == "non_actionable" else None,
        },
    }


def issue(body=SOURCE_ONLY, labels=("Bug",)):
    return BronzeIssue(7, "Session failure", body, "url", "author", labels, NOW, 0, 0)


def preview(decision="non_actionable", *, value=None, report=None):
    value = value or response(decision)
    report = report or issue(BODY if decision == "actionable" else SOURCE_ONLY)
    query = Mock(side_effect=[json.dumps(value)])
    result = prioritize_issue(
        report,
        PromptClassifier(query, AreaCatalog({}, {}), review_bugs=True),
        ScoringConfig.default(),
        AreaCatalog({}, {}),
        LabelManifest(()),
        "preview",
        PipelineMode.DRY_RUN,
    )
    query.assert_called_once()
    return result


@pytest.mark.parametrize("decision", ["actionable", "needs_info", "non_actionable"])
def test_three_decisions_and_comment_previews(tmp_path, decision):
    run, classification, _, _ = preview(
        decision,
        report=issue(BODY if decision == "actionable" else SOURCE_ONLY, ("Bug", "needs-info")),
    )
    plan = run.mutations[0]
    write_event_artifacts(
        tmp_path, run, classification, ScoringConfig.default(), "model", "sha", ()
    )
    artifact = json.loads((tmp_path / "event.json").read_text())
    body = artifact["comment"]["body"]
    assert classification.bug_review.actionability == decision
    assert plan.close_as_non_actionable == (decision == "non_actionable")
    assert plan.target.needs_info == (decision == "needs_info")
    assert artifact["mutation"]["close_as_non_actionable"] == plan.close_as_non_actionable
    if decision == "needs_info":
        assert "Please update the issue by" in body
    else:
        assert "needs-info" in plan.labels_remove
        assert '"needs_info_deadline":null' in body
    if decision == "non_actionable":
        assert "recommend closing as **not planned**" in body
        assert "please open a new issue" in body
        previous = body.replace('"needs_info_deadline":null', '"needs_info_deadline":"2026-09-25"')
        assert preserve_needs_info_deadline(body, previous) == body
    else:
        assert "recommend closing" not in body


@pytest.mark.parametrize("quote", ["Reconnect Wi-Fi.", "Invented command."])
def test_observed_unreadable_bug_gets_a_grounded_summary(quote):
    value = response()
    value["bug_review"].update(
        readability="needs_summary",
        clarification={
            "summary": "After Wi-Fi reconnects, the session transcript stays blank.",
            "reproduction_steps": [{"text": "Reconnect Wi-Fi.", "source_quote": quote}],
        },
    )
    run, classification, _, _ = preview("actionable", value=value)
    body = build_triage_comment(run.ranked[0], run.mutations[0], (), NOW)
    assert "Problem in plain English" in body
    assert classification.bug_review.actionability == "actionable"
    assert ("1. Reconnect Wi-Fi." in body) == (quote in BODY)
    assert ("not been independently verified" in body) == (quote in BODY)
    assert not run.mutations[0].close_as_non_actionable


def test_changed_quote_format_omits_whole_recipe_but_keeps_summary():
    value = response()
    value["reasoning"] = "A child exceeded its parent's spending limit."
    summary = "A child spent $379 despite a $100 limit."
    value["bug_review"].update(
        readability="needs_summary",
        clarification={
            "summary": summary,
            "reproduction_steps": [
                {"text": "Configure a $100 limit.", "source_quote": "Configure a $100 limit."},
                {"text": "Dispatch a child.", "source_quote": "Dispatch a child."},
            ],
        },
    )
    run, classification, _, _ = preview(
        "actionable",
        value=value,
        report=issue("Configure a `$100` limit. Dispatch a child. The child spent $379."),
    )
    review = classification.bug_review
    assert review.actionability == "actionable" and review.readability == "needs_summary"
    assert review.clarification.summary == summary
    assert review.clarification.reproduction_steps == ()
    body = build_triage_comment(run.ranked[0], run.mutations[0], (), NOW)
    assert summary in body and "Steps to reproduce" not in body
    assert not run.mutations[0].close_as_non_actionable
    assert not run.mutations[0].target.needs_info


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```\n{}\n```"])
def test_trailing_commas_preserve_the_complete_response_and_quoted_text(wrapper):
    reasoning = 'Logs include ",}" and ",]", a \\ path, and ```json fences.'
    raw = (
        '{"bug_review":{"actionability":"actionable",},"type":"Bug",'
        '"area_keys":["terminals",],"reasoning":' + json.dumps(reasoning) + ",}"
    )
    assert _parse_json_object(wrapper.format(raw)) == {
        "bug_review": {"actionability": "actionable"},
        "type": "Bug",
        "area_keys": ["terminals"],
        "reasoning": reasoning,
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"bug_review":{"type":"Bug"},"type":',
        '{"bug_review":{"type":"Bug"},"type":"Bug"',
        '{"bug_review":{"type":"Bug"},"type":,}',
        '{"bug_review":{"type":"Bug"},"area_keys":[,]}',
        '[{"type":"Bug"}]',
        '{"type":"Bug"} {"type":"Feature"}',
        '```json\n{"type":"Bug"}',
    ],
)
def test_malformed_response_never_falls_back_to_a_nested_object(raw):
    with pytest.raises(ValueError, match="classifier.*JSON"):
        _parse_json_object(raw)


@pytest.mark.parametrize("kind,enabled", [("Feature", True), ("Docs", True), ("Bug", False)])
def test_other_types_and_disabled_review_keep_existing_behavior(kind, enabled):
    value = {**response(), "type": kind, "bug_review": "ignored"}
    classifier = PromptClassifier(
        lambda _: json.dumps(value), AreaCatalog({}, {}), review_bugs=enabled
    )
    assert classifier.classify(issue(BODY).content()).bug_review is None


def test_missing_closure_quote_requests_clarification():
    value = response("non_actionable")
    value["bug_review"]["source_only_quote"] = None
    run, classification, _, _ = preview(value=value)
    assert classification.bug_review.actionability == "needs_info"
    assert classification.bug_review.source_only_quote is None
    assert run.mutations[0].target.needs_info
    assert not run.mutations[0].close_as_non_actionable


@pytest.mark.parametrize(
    "bad", ["invented_quote", "observed_closure", "code_actionable", "missing_summary"]
)
def test_inconsistent_or_ungrounded_model_output_is_rejected(bad):
    decision = "non_actionable" if bad in ("invented_quote", "observed_closure") else "actionable"
    value = response(decision)
    if bad == "invented_quote":
        value["bug_review"]["source_only_quote"] = "Invented admission."
    elif bad == "observed_closure":
        value["evidence_kind"] = "observed_intermittent"
    elif bad == "code_actionable":
        value["evidence_kind"] = "code_analysis"
    else:
        value["bug_review"]["readability"] = "needs_summary"
    with pytest.raises(ValueError):
        preview(decision, value=value)


def test_single_model_call_sees_evidence_after_the_old_cutoff():
    observation = "I reproduced the failure yesterday."
    report = issue(SOURCE_ONLY + " Source detail." * 1000 + observation)
    query = Mock(side_effect=[json.dumps(response("actionable"))])

    classification = PromptClassifier(query, AreaCatalog({}, {}), review_bugs=True).classify(
        report.content()
    )
    query.assert_called_once()
    prompt = query.call_args.args[0]
    assert report.body in prompt and report.title in prompt
    assert classification.bug_review.actionability == "actionable"


def test_oversized_report_is_not_partially_reviewed():
    classifier = PromptClassifier(
        lambda _: pytest.fail("Partial review"), AreaCatalog({}, {}), review_bugs=True
    )
    with pytest.raises(ValueError, match="manual review required"):
        classifier.classify(issue("x" * MAX_BUG_REVIEW_CHARACTERS).content())


def test_old_and_long_author_comments_are_preserved():
    payload = {
        "number": 7,
        "title": "Session failure",
        "body": SOURCE_ONLY,
        "user": {"login": "author"},
        "state": "open",
        "created_at": NOW.isoformat(),
    }
    comments = [{"user": {"login": "author"}, "body": "Logs. " * 800 + BODY}]
    comments += [{"user": {"login": "author"}, "body": f"Update {n}"} for n in range(100)]

    def transport(method, path, body):
        if "/comments" not in path:
            return payload
        page = int(path.rsplit("=", 1)[1])
        return comments[(page - 1) * 100 : page * 100]

    report = GitHubClient("fake", "org/repo", transport).open_issue(7)
    assert BODY in report.body
    assert all(comment["body"] in report.body for comment in comments)


class Client:
    def __init__(self, *, stale=False, fail=None, labels=("Bug", "needs-info")):
        self.report = issue(labels=labels)
        self.events = []
        self.comments = []
        self.stale, self.fail = stale, fail

    def sync_missing_labels(self, manifest):
        pass

    def issue_labels(self, number):
        return self.report.labels

    def open_issue(self, number):
        changed = self.stale == "before" or (self.stale == "after" and self.comments)
        return replace(self.report, body=BODY) if changed else self.report

    def apply_labels(self, number, added, removed):
        self.events.append("labels")
        self.report = replace(
            self.report, labels=tuple((set(self.report.labels) - set(removed)) | set(added))
        )

    def upsert_issue_comment(self, number, body):
        if self.fail == "comment":
            raise RuntimeError("comment failed")
        self.events.append("comment")
        self.comments.append(body)
        return 1

    def close_issue(self, number):
        if self.fail == "close":
            raise RuntimeError("close failed")
        self.events.append("close")


def apply(client, *, mode=PipelineMode.APPLY):
    run, _, planner, states = preview()
    return GitHubMutationSink(client, LabelManifest(()), planner, states).apply_with_plans(
        replace(run, mode=mode)
    )


def test_apply_explains_before_closing_and_removes_reopen_label():
    client = Client()
    assert apply(client)[0].close_as_non_actionable
    assert client.events == ["labels", "comment", "close"]
    assert "needs-info" not in client.report.labels


@pytest.mark.parametrize("label", ["security", "duplicate", "Pinned"])
def test_live_exemptions_block_closure_without_requesting_evidence(label):
    client = Client(labels=("Bug", label))
    assert not apply(client)[0].close_as_non_actionable
    assert "close" not in client.events
    assert f"exempt ({label.lower()})" in client.comments[0]
    assert "More evidence needed" not in client.comments[0]
    assert '"needs_info_deadline":null' in client.comments[0]


@pytest.mark.parametrize("failure", ["comment", "close"])
def test_api_failure_does_not_claim_successful_closure(failure):
    client = Client(fail=failure)
    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        apply(client)
    assert "close" not in client.events
    if client.comments:
        assert "recommend closing" in client.comments[0]


@pytest.mark.parametrize("when", ["before", "after"])
def test_edit_before_closure_skips_the_stale_assessment(when):
    client = Client(stale=when)
    plan = apply(client)[0]
    assert "close" not in client.events
    assert "non_actionable_stale_assessment" in plan.blocked
    assert not plan.close_as_non_actionable
    if when == "after":
        assert "recommend closing" in client.comments[0]
    else:
        assert not client.events


def test_stale_issue_does_not_stop_later_issues():
    class BatchClient(Client):
        def issue_labels(self, number):
            self.report = issue()
            return self.report.labels

        def open_issue(self, number):
            return replace(self.report, body=BODY) if number == 8 else self.report

        def close_issue(self, number):
            self.events.append(("close", number))

    client = BatchClient()
    run, _, planner, states = preview()
    item = run.ranked[0]
    ranked = tuple(replace(item, issue=replace(item.issue, number=n)) for n in (7, 8, 9))
    plans = GitHubMutationSink(client, LabelManifest(()), planner, states).apply_with_plans(
        replace(run, mode=PipelineMode.APPLY, ranked=ranked, mutations=planner.plan_all(ranked, {}))
    )
    assert [p.close_as_non_actionable for p in plans] == [True, False, True]
    assert ("close", 7) in client.events and ("close", 9) in client.events
    assert set(states.load()) == {7, 9}


def test_dry_run_cannot_apply():
    client = Client()
    with pytest.raises(ValueError, match="require apply mode"):
        apply(client, mode=PipelineMode.DRY_RUN)
    assert not client.events
