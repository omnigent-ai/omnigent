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


async def test_full_server_tool_call_and_policy_deny(databricks_profile: str) -> None:
    profile = resolve_profile("openai-agents")
    reason = FullServerDriver.unavailable(profile, databricks_profile=databricks_profile)
    if reason is not None:
        pytest.skip(f"full-server unavailable: {reason}")

    with FullServerDriver(profile, databricks_profile=databricks_profile) as driver:
        allowed = driver.tool_probe_turn(deny=False, timeout=180)
        denied = driver.tool_probe_turn(deny=True, timeout=180)

    assert [tc["name"] for tc in allowed.tool_calls] == ["list_files"], (
        f"expected a list_files call, got {allowed.tool_calls} ({allowed.error})"
    )
    assert not allowed.tool_call_denied, "tool call was blocked without a deny policy"

    assert denied.tool_call_denied, (
        f"tool_call deny policy did not block the call: {denied.tool_calls} ({denied.error})"
    )


async def test_full_server_interrupt(databricks_profile: str) -> None:
    profile = resolve_profile("openai-agents")
    reason = FullServerDriver.unavailable(profile, databricks_profile=databricks_profile)
    if reason is not None:
        pytest.skip(f"full-server unavailable: {reason}")

    with FullServerDriver(profile, databricks_profile=databricks_profile) as driver:
        result = driver.interrupt_probe_turn(timeout=120)

    assert not result.timed_out, "interrupt turn never settled"
    assert result.cancelled, "interrupt did not produce a cancellation marker"


async def test_full_server_streaming(databricks_profile: str) -> None:
    profile = resolve_profile("openai-agents")
    reason = FullServerDriver.unavailable(profile, databricks_profile=databricks_profile)
    if reason is not None:
        pytest.skip(f"full-server unavailable: {reason}")

    with FullServerDriver(profile, databricks_profile=databricks_profile) as driver:
        result = driver.streaming_probe_turn(timeout=120)

    assert not result.timed_out, f"streaming turn never reached a terminal event ({result.error})"
    assert result.text_delta_count > 1, (
        f"expected token-level streaming (>1 delta), saw {result.text_delta_count}"
    )
