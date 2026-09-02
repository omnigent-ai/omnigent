"""Tests for the resolve-agent driver's pure helpers (``dev/resolve.py``).

These cover the input-normalization and branch-slug logic that ``dev/resolve.py``
uses before it launches the agent — the parts worth pinning because a wrong parse
silently feeds the agent the wrong reproduction. The subprocess / git plumbing
(``main``) is exercised manually, not here.
"""

from __future__ import annotations

import json
import re

import pytest

from dev.resolve import (
    branch_slug,
    build_payload,
    parse_ci_run_url,
    parse_session_ref,
)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("http://localhost:6767/c/dc59e331-abc", "dc59e331-abc"),
        ("https://omni.example.com/c/dc59e331-abc/", "dc59e331-abc"),
        ("http://host:6767/sessions/dc59e331-abc", "dc59e331-abc"),
        ("dc59e331-abc", "dc59e331-abc"),
        ("http://localhost:6767/c/abc?foo=1#frag", "abc"),
    ],
)
def test_parse_session_ref(ref: str, expected: str) -> None:
    assert parse_session_ref(ref) == expected


def test_parse_session_ref_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_session_ref("   ")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/omnigent-ai/omnigent-internal/actions/runs/30974269184",
            {"org": "omnigent-ai", "repo": "omnigent-internal", "run_id": "30974269184"},
        ),
        (
            "https://github.com/o/r/actions/runs/123/job/456",
            {"org": "o", "repo": "r", "run_id": "123"},
        ),
        (
            "https://github.com/o/r/actions/runs/123/attempts/2",
            {"org": "o", "repo": "r", "run_id": "123"},
        ),
    ],
)
def test_parse_ci_run_url(url: str, expected: dict[str, str]) -> None:
    assert parse_ci_run_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/omnigent-ai/omnigent/pull/4099",
        "https://github.com/omnigent-ai/omnigent/actions",
        "not a url",
        "",
        # A different host that merely contains the run path — the old
        # unanchored regex would have wrongly accepted this.
        "https://evil.example.com/github.com/o/r/actions/runs/123",
        # Trailing extra path beyond the recognized job/attempts suffixes.
        "https://github.com/o/r/actions/runs/123/steps/9",
        # Non-http scheme.
        "ftp://github.com/o/r/actions/runs/123",
    ],
)
def test_parse_ci_run_url_rejects_non_run_urls(url: str) -> None:
    assert parse_ci_run_url(url) is None


def test_build_payload_session() -> None:
    payload = json.loads(build_payload(session="http://h:6767/c/abc", ci_link=None))
    assert payload == {"session": "abc"}


def test_build_payload_ci_link() -> None:
    url = "https://github.com/o/r/actions/runs/123"
    payload = json.loads(build_payload(session=None, ci_link=url))
    assert payload == {"ci_link": url}


def test_build_payload_skip_push_added_only_when_true() -> None:
    # Off by default: key absent entirely.
    assert "skip_push" not in json.loads(build_payload(session="abc", ci_link=None))
    assert "skip_push" not in json.loads(
        build_payload(session="abc", ci_link=None, skip_push=False)
    )
    # On: key present and true.
    payload = json.loads(build_payload(session="abc", ci_link=None, skip_push=True))
    assert payload == {"session": "abc", "skip_push": True}


def test_build_payload_requires_exactly_one() -> None:
    with pytest.raises(ValueError):
        build_payload(session=None, ci_link=None)
    with pytest.raises(ValueError):
        build_payload(session="abc", ci_link="https://github.com/o/r/actions/runs/1")


def test_build_payload_rejects_bad_ci_link() -> None:
    with pytest.raises(ValueError):
        build_payload(session=None, ci_link="https://github.com/o/r/pull/1")


def test_branch_slug_ci_link_is_run_id() -> None:
    assert branch_slug(
        session=None, ci_link="https://github.com/o/r/actions/runs/30974269184"
    ) == ("30974269184")


def test_branch_slug_session_uses_leading_id_segment() -> None:
    # The slug comes from the session pointer the caller passed — never from a
    # guessed repro worktree — so it can't show an unrelated bug's number.
    assert branch_slug(session="http://h:6767/c/dc59e331-abcd-ef01", ci_link=None) == "dc59e331"
    assert branch_slug(session="dc59e331-abcd", ci_link=None) == "dc59e331"


def test_branch_slug_session_sanitizes_and_bounds_length() -> None:
    slug = branch_slug(session="weird id/with*chars", ci_link=None)
    assert re.fullmatch(r"[A-Za-z0-9._-]+", slug)
    assert len(slug) <= 16


def test_branch_slug_fallback() -> None:
    assert branch_slug(session=None, ci_link=None) == "bug"
