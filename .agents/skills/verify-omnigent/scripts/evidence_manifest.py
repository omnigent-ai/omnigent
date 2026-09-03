#!/usr/bin/env python3
"""Create and finalize portable verification evidence manifests."""

from __future__ import annotations

import argparse
import binascii
import configparser
import hashlib
import json
import mimetypes
import os
import re
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_support import (
    cleanup_strict_environment,
    compare_snapshots,
    portable_argv,
    repository_snapshot,
)

SCHEMA_VERSION = 1
ADAPTER_ENV = "OMNIGENT_VERIFY_ADAPTER"
UI_PROFILES = {
    "smoke",
    "collaboration",
    "automations",
    "core",
    "full-ui",
    "web-ui",
    "desktop",
}
ORCHESTRATION_PROFILES = {"auto", "all-surfaces"}
PYTEST_PROFILES = {*UI_PROFILES, "server", "harness-client", "db-migration-deploy"}
SUPPORTED_BENCHMARK_SCHEMA_VERSIONS = {6}
_TRACE_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|token|password|secret)\b"
        r"\s*[:=]\s*)[A-Za-z0-9._~+/=-]+"
    ),
)
_TRACE_SECRET_LITERAL = re.compile(
    r"(?i)\b[A-Za-z0-9._~+/=-]*(?:secret|password|token)"
    r"[A-Za-z0-9._~+/=-]*\b"
)
_SAFE_ADAPTER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
FINAL_STATUSES = {"passed", "failed", "interrupted", "blocked"}
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_DEPTH = 4
MAX_DECODE_BYTES = 64 * 1024 * 1024
PRIVACY_FAILURE_MARKER = ".privacy-cleanup-failed.json"
REQUIRED_PROFILE_TESTS: dict[str, frozenset[str]] = {
    "collaboration": frozenset(
        {
            "tests/e2e_ui/collaboration/test_permissions_modal.py",
            "tests/e2e_ui/collaboration/test_sharing_journey.py",
            "tests/e2e_ui/collaboration/test_sharing_mode_off.py",
            "tests/e2e_ui/collaboration/test_collab_realtime.py",
            "tests/e2e_ui/collaboration/test_author_label.py",
        }
    )
}


class _RunRecord(TypedDict):
    id: str
    profile: str
    doctor_profile: str
    phase: str
    command_argv: list[str]
    selected_tests: list[str]


class _Timestamps(TypedDict):
    started_utc: str
    finished_utc: str | None
    duration_seconds: float | None


class _Manifest(TypedDict, total=False):
    schema_version: int
    status: str
    run: _RunRecord
    adapter: dict[str, object]
    repository: dict[str, object]
    timestamps: _Timestamps
    tests: list[dict[str, object]]
    artifacts: list[dict[str, object]]
    orchestration: object
    children: list[dict[str, object]]
    cleanup: dict[str, object]
    downstream_universe: dict[str, object]
    exit_status: int | None
    signal: str | None
    limitations: list[str]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _run_git(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _repository_state(limitations: list[str]) -> dict[str, object]:
    commit_result = _run_git("rev-parse", "--verify", "HEAD")
    status_result = _run_git("status", "--porcelain=v1", "--untracked-files=all")
    if (
        commit_result is None
        or status_result is None
        or commit_result.returncode != 0
        or status_result.returncode != 0
    ):
        limitations.append("Repository commit or dirty state could not be read.")
        return {"commit": None, "dirty": None, "status_sha256": None}

    status = status_result.stdout.encode("utf-8")
    return {
        "commit": commit_result.stdout.strip(),
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status).hexdigest(),
    }


def _adapter_identity(limitations: list[str]) -> dict[str, object]:
    raw = os.environ.get(ADAPTER_ENV, "").strip().lower()
    if not raw:
        limitations.append(
            f"Adapter identity was not reported through {ADAPTER_ENV}; it is unspecified."
        )
        identity = "unspecified"
    elif not _SAFE_ADAPTER.fullmatch(raw):
        limitations.append(
            f"Adapter identity from {ADAPTER_ENV} was invalid and was not recorded verbatim."
        )
        identity = "invalid"
    else:
        identity = raw
    return {
        "identity": identity,
        "source": ADAPTER_ENV,
        "self_reported": True,
    }


def _read_manifest(run_dir: Path) -> _Manifest:
    path = run_dir / "manifest.json"
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported manifest at {path}")
    if (
        not isinstance(value.get("run"), dict)
        or not isinstance(value.get("limitations"), list)
        or not isinstance(value.get("children"), list)
        or not isinstance(value.get("tests"), list)
        or not isinstance(value.get("artifacts"), list)
        or not isinstance(value.get("cleanup"), dict)
    ):
        raise ValueError(f"manifest at {path} has an invalid structure")
    return cast("_Manifest", value)


def _repository_snapshot_path(run_dir: Path) -> Path:
    configured = os.environ.get("OMNIGENT_VERIFY_CONTROL_SNAPSHOT")
    return Path(configured) if configured else run_dir / "repository-before.json"


def initialize(run_dir: Path, profile: str, doctor_profile: str) -> None:
    limitations: list[str] = [
        "Process cleanup uses ancestry plus an inherited private descriptor, not a hard "
        "OS sandbox; a hostile child that closes all inherited descriptors before a "
        "sub-observation escape may evade cleanup."
    ]
    repository_root = Path(os.environ.get("OMNIGENT_VERIFY_REPO_ROOT", Path.cwd()))
    try:
        snapshot = repository_snapshot(
            repository_root,
            deep_dependencies=os.environ.get("OMNIGENT_VERIFY_DEEP_DEPENDENCY_SNAPSHOT") == "1",
        )
        _atomic_write_json(_repository_snapshot_path(run_dir), snapshot)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        limitations.append(
            f"Initial repository snapshot failed ({type(exc).__name__}); verification is blocked."
        )
    if profile == "harness-client":
        limitations.append(
            "The harness/client profile checks unit behavior and the offline capability matrix; "
            "it does not claim a live harness turn. Run harness-live with "
            "OMNIGENT_VERIFY_HARNESS set for credentialed proof."
        )
    elif profile == "cli":
        limitations.append(
            "Credentialed CLI REPL verification was not requested because it requires inherited "
            "user credentials and relaxes HOME isolation."
        )
    elif profile == "desktop":
        limitations.append(
            "Electron signing and notarization were not requested; the profile builds an "
            "unsigned current-platform package."
        )
    manifest: _Manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "running",
        "run": {
            "id": run_dir.name,
            "profile": profile,
            "doctor_profile": doctor_profile,
            "phase": "initializing",
            "command_argv": [],
            "selected_tests": [],
        },
        "adapter": _adapter_identity(limitations),
        "repository": _repository_state(limitations),
        "timestamps": {
            "started_utc": _utc_now(),
            "finished_utc": None,
            "duration_seconds": None,
        },
        "tests": [],
        "artifacts": [],
        "orchestration": None,
        "children": [],
        "cleanup": {"status": "pending", "details": []},
        "downstream_universe": {"status": "not_requested"},
        "exit_status": None,
        "signal": None,
        "limitations": limitations,
    }
    _atomic_write_json(run_dir / "manifest.json", manifest)


def record_plan(run_dir: Path, plan_path: Path) -> None:
    manifest = _read_manifest(run_dir)
    with plan_path.open(encoding="utf-8") as handle:
        plan = json.load(handle)
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError(f"unsupported orchestration plan at {plan_path}")
    manifest["orchestration"] = plan
    for optional in plan.get("optional_lanes", []):
        if not isinstance(optional, dict) or optional.get("status") != "not_requested":
            continue
        manifest["limitations"].append(
            f"Optional lane {optional.get('lane', '<unknown>')} was not requested. "
            f"Opt in: {optional.get('opt_in', '<unspecified>')}"
        )
    _atomic_write_json(run_dir / "manifest.json", manifest)


def add_child(
    run_dir: Path,
    lane: str,
    child_manifest: Path,
    exit_status: int,
) -> None:
    manifest = _read_manifest(run_dir)
    existing = next(
        (
            item
            for item in manifest["children"]
            if isinstance(item, dict) and item.get("lane") == lane
        ),
        {},
    )
    child: dict[str, object] = {
        "lane": lane,
        "required": True,
        "manifest": None,
        "manifest_sha256": None,
        "status": "unavailable",
        "exit_status": exit_status,
    }
    if isinstance(existing, dict) and "baseline_comparison" in existing:
        child["baseline_comparison"] = existing["baseline_comparison"]
    try:
        resolved_run = run_dir.resolve(strict=True)
        resolved_child = child_manifest.resolve(strict=True)
        resolved_child.relative_to(resolved_run)
        raw = child_manifest.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported child manifest")
        child.update(
            {
                "manifest": resolved_child.relative_to(resolved_run).as_posix(),
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "status": value.get("status", "unavailable"),
                "exit_status": value.get("exit_status", exit_status),
                "profile": value.get("run", {}).get("profile"),
            }
        )
        if lane == "universe":
            downstream = value.get("downstream_universe")
            if isinstance(downstream, dict):
                manifest["downstream_universe"] = downstream
            else:
                manifest["limitations"].append(
                    "The Universe child manifest did not record downstream status."
                )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        manifest["limitations"].append(
            f"Required lane {lane} produced no valid child manifest ({type(exc).__name__})."
        )
    manifest["children"] = [
        item
        for item in manifest["children"]
        if isinstance(item, dict) and item.get("lane") != lane
    ]
    manifest["children"].append(child)
    _atomic_write_json(run_dir / "manifest.json", manifest)


def register_children(run_dir: Path, lanes: list[str]) -> None:
    manifest = _read_manifest(run_dir)
    existing = {
        item.get("lane"): item
        for item in manifest["children"]
        if isinstance(item, dict) and isinstance(item.get("lane"), str)
    }
    manifest["children"] = [
        existing.get(
            lane,
            {
                "lane": lane,
                "required": True,
                "manifest": None,
                "manifest_sha256": None,
                "status": "pending",
                "exit_status": None,
            },
        )
        for lane in lanes
    ]
    _atomic_write_json(run_dir / "manifest.json", manifest)


def mark_child(run_dir: Path, lane: str, status: str, reason: str | None = None) -> None:
    manifest = _read_manifest(run_dir)
    found = False
    for child in manifest["children"]:
        if not isinstance(child, dict) or child.get("lane") != lane:
            continue
        child["status"] = status
        if reason:
            child["reason"] = reason
        found = True
        break
    if not found:
        raise ValueError(f"lane {lane!r} was not registered")
    _atomic_write_json(run_dir / "manifest.json", manifest)


def record_baseline(run_dir: Path, lane: str, comparison_path: Path) -> None:
    manifest = _read_manifest(run_dir)
    resolved_run = run_dir.resolve(strict=True)
    resolved_comparison = comparison_path.resolve(strict=True)
    resolved_comparison.relative_to(resolved_run)
    raw = comparison_path.read_bytes()
    comparison = json.loads(raw)
    if not isinstance(comparison, dict) or comparison.get("schema_version") != 1:
        raise ValueError("unsupported baseline comparison")
    payload = {
        "classification": comparison.get("classification", "could_not_compare"),
        "comparison": resolved_comparison.relative_to(resolved_run).as_posix(),
        "comparison_sha256": hashlib.sha256(raw).hexdigest(),
        "base_manifest": comparison.get("base_manifest"),
        "base_manifest_sha256": comparison.get("base_manifest_sha256"),
        "cleanup": comparison.get("cleanup"),
    }
    for child in manifest["children"]:
        if isinstance(child, dict) and child.get("lane") == lane:
            child["baseline_comparison"] = payload
            _atomic_write_json(run_dir / "manifest.json", manifest)
            return
    raise ValueError(f"lane {lane!r} was not registered")


def prepare(
    run_dir: Path,
    phase: str,
    command_argv: list[str],
    selected_tests: list[str],
) -> None:
    manifest = _read_manifest(run_dir)
    required_tests = REQUIRED_PROFILE_TESTS.get(manifest["run"]["profile"], frozenset())
    missing_tests = sorted(required_tests.difference(selected_tests))
    if missing_tests:
        raise ValueError(
            "selected tests omit required profile coverage: " + ", ".join(missing_tests)
        )
    manifest["run"]["phase"] = phase
    manifest["run"]["command_argv"] = portable_argv(
        command_argv,
        repo_root=Path(os.environ.get("OMNIGENT_VERIFY_REPO_ROOT", Path.cwd())),
        run_dir=run_dir,
        skill_root=Path(os.environ["OMNIGENT_VERIFY_SKILL_ROOT"])
        if os.environ.get("OMNIGENT_VERIFY_SKILL_ROOT")
        else None,
    )
    manifest["run"]["selected_tests"] = selected_tests
    _atomic_write_json(run_dir / "manifest.json", manifest)


def _parse_junit(path: Path, limitations: list[str]) -> list[dict[str, object]]:
    if not path.is_file():
        limitations.append("No JUnit report was produced, so per-test results are unavailable.")
        return []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        limitations.append(f"The JUnit report could not be parsed ({type(exc).__name__}).")
        return []

    results: list[dict[str, object]] = []
    for case in root.iter("testcase"):
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "<unnamed>")
        nodeid = f"{classname}::{name}" if classname else name
        if case.find("error") is not None:
            status = "error"
        elif case.find("failure") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        try:
            duration = float(case.attrib.get("time", "0"))
        except ValueError:
            duration = None
        results.append(
            {
                "nodeid": nodeid,
                "status": status,
                "duration_seconds": duration,
            }
        )
    return results


def _mime_type(path: Path) -> str:
    overrides = {
        ".json": "application/json",
        ".log": "text/plain",
        ".png": "image/png",
        ".webm": "video/webm",
        ".xml": "application/xml",
        ".zip": "application/zip",
    }
    return (
        overrides.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or ("application/octet-stream")
    )


def _authority(relative_path: Path) -> str:
    parts = relative_path.parts
    if parts and parts[0] == "playwright":
        return "playwright-test-bound"
    if parts and parts[0] == "supplemental":
        return "supplemental-browser-tool"
    if relative_path.name == "junit.xml":
        return "pytest-report"
    if relative_path.name == "run.log":
        return "verification-runner"
    if relative_path.name == "cleanup.json":
        return "pytest-cleanup-marker"
    if relative_path.name == "benchmark.json":
        return "omnigent-benchmark"
    if relative_path.name == "baseline-benchmark.json":
        return "omnigent-baseline-benchmark"
    if relative_path.name == "performance-comparison.json":
        return "omnigent-benchmark-comparison"
    if parts and parts[0] == "baseline" and relative_path.name == "comparison.json":
        return "verification-baseline-comparison"
    if parts and parts[0] == "baseline" and relative_path.suffix == ".log":
        return "verification-baseline-runner"
    if relative_path.name == "steps.json":
        return "verification-step-report"
    if relative_path.name == "lane-plan.json":
        return "verification-lane-plan"
    if relative_path.name == "repository-before.json":
        return "verification-repository-snapshot"
    if relative_path.name in {"harness-matrix.json", "harness-live.json"}:
        return "harness-bench-report"
    if relative_path.name == "desktop-build.json":
        return "electron-build-report"
    if relative_path.name == "universe.json":
        return "universe-compatibility-report"
    if parts and parts[0] == "universe-bazel":
        return "universe-bazel-output"
    if parts and parts[0] == "cli":
        return "cli-pty-evidence"
    return "unclassified"


def _artifact_inventory(
    run_dir: Path,
    limitations: list[str],
    excluded: set[Path] | None = None,
) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    secrets_to_find = _known_secret_values()
    for path in sorted(run_dir.rglob("*")):
        try:
            relative_parts = path.relative_to(run_dir).parts
            if relative_parts[:1] == ("children",):
                continue
            if relative_parts[:1] == ("baseline",) and "evidence" in relative_parts:
                continue
        except ValueError:
            continue
        if path == run_dir / "manifest.json" or path.name.startswith(".manifest.json."):
            continue
        if path.name == "comparison-capability":
            continue
        if path.name == PRIVACY_FAILURE_MARKER:
            continue
        if excluded and path.absolute() in excluded:
            limitations.append("A suspect artifact was excluded from the inventory.")
            continue
        reference = _safe_artifact_reference(path, run_dir, secrets_to_find)
        if reference.startswith("<redacted-artifact-"):
            limitations.append(
                f"Suspect artifact path {reference!r} was excluded from the inventory."
            )
            continue
        if path.is_symlink():
            limitations.append(f"Symlink artifact {reference!r} was excluded from the inventory.")
            continue
        if not path.is_file():
            continue
        try:
            relative_path = path.relative_to(run_dir)
            resolved = path.resolve(strict=True)
            resolved.relative_to(run_dir.resolve(strict=True))
        except (OSError, ValueError):
            limitations.append("An artifact outside the evidence directory was excluded.")
            continue
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            limitations.append(f"Artifact {reference!r} could not be read and was excluded.")
            continue
        artifacts.append(
            {
                "path": relative_path.as_posix(),
                "mime": _mime_type(path),
                "sha256": digest.hexdigest(),
                "bytes": size,
                "authority": _authority(relative_path),
            }
        )
    return artifacts


def _known_secret_values() -> tuple[bytes, ...]:
    values = [
        os.environ[key]
        for key in ("OPENAI_API_KEY", "DATABRICKS_TOKEN", "CURSOR_API_KEY")
        if os.environ.get(key)
    ]
    selected = os.environ.get("OMNIGENT_VERIFY_DATABRICKS_PROFILE")
    config_path = os.environ.get("DATABRICKS_CONFIG_FILE")
    if not config_path and os.environ.get("HOME"):
        config_path = str(Path(os.environ["HOME"]) / ".databrickscfg")
    if selected and config_path:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(config_path, encoding="utf-8")
            values.extend(
                value
                for key, value in parser.items(selected)
                if any(word in key.lower() for word in ("token", "password", "secret")) and value
            )
        except (configparser.Error, OSError):
            pass
    return tuple(dict.fromkeys(value.encode() for value in values if value))


def _stream_contains_secret(
    handle: BinaryIO,
    secrets_to_find: tuple[bytes, ...],
    limit: int,
) -> bool:
    overlap = max((len(value) for value in secrets_to_find), default=1) - 1
    previous = b""
    consumed = 0
    while chunk := handle.read(min(1024 * 1024, limit - consumed + 1)):
        consumed += len(chunk)
        if consumed > limit:
            raise ValueError("artifact exceeds privacy scan limits")
        payload = previous + chunk
        if any(secret in payload for secret in secrets_to_find):
            return True
        previous = payload[-overlap:] if overlap else b""
    return False


def _archive_contains_secret(
    archive: zipfile.ZipFile,
    secrets_to_find: tuple[bytes, ...],
    budget: dict[str, int],
    *,
    depth: int,
) -> bool:
    if depth > MAX_ARCHIVE_DEPTH:
        raise ValueError("archive nesting exceeds privacy scan limit")
    members = archive.infolist()
    budget["members"] += len(members)
    if budget["members"] > MAX_ARCHIVE_MEMBERS:
        raise ValueError("archive member count exceeds privacy scan limit")
    for info in members:
        member = Path(info.filename)
        if (
            member.is_absolute()
            or ".." in member.parts
            or info.flag_bits & 0x1
            or info.file_size < 0
        ):
            raise ValueError("archive contains an unsafe member")
        budget["expanded"] += info.file_size
        if budget["expanded"] > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError("archive expanded bytes exceed privacy scan limit")
        if info.is_dir():
            continue
        with (
            archive.open(info) as source,
            tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as staged,
        ):
            matched = False
            overlap = max((len(value) for value in secrets_to_find), default=1) - 1
            previous = b""
            consumed = 0
            while chunk := source.read(1024 * 1024):
                consumed += len(chunk)
                if consumed > info.file_size:
                    raise ValueError("archive member exceeded declared size")
                payload = previous + chunk
                if any(secret in payload for secret in secrets_to_find):
                    matched = True
                previous = payload[-overlap:] if overlap else b""
                staged.write(chunk)
            if consumed != info.file_size:
                raise ValueError("archive member size did not match declaration")
            if matched:
                return True
            staged.seek(0)
            if zipfile.is_zipfile(staged):
                staged.seek(0)
                with zipfile.ZipFile(staged) as nested:
                    if _archive_contains_secret(
                        nested,
                        secrets_to_find,
                        budget,
                        depth=depth + 1,
                    ):
                        return True
    return False


def _artifact_contains_secret(path: Path, secrets_to_find: tuple[bytes, ...]) -> bool:
    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ValueError("top-level artifact exceeds privacy scan limit")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return _archive_contains_secret(
                archive,
                secrets_to_find,
                {"members": 0, "expanded": 0},
                depth=1,
            )
    with path.open("rb") as handle:
        return _stream_contains_secret(handle, secrets_to_find, MAX_ARTIFACT_BYTES)


def _redact_json_secrets(
    value: object,
    known: tuple[str, ...],
    *,
    sensitive: bool = False,
) -> object:
    if sensitive:
        return "[redacted-sensitive-value]"
    if isinstance(value, dict):
        return {
            key: _redact_json_secrets(
                item,
                known,
                sensitive=any(
                    word in str(key).lower()
                    for word in ("token", "password", "secret", "api_key", "authorization")
                ),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_secrets(item, known) for item in value]
    if isinstance(value, str):
        for secret in known:
            value = value.replace(secret, "[redacted-known-secret]")
        for pattern in _TRACE_SECRET_PATTERNS:
            value = pattern.sub(r"\1[redacted]", value)
        return _TRACE_SECRET_LITERAL.sub("[redacted-secret-literal]", value)
    return value


def _redact_trace_text(text: str, known: tuple[str, ...]) -> str:
    lines = text.splitlines()
    if lines:
        try:
            parsed = [json.loads(line) for line in lines if line.strip()]
        except json.JSONDecodeError:
            parsed = []
        if parsed and len(parsed) == len([line for line in lines if line.strip()]):
            return "\n".join(
                json.dumps(_redact_json_secrets(item, known), separators=(",", ":"))
                for item in parsed
            ) + ("\n" if text.endswith("\n") else "")
    try:
        parsed_document = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        return json.dumps(
            _redact_json_secrets(parsed_document, known),
            separators=(",", ":"),
        )
    for secret in known:
        text = text.replace(secret, "[redacted-known-secret]")
    for pattern in _TRACE_SECRET_PATTERNS:
        text = pattern.sub(r"\1[redacted]", text)
    return _TRACE_SECRET_LITERAL.sub("[redacted-secret-literal]", text)


def _valid_png(path: Path) -> bool:
    """Decode enough of an 8-bit Playwright PNG to prove nonempty image data."""
    try:
        if path.stat().st_size > MAX_DECODE_BYTES:
            return False
        payload = path.read_bytes()
        if payload[:8] != b"\x89PNG\r\n\x1a\n":
            return False
        cursor = 8
        ihdr: tuple[int, int, int, int, int] | None = None
        compressed = bytearray()
        ended = False
        while cursor + 12 <= len(payload):
            length = struct.unpack(">I", payload[cursor : cursor + 4])[0]
            kind = payload[cursor + 4 : cursor + 8]
            end = cursor + 12 + length
            if end > len(payload):
                return False
            data = payload[cursor + 8 : cursor + 8 + length]
            expected_crc = struct.unpack(">I", payload[cursor + 8 + length : end])[0]
            if binascii.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
                return False
            if kind == b"IHDR":
                if ihdr is not None or length != 13:
                    return False
                width, height, depth, color, compression, filtering, interlace = struct.unpack(
                    ">IIBBBBB", data
                )
                if (
                    width <= 0
                    or height <= 0
                    or depth != 8
                    or color not in {0, 2, 4, 6}
                    or compression != 0
                    or filtering != 0
                    or interlace != 0
                ):
                    return False
                ihdr = (width, height, depth, color, interlace)
            elif kind == b"IDAT":
                compressed.extend(data)
            elif kind == b"IEND":
                ended = length == 0
                break
            cursor = end
        if ihdr is None or not compressed or not ended:
            return False
        width, height, _depth, color, _interlace = ihdr
        channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
        row_size = 1 + width * channels
        expected = row_size * height
        if expected > MAX_DECODE_BYTES:
            return False
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(bytes(compressed), expected + 1)
        decoded += decoder.flush()
        return (
            len(decoded) == expected
            and decoder.eof
            and all(decoded[offset] <= 4 for offset in range(0, expected, row_size))
        )
    except (OSError, ValueError, struct.error, zlib.error):
        return False


def _valid_playwright_trace(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            return False
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if (
                not members
                or len(members) > MAX_ARCHIVE_MEMBERS
                or sum(info.file_size for info in members) > MAX_ARCHIVE_EXPANDED_BYTES
            ):
                return False
            trace = next(
                (
                    info
                    for info in members
                    if Path(info.filename).name == "trace.trace"
                    and 0 < info.file_size <= MAX_DECODE_BYTES
                ),
                None,
            )
            if trace is None:
                return False
            with archive.open(trace) as handle:
                payload = handle.read(trace.file_size + 1)
            if len(payload) != trace.file_size:
                return False
            lines = [line for line in payload.decode("utf-8").splitlines() if line.strip()]
            return bool(lines) and all(isinstance(json.loads(line), dict) for line in lines)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile):
        return False


def _safe_artifact_reference(
    path: Path,
    run_dir: Path,
    secrets_to_find: tuple[bytes, ...],
) -> str:
    raw = os.fsencode(path.relative_to(run_dir))
    if any(secret in raw for secret in secrets_to_find):
        return f"<redacted-artifact-{hashlib.sha256(raw).hexdigest()[:12]}>"
    return path.relative_to(run_dir).as_posix()


def _remove_artifact(path: Path, run_dir: Path) -> tuple[bool, str | None]:
    try:
        path.relative_to(run_dir)
        parent = path.parent.resolve(strict=True)
        parent.relative_to(run_dir.resolve(strict=True))
        parent_stat = parent.stat()
    except (OSError, ValueError):
        return False, "unsafe artifact location"
    original_mode = parent_stat.st_mode
    changed_mode = False
    restore_error = False
    try:
        try:
            path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
        except PermissionError:
            if parent_stat.st_uid != os.getuid():
                return False, "artifact parent is not owned by the verifier user"
            parent.chmod(original_mode | 0o700)
            changed_mode = True
            path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
    except OSError as exc:
        return False, f"artifact removal failed ({type(exc).__name__})"
    finally:
        if changed_mode and parent.exists():
            try:
                parent.chmod(original_mode)
            except OSError:
                restore_error = True
    if restore_error:
        return False, "artifact parent permissions could not be restored"
    if path.exists() or path.is_symlink():
        return False, "artifact removal could not be confirmed"
    return True, None


def _mark_privacy_cleanup_failure(run_dir: Path) -> None:
    _atomic_write_json(
        run_dir / PRIVACY_FAILURE_MARKER,
        {"schema_version": 1, "status": "failed"},
    )


def sanitize_trace_archives(
    run_dir: Path,
    suspect_paths: set[Path] | None = None,
) -> list[str]:
    """Redact text trace members; delete traces that cannot be transformed safely."""
    blockers: list[str] = []
    known = tuple(value.decode() for value in _known_secret_values())
    for path in sorted(run_dir.rglob("*.zip")):
        if "trace" not in path.name.lower() or path.is_symlink():
            continue
        temporary = path.with_name(f".{path.name}.privacy.tmp")
        try:
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("trace exceeds top-level artifact limit")
            with (
                zipfile.ZipFile(path, "r") as source,
                zipfile.ZipFile(temporary, "w") as destination,
            ):
                seen: set[str] = set()
                expanded = 0
                members = source.infolist()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise ValueError("trace member count exceeds limit")
                for info in members:
                    member = Path(info.filename)
                    if (
                        info.filename in seen
                        or member.is_absolute()
                        or ".." in member.parts
                        or info.flag_bits & 0x1
                    ):
                        raise ValueError("unsafe trace member")
                    seen.add(info.filename)
                    expanded += info.file_size
                    if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise ValueError("trace expanded bytes exceed limit")
                    if info.file_size > MAX_DECODE_BYTES:
                        raise ValueError("trace member exceeds decode limit")
                    with source.open(info) as member_source:
                        payload = member_source.read(info.file_size + 1)
                    if len(payload) != info.file_size:
                        raise ValueError("trace member size did not match declaration")
                    try:
                        text = payload.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        if any(value.encode() in payload for value in known):
                            raise ValueError("known secret in binary trace member") from exc
                        destination.writestr(info, payload)
                        continue
                    text = _redact_trace_text(text, known)
                    destination.writestr(info, text.encode())
            with zipfile.ZipFile(temporary, "r") as sanitized:
                if sanitized.testzip() is not None:
                    raise ValueError("invalid sanitized trace")
            os.replace(temporary, path)
        except (OSError, ValueError, zipfile.BadZipFile):
            _mark_privacy_cleanup_failure(run_dir)
            if suspect_paths is not None:
                suspect_paths.add(path.absolute())
            reference = _safe_artifact_reference(
                path,
                run_dir,
                _known_secret_values(),
            )
            removed, error = _remove_artifact(path, run_dir)
            outcome = "was deleted" if removed else f"removal was not confirmed ({error})"
            blockers.append(
                "PRIVACY CLEANUP FAILURE: "
                f"trace artifact {reference!r} could not be privacy-transformed; {outcome}."
            )
        finally:
            temporary.unlink(missing_ok=True)
    return blockers


def remove_known_secret_artifacts(
    run_dir: Path,
    suspect_paths: set[Path] | None = None,
) -> list[str]:
    """Delete artifacts containing explicitly known credentials, including ZIP members."""
    secrets_to_find = _known_secret_values()
    blockers: list[str] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        reference = _safe_artifact_reference(path, run_dir, secrets_to_find)
        path_match = reference.startswith("<redacted-artifact-")
        try:
            matched = path_match or _artifact_contains_secret(path, secrets_to_find)
        except (OSError, ValueError, zipfile.BadZipFile):
            matched = True
        if not matched:
            continue
        removed, error = _remove_artifact(path, run_dir)
        if suspect_paths is not None:
            suspect_paths.add(path.absolute())
        if removed:
            blockers.append(
                f"Artifact {reference!r} contained or could expose an explicitly known "
                "secret and was deleted."
            )
        else:
            _mark_privacy_cleanup_failure(run_dir)
            blockers.append(
                "PRIVACY CLEANUP FAILURE: "
                f"artifact {reference!r} contained or could expose an explicitly known "
                f"secret; removal was not confirmed ({error})."
            )
    for path in sorted(
        (item for item in run_dir.rglob("*") if item.is_dir() and not item.is_symlink()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        reference = _safe_artifact_reference(path, run_dir, secrets_to_find)
        if not reference.startswith("<redacted-artifact-"):
            continue
        removed, error = _remove_artifact(path, run_dir)
        if not removed:
            blockers.append(
                "PRIVACY CLEANUP FAILURE: "
                f"directory {reference!r} exposed a known secret in its path; "
                f"removal was not confirmed ({error})."
            )
    return blockers


def validate_required_outputs(run_dir: Path, profile: str) -> list[str]:
    """Return blockers when a successful UI run lacks test-bound proof."""
    suspect_paths: set[Path] = set()
    privacy_blockers = [
        *sanitize_trace_archives(run_dir, suspect_paths),
        *remove_known_secret_artifacts(run_dir, suspect_paths),
    ]
    if privacy_blockers:
        return privacy_blockers
    if profile == "perf":
        benchmark = run_dir / "benchmark.json"
        if not benchmark.is_file():
            return ["The performance run produced no benchmark.json."]
        try:
            value = json.loads(benchmark.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"benchmark.json is not valid JSON ({type(exc).__name__})."]
        perf_blockers: list[str] = []
        if (
            not isinstance(value, dict)
            or value.get("schema_version") not in SUPPORTED_BENCHMARK_SCHEMA_VERSIONS
        ):
            perf_blockers.append("benchmark.json has no supported schema version.")
            return perf_blockers
        if not value.get("git_sha"):
            perf_blockers.append("benchmark.json has no commit identity.")
        config = value.get("config")
        if not isinstance(config, dict) or not config.get("backend"):
            perf_blockers.append("benchmark.json has no backend/config identity.")
        configured_runs = config.get("runs") if isinstance(config, dict) else None
        if (
            not isinstance(configured_runs, int)
            or isinstance(configured_runs, bool)
            or configured_runs <= 0
        ):
            perf_blockers.append("benchmark.json has no valid configured run count.")
        journeys = value.get("journeys")
        if not isinstance(journeys, dict) or not journeys:
            perf_blockers.append("benchmark.json contains no journey results.")
            return perf_blockers
        for name, journey in journeys.items():
            if not isinstance(journey, dict):
                perf_blockers.append(f"Benchmark journey {name} is invalid.")
                continue
            summary = journey.get("summary")
            if not isinstance(summary, dict):
                perf_blockers.append(f"Benchmark journey {name} has no summary.")
                continue
            required = {
                "avg_p50_ms",
                "avg_p99_ms",
                "avg_http_requests_per_op",
                "network_routes",
                "runs_ok",
                "runs_total",
            }
            missing = sorted(required - summary.keys())
            if missing:
                perf_blockers.append(
                    f"Benchmark journey {name} lacks required evidence: {', '.join(missing)}."
                )
                continue
            runs_ok = summary.get("runs_ok")
            runs_total = summary.get("runs_total")
            if (
                not isinstance(runs_ok, int)
                or isinstance(runs_ok, bool)
                or not isinstance(runs_total, int)
                or isinstance(runs_total, bool)
                or runs_total <= 0
                or runs_ok != runs_total
                or runs_total != configured_runs
            ):
                perf_blockers.append(
                    f"Benchmark journey {name} did not complete every run "
                    f"(runs_ok={runs_ok!r}, runs_total={runs_total!r})."
                )
            if journey.get("skipped") or journey.get("skipped_reason"):
                perf_blockers.append(f"Benchmark journey {name} was skipped.")
            runs = journey.get("runs")
            if not isinstance(runs, list) or not runs:
                perf_blockers.append(f"Benchmark journey {name} contains no run evidence.")
                continue
            successful_runs = sum(
                1 for run in runs if isinstance(run, dict) and run.get("n_failures") == 0
            )
            if (
                len(runs) != runs_total
                or successful_runs != runs_ok
                or len(runs) != configured_runs
            ):
                perf_blockers.append(
                    f"Benchmark journey {name} run evidence contradicts configured "
                    "and summarized counts."
                )
            for index, run in enumerate(runs):
                failures = run.get("n_failures") if isinstance(run, dict) else None
                if not isinstance(failures, int) or isinstance(failures, bool) or failures != 0:
                    perf_blockers.append(
                        f"Benchmark journey {name} run {index} has "
                        f"n_failures={failures!r}; functional success is required."
                    )
        return perf_blockers
    if profile not in UI_PROFILES:
        return []
    files = [
        path
        for path in (run_dir / "playwright").rglob("*")
        if path.is_file() and not path.is_symlink()
    ]
    blockers: list[str] = []
    junit = run_dir / "junit.xml"
    executed_nodeids: dict[str, int] = {}
    if not junit.is_file():
        blockers.append("The UI run produced no JUnit report.")
    else:
        try:
            root = ET.parse(junit).getroot()
            for case in root.iter("testcase"):
                if case.find("skipped") is None:
                    properties = case.find("properties")
                    identity_values = (
                        [
                            item.attrib.get("value")
                            for item in properties.findall("property")
                            if item.attrib.get("name") == "omnigent_nodeid_sha256"
                        ]
                        if properties is not None
                        else []
                    )
                    context_values = (
                        [
                            item.attrib.get("value")
                            for item in properties.findall("property")
                            if item.attrib.get("name") == "omnigent_browser_context_count"
                        ]
                        if properties is not None
                        else []
                    )
                    if (
                        len(identity_values) != 1
                        or not isinstance(identity_values[0], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", identity_values[0])
                        or identity_values[0] in executed_nodeids
                        or len(context_values) != 1
                        or not isinstance(context_values[0], str)
                        or not context_values[0].isdigit()
                    ):
                        blockers.append(
                            "An executed UI case lacks one unique canonical node-id identity."
                        )
                        continue
                    executed_nodeids[identity_values[0]] = int(context_values[0])
        except (OSError, ET.ParseError):
            blockers.append("The UI JUnit report is unreadable.")
    if not executed_nodeids:
        blockers.append("The UI run contains no executed Pytest test.")
    expected_browser_tests = {
        identity for identity, count in executed_nodeids.items() if count > 0
    }
    metadata_paths = [path for path in files if path.name == "metadata.json"]
    if expected_browser_tests and not metadata_paths:
        blockers.append("The UI run produced no browser context metadata.")
    if expected_browser_tests and not any(path.suffix.lower() == ".png" for path in files):
        blockers.append("The UI run produced no test-bound screenshot.")
    if expected_browser_tests and not any(path.suffix.lower() == ".zip" for path in files):
        blockers.append("The UI run produced no test-bound trace.")
    valid_contexts = 0
    covered_tests: set[str] = set()
    for metadata_path in metadata_paths:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            nodeid = metadata["nodeid"]
            nodeid_sha256 = metadata["nodeid_sha256"]
            if (
                metadata.get("schema_version") != 1
                or metadata.get("lifecycle") != "closed"
                or metadata.get("context_style")
                not in {"managed-sync", "direct-sync", "direct-async"}
                or not isinstance(nodeid, str)
                or not isinstance(nodeid_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", nodeid_sha256)
                or nodeid_sha256 not in executed_nodeids
                or metadata_path.parent.parent.name != f"test-{nodeid_sha256[:20]}"
                or not isinstance(metadata.get("events"), list)
            ):
                raise ValueError("invalid metadata identity")
            context_files = [
                path
                for path in metadata_path.parent.rglob("*")
                if path.is_file() and not path.is_symlink()
            ]
            screenshots = [path for path in context_files if path.suffix.lower() == ".png"]
            traces = [path for path in context_files if path.suffix.lower() == ".zip"]
            if not screenshots or not all(_valid_png(path) for path in screenshots):
                raise ValueError("context has no screenshot")
            if not traces or not all(_valid_playwright_trace(path) for path in traces):
                raise ValueError("context has no trace")
            covered_tests.add(nodeid_sha256)
            valid_contexts += 1
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            blockers.append(
                f"Browser context metadata is invalid or uncorrelated: "
                f"{metadata_path.relative_to(run_dir)}."
            )
    if expected_browser_tests and valid_contexts == 0:
        blockers.append("The UI run produced no authenticated complete browser context.")
    if expected_browser_tests - covered_tests:
        blockers.append("One or more executed UI tests have no correlated browser context.")
    unexpected_browser_tests = covered_tests - expected_browser_tests
    if unexpected_browser_tests:
        blockers.append("Browser evidence is attributed to a browserless UI test.")
    for identity, expected_count in executed_nodeids.items():
        if expected_count == 0:
            continue
        actual_count = sum(
            1
            for metadata_path in metadata_paths
            if metadata_path.parent.parent.name == f"test-{identity[:20]}"
        )
        if actual_count != expected_count:
            blockers.append("An executed UI test has a mismatched browser context evidence count.")
    return blockers


def _orchestration_cleanup(manifest: _Manifest) -> dict[str, object]:
    children = [item for item in manifest.get("children", []) if isinstance(item, dict)]
    attempted = [item for item in children if item.get("manifest")]
    details: list[str] = []
    unknown = False
    for child in attempted:
        child_manifest = child.get("_loaded_manifest")
        cleanup = child_manifest.get("cleanup") if isinstance(child_manifest, dict) else None
        status = cleanup.get("status") if isinstance(cleanup, dict) else "unknown"
        details.append(f"{child.get('lane')}: {status}")
        if status not in {"completed", "not_applicable"}:
            unknown = True
        baseline = child.get("baseline_comparison")
        baseline_cleanup = baseline.get("cleanup") if isinstance(baseline, dict) else None
        if isinstance(baseline_cleanup, dict):
            baseline_status = baseline_cleanup.get("status", "unknown")
            details.append(f"{child.get('lane')} baseline worktree: {baseline_status}")
            if baseline_status != "completed":
                unknown = True
    for child in children:
        child.pop("_loaded_manifest", None)
    if not attempted:
        return {"status": "not_applicable", "details": details}
    return {"status": "unknown" if unknown else "completed", "details": details}


def _cleanup_state(
    run_dir: Path,
    profile: str,
    signal_name: str | None,
    manifest: _Manifest,
) -> dict[str, object]:
    if profile in ORCHESTRATION_PROFILES:
        return _orchestration_cleanup(manifest)
    if profile in {
        "doctor",
        "backend",
        "quality-gates",
        "server",
        "db-migration-deploy",
        "perf",
        "cli",
        "harness-client",
        "harness-live",
        "universe",
    }:
        return {"status": "not_applicable", "details": []}
    marker = run_dir / "cleanup.json"
    if marker.is_file():
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and value.get("status") == "completed":
            return {"status": "completed", "details": ["pytest session cleanup completed"]}
    detail = (
        "verification was interrupted before cleanup was confirmed"
        if signal_name
        else ("pytest did not confirm session cleanup")
    )
    return {"status": "unknown", "details": [detail]}


def _append_artifact_limitations(
    manifest: _Manifest,
    artifacts: list[dict[str, object]],
) -> None:
    paths = [path for item in artifacts if isinstance(path := item.get("path"), str)]
    if "run.log" not in paths:
        manifest["limitations"].append("The expected verification runner log is missing.")
    if manifest["run"]["profile"] == "perf" and "benchmark.json" not in paths:
        manifest["limitations"].append("The performance profile produced no benchmark.json.")
    if (
        manifest["run"]["profile"]
        in {
            "quality-gates",
            "perf",
            "server",
            "db-migration-deploy",
            "cli",
            "web-ui",
            "desktop",
            "harness-client",
            "harness-live",
            "universe",
        }
        and "steps.json" not in paths
    ):
        manifest["limitations"].append("The expected per-step command report is missing.")
    unclassified = [
        path
        for item in artifacts
        if item.get("authority") == "unclassified" and isinstance(path := item.get("path"), str)
    ]
    if unclassified:
        manifest["limitations"].append(
            "Some artifacts have unclassified authority: " + ", ".join(unclassified)
        )
    if manifest["run"]["profile"] not in UI_PROFILES:
        return
    manifest["limitations"].extend(
        [
            "Managed synchronous-context videos are retained only for failed tests; "
            "successful managed tests rely on screenshots and traces.",
            "Trace text receives deterministic known-pattern redaction, but unnamed page "
            "secrets may evade heuristics; evidence is not generally sanitized.",
            "Known credential values are byte-scanned through ZIP members, screenshots, "
            "and videos. Image/video pixels are not OCR-scanned, so visibly rendered "
            "unknown secrets remain outside this proof.",
            "Network evidence is observed metadata only, not a complete HAR, and excludes "
            "headers and bodies.",
        ]
    )
    context_metadata = [path for path in paths if path.endswith("/metadata.json")]
    if not context_metadata:
        manifest["limitations"].append(
            "No browser context metadata was produced; selected tests may not have created a "
            "browser, or browser setup failed."
        )
        return
    if not any(path.endswith(".png") for path in paths):
        manifest["limitations"].append(
            "Browser contexts ran, but no screenshot file was produced."
        )
    if not any(path.endswith(".zip") for path in paths):
        manifest["limitations"].append("Browser contexts ran, but no trace file was produced.")
    if manifest["status"] != "passed" and not any(path.endswith(".webm") for path in paths):
        manifest["limitations"].append(
            "The verification failed, but no browser video file was produced."
        )


def _append_orchestration_limitations(manifest: _Manifest) -> None:
    orchestration = manifest.get("orchestration")
    if not isinstance(orchestration, dict):
        manifest["limitations"].append("The orchestration profile produced no lane decision.")
        return
    selected = orchestration.get("selected_lanes", [])
    child_by_lane = {
        child.get("lane"): child
        for child in manifest.get("children", [])
        if isinstance(child, dict)
    }
    for lane in selected:
        child = child_by_lane.get(lane)
        if child is None:
            manifest["limitations"].append(f"Required lane {lane} has no child evidence manifest.")
        elif child.get("status") != "passed":
            manifest["limitations"].append(
                f"Required lane {lane} did not pass; inspect {child.get('manifest') or 'run.log'}."
            )


def _reconcile_children(
    run_dir: Path,
    manifest: _Manifest,
    parent_status: str,
    parent_exit_status: int,
    signal_name: str | None,
) -> None:
    orchestration = manifest.get("orchestration")
    if not isinstance(orchestration, dict):
        return
    selected = orchestration.get("selected_lanes", [])
    if not isinstance(selected, list):
        return
    by_lane = {
        item.get("lane"): item
        for item in manifest.get("children", [])
        if isinstance(item, dict) and isinstance(item.get("lane"), str)
    }
    reconciled: list[dict[str, object]] = []
    for lane in selected:
        if not isinstance(lane, str):
            continue
        child = by_lane.get(
            lane,
            {
                "lane": lane,
                "required": True,
                "manifest": None,
                "manifest_sha256": None,
                "status": "pending",
                "exit_status": None,
            },
        )
        manifests = sorted((run_dir / "children" / lane).glob("*/manifest.json"))
        if len(manifests) == 1:
            child_path = manifests[0]
            try:
                expected_sha256 = child.get("manifest_sha256")
                reconciled_stale = False
                child_value = json.loads(child_path.read_text(encoding="utf-8"))
                if not isinstance(child_value, dict) or child_value.get("schema_version") != 1:
                    raise ValueError("unsupported child manifest")
                if child_value.get("status") == "running":
                    child_status = "interrupted" if parent_status == "interrupted" else "failed"
                    finalize(
                        child_path.parent,
                        child_status,
                        parent_exit_status or 1,
                        signal_name,
                    )
                    child_value = json.loads(child_path.read_text(encoding="utf-8"))
                    reconciled_stale = True
                    manifest["limitations"].append(
                        f"Parent reconciliation finalized stale child {lane} as {child_status}."
                    )
                raw = child_path.read_bytes()
                observed_sha256 = hashlib.sha256(raw).hexdigest()
                if expected_sha256 is None and parent_status == "passed":
                    raise ValueError("child manifest was never authenticated by the parent")
                if (
                    expected_sha256 is not None
                    and expected_sha256 != observed_sha256
                    and not reconciled_stale
                ):
                    raise ValueError("child manifest hash changed after registration")
                if (
                    child_value.get("run", {}).get("phase") != "finished"
                    or child_value.get("run", {}).get("profile") != lane
                ):
                    raise ValueError("child manifest identity or finalization is invalid")
                child.update(
                    {
                        "manifest": child_path.relative_to(run_dir).as_posix(),
                        "manifest_sha256": observed_sha256,
                        "status": child_value.get("status", "unavailable"),
                        "exit_status": child_value.get("exit_status"),
                        "profile": child_value.get("run", {}).get("profile"),
                        "_loaded_manifest": child_value,
                    }
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                child["status"] = "unavailable"
                child["reason"] = f"Child manifest could not be reconciled ({type(exc).__name__})."
        elif len(manifests) > 1:
            child["status"] = "unavailable"
            child["reason"] = "Multiple child manifests were produced for one required lane."
        elif not manifests:
            if parent_status == "interrupted":
                child["status"] = "interrupted"
                child["exit_status"] = parent_exit_status
                child["reason"] = "Parent orchestration was interrupted before child completion."
            elif parent_status == "blocked":
                child["status"] = "blocked"
                child["exit_status"] = parent_exit_status
                child["reason"] = "Prerequisite preflight blocked lane startup."
            elif parent_status == "passed":
                child["status"] = "unavailable"
                child["exit_status"] = None
                child["reason"] = "Required finalized child manifest is missing."
            else:
                child["status"] = "blocked"
                child["exit_status"] = None
                child["reason"] = "An earlier required lane failed before this lane started."
        reconciled.append(child)
    manifest["children"] = reconciled


def _required_child_errors(manifest: _Manifest) -> list[str]:
    orchestration = manifest.get("orchestration")
    if not isinstance(orchestration, dict):
        return ["orchestration decision is unavailable"]
    selected = orchestration.get("selected_lanes")
    children = manifest.get("children")
    if not isinstance(selected, list) or not isinstance(children, list):
        return ["required lane schema is invalid"]
    errors = []
    for lane in selected:
        matches = [
            child for child in children if isinstance(child, dict) and child.get("lane") == lane
        ]
        if len(matches) != 1:
            errors.append(f"required lane {lane!r} has {len(matches)} child records")
            continue
        child = matches[0]
        if (
            child.get("status") != "passed"
            or child.get("exit_status") != 0
            or not child.get("manifest")
            or not child.get("manifest_sha256")
        ):
            errors.append(f"required lane {lane!r} lacks successful authenticated evidence")
        baseline = child.get("baseline_comparison")
        if isinstance(baseline, dict) and baseline.get("cleanup", {}).get("status") != "completed":
            errors.append(f"required lane {lane!r} baseline cleanup is incomplete")
    return errors


def finalize(
    run_dir: Path,
    status: str,
    exit_status: int,
    signal_name: str | None,
) -> None:
    manifest = _read_manifest(run_dir)
    state_changes: list[str]
    try:
        before = json.loads(_repository_snapshot_path(run_dir).read_text(encoding="utf-8"))
        repository_root = Path(os.environ.get("OMNIGENT_VERIFY_REPO_ROOT", Path.cwd()))
        state_changes = compare_snapshots(
            before,
            repository_snapshot(
                repository_root,
                deep_dependencies=(
                    os.environ.get("OMNIGENT_VERIFY_DEEP_DEPENDENCY_SNAPSHOT") == "1"
                ),
            ),
        )
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        state_changes = [f"repository snapshot failed ({type(exc).__name__})"]
    if state_changes:
        status = "blocked" if manifest["run"]["profile"] == "doctor" else "failed"
        exit_status = exit_status or 1
        manifest["limitations"].append(
            "Repository mutation invariant failed: " + "; ".join(state_changes)
        )
    manifest["status"] = status
    manifest["exit_status"] = exit_status
    manifest["signal"] = signal_name
    manifest["run"]["phase"] = "finished"
    manifest["timestamps"]["finished_utc"] = _utc_now()
    try:
        started_value = manifest["timestamps"]["started_utc"]
        finished_value = manifest["timestamps"]["finished_utc"]
        if finished_value is None:
            raise ValueError("finished timestamp is missing")
        started = datetime.fromisoformat(started_value.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(finished_value.replace("Z", "+00:00"))
        manifest["timestamps"]["duration_seconds"] = round((finished - started).total_seconds(), 3)
    except (TypeError, ValueError):
        manifest["limitations"].append("Verification duration could not be calculated.")

    if manifest["run"]["profile"] in ORCHESTRATION_PROFILES:
        _reconcile_children(run_dir, manifest, status, exit_status, signal_name)
        child_errors = _required_child_errors(manifest)
        if child_errors and status == "passed":
            status = "failed"
            exit_status = exit_status or 1
            manifest["status"] = status
            manifest["exit_status"] = exit_status
            manifest["limitations"].extend(child_errors)
    if manifest["run"]["profile"] == "universe":
        report_path = run_dir / "universe.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or report.get("schema_version") != 1:
                raise ValueError("unsupported Universe report")
            manifest["downstream_universe"] = report
            report_limitations = report.get("limitations", [])
            if isinstance(report_limitations, list):
                manifest["limitations"].extend(
                    item for item in report_limitations if isinstance(item, str)
                )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest["downstream_universe"] = {"status": "unavailable"}
            manifest["limitations"].append(
                f"The Universe report could not be loaded ({type(exc).__name__})."
            )
    if (run_dir / "junit.xml").is_file() or manifest["run"]["profile"] in UI_PROFILES:
        manifest["tests"] = _parse_junit(run_dir / "junit.xml", manifest["limitations"])
    if (
        manifest["run"]["profile"] in PYTEST_PROFILES
        and status == "passed"
        and not any(test.get("status") != "skipped" for test in manifest["tests"])
    ):
        status = "failed"
        exit_status = exit_status or 1
        manifest["status"] = status
        manifest["exit_status"] = exit_status
        manifest["limitations"].append(
            "The selected Pytest suite produced no non-skipped test evidence."
        )
    final_repository = _repository_state(manifest["limitations"])
    manifest["repository"]["final_status_sha256"] = final_repository["status_sha256"]
    manifest["repository"]["unchanged"] = not state_changes
    if manifest["repository"]["unchanged"] is False:
        manifest["limitations"].append("Repository status changed during verification.")
    manifest["cleanup"] = _cleanup_state(
        run_dir,
        manifest["run"]["profile"],
        signal_name,
        manifest,
    )
    if manifest["run"]["profile"] == "perf":
        comparison_path = run_dir / "performance-comparison.json"
        if comparison_path.is_file():
            try:
                comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
                comparison_cleanup = comparison.get("cleanup")
                if not isinstance(comparison_cleanup, dict):
                    raise ValueError("missing cleanup")
                manifest["cleanup"] = comparison_cleanup
                if comparison_cleanup.get("status") != "completed" and status == "passed":
                    status = "failed"
                    exit_status = exit_status or 1
                    manifest["status"] = status
                    manifest["exit_status"] = exit_status
                    manifest["limitations"].append(
                        "Performance comparison cleanup was not confirmed complete."
                    )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                manifest["cleanup"] = {"status": "unknown", "details": []}
                if status == "passed":
                    manifest["status"] = "failed"
                    manifest["exit_status"] = exit_status or 1
                manifest["limitations"].append(
                    f"Performance cleanup evidence is invalid ({type(exc).__name__})."
                )
    try:
        cleanup_strict_environment(run_dir, all_runs=True)
    except (OSError, RuntimeError) as exc:
        manifest["cleanup"] = {
            "status": "failed",
            "details": [f"sensitive runtime cleanup failed ({type(exc).__name__})"],
        }
        if manifest["status"] == "passed":
            manifest["status"] = "failed"
            manifest["exit_status"] = manifest["exit_status"] or 1
        manifest["limitations"].append(
            "Sensitive disposable runtime state may remain after verification."
        )
    suspect_paths: set[Path] = set()
    privacy_blockers = [
        *sanitize_trace_archives(run_dir, suspect_paths),
        *remove_known_secret_artifacts(run_dir, suspect_paths),
    ]
    prior_privacy_failure = (run_dir / PRIVACY_FAILURE_MARKER).is_file()
    if privacy_blockers:
        manifest["status"] = "blocked" if manifest["run"]["profile"] == "doctor" else "failed"
        manifest["exit_status"] = manifest["exit_status"] or 1
        manifest["limitations"].extend(privacy_blockers)
        cleanup_failures = [
            item for item in privacy_blockers if item.startswith("PRIVACY CLEANUP FAILURE:")
        ]
        if cleanup_failures or prior_privacy_failure:
            manifest["cleanup"] = {
                "status": "failed",
                "details": [
                    "A suspect artifact could not be confirmed absent after privacy cleanup."
                ],
            }
    elif prior_privacy_failure:
        manifest["status"] = "blocked" if manifest["run"]["profile"] == "doctor" else "failed"
        manifest["exit_status"] = manifest["exit_status"] or 1
        manifest["cleanup"] = {
            "status": "failed",
            "details": ["A suspect artifact could not be privacy-transformed during this run."],
        }
        manifest["limitations"].append(
            "PRIVACY CLEANUP FAILURE: a suspect artifact failed privacy transformation."
        )
    artifacts = _artifact_inventory(run_dir, manifest["limitations"], suspect_paths)
    manifest["artifacts"] = artifacts
    _append_artifact_limitations(manifest, artifacts)
    if manifest["run"]["profile"] in ORCHESTRATION_PROFILES:
        _append_orchestration_limitations(manifest)
    manifest["limitations"] = list(dict.fromkeys(manifest["limitations"]))
    _atomic_write_json(run_dir / "manifest.json", manifest)


def _json_string_list(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise argparse.ArgumentTypeError("expected a JSON array of strings")
    return parsed


def print_summary(run_dir: Path) -> None:
    manifest = _read_manifest(run_dir)
    profile = manifest["run"]["profile"]
    print(f"verification summary: {profile} {manifest['status']}")
    try:
        blocker_lines = [
            line
            for line in (run_dir / "run.log")
            .read_text(
                encoding="utf-8",
                errors="replace",
            )
            .splitlines()
            if "blocker:" in line.lower() or line.startswith("BLOCKER:")
        ]
    except OSError:
        blocker_lines = []
    for line in blocker_lines[-3:]:
        print(line)
    orchestration = manifest.get("orchestration")
    if isinstance(orchestration, dict):
        selected = orchestration.get("selected_lanes", [])
        print("selected lanes: " + (", ".join(selected) if selected else "none"))
        for child in manifest.get("children", []):
            if not isinstance(child, dict):
                continue
            detail = f"- {child.get('lane')}: {child.get('status')}"
            baseline = child.get("baseline_comparison")
            if isinstance(baseline, dict):
                detail += f" ({baseline.get('classification', 'could_not_compare')})"
            detail += f" -> {child.get('manifest') or 'no child manifest'}"
            print(detail)
    cleanup = manifest.get("cleanup", {})
    print(f"cleanup: {cleanup.get('status', 'unknown')}")
    next_command = "none; verification passed"
    failed_child = next(
        (
            child
            for child in manifest.get("children", [])
            if isinstance(child, dict) and child.get("status") != "passed"
        ),
        None,
    )
    if manifest["status"] == "blocked":
        target = profile if profile != "doctor" else manifest["run"]["doctor_profile"]
        next_command = f".agents/skills/verify-omnigent/scripts/verify.sh doctor {target}"
        base_ref = orchestration.get("base_ref") if isinstance(orchestration, dict) else None
        if target == "auto" and base_ref:
            next_command += f" --base-ref {base_ref}"
    elif failed_child is not None:
        next_command = (
            f".agents/skills/verify-omnigent/scripts/verify.sh {failed_child.get('lane')}"
        )
    elif manifest["status"] != "passed":
        next_command = f".agents/skills/verify-omnigent/scripts/verify.sh {profile}"
    print(f"next command: {next_command}")


def assert_finalized(run_dir: Path, expected_status: str) -> None:
    manifest = _read_manifest(run_dir)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("finalized manifest schema is invalid")
    if manifest.get("status") != expected_status:
        raise ValueError(
            f"finalized manifest status is {manifest.get('status')!r}, "
            f"expected {expected_status!r}"
        )
    if manifest.get("run", {}).get("phase") != "finished":
        raise ValueError("manifest did not reach the finished phase")
    exit_status = manifest.get("exit_status")
    if not isinstance(exit_status, int) or isinstance(exit_status, bool):
        raise ValueError("finalized manifest exit status is invalid")
    if expected_status == "passed" and exit_status != 0:
        raise ValueError("passed manifest has a nonzero exit status")
    if expected_status != "passed" and exit_status == 0:
        raise ValueError("non-passed manifest has a zero exit status")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--run-dir", type=Path, required=True)
    init_parser.add_argument("--profile", required=True)
    init_parser.add_argument("--doctor-profile", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--run-dir", type=Path, required=True)
    prepare_parser.add_argument("--phase", required=True)
    prepare_parser.add_argument("--argv-json", type=_json_string_list, required=True)
    prepare_parser.add_argument("--tests-json", type=_json_string_list, required=True)

    plan_parser = subparsers.add_parser("record-plan")
    plan_parser.add_argument("--run-dir", type=Path, required=True)
    plan_parser.add_argument("--plan", type=Path, required=True)

    child_parser = subparsers.add_parser("add-child")
    child_parser.add_argument("--run-dir", type=Path, required=True)
    child_parser.add_argument("--lane", required=True)
    child_parser.add_argument("--child-manifest", type=Path, required=True)
    child_parser.add_argument("--exit-status", type=int, required=True)

    register_parser = subparsers.add_parser("register-children")
    register_parser.add_argument("--run-dir", type=Path, required=True)
    register_parser.add_argument("--lanes-json", type=_json_string_list, required=True)

    mark_parser = subparsers.add_parser("mark-child")
    mark_parser.add_argument("--run-dir", type=Path, required=True)
    mark_parser.add_argument("--lane", required=True)
    mark_parser.add_argument(
        "--status",
        choices=("pending", "running", "passed", "failed", "interrupted", "blocked"),
        required=True,
    )
    mark_parser.add_argument("--reason")

    baseline_parser = subparsers.add_parser("record-baseline")
    baseline_parser.add_argument("--run-dir", type=Path, required=True)
    baseline_parser.add_argument("--lane", required=True)
    baseline_parser.add_argument("--comparison", type=Path, required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--run-dir", type=Path, required=True)
    validate_parser.add_argument("--profile", required=True)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--run-dir", type=Path, required=True)
    finalize_parser.add_argument(
        "--status",
        choices=sorted(FINAL_STATUSES),
        required=True,
    )
    finalize_parser.add_argument("--exit-status", type=int, required=True)
    finalize_parser.add_argument("--signal")

    assert_parser = subparsers.add_parser("assert-finalized")
    assert_parser.add_argument("--run-dir", type=Path, required=True)
    assert_parser.add_argument("--expected-status", choices=sorted(FINAL_STATUSES), required=True)

    summary_parser = subparsers.add_parser("summary")
    summary_parser.add_argument("--run-dir", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.action == "init":
        initialize(args.run_dir, args.profile, args.doctor_profile)
    elif args.action == "prepare":
        prepare(args.run_dir, args.phase, args.argv_json, args.tests_json)
    elif args.action == "record-plan":
        record_plan(args.run_dir, args.plan)
    elif args.action == "add-child":
        add_child(args.run_dir, args.lane, args.child_manifest, args.exit_status)
    elif args.action == "register-children":
        register_children(args.run_dir, args.lanes_json)
    elif args.action == "mark-child":
        mark_child(args.run_dir, args.lane, args.status, args.reason)
    elif args.action == "record-baseline":
        record_baseline(args.run_dir, args.lane, args.comparison)
    elif args.action == "validate":
        blockers = validate_required_outputs(args.run_dir, args.profile)
        for blocker in blockers:
            print(f"verification blocker: {blocker}", file=sys.stderr)
        raise SystemExit(1 if blockers else 0)
    elif args.action == "finalize":
        finalize(args.run_dir, args.status, args.exit_status, args.signal)
    elif args.action == "assert-finalized":
        assert_finalized(args.run_dir, args.expected_status)
    else:
        print_summary(args.run_dir)


if __name__ == "__main__":
    main()
