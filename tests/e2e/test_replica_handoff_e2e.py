"""Full-stack rollout regressions through the web client's send API.

Run with OMNIGENT_E2E_REPLICA_HANDOFF=1 after installing web dependencies and
Chromium. The companion UI suite types the same messages in the composer.
"""

import pytest
from playwright.sync_api import Page

from tests._helpers.replica_handoff import HandoffLab
from tests._helpers.replica_handoff_journeys import (
    CASES,
    run_handoff_case,
)
from tests._helpers.replica_handoff_journeys import (
    replica_lab as _replica_lab,  # noqa: F401 -- registers the shared pytest fixture
)


@pytest.mark.timeout(180)
@pytest.mark.parametrize("case", CASES)
def test_replica_handoff_client(page: Page, replica_lab: HandoffLab, case: str) -> None:
    run_handoff_case(page, replica_lab, case, ui=False)
