"""Which Claude Code releases can call which models.

Some models refuse an older client: the API answers ``Claude Code 2.1.217 does
not support this model; version 2.1.280 or newer is required``. A Default
launch that pins such a model fails its first turn, so the launch catalog moves
its Default onto a model the installed client can call. The installed release
is the one the catalog probe itself reported; the floors come from the owned
table in :mod:`omnigent.models.model_fallbacks` plus the ones failed turns
taught this host.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from filelock import FileLock, Timeout
from packaging.version import InvalidVersion, Version

from omnigent.models.claude_model_vocabulary import (
    CLAUDE_MODEL_ALIASES,
    canonical_claude_id,
    normalized_model_id,
)
from omnigent.models.model_fallbacks import CLAUDE_MODEL_MIN_CLIENT_VERSIONS

_logger = logging.getLogger(__name__)

#: Set to ``0`` to launch the catalog Default as the CLI reports it, whatever
#: the installed release; failed turns then teach nothing either.
FLOOR_ENV_VAR = "OMNIGENT_CLAUDE_DEFAULT_MODEL_FLOOR"

_CLAUDE_ID_RE = re.compile(
    rf"^claude-({'|'.join(CLAUDE_MODEL_ALIASES)})-(\d+)(?:-(\d{{1,2}})(?!\d))?", re.ASCII
)
_RELEASE_RE = re.compile(r"\d+\.\d+\.\d+", re.ASCII)
_REFUSAL_RE = re.compile(
    r"Claude Code (\d+\.\d+\.\d+) does not support this model; "
    r"version (\d+\.\d+\.\d+) or newer is required\.?",
    re.ASCII,
)

_FLOORS_FILE = "model-client-floors.json"
_FLOORS_LOCK_TIMEOUT_S = 5.0
_MAX_LEARNED_FLOORS = 32
_LEARNED_FLOOR_TTL_S = 30 * 24 * 3600.0
_CLOCK_SKEW_S = 24 * 3600.0


def floor_enabled() -> bool:
    """Whether the client-version floor is in force (:data:`FLOOR_ENV_VAR` is not ``0``)."""
    return os.environ.get(FLOOR_ENV_VAR, "").strip().lower() not in {"0", "false", "no", "off"}


def _claude_id_match(model: str) -> re.Match[str] | None:
    """Match *model* as a Claude id, however a gateway prefixes or suffixes it."""
    canonical = canonical_claude_id(model)
    return _CLAUDE_ID_RE.match(canonical) if canonical else None


def model_floor_key(model: str) -> str | None:
    """
    The key a model's client-version floor is filed under.

    Claude ids fold to family and generation, so ``system.ai.<id>[1m]``,
    ``databricks-<id>``, ``anthropic/<id>`` and a dated id all name one model.
    Any other id keys on its prefix-folded spelling.

    :param model: A picker id or wire model id, e.g.
        ``"system.ai.claude-opus-5-5[1m]"``.
    :returns: The key, e.g. ``"opus-5-5"``, or ``None`` for an empty id.
    """
    match = _claude_id_match(model)
    if match is None:
        return normalized_model_id(model) or None
    return "-".join(part for part in match.groups() if part)


_BUILTIN_MIN_CLIENT_VERSIONS: dict[str, str] = {
    key: release
    for model_id, release in CLAUDE_MODEL_MIN_CLIENT_VERSIONS.items()
    if (key := model_floor_key(model_id))
}


def _is_release(value: object) -> TypeGuard[str]:
    """Whether *value* is a plain ``X.Y.Z`` release string."""
    return isinstance(value, str) and _RELEASE_RE.fullmatch(value) is not None


def _older_than(installed: str, floor: str) -> bool:
    """Whether release *installed* is older than *floor*; unreadable means no."""
    try:
        return Version(installed) < Version(floor)
    except InvalidVersion:
        return False


# ---------------------------------------------------------- learned floors


@dataclass(frozen=True)
class ClientRefusal:
    """
    An API refusal of a model to a Claude Code release that is too old.

    :param client: The release the API saw, e.g. ``"2.1.217"``.
    :param floor: The release it asked for, e.g. ``"2.1.280"``.
    """

    client: str
    floor: str


def client_refusal(failure_context: Mapping[str, object] | None) -> ClientRefusal | None:
    """
    Read a too-old-client refusal from a failed turn's structured evidence.

    Only an API 400 ``invalid_request_error`` whose message is exactly the
    refusal counts. That evidence comes from an anchored ``API Error: 400 {...}``
    record (``claude_failure_context`` in ``failure_telemetry``), never from free
    text, which can be ordinary assistant prose.

    :param failure_context: The hook record's failure evidence, or ``None``.
    :returns: The refusal, or ``None`` when the evidence is anything else or
        asks for a release no newer than the client's own.
    """
    if not failure_context:
        return None
    if failure_context.get("http_status") != 400:
        return None
    if failure_context.get("provider_error_type") != "invalid_request_error":
        return None
    message = failure_context.get("native_error_message")
    match = _REFUSAL_RE.fullmatch(message.strip()) if isinstance(message, str) else None
    if match is None:
        return None
    client, floor = match.groups()
    return ClientRefusal(client, floor) if _older_than(client, floor) else None


def _floors_path() -> Path:
    """The per-host file holding the floors failed turns taught."""
    from omnigent.harnesses.claude_native.state import _claude_native_state_root

    return _claude_native_state_root() / _FLOORS_FILE


def _valid_record(item: object) -> dict[str, Any] | None:
    """One learned-floor record, or ``None`` when *item* is damaged or nonsensical."""
    if not isinstance(item, dict):
        return None
    scope, model, floor = item.get("scope"), item.get("model"), item.get("floor")
    client, learned_at = item.get("refused_client"), item.get("learned_at")
    if not (isinstance(scope, str) and scope and isinstance(model, str) and model):
        return None
    if not (_is_release(floor) and _is_release(client) and _older_than(client, floor)):
        return None
    if isinstance(learned_at, bool) or not isinstance(learned_at, (int, float)):
        return None
    if not math.isfinite(learned_at):
        return None
    return {
        "scope": scope,
        "model": model,
        "floor": floor,
        "refused_client": client,
        "learned_at": float(learned_at),
    }


def _read_floor_records() -> list[dict[str, Any]]:
    """Every valid record in the floors file; ``[]`` when it is absent or damaged."""
    try:
        payload = json.loads(_floors_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = payload.get("floors") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    return [record for item in raw if (record := _valid_record(item)) is not None]


def _live(record: Mapping[str, Any], now: float) -> bool:
    """Whether a record is neither older than its lifetime nor dated in the future."""
    age = now - record["learned_at"]
    return -_CLOCK_SKEW_S <= age <= _LEARNED_FLOOR_TTL_S


def learned_min_client_versions(
    scope: str | None, installed: str | None, *, now: float | None = None
) -> dict[str, str]:
    """
    The floors failed turns taught this host that still hold.

    A floor holds for the catalog (*scope*) it was learned under, only while
    the installed release is the very one that was refused, and for a limited
    time; a wrong lesson therefore ends with an upgrade or a month.

    :param scope: The catalog fingerprint, or ``None`` when unknown.
    :param installed: The installed Claude Code release, or ``None`` when unknown.
    :param now: Epoch seconds; defaults to the current time.
    :returns: :func:`model_floor_key` to release, e.g. ``{"opus-5-6": "2.1.300"}``.
        Empty when the file is absent, damaged or holds nothing applicable.
    """
    if scope is None or installed is None:
        return {}
    moment = time.time() if now is None else now
    floors: dict[str, str] = {}
    for record in _read_floor_records():
        if record["scope"] != scope or record["refused_client"] != installed:
            continue
        if not _live(record, moment):
            continue
        known = floors.get(record["model"])
        floors[record["model"]] = (
            record["floor"] if known is None else max(known, record["floor"], key=Version)
        )
    return floors


def record_min_client_version(
    model: str,
    floor: str,
    *,
    refused_client: str,
    scope: str,
    now: float | None = None,
) -> bool:
    """
    Remember that *model* needs Claude Code *floor* or newer.

    Best-effort and tiny: one record per catalog and model, the newest
    :data:`_MAX_LEARNED_FLOORS` kept, expired ones dropped, the read-modify-write
    under a cross-process lock. A family alias (which names no single model) is
    never recorded, nor is a refusal that asks for no newer release than the
    client it refused.

    :param model: The model the refused turn ran, e.g.
        ``"system.ai.claude-opus-5-6[1m]"``.
    :param floor: The release the refusal asked for, e.g. ``"2.1.300"``.
    :param refused_client: The release the API refused, e.g. ``"2.1.217"``.
    :param scope: The catalog fingerprint the session launched under.
    :param now: Epoch seconds; defaults to the current time.
    :returns: Whether the file was written.
    """
    key = model_floor_key(model)
    if not floor_enabled() or key is None or not scope:
        return False
    if normalized_model_id(model) in CLAUDE_MODEL_ALIASES:
        return False
    if not (_is_release(floor) and _is_release(refused_client)):
        return False
    if not _older_than(refused_client, floor):
        return False
    moment = time.time() if now is None else now
    path = _floors_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(f"{path}.lock", timeout=_FLOORS_LOCK_TIMEOUT_S):
            kept = [
                record
                for record in _read_floor_records()
                if _live(record, moment) and (record["scope"], record["model"]) != (scope, key)
            ]
            kept.append(
                {
                    "scope": scope,
                    "model": key,
                    "floor": floor,
                    "refused_client": refused_client,
                    "learned_at": moment,
                }
            )
            kept.sort(key=lambda record: record["learned_at"])
            _write_floor_records(path, kept[-_MAX_LEARNED_FLOORS:])
    except (OSError, Timeout):
        _logger.warning("could not persist the Claude Code model floors", exc_info=True)
        return False
    _logger.info("model %s needs Claude Code %s or newer", key, floor)
    return True


def _write_floor_records(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    """Replace the floors file atomically."""
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            json.dump({"floors": list(records)}, tmp, separators=(",", ":"))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def learn_from_refusal(
    failure_context: Mapping[str, object] | None,
    *,
    installed: str | None,
    model: str | None,
    scope: str | None,
    now: float | None = None,
) -> bool:
    """
    Record the floor a failed turn's too-old-client refusal names.

    The evidence must be the structured refusal (:func:`client_refusal`), the
    release it quotes must be the installed one (and the one the native CLI
    reported, when it reported any), and the model and catalog must be known.
    Anything less is not evidence about this host's catalog.

    :param failure_context: The ``StopFailure`` record's failure evidence.
    :param installed: The release the launch catalog was probed on.
    :param model: The model the turn ran, e.g. the status line's current one.
    :param scope: The catalog fingerprint the session launched under.
    :param now: Epoch seconds; defaults to the current time.
    :returns: Whether a floor was recorded.
    """
    refusal = client_refusal(failure_context)
    if refusal is None or installed is None or refusal.client != installed:
        return False
    native = (failure_context or {}).get("native_cli_version")
    if isinstance(native, str) and native != refusal.client:
        return False
    if not model or scope is None:
        return False
    return record_min_client_version(
        model, refusal.floor, refused_client=refusal.client, scope=scope, now=now
    )


# -------------------------------------------------------------- the demotion


def min_client_version(model: str, learned: Mapping[str, str] | None = None) -> str | None:
    """
    The oldest Claude Code release known to call *model*.

    :param model: A picker id or wire model id.
    :param learned: Floors that hold on this host, as returned by
        :func:`learned_min_client_versions`; ``None`` for the owned table alone.
    :returns: The higher of the owned and learned floors, or ``None`` when
        neither names the model.
    """
    key = model_floor_key(model)
    if key is None:
        return None
    floors = [
        floor
        for floor in (_BUILTIN_MIN_CLIENT_VERSIONS.get(key), (learned or {}).get(key))
        if floor
    ]
    return max(floors, key=Version) if floors else None


def _row_min_version(row: Mapping[str, Any], learned: Mapping[str, str]) -> str | None:
    """The release a catalog row's model needs, judged by its id and wire model."""
    floors = [
        floor
        for token in (row.get("model"), row.get("id"))
        if isinstance(token, str) and token
        if (floor := min_client_version(token, learned))
    ]
    return max(floors, key=Version) if floors else None


def _needs_newer_client(
    row: Mapping[str, Any], installed: str, learned: Mapping[str, str]
) -> bool:
    """Whether *installed* is too old to call the row's model."""
    floor = _row_min_version(row, learned)
    return floor is not None and _older_than(installed, floor)


def _row_match(row: Mapping[str, Any]) -> re.Match[str] | None:
    """The Claude-id match of a row's wire model (or id), when it spells one."""
    return _claude_id_match(str(row.get("model") or row.get("id") or ""))


def _family(row: Mapping[str, Any]) -> str | None:
    """The Claude family a row names, e.g. ``"sonnet"``, or ``None``."""
    match = _row_match(row)
    return match.group(1) if match else None


def _recency_key(row: Mapping[str, Any]) -> tuple[int, ...]:
    """Order rows by Claude generation; a row naming no generation ranks last."""
    match = _row_match(row)
    if match is None:
        return ()
    return (1, int(match.group(2)), int(match.group(3) or 0))


def _launch_spelling(row: Mapping[str, Any]) -> str:
    """The model id a launch of *row* passes to ``--model``."""
    return str(row.get("model") or row.get("id") or "")


def _label(row: Mapping[str, Any]) -> str:
    """The name to show a user for *row*."""
    return str(row.get("displayName") or _launch_spelling(row))


@dataclass(frozen=True)
class DefaultDemotion:
    """
    A catalog Default moved off a model the installed Claude Code cannot call.

    :param rows: The catalog with ``isDefault`` moved onto *chosen*.
    :param cli_version: The installed release, e.g. ``"2.1.217"``.
    :param wanted: The row that was the Default.
    :param min_version: The release *wanted* needs, e.g. ``"2.1.280"``.
    :param chosen: The row that is the Default now.
    """

    rows: list[dict[str, Any]]
    cli_version: str
    wanted: Mapping[str, Any]
    min_version: str
    chosen: Mapping[str, Any]

    @property
    def wanted_model(self) -> str:
        """The model id the demoted Default would have launched."""
        return _launch_spelling(self.wanted)

    @property
    def chosen_model(self) -> str:
        """The model id the Default launches now."""
        return _launch_spelling(self.chosen)

    def notice(self) -> str:
        """The explanation shown to the session's user."""
        return (
            f"Claude Code {self.cli_version} can't run {_label(self.wanted)}; it needs "
            f"{self.min_version} or newer. This session uses {_label(self.chosen)} instead. "
            "Update Claude Code on the host (for example `claude update`) to use it."
        )

    def notice_source_id(self, session_id: str) -> str:
        """
        The idempotency key of this session's notice.

        The same session on the same release and replacement model gets the
        same key, so a relaunch re-posts the notice as a no-op.
        """
        key = f"claude-native-default-demoted:{session_id}:{self.cli_version}:{self.chosen_model}"
        return key[:256]


class FlooredRows(list[dict[str, Any]]):
    """
    Catalog rows read against the installed Claude Code release.

    Equal to the plain row list; the attributes carry what the read decided, so
    a launch neither recomputes it nor rereads the store for it.

    :param rows: The rows to serve, with the Default already moved.
    :param demotion: What moved the Default, or ``None`` when it stayed.
    :param cli_version: The release the catalog was probed on, or ``None``.
    :param scope: The catalog fingerprint learned floors are filed under.
    """

    def __init__(
        self,
        rows: Iterable[Mapping[str, Any]] = (),
        *,
        demotion: DefaultDemotion | None = None,
        cli_version: str | None = None,
        scope: str | None = None,
    ) -> None:
        super().__init__(dict(row) for row in rows)
        self.demotion = demotion
        self.cli_version = cli_version
        self.scope = scope


def floor_catalog_default(
    rows: Sequence[Mapping[str, Any]], *, installed: str | None, scope: str | None
) -> FlooredRows:
    """
    Move the catalog Default off a model the installed Claude Code cannot call.

    The Default moves to the newest row of its own family the client can call,
    else to the newest row of any family; every other row stays, so an explicit
    pick of the demoted model still launches as asked. Nothing moves when the
    release is unknown, the Default needs nothing newer, nothing callable can
    replace it, or :data:`FLOOR_ENV_VAR` opts out.

    :param rows: Catalog rows, e.g. ``[{"id": "opus", "model": "...",
        "isDefault": True}]``.
    :param installed: The release the catalog was probed on, or ``None``.
    :param scope: The catalog fingerprint learned floors are filed under.
    :returns: The rows to serve, carrying the demotion when there was one.
    """
    demotion = (
        _demote(rows, installed, scope) if installed is not None and floor_enabled() else None
    )
    return FlooredRows(
        demotion.rows if demotion is not None else rows,
        demotion=demotion,
        cli_version=installed,
        scope=scope,
    )


def _demote(
    rows: Sequence[Mapping[str, Any]], installed: str, scope: str | None
) -> DefaultDemotion | None:
    """The demotion of *rows*' Default on release *installed*, if it needs one."""
    wanted = next((row for row in rows if row.get("isDefault") is True), None)
    if wanted is None:
        return None
    learned = learned_min_client_versions(scope, installed)
    min_version = _row_min_version(wanted, learned)
    if min_version is None or not _older_than(installed, min_version):
        return None
    candidates = [
        row
        for row in rows
        if row is not wanted and not _needs_newer_client(row, installed, learned)
    ]
    wanted_family = _family(wanted)
    same_family = [row for row in candidates if wanted_family and _family(row) == wanted_family]
    pool = same_family or candidates
    if not pool:
        return None
    chosen = max(pool, key=_recency_key)
    moved = [
        {**row, "isDefault": True}
        if row is chosen
        else {key: value for key, value in row.items() if key != "isDefault"}
        for row in rows
    ]
    return DefaultDemotion(
        rows=moved,
        cli_version=installed,
        wanted=wanted,
        min_version=min_version,
        chosen=chosen,
    )


__all__ = [
    "FLOOR_ENV_VAR",
    "ClientRefusal",
    "DefaultDemotion",
    "FlooredRows",
    "client_refusal",
    "floor_catalog_default",
    "floor_enabled",
    "learn_from_refusal",
    "learned_min_client_versions",
    "min_client_version",
    "model_floor_key",
    "record_min_client_version",
]
