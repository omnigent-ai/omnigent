"""
Keep Omnigent's ``/model`` switches out of the user's default Claude model.

In an interactive session, Claude Code saves every ``/model <name>`` as the
user's default in their ``settings.json``, and the inline command has no
session-only form. Omnigent types ``/model`` for Smart Routing, the routed
first message and the web model picker, so each switch used to change the
model of every later Claude Code session.

One record per settings file, shared by every runner of the OS user, holds the
user's own default and the models Omnigent typed whose save has not been seen
yet. A save of a pending model is put back to that default, touching only the
``model`` key; any other change to the key is the user's and becomes the new
default. The live session keeps the switched model, which Claude holds in
memory.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

_logger = logging.getLogger(__name__)

# A switch queued behind a long turn saves when the turn ends; one that never
# lands stops being pending after this.
PENDING_SWITCH_TTL_S = 30 * 60
_WATCH_POLL_S = 0.25
_LOCK_TIMEOUT_S = 5.0
_RESTORE_ATTEMPTS = 3
# A watcher that keeps failing (locks, filesystem) backs off, then gives up;
# the next switch restarts it and the record keeps the default meanwhile.
_WATCH_MAX_FAILURES = 12
_WATCH_MAX_BACKOFF_S = 30.0


_MISSING = object()
# A settings file that is mid-write or not a JSON object.
_UNREADABLE = object()

_watchers: dict[tuple[Path, Path], threading.Thread] = {}
_watchers_lock = threading.Lock()


@dataclass(frozen=True)
class PendingModelSwitch:
    """A ``/model`` switch Omnigent registered before typing it."""

    settings_path: Path
    state_dir: Path
    model: str

    def cancel(self) -> None:
        """Forget the switch: the command never reached Claude."""
        try:
            with _locked_record(self.state_dir, self.settings_path) as record_path:
                record = _load_record(record_path)
                if record is None:
                    return
                pending = _live_pending(record)
                # Already settled when another switch keeps the record alive.
                match = next((p for p in pending if p["model"] == self.model), None)
                if match is not None:
                    pending.remove(match)
                _save_record(record_path, record, pending)
        except (OSError, FileLockTimeout):
            _logger.warning(
                "claude-native: could not cancel a pending /model record", exc_info=True
            )


def begin_model_switch(
    settings_path: Path, model_arg: str, *, state_dir: Path
) -> PendingModelSwitch | None:
    """
    Record a ``/model`` switch about to be typed, and watch for its save.

    The first pending switch snapshots the user's default; later ones reuse
    it, so a save another session has not undone yet is never taken for it.

    :param settings_path: The user ``settings.json`` the pane's Claude saves to.
    :param model_arg: The ``/model`` argument, e.g. ``"claude-sonnet-5-5"``.
    :param state_dir: Directory for the shared records, private to the OS user.
    :returns: The pending switch, or ``None`` when nothing needs undoing (the
        default is already *model_arg*) or the record could not be written.
    """
    model = _normalized(model_arg)
    settings_path = _canonical(settings_path)
    try:
        with _locked_record(state_dir, settings_path) as record_path:
            record: dict[str, Any] | None = _load_record(record_path)
            pending = _live_pending(record) if record is not None else []
            if record is None or not pending:
                current = _read_settings_model(settings_path)
                if current is _UNREADABLE:
                    return None
                record = {"settings_path": str(settings_path), "default": _encode(current)}
            if _normalized_value(_decode(record["default"])) == model:
                return None
            pending.append({"model": model, "expires_at": time.time() + PENDING_SWITCH_TTL_S})
            _save_record(record_path, record, pending)
    except (OSError, FileLockTimeout):
        _logger.warning(
            "claude-native: could not record the default model before /model; "
            "this switch may change the user's default",
            exc_info=True,
        )
        return None
    _ensure_watcher(settings_path, state_dir)
    return PendingModelSwitch(settings_path=settings_path, state_dir=state_dir, model=model)


def settle_default_model(settings_path: Path, *, state_dir: Path) -> bool:
    """
    Undo a pending switch's save, or adopt the user's own change.

    :param settings_path: The user ``settings.json``.
    :param state_dir: Directory for the shared records.
    :returns: ``True`` while switches are still pending.
    """
    settings_path = _canonical(settings_path)
    with _locked_record(state_dir, settings_path) as record_path:
        record = _load_record(record_path)
        if record is None:
            return False
        loaded = json.dumps(record, sort_keys=True)
        pending = _live_pending(record)
        default = _decode(record["default"])
        current = _read_settings_model(settings_path)
        if current is not _UNREADABLE and current != default:
            saved = next(
                (p for p in pending if p["model"] == _normalized_value(current)),
                None,
            )
            if saved is None:
                # Not a model Omnigent typed: the user's own pick is the default now.
                record["default"] = _encode(current)
            else:
                try:
                    _write_settings_model(settings_path, default)
                except (OSError, ValueError):
                    # Kept pending, so the next pass retries; warn only once per switch.
                    if not saved.get("restore_failed"):
                        saved["restore_failed"] = True
                        _logger.warning(
                            "claude-native: could not restore the default model in %s; retrying",
                            settings_path,
                            exc_info=True,
                        )
                else:
                    pending.remove(saved)
        # An idle poll leaves the record as it is rather than rewriting it.
        if not pending or json.dumps({**record, "pending": pending}, sort_keys=True) != loaded:
            _save_record(record_path, record, pending)
        return bool(pending)


def _ensure_watcher(settings_path: Path, state_dir: Path) -> None:
    """Start this process's watcher for *settings_path* unless one is running."""
    key = (settings_path, state_dir)
    with _watchers_lock:
        running = _watchers.get(key)
        if running is not None and running.is_alive():
            return
        watcher = threading.Thread(
            target=_watch,
            args=(settings_path, state_dir),
            name="claude-default-model-guard",
            daemon=True,
        )
        _watchers[key] = watcher
        watcher.start()


def _watch(settings_path: Path, state_dir: Path) -> None:
    """Settle *settings_path* until no switch is pending."""
    failures = 0
    while True:
        try:
            if not settle_default_model(settings_path, state_dir=state_dir):
                # Re-check under the lock _ensure_watcher takes, so a switch
                # registered while this thread winds down is still watched.
                with _watchers_lock:
                    if not settle_default_model(settings_path, state_dir=state_dir):
                        _watchers.pop((settings_path, state_dir), None)
                        return
            failures = 0
        except (OSError, FileLockTimeout):
            failures += 1
            if failures == 1 or failures > _WATCH_MAX_FAILURES:
                _logger.warning(
                    "claude-native: could not settle the default model in %s (%s)",
                    settings_path,
                    "giving up" if failures > _WATCH_MAX_FAILURES else "retrying",
                    exc_info=True,
                )
            if failures > _WATCH_MAX_FAILURES:
                with _watchers_lock:
                    _watchers.pop((settings_path, state_dir), None)
                return
            time.sleep(min(_WATCH_POLL_S * 2**failures, _WATCH_MAX_BACKOFF_S))
            continue
        time.sleep(_WATCH_POLL_S)


@contextlib.contextmanager
def _locked_record(state_dir: Path, settings_path: Path) -> Iterator[Path]:
    """Hold the cross-process lock for *settings_path*'s record."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(str(settings_path).encode()).hexdigest()[:16]
    record_path = state_dir / f"{digest}.json"
    with FileLock(str(state_dir / f"{digest}.lock"), timeout=_LOCK_TIMEOUT_S, mode=0o600):
        yield record_path


def _load_record(record_path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("default"), dict):
        return None
    return record


def _live_pending(record: dict[str, Any]) -> list[dict[str, Any]]:
    now = time.time()
    pending = record.get("pending")
    return [
        entry
        for entry in (pending if isinstance(pending, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("model"), str)
        and isinstance(entry.get("expires_at"), (int, float))
        and entry["expires_at"] > now
    ]


def _save_record(record_path: Path, record: dict[str, Any], pending: list[dict[str, Any]]) -> None:
    if not pending:
        record_path.unlink(missing_ok=True)
        return
    record["pending"] = pending
    _atomic_write(record_path, json.dumps(record), mode=0o600)


def _encode(value: object) -> dict[str, Any]:
    return {"present": False} if value is _MISSING else {"present": True, "value": value}


def _decode(encoded: dict[str, Any]) -> object:
    return encoded.get("value") if encoded.get("present") else _MISSING


def _canonical(path: Path) -> Path:
    """The real file behind *path*, so symlinked aliases share one record and lock."""
    return Path(os.path.realpath(path))


def _normalized(model: str) -> str:
    return model.strip().lower()


def _normalized_value(value: object) -> str | None:
    return _normalized(value) if isinstance(value, str) else None


def _read_settings_model(path: Path) -> object:
    """
    Read the ``model`` key of a Claude settings file.

    :param path: Settings file.
    :returns: The value; ``_MISSING`` when the file or key is absent;
        ``_UNREADABLE`` when the file is not a JSON object.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _MISSING
    except (OSError, ValueError):
        return _UNREADABLE
    if not isinstance(payload, dict):
        return _UNREADABLE
    return payload.get("model", _MISSING)


def _write_settings_model(path: Path, value: object) -> None:
    """
    Set the ``model`` key of a Claude settings file, or remove it for ``_MISSING``.

    :raises OSError: If the file cannot be rewritten.
    :raises ValueError: If the file is not a JSON object.
    """
    target = path.resolve()
    for _attempt in range(_RESTORE_ATTEMPTS):
        raw = target.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"{target} is not a JSON object")
        if value is _MISSING:
            payload.pop("model", None)
        else:
            payload["model"] = value
        # Claude's own layout: two-space indent, raw UTF-8, trailing newline.
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        # Retry when an edit is detected before the replace.
        if _atomic_write(
            target,
            text,
            mode=stat.S_IMODE(target.stat().st_mode),
            unchanged=lambda raw=raw: target.read_text(encoding="utf-8") == raw,
        ):
            _logger.info("claude-native: kept the default model in %s", path)
            return
    raise OSError(f"{target} kept changing while its default model was restored")


def _atomic_write(
    target: Path, text: str, *, mode: int, unchanged: Callable[[], bool] = lambda: True
) -> bool:
    """Replace *target* with *text*, unless *unchanged* reports another write first."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(text)
        os.chmod(tmp_path, mode)
        if not unchanged():
            return False
        os.replace(tmp_path, target)
        tmp_path = None
        return True
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
