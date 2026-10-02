"""Frozen name-template contract: append rows; never edit outputs."""

from datetime import UTC, datetime

import pytest

from omnigent.server.scheduled.name_template import (
    NameTemplateError,
    render_session_name,
    validate_name_template,
)

REFERENCE_EPOCH = 1790946300
TIMEZONE = "America/New_York"

CONTRACT_TABLE = (
    ("{{YYYY}}", "2026"),
    ("{{MMMM}}", "October"),
    ("{{MMM}}", "Oct"),
    ("{{Mon}}", "Oct"),
    ("{{MM}}", "10"),
    ("{{DD}}", "02"),
    ("{{dddd}}", "Friday"),
    ("{{ddd}}", "Fri"),
    ("{{HH}}", "09"),
    ("{{HH:mm}}", "09:05"),
    ("Open PR Rebase - {{YYYY-MM-DD}}", "Open PR Rebase - 2026-10-02"),
    ("Open PR Rebase - {{Mon DD}}", "Open PR Rebase - Oct 02"),
    ("Open PR Rebase - {{MMM DD}}", "Open PR Rebase - Oct 02"),
    ("Hourly check - {{YYYY-MM-DD HH:mm}}", "Hourly check - 2026-10-02 09:05"),
    ("Weekly review - {{dddd}}, {{MMM DD}}", "Weekly review - Friday, Oct 02"),
    ("{{ MMM DD }}", "Oct 02"),
    ("{{\tMMM DD\t}}", "Oct 02"),
    ("{{YYYY}}{{MM}}{{DD}}", "20261002"),
    (r"\{{YYYY}} {{MMM}}", "{{YYYY}} Oct"),
    (r"\{{env}} {{YYYY}}", "{{env}} 2026"),
    (r"\{{ {{YYYY}}", "{{ 2026"),
    (r"lone { } }} \ {{YYYY}}", r"lone { } }} \ 2026"),
    ("{{YYYY / MM.DD_HH:mm, ddd}}", "2026 / 10.02_09:05, Fri"),
    ("{{MM:HH:mm}}", "10:09:05"),
)


@pytest.mark.parametrize("name,expected", CONTRACT_TABLE)
def test_frozen_contract(name: str, expected: str) -> None:
    validate_name_template(name)
    assert render_session_name(name, REFERENCE_EPOCH, TIMEZONE) == expected


@pytest.mark.parametrize(
    "body,hint",
    [
        ("mon", "use MMM (or Mon)"),
        ("MON", "use MMM (or Mon)"),
        ("Month", "use MMMM"),
        ("Day", "use ddd / dddd"),
        ("Dy", "use ddd / dddd"),
        ("EEE", "use ddd / dddd"),
        ("EEEE", "use ddd / dddd"),
        ("yyyy", "use YYYY"),
        ("yy", "use YYYY"),
        ("YY", "use YYYY"),
        ("dd", "use DD or ddd"),
        ("d", "use DD or ddd"),
        ("D", "use DD or ddd"),
        ("DDD", "day of year is not supported"),
        ("DDDD", "day of year is not supported"),
        ("M", "use MM"),
        ("H", "use HH:mm (24-hour)"),
        ("h", "use HH:mm (24-hour)"),
        ("hh", "use HH:mm (24-hour)"),
        ("A", "use HH:mm (24-hour)"),
        ("a", "use HH:mm (24-hour)"),
        ("ss", "seconds are not supported"),
        ("date", "use {{YYYY-MM-DD}}"),
        ("time", "use {{HH:mm}}"),
        ("%Y", "strftime codes are not supported"),
        ("env %Y", "strftime codes are not supported"),
        (
            "YYYYMMDD",
            "separate the tokens or use adjacent placeholders, e.g. {{YYYY}}{{MM}}{{DD}}",
        ),
        ("MonDD", "separate the tokens or use adjacent placeholders, e.g. {{YYYY}}{{MM}}{{DD}}"),
        ("mm", "mm is minutes and needs HH (e.g. {{HH:mm}}); MM is the month"),
        ("YYYY-mm-DD", "mm is minutes and needs HH (e.g. {{HH:mm}}); MM is the month"),
        ("HH:MM", "mm is minutes and needs HH (e.g. {{HH:mm}}); MM is the month"),
        ("HH : MM", "mm is minutes and needs HH (e.g. {{HH:mm}}); MM is the month"),
        ("HH:MM:mm", "mm is minutes and needs HH (e.g. {{HH:mm}}); MM is the month"),
        ("run", "supported tokens"),
        ("agent", "supported tokens"),
        ("run.number", "supported tokens"),
        ("env", "supported tokens"),
        ("YYYY1", "supported tokens"),
        ("YYYY$", "supported tokens"),
        ("'YYYY'", "supported tokens"),
        ("[YYYY]", "supported tokens"),
        ("YYYY\tMM", "supported tokens"),
        ("", "supported tokens"),
        ("-YYYY", "supported tokens"),
        ("YYYY-", "supported tokens"),
    ],
)
def test_invalid_patterns_have_hints(body: str, hint: str) -> None:
    name = "{{" + body + "}}"
    with pytest.raises(NameTemplateError) as validation:
        validate_name_template(name)
    assert hint in str(validation.value)
    if hint != "supported tokens":
        assert not str(validation.value).startswith("supported tokens")
    with pytest.raises(NameTemplateError):
        render_session_name(name, REFERENCE_EPOCH, TIMEZONE)


@pytest.mark.parametrize(
    "name", ["{{YYYY", "{{YYYY\n}}", "{{YYYY\r}}", "{{{YYYY}}}", "{{YYYY}DD}}", "{{{{YYYY}}}}"]
)
def test_unclosed_or_nested_placeholders(name: str) -> None:
    with pytest.raises(NameTemplateError):
        validate_name_template(name)
    with pytest.raises(NameTemplateError):
        render_session_name(name, REFERENCE_EPOCH, TIMEZONE)


def test_alias_is_permanent_and_whole_run_only() -> None:
    assert render_session_name("{{Mon}}", REFERENCE_EPOCH, TIMEZONE) == render_session_name(
        "{{MMM}}", REFERENCE_EPOCH, TIMEZONE
    )
    with pytest.raises(NameTemplateError):
        validate_name_template("{{MonDD}}")


@pytest.mark.parametrize("utc_hour", [5, 6])
def test_dst_fall_back(utc_hour: int) -> None:
    epoch = int(datetime(2026, 11, 1, utc_hour, 30, tzinfo=UTC).timestamp())
    assert render_session_name("{{HH:mm}}", epoch, TIMEZONE) == "01:30"


def test_plain_names_are_identity_without_validation() -> None:
    assert render_session_name("{{YYYY}}", REFERENCE_EPOCH, TIMEZONE) == "2026"
    for name in ("", "lone { } }} \\", "plain" * 100):
        validate_name_template(name)
        assert render_session_name(name, REFERENCE_EPOCH, "not/a/timezone") is name


@pytest.mark.parametrize("name", ["é" * 249 + "{{YYYY}}", "é" * 250 + r"\{{env}}"])
def test_template_limit_counts_code_points(name: str) -> None:
    with pytest.raises(NameTemplateError, match="256"):
        validate_name_template(name)


@pytest.mark.parametrize("token, expected", [("MMMM", "September"), ("dddd", "Wednesday")])
def test_worst_case_template_stays_within_render_limit(token: str, expected: str) -> None:
    name = "{{" + " ".join([token] * 50) + "}}abc"
    assert len(name) == 256
    validate_name_template(name)
    september = int(datetime(2026, 9, 30, tzinfo=UTC).timestamp())
    rendered = render_session_name(name, september, "UTC")
    assert rendered == " ".join([expected] * 50) + "abc"
    assert len(rendered) == 502 <= 768


@pytest.mark.timeout(2, method="signal")
@pytest.mark.parametrize("render", [False, True], ids=["validate", "render"])
def test_adversarial_letter_run_finishes_with_normal_error(render: bool) -> None:
    name = "{{" + "M" * 251 + "X}}"
    assert len(name) == 256
    with pytest.raises(NameTemplateError, match=r"^supported tokens:"):
        if render:
            render_session_name(name, REFERENCE_EPOCH, TIMEZONE)
        else:
            validate_name_template(name)
