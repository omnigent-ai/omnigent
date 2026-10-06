"""Which Claude Code releases can call which models.

Some models refuse an older client: the API answers ``Claude Code 2.1.217 does
not support this model; version 2.1.280 or newer is required``. A Default
launch that pins such a model fails its first turn, so the launch catalog moves
its Default onto a model the installed client can call. The floors come from a
built-in table plus the ones failed turns taught this host.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from omnigent.models.claude_model_vocabulary import (
    CLAUDE_MODEL_ALIASES,
    canonical_claude_id,
    normalized_model_id,
)

_logger = logging.getLogger(__name__)

#: Oldest Claude Code release that can call each model, keyed by
#: :func:`model_floor_key` (family and generation, so a gateway prefix, the
#: ``[1m]`` marker or a dated suffix all land on the same key).
_BUILTIN_MIN_CLIENT_VERSIONS: dict[str, str] = {
    "opus-5-5": "2.1.280",
    "sonnet-5-5": "2.1.280",
}

_CLAUDE_ID_RE = re.compile(
    r"^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2})(?!\d))?", re.ASCII
)
_CLI_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\s*\(Claude Code\)", re.ASCII)
_UNSUPPORTED_MODEL_RE = re.compile(
    r"does not support this model\W+version\s+(\d+\.\d+\.\d+)\s+or newer is required",
    re.ASCII | re.IGNORECASE,
)
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+", re.ASCII)

_FLOORS_FILE = "model-client-floors.json"
_MAX_LEARNED_FLOORS = 32

_VERSION_PROBE_TIMEOUT_S = 10.0
# A launcher can upgrade the CLI under an unchanged executable, so even a
# known release is read again this often. An unreadable one is retried sooner,
# but still not on every catalog read.
_VERSION_REREAD_S = 3600.0
_UNKNOWN_VERSION_RETRY_S = 300.0
_VERSION_CACHE_MAX_ENTRIES = 16
_version_cache: dict[tuple[str, int, int], tuple[str | None, float]] = {}
_version_cache_lock = threading.Lock()


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


def parse_cli_version(text: str) -> str | None:
    """
    Read the release from ``claude --version`` output.

    Only Claude Code's own ``<version> (Claude Code)`` line counts, so a
    launcher wrapper that prints a banner of its own is never mistaken for it.

    :param text: Combined stdout and stderr, e.g. ``"2.1.217 (Claude Code)"``.
    :returns: The release, e.g. ``"2.1.217"``, or ``None``.
    """
    match = _CLI_VERSION_RE.search(text)
    return match.group(1) if match else None


def unsupported_model_min_version(text: str | None) -> str | None:
    """
    The release a "does not support this model" refusal asks for.

    :param text: A failed turn's error text, e.g. ``"API Error: 400 ... Claude
        Code 2.1.217 does not support this model; version 2.1.280 or newer is
        required"``.
    :returns: The required release, e.g. ``"2.1.280"``, or ``None``.
    """
    match = _UNSUPPORTED_MODEL_RE.search(text or "")
    return match.group(1) if match else None


def installed_cli_version() -> str | None:
    """
    The installed Claude Code release, read once per binary identity.

    Runs the same executable the catalog probe launches (launcher plugin
    included). The answer is cached per executable identity, and read again
    after :data:`_VERSION_REREAD_S` (:data:`_UNKNOWN_VERSION_RETRY_S` when the
    last read printed no Claude Code version).

    :returns: The release, e.g. ``"2.1.217"``, or ``None`` when unknown.
    """
    from omnigent.claude_launcher import resolve_claude_launch
    from omnigent.models.model_catalog_store import binary_identity

    command, args = resolve_claude_launch("claude", ["--version"])
    identity = binary_identity(command)
    if identity is None:
        return None
    now = time.monotonic()
    with _version_cache_lock:
        cached = _version_cache.get(identity)
    if cached is not None:
        version, read_at = cached
        if now - read_at < (_VERSION_REREAD_S if version else _UNKNOWN_VERSION_RETRY_S):
            return version
    version = _probe_cli_version(command, args)
    with _version_cache_lock:
        if len(_version_cache) >= _VERSION_CACHE_MAX_ENTRIES:
            _version_cache.clear()
        _version_cache[identity] = (version, now)
    return version


def _probe_cli_version(command: str, args: list[str]) -> str | None:
    """Run ``<command> <args>`` and read the Claude Code release it prints."""
    try:
        completed = subprocess.run(
            [command, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _logger.debug("Claude Code version probe failed", exc_info=True)
        return None
    return parse_cli_version((completed.stdout or "") + "\n" + (completed.stderr or ""))


def _floors_path() -> Path:
    """The per-host file holding the floors failed turns taught."""
    from omnigent.harnesses.claude_native.state import _claude_native_state_root

    return _claude_native_state_root() / _FLOORS_FILE


def _is_release(value: object) -> bool:
    """Whether *value* is a plain ``X.Y.Z`` release string."""
    return isinstance(value, str) and _VERSION_RE.fullmatch(value) is not None


def learned_min_client_versions() -> dict[str, str]:
    """
    The floors failed turns taught this host, by :func:`model_floor_key`.

    :returns: Key to release, e.g. ``{"opus-5-6": "2.1.300"}``. Empty when the
        file is absent, unreadable or damaged; entries that are not a plain
        release are dropped.
    """
    try:
        payload = json.loads(_floors_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    floors = payload.get("floors") if isinstance(payload, dict) else None
    if not isinstance(floors, dict):
        return {}
    return {key: value for key, value in floors.items() if _is_release(value) and key}


def record_min_client_version(model: str, min_version: str) -> bool:
    """
    Remember that *model* needs Claude Code *min_version* or newer.

    Best-effort and tiny: the newest :data:`_MAX_LEARNED_FLOORS` floors are
    kept, a floor is only ever raised, and a family alias (which names no
    single model) is never recorded.

    :param model: The model the refused turn ran, e.g.
        ``"system.ai.claude-opus-5-6[1m]"``.
    :param min_version: The release the refusal asked for, e.g. ``"2.1.300"``.
    :returns: Whether the file changed.
    """
    key = model_floor_key(model)
    if key is None or not _is_release(min_version):
        return False
    if normalized_model_id(model) in CLAUDE_MODEL_ALIASES:
        return False
    floors = learned_min_client_versions()
    if key in floors and Version(floors[key]) >= Version(min_version):
        return False
    floors[key] = min_version
    while len(floors) > _MAX_LEARNED_FLOORS:
        floors.pop(next(iter(floors)))
    path = _floors_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                json.dump({"floors": floors}, tmp, separators=(",", ":"))
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError:
        _logger.warning("could not persist the Claude Code model floors", exc_info=True)
        return False
    _logger.info("model %s needs Claude Code %s or newer", key, min_version)
    return True


def min_client_version(model: str, learned: Mapping[str, str] | None = None) -> str | None:
    """
    The oldest Claude Code release known to call *model*.

    :param model: A picker id or wire model id.
    :param learned: Floors learned on this host, as returned by
        :func:`learned_min_client_versions`; ``None`` for the built-in table alone.
    :returns: The higher of the built-in and learned floors, or ``None`` when
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


def _older_than(installed: str, floor: str) -> bool:
    """Whether release *installed* is older than *floor*; unreadable means no."""
    try:
        return Version(installed) < Version(floor)
    except InvalidVersion:
        return False


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


def _recency_key(row: Mapping[str, Any]) -> tuple[int, ...]:
    """Order rows by Claude generation; a row naming no generation ranks last."""
    match = _claude_id_match(str(row.get("model") or row.get("id") or ""))
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
        """The one-line explanation shown to the session's user."""
        return (
            f"Claude Code {self.cli_version} can't run {_label(self.wanted)} "
            f"(needs {self.min_version} or newer), so this session uses "
            f"{_label(self.chosen)}. Run `claude update` to use it."
        )


async def demote_default_for_installed_client(
    rows: Sequence[Mapping[str, Any]],
) -> DefaultDemotion | None:
    """
    Move the catalog Default off a model the installed Claude Code cannot call.

    The release is looked up (see :func:`installed_cli_version`) only when the
    Default has a known floor, so a catalog that never names such a model costs
    no subprocess. The Default moves to the newest row the client can call;
    every other row stays, so an explicit pick of the demoted model still
    launches as asked.

    :param rows: Catalog rows, e.g. ``[{"id": "opus", "model": "...",
        "isDefault": True}]``.
    :returns: The demotion, or ``None`` when the Default is callable, nothing
        callable can replace it, or the installed release is unknown.
    """
    wanted = next((row for row in rows if row.get("isDefault") is True), None)
    if wanted is None:
        return None
    learned = learned_min_client_versions()
    min_version = _row_min_version(wanted, learned)
    if min_version is None:
        return None
    installed = await asyncio.to_thread(installed_cli_version)
    if installed is None or not _older_than(installed, min_version):
        return None
    candidates = [
        row
        for row in rows
        if row is not wanted and not _needs_newer_client(row, installed, learned)
    ]
    if not candidates:
        return None
    chosen = max(candidates, key=_recency_key)
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
    "DefaultDemotion",
    "demote_default_for_installed_client",
    "installed_cli_version",
    "learned_min_client_versions",
    "min_client_version",
    "model_floor_key",
    "parse_cli_version",
    "record_min_client_version",
    "unsupported_model_min_version",
]
