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
from issue_prioritization.comments import COMMENT_MARKER, build_code_only_comment
from issue_prioritization.event import apply_code_only_closure
from issue_prioritization.github import GitHubClient

BODY = "Open a session. Reconnect Wi-Fi. The transcript stays blank."
SOURCE_ONLY = "Found only by reading source. Nobody executed this sequence."
NOW = datetime(2026, 9, 18, tzinfo=UTC)


def response(decision="non_actionable"):
    observed = decision == "actionable"
    return {
        "type": "Bug",
        "impact": "low",
        "reasoning": "Only an internal source audit is described.",
        "evidence_kind": "direct_steps" if observed else "code_analysis",
        "information_status": "sufficient" if observed else "needs_info",
        "missing_information": [] if observed else ["observed_behavior"],
        "source_only_quote": SOURCE_ONLY if decision == "non_actionable" else None,
        "has_user_facing_repro": observed,
    }


def issue(body=SOURCE_ONLY, labels=("Bug", "needs-info")):
    return BronzeIssue(7, "Session failure", body, "url", "author", labels, NOW, 0, 0)


def classify(value=None, report=None, enabled=True):
    query = Mock(return_value=json.dumps(value or response()))
    result = PromptClassifier(query, AreaCatalog({}, {}), review_bugs=enabled).classify(
        (report or issue()).content()
    )
    query.assert_called_once()
    return result


@pytest.mark.parametrize("decision", ["actionable", "needs_info", "non_actionable"])
def test_three_decisions(decision):
    result = classify(response(decision))
    assert bool(result.source_only_quote) == (decision == "non_actionable")
    if result.source_only_quote:
        comment = build_code_only_comment(result)
        assert "recommend closing as **not planned**" in comment
        assert "please open a new issue" in comment


@pytest.mark.parametrize("kind,enabled", [("Feature", True), ("Docs", True), ("Bug", False)])
def test_other_types_and_disabled_review_keep_existing_behavior(kind, enabled):
    assert classify({**response(), "type": kind}, enabled=enabled).source_only_quote is None


@pytest.mark.parametrize("repro", [True, None, "false", 0, 1, [], {}])
def test_user_facing_or_uncertain_repro_prevents_closure_even_with_source_only_quote(repro):
    assert classify({**response(), "has_user_facing_repro": repro}).source_only_quote is None


@pytest.mark.parametrize("quote", [None, "", "Invented admission."])
def test_missing_or_ungrounded_closure_quote_prevents_closure(quote):
    assert classify({**response(), "source_only_quote": quote}).source_only_quote is None


def test_observed_evidence_prevents_closure():
    assert (
        classify({**response(), "evidence_kind": "observed_intermittent"}).source_only_quote is None
    )


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```\n{}\n```"])
def test_trailing_commas_preserve_the_complete_response_and_quoted_text(wrapper):
    reasoning = 'Logs include ",}" and ",]", a \\ path, and ```json fences.'
    raw = (
        '{"details":{"actionability":"actionable",},"type":"Bug",'
        '"area_keys":["terminals",],"reasoning":' + json.dumps(reasoning) + ",}"
    )
    assert _parse_json_object(wrapper.format(raw)) == {
        "details": {"actionability": "actionable"},
        "type": "Bug",
        "area_keys": ["terminals"],
        "reasoning": reasoning,
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"details":{"type":"Bug"},"type":',
        '{"details":{"type":"Bug"},"type":"Bug"',
        '{"details":{"type":"Bug"},"type":,}',
        '{"details":{"type":"Bug"},"area_keys":[,]}',
        '[{"type":"Bug"}]',
        '{"type":"Bug"} {"type":"Feature"}',
        '```json\n{"type":"Bug"}',
        '```json\n{"type":"Bug"}\n```\nAdditional explanation.',
        '{"type":"Bug"}\nAdditional explanation.',
        '```json\n{"type":"Bug"}\n```\n```json\n{"type":"Feature"}\n```',
    ],
)
def test_malformed_response_never_falls_back_to_a_nested_object(raw):
    with pytest.raises(ValueError, match="classifier.*JSON"):
        _parse_json_object(raw)


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
    assert classification.source_only_quote is None


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

    report = GitHubClient("fake", "org/repo", transport).open_issue(7, full_author_history=True)
    assert BODY in report.body
    assert all(comment["body"] in report.body for comment in comments)


def test_marker_reference_in_author_reply_is_not_updated_as_the_triage_comment():
    reply = f"The comment containing `{COMMENT_MARKER}` missed my reproduction steps."
    transport = Mock(side_effect=[[{"id": 1, "body": reply}], {"id": 2}])
    body = f"<!-- {COMMENT_MARKER} {{}} -->\nAssessment."
    client = GitHubClient("fake", "org/repo", transport)
    assert client.upsert_issue_comment(7, body) == 2
    assert transport.call_args.args == ("POST", "/issues/7/comments", {"body": body})


class Client:
    def __init__(self, *, stale=False, fail=None, labels=("Bug", "needs-info")):
        self.report = issue(labels=labels)
        self.events = []
        self.comments = []
        self.stale, self.fail = stale, fail

    def open_issue(self, number, *, full_author_history=False):
        assert full_author_history
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
        assert "needs-info" in self.report.labels
        if self.fail == "close":
            raise RuntimeError("close failed")
        self.events.append("close")


def apply(client):
    return apply_code_only_closure(client, classify(report=client.report))


def test_apply_explains_before_closing_and_removes_reopen_label():
    client = Client()
    assert apply(client) == "applied"
    assert client.events == ["comment", "close", "labels"]
    assert "needs-info" not in client.report.labels


@pytest.mark.parametrize("label", ["security", "duplicate", "Pinned"])
def test_live_exemptions_block_closure_without_requesting_evidence(label):
    client = Client(labels=("Bug", label))
    assert apply(client) == "skipped_stale"
    assert not client.events


@pytest.mark.parametrize("failure", ["comment", "close"])
def test_api_failure_does_not_claim_successful_closure(failure):
    client = Client(fail=failure)
    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        apply(client)
    assert "close" not in client.events
    assert "needs-info" in client.report.labels


@pytest.mark.parametrize("when", ["before", "after"])
def test_edit_before_closure_skips_the_stale_assessment(when):
    client = Client(stale=when)
    assert apply(client) == "skipped_stale"
    assert "close" not in client.events
    assert "needs-info" in client.report.labels
    if when == "after":
        assert "recommend closing" in client.comments[0]
        assert "Automatic closure skipped" in client.comments[-1]
    else:
        assert not client.events


@pytest.mark.parametrize(
    "user", [{"login": "github-actions[bot]", "type": "Bot"}, {"login": "author", "type": "User"}]
)
@pytest.mark.parametrize("author_reply", [False, True])
def test_triage_as_issue_author_preserves_only_genuine_followups(user, author_reply):
    client = Client()
    followup = f"More source analysis; the earlier `{COMMENT_MARKER}` comment missed this."
    old_comment = f"<!-- {COMMENT_MARKER} {{}} -->\nOld triage assessment."

    def transport(method, path, payload):
        assert method == "GET"
        if "/comments" in path:
            comments = [followup, client.comments[-1] if client.comments else old_comment]
            if client.comments and author_reply:
                assert "needs-info" in client.report.labels
                comments.append(BODY)
            return [{"user": user, "body": body} for body in comments]
        return {
            "number": 7,
            "title": client.report.title,
            "body": client.report.body,
            "user": user,
            "labels": list(client.report.labels),
            "state": "open",
            "created_at": NOW.isoformat(),
        }

    client.open_issue = GitHubClient("fake", "org/repo", transport).open_issue
    report = client.open_issue(7, full_author_history=True)
    assert followup in report.body and old_comment not in report.body
    status = apply_code_only_closure(client, classify(report=report))
    assert status == ("skipped_stale" if author_reply else "applied")
    assert ("close" in client.events) == (not author_reply)
    assert (
        client.open_issue(7, full_author_history=True).content().content_hash
        == report.content().content_hash
    ) == (not author_reply)
    if author_reply:
        assert "Automatic closure skipped" in client.comments[-1]
        assert "recommend closing" not in client.comments[-1]
    else:
        assert client.events == ["comment", "close", "labels"]
