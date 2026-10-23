"""Scheduler settings reject unsafe timing and capacity values before startup."""

import pytest
from pydantic import ValidationError

from omnigent.server.scheduled.config import ScheduledTaskSchedulerConfig


@pytest.mark.parametrize("field", list(ScheduledTaskSchedulerConfig.model_fields))
@pytest.mark.parametrize("value", [0, -1, True, "30", None])
def test_rejects_invalid_settings(field, value):
    with pytest.raises(ValidationError):
        ScheduledTaskSchedulerConfig.model_validate({field: value})


@pytest.mark.parametrize("field", ["poll_seconds", "renewal_seconds", "recovery_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_intervals_must_be_finite(field, value):
    with pytest.raises(ValidationError):
        ScheduledTaskSchedulerConfig.model_validate({field: value})


@pytest.mark.parametrize("field", ["lease_seconds", "batch_size", "max_startups"])
def test_capacity_and_lease_use_whole_numbers(field):
    with pytest.raises(ValidationError):
        ScheduledTaskSchedulerConfig.model_validate({field: 1.5})


@pytest.mark.parametrize("renewal", [120, 121])
def test_renewal_must_precede_expiry(renewal):
    with pytest.raises(ValidationError, match="renewal_seconds must be shorter"):
        ScheduledTaskSchedulerConfig(renewal_seconds=renewal)


def test_partial_settings_keep_defaults_and_are_immutable():
    config = ScheduledTaskSchedulerConfig(poll_seconds=0.5)
    assert config.model_dump() == {
        "poll_seconds": 0.5,
        "renewal_seconds": 30,
        "lease_seconds": 120,
        "batch_size": 100,
        "max_startups": 100,
        "recovery_seconds": 30,
    }
    with pytest.raises(ValidationError, match="frozen"):
        config.lease_seconds = 10
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ScheduledTaskSchedulerConfig.model_validate({"poll_second": 60})
