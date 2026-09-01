from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_REPORT_SCHEMA_VERSION = 1
_MAX_REPORT_AGE_SECONDS = 30 * 24 * 60 * 60
_REQUIRED_CAPABILITIES = (
    "individual_record_deletion",
    "bank_deletion",
    "tenant_partitioning",
    "memory_retention",
    "export",
    "backup_deletion",
    "idempotent_capture",
)


class HindsightCapabilityGateError(RuntimeError):
    pass


def hindsight_endpoint_sha256(api_url: str) -> str:
    parsed = urlsplit(api_url.strip())
    loopback = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if (parsed.scheme != "https" and not loopback) or not parsed.netloc:
        raise HindsightCapabilityGateError("Hindsight API URL must use HTTPS or loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HindsightCapabilityGateError(
            "Hindsight API URL must not contain credentials, a query, or a fragment"
        )
    normalized = urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def require_hindsight_write_capabilities(
    api_url: str,
    *,
    server_version: str,
    environ: Mapping[str, str] | None = None,
    now: int | None = None,
    installed_client_version: str | None = None,
) -> None:
    source = os.environ if environ is None else environ
    if source.get("OMNIGENT_HINDSIGHT_WRITES_ENABLED", "").strip().lower() not in {
        "1",
        "true",
    }:
        raise HindsightCapabilityGateError("OMNIGENT_HINDSIGHT_WRITES_ENABLED is not enabled")
    report_path = source.get("OMNIGENT_HINDSIGHT_CAPABILITY_REPORT", "").strip()
    if not report_path:
        raise HindsightCapabilityGateError("OMNIGENT_HINDSIGHT_CAPABILITY_REPORT is required")
    try:
        report: object = json.loads(Path(report_path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HindsightCapabilityGateError(
            "Hindsight capability report is unreadable or invalid"
        ) from exc
    if not isinstance(report, dict) or report.get("schema_version") != _REPORT_SCHEMA_VERSION:
        raise HindsightCapabilityGateError("Hindsight capability report schema is unsupported")
    if report.get("provider") != "hindsight":
        raise HindsightCapabilityGateError("Hindsight capability report provider is invalid")
    if report.get("api_url_sha256") != hindsight_endpoint_sha256(api_url):
        raise HindsightCapabilityGateError(
            "Hindsight capability report does not match the configured endpoint"
        )

    checked_at = report.get("checked_at")
    valid_until = report.get("valid_until")
    current_time = int(time.time()) if now is None else now
    if (
        not isinstance(checked_at, int)
        or isinstance(checked_at, bool)
        or not isinstance(valid_until, int)
        or isinstance(valid_until, bool)
        or checked_at > current_time + 300
        or valid_until <= current_time
        or valid_until > checked_at + _MAX_REPORT_AGE_SECONDS
    ):
        raise HindsightCapabilityGateError(
            "Hindsight capability report is expired or has an invalid validity window"
        )

    if installed_client_version is None:
        try:
            installed_client_version = version("hindsight-client")
        except PackageNotFoundError as exc:
            raise HindsightCapabilityGateError("hindsight-client is not installed") from exc
    if report.get("client_version") != installed_client_version:
        raise HindsightCapabilityGateError(
            "Hindsight capability report does not match the installed client version"
        )
    live_server_version = server_version.strip()
    if not live_server_version:
        raise HindsightCapabilityGateError("live Hindsight server version is required")
    if report.get("server_version") != live_server_version:
        raise HindsightCapabilityGateError(
            "Hindsight capability report does not match the live server version"
        )

    capabilities = report.get("capabilities")
    if not isinstance(capabilities, dict):
        raise HindsightCapabilityGateError(
            "Hindsight capability report is missing capability evidence"
        )
    failed: list[str] = []
    for capability in _REQUIRED_CAPABILITIES:
        evidence = capabilities.get(capability)
        if (
            not isinstance(evidence, dict)
            or evidence.get("status") != "passed"
            or not isinstance(evidence.get("evidence"), str)
            or not evidence["evidence"].strip()
        ):
            failed.append(capability)
    if failed:
        raise HindsightCapabilityGateError(
            f"Hindsight lifecycle capabilities are not proven: {', '.join(failed)}"
        )
