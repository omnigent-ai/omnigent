from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def prepare_stage_one(
    evidence_pack: Path,
    policy_pack: Path,
    processing_model: Path,
    output_schema: Path,
) -> dict[str, Any]:
    return {
        "stage": "blind-evidence-review",
        "allowed_context": [
            "evidence_pack",
            "policy_pack",
            "processing_model",
            "output_schema",
        ],
        "evidence_pack": evidence_pack.read_text(encoding="utf-8"),
        "policy_pack": _load_json(policy_pack),
        "processing_model": _load_json(processing_model),
        "output_schema": _load_json(output_schema),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-pack", required=True, type=Path)
    parser.add_argument("--policy-pack", required=True, type=Path)
    parser.add_argument("--processing-model", required=True, type=Path)
    parser.add_argument("--output-schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = prepare_stage_one(
        args.evidence_pack,
        args.policy_pack,
        args.processing_model,
        args.output_schema,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"prepared: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
