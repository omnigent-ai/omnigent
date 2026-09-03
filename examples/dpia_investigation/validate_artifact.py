from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

DIMENSIONS = {
    "purpose-scope",
    "lifecycle-flows",
    "legal-basis",
    "necessity-proportionality",
    "student-harms-rights",
    "vendor-transfer",
    "security-controls",
    "transparency-retention",
}
LIFECYCLE_STAGES = {
    "collection",
    "storage",
    "access",
    "use",
    "sharing",
    "transfer",
    "retention",
    "deletion",
}
RISK_IDS = {
    "risk-false-positive",
    "risk-false-negative",
    "risk-discrimination",
    "risk-disability-disclosure",
    "risk-chilling-effect",
    "risk-function-creep",
    "risk-unauthorised-access",
    "risk-transfer",
}
REFERENCE_KEYS = {"evidence_ids", "source_evidence_ids"}
_BUNDLE = Path(__file__).resolve().parent


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _collect_references(value: Any, keys: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            else:
                found.update(_collect_references(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_references(child, keys))
    return found


def _duplicates(values: list[str]) -> set[str]:
    return {value for value in values if values.count(value) > 1}


def _evidence_sections(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^## (EV-\d+)\b.*$", text, flags=re.MULTILINE))
    return {
        match.group(1): text[
            match.start() : matches[index + 1].start() if index + 1 < len(matches) else len(text)
        ]
        for index, match in enumerate(matches)
    }


def _normalise_text(value: str) -> str:
    return " ".join(value.replace(">", " ").split()).casefold()


def _same_field(
    artifact: dict[str, Any],
    expected: dict[str, Any],
    field: str,
    source: str,
) -> list[str]:
    if artifact.get(field) == expected.get(field):
        return []
    return [
        f"{field}: {artifact.get(field)!r} does not match {source} value {expected.get(field)!r}"
    ]


def _exact_coverage(values: list[str], expected: set[str], label: str) -> list[str]:
    errors: list[str] = []
    duplicates = _duplicates(values)
    if duplicates:
        errors.append(f"{label}: duplicate values {sorted(duplicates)}")
    actual = set(values)
    if actual != expected:
        errors.append(
            f"{label}: missing {sorted(expected - actual)}; unexpected {sorted(actual - expected)}"
        )
    return errors


def _unknown_references(
    artifact: dict[str, Any],
    keys: set[str],
    allowed: set[str],
    label: str,
) -> list[str]:
    unknown = _collect_references(artifact, keys) - allowed
    return [f"{label}: unknown references {sorted(unknown)}"] if unknown else []


def _context_errors(
    artifact: dict[str, Any],
    *,
    evidence_pack: Path,
    policy_pack: Path,
    processing_model: dict[str, Any] | None,
    assessment: dict[str, Any] | None,
    stage_one: dict[str, Any] | None,
    stage_one_path: Path | None,
) -> list[str]:
    errors: list[str] = []
    artifact_type = artifact.get("artifact")
    evidence_sections = _evidence_sections(evidence_pack)
    source_evidence_ids = set(evidence_sections)
    policy = _load_json(policy_pack)
    policy_rule_ids = {
        rule["id"]
        for rule in policy.get("rules", [])
        if isinstance(rule, dict) and isinstance(rule.get("id"), str)
    }

    if artifact_type == "processing-model":
        evidence = artifact.get("evidence", [])
        artifact_evidence_ids = [
            item.get("id")
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        errors.extend(
            _exact_coverage(artifact_evidence_ids, source_evidence_ids, "evidence coverage")
        )
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            evidence_id = item["id"]
            excerpt = item.get("excerpt")
            if (
                isinstance(excerpt, str)
                and evidence_id in evidence_sections
                and _normalise_text(excerpt) not in _normalise_text(evidence_sections[evidence_id])
            ):
                errors.append(f"evidence.{evidence_id}.excerpt: not found in the source pack")
        errors.extend(
            _unknown_references(
                artifact,
                REFERENCE_KEYS,
                set(artifact_evidence_ids),
                "processing-model evidence",
            )
        )
        fact_versions = [
            fact.get("processing_model_version")
            for fact in artifact.get("facts", [])
            if isinstance(fact, dict)
        ]
        mismatched_fact_versions = {
            version
            for version in fact_versions
            if version != artifact.get("processing_model_version")
        }
        if mismatched_fact_versions:
            errors.append(
                "facts.processing_model_version: values do not match the artifact version "
                f"{sorted(mismatched_fact_versions, key=str)}"
            )
        stages = [
            item.get("stage")
            for item in artifact.get("lifecycle", [])
            if isinstance(item, dict) and isinstance(item.get("stage"), str)
        ]
        errors.extend(_exact_coverage(stages, LIFECYCLE_STAGES, "lifecycle stages"))
        return errors

    if artifact_type == "dpia-request":
        unknowns = [item for item in artifact.get("known_unknowns", []) if isinstance(item, str)]
        duplicate_unknowns = _duplicates(unknowns)
        if duplicate_unknowns:
            errors.append(f"known_unknowns: duplicate values {sorted(duplicate_unknowns)}")
        return errors

    if artifact_type == "stakeholder-response":
        question_ids = [
            item["question_id"]
            for item in artifact.get("answers", [])
            if isinstance(item, dict) and isinstance(item.get("question_id"), str)
        ]
        duplicate_questions = _duplicates(question_ids)
        if duplicate_questions:
            errors.append(f"answers: duplicate question ids {sorted(duplicate_questions)}")
        return errors

    if artifact_type == "dpia-outcome":
        if artifact.get("decision") == "approved-with-conditions" and not artifact.get(
            "conditions"
        ):
            errors.append("conditions: required for an approved-with-conditions decision")
        return errors

    if processing_model is None:
        return [f"{artifact_type}: --processing-model is required"]
    errors.extend(_same_field(artifact, processing_model, "case_id", "processing model"))
    errors.extend(
        _same_field(
            artifact,
            processing_model,
            "processing_model_version",
            "processing model",
        )
    )
    if artifact.get("policy_pack_version") != policy.get("version"):
        errors.append(
            "policy_pack_version: "
            f"{artifact.get('policy_pack_version')!r} does not match policy pack value "
            f"{policy.get('version')!r}"
        )
    processing_evidence_ids = {
        item["id"]
        for item in processing_model.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    errors.extend(
        _unknown_references(
            artifact,
            REFERENCE_KEYS,
            processing_evidence_ids,
            f"{artifact_type} evidence",
        )
    )
    errors.extend(
        _unknown_references(
            artifact,
            {"policy_rule_ids"},
            policy_rule_ids,
            f"{artifact_type} policy",
        )
    )

    if artifact_type == "assessment-findings":
        dimensions = [
            item.get("dimension_id")
            for item in artifact.get("determinations", [])
            if isinstance(item, dict) and isinstance(item.get("dimension_id"), str)
        ]
        errors.extend(_exact_coverage(dimensions, DIMENSIONS, "determination dimensions"))
        finding_ids = [
            item.get("id")
            for item in artifact.get("determinations", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        duplicate_finding_ids = _duplicates(finding_ids)
        if duplicate_finding_ids:
            errors.append(f"determinations: duplicate ids {sorted(duplicate_finding_ids)}")
        risk_ids = [
            item.get("id")
            for item in artifact.get("risks", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        errors.extend(_exact_coverage(risk_ids, RISK_IDS, "risk register"))
        return errors

    if artifact_type == "correction-proposal":
        processing_facts = {
            item["id"]: item
            for item in processing_model.get("facts", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        target_facts = [
            item for item in artifact.get("target_facts", []) if isinstance(item, dict)
        ]
        target_fact_ids = [
            item["fact_id"] for item in target_facts if isinstance(item.get("fact_id"), str)
        ]
        duplicate_fact_ids = _duplicates(target_fact_ids)
        if duplicate_fact_ids:
            errors.append(f"target_facts: duplicate ids {sorted(duplicate_fact_ids)}")
        unknown_fact_ids = set(target_fact_ids) - set(processing_facts)
        if unknown_fact_ids:
            errors.append(f"target_facts: unknown ids {sorted(unknown_fact_ids)}")
        for target in target_facts:
            fact_id = target.get("fact_id")
            if fact_id not in processing_facts:
                continue
            current_value = processing_facts[fact_id].get("value")
            if target.get("current_value") != current_value:
                errors.append(
                    f"target_facts.{fact_id}.current_value: {target.get('current_value')!r} "
                    f"does not match processing model value {current_value!r}"
                )
            if target.get("proposed_value") == current_value:
                errors.append(
                    f"target_facts.{fact_id}.proposed_value: must change the current value"
                )
        evidence_ref_ids = {
            item.get("evidence_id")
            for item in artifact.get("new_evidence_refs", [])
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
        }
        unknown_evidence_ids = evidence_ref_ids - processing_evidence_ids
        if unknown_evidence_ids:
            errors.append(f"new_evidence_refs: unknown references {sorted(unknown_evidence_ids)}")
        current_version = processing_model.get("processing_model_version")
        version_bump = artifact.get("expected_version_bump", {})
        from_version = version_bump.get("from") if isinstance(version_bump, dict) else None
        to_version = version_bump.get("to") if isinstance(version_bump, dict) else None
        if from_version != current_version:
            errors.append(
                "expected_version_bump.from: "
                f"{from_version!r} does not match current version {current_version!r}"
            )
        if isinstance(current_version, int) and to_version != current_version + 1:
            errors.append(
                "expected_version_bump.to: "
                f"{to_version!r} does not match next version {current_version + 1!r}"
            )
        affected_ids = set(artifact.get("affected_finding_ids", []))
        stale_ids = set(artifact.get("stale_finding_ids", []))
        if not stale_ids.issubset(affected_ids):
            errors.append(
                "stale_finding_ids: not present in affected_finding_ids "
                f"{sorted(stale_ids - affected_ids)}"
            )
        return errors

    if artifact_type == "verification-stage-1":
        if artifact.get("blind_review_compromised") is True:
            errors.append(
                "blind_review_compromised: Stage 1 must be discarded and "
                "restarted in a fresh verifier session"
            )
            return errors
        dimensions = [
            item.get("dimension_id")
            for item in artifact.get("evidence_coverage", [])
            if isinstance(item, dict) and isinstance(item.get("dimension_id"), str)
        ]
        errors.extend(_exact_coverage(dimensions, DIMENSIONS, "blind evidence dimensions"))
        return errors

    if artifact_type != "verification":
        return [f"artifact: unsupported value {artifact_type!r}"]
    if assessment is None:
        errors.append("verification: --assessment is required")
    if stage_one is None or stage_one_path is None:
        errors.append("verification: --stage-one is required")
    if errors:
        return errors
    assert assessment is not None
    assert stage_one is not None
    assert stage_one_path is not None
    errors.extend(_same_field(artifact, assessment, "case_id", "assessment"))
    errors.extend(
        _same_field(
            artifact,
            assessment,
            "processing_model_version",
            "assessment",
        )
    )
    errors.extend(_same_field(artifact, assessment, "policy_pack_version", "assessment"))
    errors.extend(_same_field(stage_one, processing_model, "case_id", "processing model"))
    errors.extend(
        _same_field(
            stage_one,
            processing_model,
            "processing_model_version",
            "processing model",
        )
    )
    if stage_one.get("blind_review_completed") is not True:
        errors.append("stage-one.blind_review_completed: must be true")
    if stage_one.get("blind_review_compromised") is not False:
        errors.append("stage-one.blind_review_compromised: must be false")
    actual_hash = hashlib.sha256(stage_one_path.read_bytes()).hexdigest()
    if artifact.get("stage_1_artifact_sha256") != actual_hash:
        errors.append(
            f"stage_1_artifact_sha256: does not match the frozen Stage-1 artifact ({actual_hash})"
        )
    assessment_finding_ids = [
        item.get("id")
        for item in assessment.get("determinations", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    citation_finding_ids = [
        item.get("finding_id")
        for item in artifact.get("citation_checks", [])
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    ]
    errors.extend(
        _exact_coverage(
            citation_finding_ids,
            set(assessment_finding_ids),
            "verification finding ids",
        )
    )
    allowed_claim_ids = set(assessment_finding_ids) | {
        item["id"]
        for item in assessment.get("risks", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    unknown_claims = set(artifact.get("unsupported_claim_ids", [])) - allowed_claim_ids
    if unknown_claims:
        errors.append(f"unsupported_claim_ids: unknown ids {sorted(unknown_claims)}")
    return errors


def validate_artifact(
    schema_path: Path,
    artifact_path: Path,
    *,
    evidence_pack: Path = _BUNDLE / "resources" / "case-evidence.md",
    policy_pack: Path = _BUNDLE / "resources" / "uk-policy-pack.json",
    processing_model_path: Path | None = None,
    assessment_path: Path | None = None,
    stage_one_path: Path | None = None,
) -> list[str]:
    schema = _load_json(schema_path)
    artifact = _load_json(artifact_path)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(
            validator.iter_errors(artifact), key=lambda item: list(item.absolute_path)
        )
    ]
    if errors:
        return errors
    return _context_errors(
        artifact,
        evidence_pack=evidence_pack,
        policy_pack=policy_pack,
        processing_model=_load_json(processing_model_path) if processing_model_path else None,
        assessment=_load_json(assessment_path) if assessment_path else None,
        stage_one=_load_json(stage_one_path) if stage_one_path else None,
        stage_one_path=stage_one_path,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("schema", type=Path)
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--evidence-pack",
        type=Path,
        default=_BUNDLE / "resources" / "case-evidence.md",
    )
    parser.add_argument(
        "--policy-pack",
        type=Path,
        default=_BUNDLE / "resources" / "uk-policy-pack.json",
    )
    parser.add_argument("--processing-model", type=Path)
    parser.add_argument("--assessment", type=Path)
    parser.add_argument("--stage-one", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        errors = validate_artifact(
            args.schema,
            args.artifact,
            evidence_pack=args.evidence_pack,
            policy_pack=args.policy_pack,
            processing_model_path=args.processing_model,
            assessment_path=args.assessment,
            stage_one_path=args.stage_one,
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"invalid artifact input: {error}", file=sys.stderr)
        return 2
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 3 if any(error.startswith("blind_review_compromised:") for error in errors) else 1
    print(f"valid: {args.artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
