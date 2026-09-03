from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from examples.dpia_investigation.prepare_stage_one import prepare_stage_one
from examples.dpia_investigation.validate_artifact import validate_artifact
from omnigent.spec import load
from omnigent.spec.types import AgentSpec

_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "dpia_investigation"
_DIMENSIONS = [
    "purpose-scope",
    "lifecycle-flows",
    "legal-basis",
    "necessity-proportionality",
    "student-harms-rights",
    "vendor-transfer",
    "security-controls",
    "transparency-retention",
]
_RISKS = [
    "risk-false-positive",
    "risk-false-negative",
    "risk-discrimination",
    "risk-disability-disclosure",
    "risk-chilling-effect",
    "risk-function-creep",
    "risk-unauthorised-access",
    "risk-transfer",
]


def _one_line(value: str | None) -> str:
    return " ".join((value or "").split())


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _processing_model(version: int = 3) -> dict[str, Any]:
    return {
        "artifact": "processing-model",
        "case_id": "student-success-alert",
        "processing_model_version": version,
        "evidence": [{"id": "EV-01"}],
        "facts": [
            {
                "id": "fact-hosting",
                "value": "Hosting location is not confirmed",
            }
        ],
    }


def _correction_proposal(version: int = 3) -> dict[str, Any]:
    return {
        "artifact": "correction-proposal",
        "case_id": "student-success-alert",
        "processing_model_version": version,
        "policy_pack_version": "uk-dpia-2026.08-demo.1",
        "instruction": "Correct the hosting location using the cited procurement evidence.",
        "target_facts": [
            {
                "fact_id": "fact-hosting",
                "current_value": "Hosting location is not confirmed",
                "proposed_value": "The service is hosted in the United Kingdom.",
            }
        ],
        "new_evidence_refs": [
            {
                "evidence_id": "EV-01",
                "excerpt": "The synthetic intake records UK hosting.",
            }
        ],
        "affected_finding_ids": ["finding-6"],
        "expected_version_bump": {"from": version, "to": version + 1},
        "stale_finding_ids": ["finding-6"],
        "role_to_reassess": "privacy_assessor",
        "rationale": "Confirmed hosting changes the transfer and vendor assessment basis.",
    }


def _assessment(version: int = 3) -> dict[str, Any]:
    return {
        "artifact": "assessment-findings",
        "case_id": "student-success-alert",
        "processing_model_version": version,
        "policy_pack_version": "uk-dpia-2026.08-demo.1",
        "determinations": [
            {
                "id": f"finding-{index + 1}",
                "dimension_id": dimension,
                "question": f"Question {index + 1}",
                "rule_result": "unclear",
                "status": "needs_judgement",
                "policy_criteria": "Policy floor",
                "professional_judgement": "Officer judgement remains required",
                "reasoning": "Synthetic evidence supports a cautious finding",
                "evidence_ids": ["EV-01"],
                "policy_rule_ids": ["uk-gdpr-art35"],
                "gaps": [],
            }
            for index, dimension in enumerate(_DIMENSIONS)
        ],
        "risks": [
            {
                "id": risk_id,
                "harm": f"Synthetic harm {index + 1}",
                "affected_subjects": "Synthetic students",
                "likelihood": "medium",
                "severity": "high",
                "inherent_rating": "high",
                "controls": ["Human review"],
                "mitigation": "Test and monitor before launch",
                "residual_rating": "medium",
                "owner": "Synthetic owner",
                "due_date": "2026-10-01",
                "evidence_ids": ["EV-01"],
            }
            for index, risk_id in enumerate(_RISKS)
        ],
        "recommendation": "full_dpia_likely",
        "recommendation_rationale": "Multiple high-risk indicators require officer review",
        "unresolved_gaps": [],
        "specialist_disagreement": [],
    }


def _stage_one(version: int = 3, *, compromised: bool = False) -> dict[str, Any]:
    return {
        "artifact": "verification-stage-1",
        "case_id": "student-success-alert",
        "processing_model_version": version,
        "policy_pack_version": "uk-dpia-2026.08-demo.1",
        "blind_review_completed": not compromised,
        "blind_review_compromised": compromised,
        "evidence_coverage": []
        if compromised
        else [
            {
                "dimension_id": dimension,
                "status": "supported",
                "evidence_ids": ["EV-01"],
                "notes": "Evidence inspected without assessor context",
            }
            for dimension in _DIMENSIONS
        ],
        "contradictions": [],
        "unsupported_claims": [],
        "missing_material_facts": [],
        "policy_application_notes": [],
        "frozen_at": "2026-08-21T12:00:00Z",
    }


def _verification(stage_one_hash: str, version: int = 3) -> dict[str, Any]:
    return {
        "artifact": "verification",
        "case_id": "student-success-alert",
        "processing_model_version": version,
        "policy_pack_version": "uk-dpia-2026.08-demo.1",
        "stage_1_artifact_sha256": stage_one_hash,
        "verdict": "verified_with_caveats",
        "citation_checks": [
            {
                "finding_id": f"finding-{index + 1}",
                "evidence_valid": True,
                "policy_valid": True,
                "notes": "Citation checked",
            }
            for index in range(8)
        ],
        "unsupported_claim_ids": [],
        "disagreement": [],
        "recommendation_check": "Recommendation is supported with caveats",
        "notes": [],
    }


@pytest.fixture(scope="module")
def dpia_spec() -> AgentSpec:
    return load(_BUNDLE)


def test_dpia_bundle_has_three_independent_professional_roles(dpia_spec: AgentSpec) -> None:
    assert dpia_spec.name == "dpia-investigation"
    assert dpia_spec.executor.config.get("harness") == "codex"
    assert dpia_spec.executor.model is None
    assert sorted(dpia_spec.tools.agents) == [
        "independent_verifier",
        "privacy_assessor",
        "process_investigator",
    ]
    by_name = {agent.name: agent for agent in dpia_spec.sub_agents}
    assert set(by_name) == set(dpia_spec.tools.agents)
    for agent in by_name.values():
        assert agent.executor.config.get("harness") == "codex"
        assert agent.executor.model is None
        assert agent.executor.profile is None


def test_dpia_verifier_is_blinded_before_recommendation_review(dpia_spec: AgentSpec) -> None:
    prompt = _one_line(dpia_spec.instructions)
    stage_1 = prompt.index("In the same turn, before receiving either result")
    stage_2 = prompt.index("Compute `sha256sum")
    assert stage_1 < stage_2
    assert "Give the verifier exactly the complete text" in prompt
    assert "nothing else" in prompt
    verifier = next(
        agent for agent in dpia_spec.sub_agents if agent.name == "independent_verifier"
    )
    verifier_prompt = _one_line(verifier.instructions)
    assert "blind_review_compromised" in verifier_prompt
    assert verifier_prompt.index("STAGE 1") < verifier_prompt.index("STAGE 2")


def test_dpia_roles_keep_recommendation_and_officer_decision_separate(
    dpia_spec: AgentSpec,
) -> None:
    by_name = {agent.name: agent for agent in dpia_spec.sub_agents}
    assert "Do not assess whether a full DPIA is legally required" in _one_line(
        by_name["process_investigator"].instructions
    )
    assert "requiring Privacy Officer verification" in _one_line(
        by_name["privacy_assessor"].instructions
    )
    assert (
        "make the officer's decision"
        in _one_line(by_name["independent_verifier"].instructions).lower()
    )


def test_dpia_bundle_schemas_are_strict_json_objects() -> None:
    schemas = sorted((_BUNDLE / "schemas").glob("*.json"))
    assert [path.name for path in schemas] == [
        "assessment-findings.schema.json",
        "correction-proposal.schema.json",
        "dpia-outcome.schema.json",
        "dpia-request.schema.json",
        "processing-model.schema.json",
        "stakeholder-response.schema.json",
        "verification-stage-1.schema.json",
        "verification.schema.json",
    ]
    for path in schemas:
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"]


def test_dpia_correction_proposal_contract_and_role_prompt(dpia_spec: AgentSpec) -> None:
    prompt = _one_line(dpia_spec.instructions)
    investigator = next(
        agent for agent in dpia_spec.sub_agents if agent.name == "process_investigator"
    )
    assert "proposal-only path" in prompt
    assert "Save that object unchanged" in prompt
    assert "processing-model.schema.json" in prompt
    assert "determination dependency map" in prompt
    assert "dependency_fact_ids" in prompt
    assert "Never apply" in prompt
    assert "correction-proposal.schema.json" in _one_line(investigator.instructions)


def test_dpia_correction_validator_checks_fact_evidence_version_and_stale_set(
    tmp_path: Path,
) -> None:
    processing_path = _write_json(tmp_path / "processing.json", _processing_model())
    proposal = _correction_proposal()
    proposal["target_facts"][0]["fact_id"] = "fact-unknown"
    proposal["new_evidence_refs"][0]["evidence_id"] = "EV-99"
    proposal["expected_version_bump"] = {"from": 2, "to": 8}
    proposal["stale_finding_ids"] = ["finding-unknown"]
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)

    errors = validate_artifact(
        _BUNDLE / "schemas" / "correction-proposal.schema.json",
        proposal_path,
        processing_model_path=processing_path,
    )

    assert any("fact-unknown" in error for error in errors)
    assert any("EV-99" in error for error in errors)
    assert any("expected_version_bump" in error for error in errors)
    assert any("finding-unknown" in error for error in errors)


def test_dpia_artifact_validator_rejects_schema_invalid_json(tmp_path: Path) -> None:
    artifact = tmp_path / "invalid.json"
    artifact.write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(_BUNDLE / "validate_artifact.py"),
            str(_BUNDLE / "schemas" / "processing-model.schema.json"),
            str(artifact),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "is a required property" in result.stderr


def test_dpia_stage_one_payload_excludes_assessor_context(tmp_path: Path) -> None:
    processing_path = _write_json(tmp_path / "processing.json", _processing_model())
    payload = prepare_stage_one(
        _BUNDLE / "resources" / "case-evidence.md",
        _BUNDLE / "resources" / "uk-policy-pack.json",
        processing_path,
        _BUNDLE / "schemas" / "verification-stage-1.schema.json",
    )
    assert set(payload) == {
        "stage",
        "allowed_context",
        "evidence_pack",
        "policy_pack",
        "processing_model",
        "output_schema",
    }
    assert payload["allowed_context"] == [
        "evidence_pack",
        "policy_pack",
        "processing_model",
        "output_schema",
    ]


def test_dpia_validator_blocks_compromised_blind_review(tmp_path: Path) -> None:
    processing_path = _write_json(tmp_path / "processing.json", _processing_model())
    stage_one_path = _write_json(tmp_path / "stage-one.json", _stage_one(compromised=True))
    errors = validate_artifact(
        _BUNDLE / "schemas" / "verification-stage-1.schema.json",
        stage_one_path,
        processing_model_path=processing_path,
    )
    assert errors == [
        "blind_review_compromised: Stage 1 must be discarded and "
        "restarted in a fresh verifier session"
    ]


def test_dpia_validator_rejects_cross_version_and_phantom_evidence(tmp_path: Path) -> None:
    processing_path = _write_json(tmp_path / "processing.json", _processing_model(version=4))
    assessment = _assessment(version=3)
    assessment["determinations"][0]["evidence_ids"] = ["EV-99"]
    assessment_path = _write_json(tmp_path / "assessment.json", assessment)
    errors = validate_artifact(
        _BUNDLE / "schemas" / "assessment-findings.schema.json",
        assessment_path,
        processing_model_path=processing_path,
    )
    assert any("processing_model_version" in error for error in errors)
    assert any("EV-99" in error for error in errors)


def test_dpia_validator_requires_exact_dimension_coverage(tmp_path: Path) -> None:
    processing_path = _write_json(tmp_path / "processing.json", _processing_model())
    assessment = _assessment()
    for determination in assessment["determinations"]:
        determination["dimension_id"] = "purpose-scope"
    assessment_path = _write_json(tmp_path / "assessment.json", assessment)
    errors = validate_artifact(
        _BUNDLE / "schemas" / "assessment-findings.schema.json",
        assessment_path,
        processing_model_path=processing_path,
    )
    assert any("determination dimensions: duplicate values" in error for error in errors)
    assert any("unexpected" in error or "missing" in error for error in errors)


def test_dpia_validator_checks_frozen_hash_and_finding_ids(tmp_path: Path) -> None:
    processing_path = _write_json(tmp_path / "processing.json", _processing_model())
    assessment_path = _write_json(tmp_path / "assessment.json", _assessment())
    stage_one_path = _write_json(tmp_path / "stage-one.json", _stage_one())
    verification = _verification("0" * 64)
    verification["citation_checks"][0]["finding_id"] = "finding-unknown"
    verification_path = _write_json(tmp_path / "verification.json", verification)
    errors = validate_artifact(
        _BUNDLE / "schemas" / "verification.schema.json",
        verification_path,
        processing_model_path=processing_path,
        assessment_path=assessment_path,
        stage_one_path=stage_one_path,
    )
    assert any("stage_1_artifact_sha256" in error for error in errors)
    assert any("finding-unknown" in error for error in errors)


def _dpia_request() -> dict[str, Any]:
    return {
        "artifact": "dpia-request",
        "request_id": "req-vendor-wellbeing-mfr01",
        "requester": {"name": "Priya Shah", "team": "Procurement"},
        "project": {
            "title": "Vendor Wellbeing Analytics",
            "purpose": "Score student wellbeing survey responses to prioritise support.",
            "data_subjects": "Enrolled students",
            "personal_data": "Survey responses, student ids",
            "vendors": "Acme Analytics Ltd",
            "timeline": "Pilot in October",
        },
        "known_unknowns": ["Hosting location"],
        "submitted_at": "2026-08-22T09:00:00Z",
    }


def _stakeholder_response() -> dict[str, Any]:
    return {
        "artifact": "stakeholder-response",
        "case_id": "student-success-alert",
        "request_id": "req-vendor-wellbeing-mfr01",
        "respondent": {"name": "Jordan Ali", "team": "IT Security"},
        "answers": [
            {
                "question_id": "q-hosting",
                "response": "The model and database are hosted in London.",
            }
        ],
        "submitted_at": "2026-08-22T10:00:00Z",
    }


def _dpia_outcome() -> dict[str, Any]:
    return {
        "artifact": "dpia-outcome",
        "request_id": "req-vendor-wellbeing-mfr01",
        "case_id": "student-success-alert",
        "decision": "approved-with-conditions",
        "reasons": ["Screening indicates a full DPIA is likely before launch."],
        "conditions": [
            {
                "action": "Confirm the hosting region in the vendor contract.",
                "owner": "Procurement",
                "due": "2026-09-15",
            }
        ],
        "review_date": "2027-02-01",
        "contact": "privacy-office@university.example",
        "decided_by": "Alex Morgan",
        "decided_at": "2026-08-22T12:00:00Z",
    }


def test_dpia_request_flow_artifacts_validate_standalone(tmp_path: Path) -> None:
    for name, artifact in (
        ("dpia-request", _dpia_request()),
        ("stakeholder-response", _stakeholder_response()),
        ("dpia-outcome", _dpia_outcome()),
    ):
        artifact_path = _write_json(tmp_path / f"{name}.json", artifact)
        errors = validate_artifact(_BUNDLE / "schemas" / f"{name}.schema.json", artifact_path)
        assert errors == []


def test_dpia_request_flow_validator_rejects_gate_violations(tmp_path: Path) -> None:
    request = _dpia_request()
    request["auto_accept"] = True
    request_path = _write_json(tmp_path / "request.json", request)
    request_errors = validate_artifact(
        _BUNDLE / "schemas" / "dpia-request.schema.json", request_path
    )
    assert any("auto_accept" in error for error in request_errors)

    response = _stakeholder_response()
    response["answers"] = response["answers"] * 2
    response_path = _write_json(tmp_path / "response.json", response)
    response_errors = validate_artifact(
        _BUNDLE / "schemas" / "stakeholder-response.schema.json", response_path
    )
    assert any("duplicate question ids" in error for error in response_errors)

    outcome = _dpia_outcome()
    outcome["conditions"] = []
    outcome_path = _write_json(tmp_path / "outcome.json", outcome)
    outcome_errors = validate_artifact(
        _BUNDLE / "schemas" / "dpia-outcome.schema.json", outcome_path
    )
    assert any("conditions" in error for error in outcome_errors)
