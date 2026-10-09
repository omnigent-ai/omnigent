"""The ordinary composer stays usable through the recorded rollout failures."""

import os

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

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_REPLICA_HANDOFF") != "1",
    reason="run in the Replica handoff regressions job with Chromium and web dependencies",
)


@pytest.mark.timeout(180)
@pytest.mark.parametrize("case", CASES)
def test_replica_handoff_composer(page: Page, replica_lab: HandoffLab, case: str) -> None:
    run_handoff_case(page, replica_lab, case, ui=True)
