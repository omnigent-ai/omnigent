"""Frozen name-template contract: append rows; never edit outputs."""

import pytest

from omnigent.server.scheduled.name_template import (
    NameTemplateError,
    render_session_name,
    validate_name_template,
)

REFERENCE_EPOCH = 1790946300
TIMEZONE = "America/New_York"

CONTRACT_TABLE = (
    ("{{YYYY MMMM MMM Mon MM DD dddd ddd HH:mm}}", "2026 October Oct Oct 10 02 Friday Fri 09:05"),
    ("{{HH}}", "09"),
    ("Open PR Rebase - {{YYYY-MM-DD}}", "Open PR Rebase - 2026-10-02"),
    ("Weekly review - {{dddd}}, {{MMM DD}}", "Weekly review - Friday, Oct 02"),
    ("{{ MMM DD }}", "Oct 02"),
    ("{{YYYY}}{{MM}}{{DD}}", "20261002"),
    ("{{YYYY / MM.DD_HH:mm, ddd}}", "2026 / 10.02_09:05, Fri"),
    (r"\{{env}} {{YYYY}}", "{{env}} 2026"),
    (r"lone { } }} \ {{YYYY}}", r"lone { } }} \ 2026"),
)


@pytest.mark.parametrize("name,expected", CONTRACT_TABLE)
def test_frozen_contract(name: str, expected: str) -> None:
    validate_name_template(name)
    assert render_session_name(name, REFERENCE_EPOCH, TIMEZONE) == expected


@pytest.mark.parametrize(
    "name",
    [
        "{{yyyy-MM-dd}}",
        "{{%Y}}",
        "{{env}}",
        "{{YYYYMMDD}}",
        "{{MonDD}}",
        "{{-YYYY}}",
        "{{}}",
        "{{YYYY",
        "{{YYYY\n}}",
        "{{{YYYY}}}",
    ],
)
def test_invalid_patterns_are_rejected(name: str) -> None:
    with pytest.raises(NameTemplateError, match="supported tokens"):
        validate_name_template(name)


@pytest.mark.parametrize("body", ["YYYY-mm-DD", "HH:MM"])
def test_clock_typos_are_rejected(body: str) -> None:
    with pytest.raises(NameTemplateError, match="mm is minutes"):
        validate_name_template("{{" + body + "}}")


def test_plain_names_are_identity_without_validation() -> None:
    for name in ("", "lone { } }} \\", "x" * 1000):
        validate_name_template(name)
        assert render_session_name(name, REFERENCE_EPOCH, "not/a/timezone") is name


def test_template_limit_counts_code_points() -> None:
    accepted = "é" * 248 + "{{YYYY}}"
    rejected = "é" * 249 + "{{YYYY}}"
    assert len(accepted) == 256
    assert len(rejected) == 257
    validate_name_template(accepted)
    assert render_session_name(accepted, REFERENCE_EPOCH, TIMEZONE) == "é" * 248 + "2026"
    with pytest.raises(NameTemplateError, match="256"):
        validate_name_template(rejected)
