import pytest


@pytest.fixture(autouse=True)
def _freeze_recurrence_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # A runner stall must not trip the elapsed-time search limit; tests of that
    # limit patch this clock themselves.
    monkeypatch.setattr("omnigent.server.scheduled._bounded_rrule.monotonic", lambda: 0.0)
