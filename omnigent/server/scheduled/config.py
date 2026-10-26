"""Startup and runtime settings for scheduled tasks."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScheduledTaskSchedulerConfig(BaseModel):
    """Validated together and fixed for the lifetime of a scheduler."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)

    startup_retry_seconds: float = Field(default=30, gt=0)
    poll_seconds: float = Field(default=30, gt=0)
    renewal_seconds: float = Field(default=30, gt=0)
    lease_seconds: int = Field(default=120, gt=0)
    batch_size: int = Field(default=100, gt=0)
    max_startups: int = Field(default=100, gt=0)
    recovery_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def check_renewal_interval(self) -> Self:
        if self.renewal_seconds >= self.lease_seconds:
            raise ValueError(
                "renewal_seconds must be shorter than lease_seconds so startup leases "
                "are renewed before they expire "
                f"(received {self.renewal_seconds:g}s and {self.lease_seconds}s)."
            )
        return self


DEFAULT_SCHEDULER_CONFIG = ScheduledTaskSchedulerConfig()
