import pytest


@pytest.fixture(autouse=True)
def _freeze_recurrence_clock(frozen_recurrence_clock: None) -> None:
    """Apply the shared recurrence clock freeze to every scheduled test."""
