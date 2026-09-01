from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.memory.hindsight_gate import (
    HindsightCapabilityGateError,
    hindsight_endpoint_sha256,
    require_hindsight_write_capabilities,
)

_API_URL = "https://hindsight.example.test/"
_NOW = 2_000_000_000


def _report(path: Path, **overrides: object) -> Path:
    capabilities = {
        name: {"status": "passed", "evidence": f"proof for {name}"}
        for name in (
            "individual_record_deletion",
            "bank_deletion",
            "tenant_partitioning",
            "memory_retention",
            "export",
            "backup_deletion",
            "idempotent_capture",
        )
    }
    payload = {
        "schema_version": 1,
        "provider": "hindsight",
        "api_url_sha256": hindsight_endpoint_sha256(_API_URL),
        "client_version": "0.8.3",
        "server_version": "0.9.2",
        "checked_at": _NOW - 60,
        "valid_until": _NOW + 3_600,
        "capabilities": capabilities,
        **overrides,
    }
    path.write_text(json.dumps(payload))
    return path


def _env(path: Path) -> dict[str, str]:
    return {
        "OMNIGENT_HINDSIGHT_WRITES_ENABLED": "1",
        "OMNIGENT_HINDSIGHT_CAPABILITY_REPORT": str(path),
    }


def test_hindsight_write_gate_is_closed_by_default() -> None:
    with pytest.raises(HindsightCapabilityGateError, match="is not enabled"):
        require_hindsight_write_capabilities(
            _API_URL,
            server_version="0.9.2",
            environ={},
            now=_NOW,
            installed_client_version="0.8.3",
        )


def test_hindsight_write_gate_accepts_current_endpoint_bound_evidence(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path / "report.json")

    require_hindsight_write_capabilities(
        _API_URL,
        server_version="0.9.2",
        environ=_env(report),
        now=_NOW,
        installed_client_version="0.8.3",
    )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"api_url_sha256": hindsight_endpoint_sha256("https://other.example.test")}, "endpoint"),
        ({"client_version": "0.8.2"}, "client version"),
        ({"server_version": "0.9.1"}, "server version"),
        ({"valid_until": _NOW}, "expired"),
        ({"valid_until": _NOW + 31 * 24 * 60 * 60}, "validity window"),
        (
            {
                "capabilities": {
                    "bank_deletion": {"status": "passed", "evidence": "proof"},
                }
            },
            "not proven",
        ),
    ],
)
def test_hindsight_write_gate_rejects_stale_or_incomplete_evidence(
    tmp_path: Path,
    overrides: dict[str, object],
    match: str,
) -> None:
    report = _report(tmp_path / "report.json", **overrides)

    with pytest.raises(HindsightCapabilityGateError, match=match):
        require_hindsight_write_capabilities(
            _API_URL,
            server_version="0.9.2",
            environ=_env(report),
            now=_NOW,
            installed_client_version="0.8.3",
        )
