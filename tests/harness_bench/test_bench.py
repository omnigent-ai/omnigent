"""Bench conformance tests.

Two layers, matching the design doc:

- **Offline** (always, no network/creds): registry membership, profile
  completeness, reconciliation semantics, community-profile resolution,
  and that the matrix renders. Fast enough for every PR.
- **Live** (gated on ``--profile`` + a runnable harness CLI): runs the
  full probe set against each official harness and asserts P0 dimensions
  match what the profile declares — i.e. no ``DRIFT`` and a working
  ``basic_turn``.
"""

from __future__ import annotations

import json

import pytest

from omnigent.runtime.harnesses import _HARNESS_MODULES
from tests.harness_bench.bench import run_bench, run_harness
from tests.harness_bench.driver import SdkInprocDriver
from tests.harness_bench.manifest import OFFICIAL_PROFILES
from tests.harness_bench.probes import ALL_PROBES
from tests.harness_bench.profile import BenchProfile, resolve_profile
from tests.harness_bench.report import render_json, render_markdown
from tests.harness_bench.verdict import Priority, Verdict, reconcile

_OFFICIAL = list(OFFICIAL_PROFILES.values())
_OFFICIAL_IDS = [p.harness for p in _OFFICIAL]

# A community-style profile used to prove name-based resolution of an
# out-of-repo harness that ships its own BenchProfile.
_FAKE_PROFILE = BenchProfile(
    harness="fake-community",
    model="databricks-claude-sonnet-4-6",
    env_prefix="HARNESS_FAKE_",
    marker="FAKE_OK",
)


# ── Offline layer ───────────────────────────────────────────────


@pytest.mark.parametrize("profile", _OFFICIAL, ids=_OFFICIAL_IDS)
def test_official_harness_registered(profile: BenchProfile) -> None:
    assert profile.harness in _HARNESS_MODULES, (
        f"{profile.harness!r} has a bench profile but is not in _HARNESS_MODULES"
    )


@pytest.mark.parametrize("profile", _OFFICIAL, ids=_OFFICIAL_IDS)
def test_profile_fields_wellformed(profile: BenchProfile) -> None:
    assert profile.model, "profile must declare a test model"
    assert profile.env_prefix.endswith("_"), "env_prefix must end with '_'"
    assert profile.marker, "profile must declare a marker"


@pytest.mark.parametrize("profile", _OFFICIAL, ids=_OFFICIAL_IDS)
def test_declared_covers_every_p0_dimension(profile: BenchProfile) -> None:
    # Every P0 probe must have a declared verdict, or drift can never fire
    # for that cell — the bench would silently under-report a regression.
    for probe in ALL_PROBES:
        if probe.priority is Priority.P0:
            assert profile.declared_for(probe.name) is not Verdict.UNKNOWN, (
                f"{profile.harness!r} declares no verdict for P0 dimension {probe.name!r}"
            )


def test_reconcile_flags_concrete_mismatch() -> None:
    assert reconcile(Verdict.UNSUPPORTED, Verdict.SUPPORTED) is Verdict.DRIFT
    assert reconcile(Verdict.SUPPORTED, Verdict.UNSUPPORTED) is Verdict.DRIFT
    assert reconcile(Verdict.PARTIAL, Verdict.SUPPORTED) is Verdict.DRIFT


def test_reconcile_silent_when_either_side_inconclusive() -> None:
    assert reconcile(Verdict.SUPPORTED, Verdict.SUPPORTED) is Verdict.SUPPORTED
    assert reconcile(Verdict.SKIPPED, Verdict.SUPPORTED) is Verdict.SKIPPED
    assert reconcile(Verdict.SUPPORTED, Verdict.UNKNOWN) is Verdict.SUPPORTED


def test_resolve_official_and_community_and_unknown() -> None:
    assert resolve_profile("codex").harness == "codex"
    assert resolve_profile("tests.harness_bench.test_bench:_FAKE_PROFILE") is _FAKE_PROFILE
    with pytest.raises(KeyError):
        resolve_profile("no-such-harness")


def test_infra_failure_reason_classifies_auth_and_ignores_capability_gaps() -> None:
    from tests.harness_bench.driver import TurnResult, infra_failure_reason

    # A 403 gateway error is an environment problem -> yields a skip reason.
    auth = TurnResult(
        failed=True,
        error={
            "code": "RuntimeError",
            "message": "unexpected status 403 Forbidden: Invalid Token",
        },
    )
    reason = infra_failure_reason(auth)
    assert reason is not None
    assert "403" in reason

    # A plain failure with no infra marker is a real capability gap -> None.
    assert infra_failure_reason(TurnResult(failed=True, error="model refused the tool")) is None
    # A successful turn is never an infra failure.
    assert infra_failure_reason(TurnResult(completed=True, text="ok")) is None


async def test_offline_render_produces_matrix() -> None:
    matrix = await run_bench(_OFFICIAL, live=False)
    # Offline: nothing observed, so no drift and every cell is SKIPPED.
    assert not matrix.has_drift
    assert all(
        cell.observed is Verdict.SKIPPED for report in matrix.reports for cell in report.cells
    )
    md = render_markdown(matrix)
    assert "Harness capability matrix" in md
    for profile in _OFFICIAL:
        assert profile.harness in md
    # JSON is well-formed and carries every harness.
    payload = json.loads(render_json(matrix))
    assert {h["harness"] for h in payload["harnesses"]} == {p.harness for p in _OFFICIAL}


# ── Live layer (gated) ──────────────────────────────────────────


@pytest.fixture
def databricks_profile(request: pytest.FixtureRequest) -> str:
    profile = request.config.getoption("--profile")
    if not profile:
        pytest.skip("live bench requires --profile <name>")
    return str(profile)


@pytest.mark.parametrize("profile", _OFFICIAL, ids=_OFFICIAL_IDS)
async def test_live_harness_matches_declared(
    profile: BenchProfile, databricks_profile: str
) -> None:
    reason = SdkInprocDriver.unavailable(profile, databricks_profile=databricks_profile)
    if reason is not None:
        pytest.skip(f"{profile.harness}: {reason}")

    report = await run_harness(profile, databricks_profile=databricks_profile, live=True)

    basic = next(c for c in report.cells if c.probe_name == "basic_turn")
    if basic.observed is Verdict.SKIPPED:
        # Auth / gateway / connectivity problem (not a capability fact) —
        # the harness could not be exercised, so skip rather than fail.
        pytest.skip(f"{profile.harness}: {basic.note}")
    assert basic.observed is Verdict.SUPPORTED, (
        f"{profile.harness}: basic turn did not work ({basic.note}); "
        "the whole harness looks broken, not one capability"
    )
    drifted = [c for c in report.cells if c.is_drift]
    assert not drifted, (
        f"{profile.harness}: observed behavior drifted from the declared matrix: "
        + "; ".join(
            f"{c.title} declared {c.declared.name} but observed {c.observed.name} ({c.note})"
            for c in drifted
        )
    )
