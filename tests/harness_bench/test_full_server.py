"""Gated live test for the full-server transport foundation.

Skips without ``--profile`` (and without a workspace host / runnable CLI).
When creds are present it spins up a real server + runner via
:class:`FullServerDriver` and asserts a basic turn round-trips — the
foundation the tool/policy/streaming follow-ups build on.

Runs one harness (``openai-agents``, no vendor CLI required) to bound the
cost of spawning a server+runner per row.
"""

from __future__ import annotations

import pytest

from tests.harness_bench.full_server_driver import FullServerDriver
from tests.harness_bench.profile import resolve_profile


@pytest.fixture
def databricks_profile(request: pytest.FixtureRequest) -> str:
    profile = request.config.getoption("--profile")
    if not profile:
        pytest.skip("full-server live test requires --profile <name>")
    return str(profile)


async def test_full_server_basic_turn(databricks_profile: str) -> None:
    profile = resolve_profile("openai-agents")
    reason = FullServerDriver.unavailable(profile, databricks_profile=databricks_profile)
    if reason is not None:
        pytest.skip(f"full-server unavailable: {reason}")

    with FullServerDriver(profile, databricks_profile=databricks_profile) as driver:
        result = driver.run_turn(
            f"Reply with exactly the literal string {profile.marker} and nothing else.",
            timeout=180,
        )

    assert not result.timed_out, "basic turn did not reach a terminal state within timeout"
    assert result.completed, f"basic turn did not complete: {result.error}"
    assert result.text, "basic turn completed but produced no assistant text"
