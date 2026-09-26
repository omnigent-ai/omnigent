"""Tests for the repro-agent contract (``dev/repro-agent/AGENTS.md``).

The contract's environment-fidelity rules are what stop an unattended repro run
from reporting a scripted stand-in as the reported environment, so the rules a
rewrite must not drop are pinned here.
"""

from __future__ import annotations

from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "dev" / "repro-agent" / "AGENTS.md"


def test_seeded_harness_wire_events_are_a_named_stand_in() -> None:
    """Seeding the native forwarder's wire events instead of running the harness
    must be documented as a stand-in that caps the verdict at likely_repro."""
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    rule_paragraphs = [
        paragraph
        for paragraph in contract.split("\n\n")
        if "external_session_status" in paragraph and "external_assistant_message" in paragraph
    ]
    assert rule_paragraphs, (
        "the contract never names seeded harness wire events "
        "(external_session_status / external_assistant_message) as a stand-in"
    )
    assert any(
        "likely_repro" in paragraph and "stand-in" in paragraph for paragraph in rule_paragraphs
    ), "the seeded-wire-events rule must require likely_repro with the stand-in named"
