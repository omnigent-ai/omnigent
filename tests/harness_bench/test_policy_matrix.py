"""Unit tests for the policy_allow / policy_ask probes.

Network-free: a fake driver returns a canned TurnResult from run_policy_turn, so
the verdict logic (allowed -> SUPPORTED, elicitation -> SUPPORTED, unmeasured /
infra / timeout -> SKIPPED) is asserted without a live server.
"""

from __future__ import annotations

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.probes.policy_allow import PolicyAllowProbe
from tests.harness_bench.probes.policy_ask import PolicyAskProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Priority, Verdict

_PROFILE = BenchProfile(harness="fake", model="m", env_prefix="HARNESS_FAKE_", marker="MARK")


class _Driver:
    """Fake driver whose run_policy_turn returns a preset result per action."""

    transport = "full-server"

    def __init__(self, result: TurnResult) -> None:
        self._result = result
        self.seen_action: str | None = None

    async def run_policy_turn(self, *, action: str) -> TurnResult:
        self.seen_action = action
        return self._result


async def _allow(result: TurnResult):
    return await PolicyAllowProbe().run(_Driver(result), _PROFILE)


async def _ask(result: TurnResult):
    return await PolicyAskProbe().run(_Driver(result), _PROFILE)


def test_probes_are_p1() -> None:
    assert PolicyAllowProbe().priority is Priority.P1
    assert PolicyAskProbe().priority is Priority.P1


async def test_allow_supported_when_call_proceeds() -> None:
    r = await _allow(TurnResult(completed=True, tool_call_allowed=True))
    assert r.verdict is Verdict.SUPPORTED


async def test_allow_passes_action_allow_to_driver() -> None:
    driver = _Driver(TurnResult(completed=True, tool_call_allowed=True))
    await PolicyAllowProbe().run(driver, _PROFILE)
    assert driver.seen_action == "allow"


async def test_allow_skipped_when_unmeasured() -> None:
    r = await _allow(TurnResult())
    assert r.verdict is Verdict.SKIPPED
    assert "not observable" in r.note


async def test_allow_skipped_on_infra_failure() -> None:
    r = await _allow(TurnResult(failed=True, error={"message": "403 Forbidden"}))
    assert r.verdict is Verdict.SKIPPED


async def test_ask_supported_when_elicitation_raised() -> None:
    r = await _ask(TurnResult(completed=True, elicitation_requested=True))
    assert r.verdict is Verdict.SUPPORTED


async def test_ask_supported_even_if_turn_not_settled() -> None:
    r = await _ask(TurnResult(completed=False, elicitation_requested=True))
    assert r.verdict is Verdict.SUPPORTED


async def test_ask_passes_action_ask_to_driver() -> None:
    driver = _Driver(TurnResult(completed=True, elicitation_requested=True))
    await PolicyAskProbe().run(driver, _PROFILE)
    assert driver.seen_action == "ask"


async def test_ask_skipped_when_unmeasured() -> None:
    r = await _ask(TurnResult())
    assert r.verdict is Verdict.SKIPPED
    assert "not observable" in r.note


async def test_ask_skipped_when_no_elicitation_but_completed() -> None:
    r = await _ask(TurnResult(completed=True, tool_calls=[{"name": "list_files"}]))
    assert r.verdict is Verdict.SKIPPED


def test_resolve_elicitation_nests_id_in_data() -> None:
    """The approval event must carry elicitation_id INSIDE data (SessionEventInput
    has no top-level elicitation_id field, so a top-level id is dropped and the
    resolve is a silent no-op). Guards the payload shape the server reads."""
    from tests.harness_bench.full_server_driver import FullServerDriver

    posted: dict[str, object] = {}

    class _Client:
        def post(self, url, json):
            posted["url"] = url
            posted["json"] = json

    class _Shared:
        client = _Client()

    driver = FullServerDriver(_PROFILE, databricks_profile=None, shared=_Shared())
    driver._resolve_elicitation("sess-1", "elicit_abc")

    body = posted["json"]
    assert body["type"] == "approval"
    assert "elicitation_id" not in body, "id must not be top-level (server ignores it there)"
    assert body["data"] == {"elicitation_id": "elicit_abc", "action": "accept"}
