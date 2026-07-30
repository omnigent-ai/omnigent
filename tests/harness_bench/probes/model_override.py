"""Model-override probe — is a caller-specified model honored?

The driver launches the harness with the profile's model in
``{env_prefix}MODEL`` (a non-default id the caller chose). This probe
first checks that model passes Omnigent's override validation and family
gate — the same checks the server applies before spawn — then confirms a
live turn on that model completes, proving the id threaded through to a
real gateway route rather than being dropped.

Limitation (documented for the next iteration): a completed turn proves
the id was *accepted and routable*, not that a different id would have
routed differently. The stronger contrast probe — a family-valid but
nonexistent id must FAIL while the real id SUCCEEDS — is a phase-2
follow-up; it costs a second (deliberately failing) turn and needs the
gateway to reject unknown ids promptly.
"""

from __future__ import annotations

from omnigent.model_override import model_family_mismatch, validate_model_override
from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class ModelOverrideProbe(CapabilityProbe):
    name = "model_override"
    title = "Model override"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        # Offline half: the override mechanism itself must accept this
        # harness+model pair. A rejection here is a hard UNSUPPORTED — the
        # caller could never set this model in the first place.
        try:
            validate_model_override(profile.model)
        except ValueError as exc:
            return ProbeResult(Verdict.UNSUPPORTED, note=f"model id rejected by validator: {exc}")
        mismatch = model_family_mismatch(profile.harness, profile.model)
        if mismatch is not None:
            return ProbeResult(Verdict.UNSUPPORTED, note=f"family gate rejects model: {mismatch}")

        # Live half: the harness was spawned with {env_prefix}MODEL set to
        # profile.model; a completing turn proves the override routed.
        result = await driver.run_basic_turn(profile.marker)
        detail = {"model": profile.model, "completed": result.completed}
        if result.completed and result.text:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"turn routed on caller-specified model {profile.model!r}",
                detail=detail,
            )
        infra = infra_failure_reason(result)
        if infra is not None:
            return ProbeResult(Verdict.SKIPPED, note=infra, detail=detail)
        if result.timed_out:
            return ProbeResult(Verdict.SKIPPED, note="override turn timed out", detail=detail)
        return ProbeResult(
            Verdict.UNSUPPORTED,
            note=f"turn on {profile.model!r} did not complete: {result.error}",
            detail=detail,
        )
