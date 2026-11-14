"""Request and terminal ownership of immutable native Claude settings."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent.native.native_bridge_common import bridge_dir_preparation_lock

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance

try:
    import fcntl
except ImportError:  # pragma: no cover - native terminals require POSIX.
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)
_GENERATION = re.compile(r"[0-9a-f]{32}")
_REQUEST_FILE = re.compile(r"claude-settings-([0-9a-f]{32})\.request\.lock")
_OWNERSHIP_LOCK = re.compile(r"claude-settings-[0-9a-f]{32}\.(?:request|lease)\.lock")
_LIFECYCLE_FILE = ".claude-settings-lifecycle.json"
CLAUDE_SETTINGS_REQUEST_PROTOCOL = 1


class ClaudeSettingsRequestExpired(ValueError):
    """A request whose sender released ownership must be rebuilt before replay."""

    def __init__(self) -> None:
        super().__init__("Claude settings request has expired; rebuild the terminal request")


@dataclass(frozen=True)
class _ClaudeSettingsLifecycle:
    incarnation: str
    delete_for_session: str | None = None


def _read_lifecycle(bridge_dir: Path) -> _ClaudeSettingsLifecycle | None:
    from omnigent.harnesses.claude_native.bridge import _read_caller_settings

    try:
        payload = json.loads(_read_caller_settings(bridge_dir / _LIFECYCLE_FILE, nofollow=True))
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise ValueError("Claude settings lifecycle metadata is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) - {"schema", "incarnation", "delete_for_session"}
        or type(payload.get("schema")) is not int
        or payload["schema"] != 1
        or not isinstance(payload.get("incarnation"), str)
        or not _GENERATION.fullmatch(payload["incarnation"])
        or (
            "delete_for_session" in payload
            and (
                not isinstance(payload["delete_for_session"], str)
                or not payload["delete_for_session"]
            )
        )
    ):
        raise ValueError("Claude settings lifecycle metadata is invalid")
    return _ClaudeSettingsLifecycle(payload["incarnation"], payload.get("delete_for_session"))


def _write_lifecycle(bridge_dir: Path, lifecycle: _ClaudeSettingsLifecycle) -> None:
    from omnigent.harnesses.claude_native.bridge import _write_json_file

    payload: dict[str, object] = {"schema": 1, "incarnation": lifecycle.incarnation}
    if lifecycle.delete_for_session is not None:
        payload["delete_for_session"] = lifecycle.delete_for_session
    _write_json_file(bridge_dir / _LIFECYCLE_FILE, payload)


def prepare_claude_settings_lifecycle(bridge_dir: Path, *, reactivate: bool = False) -> str:
    """Initialize identity under the bridge lock; activation supersedes pending deletion."""
    lifecycle = _read_lifecycle(bridge_dir)
    if lifecycle is None or (reactivate and lifecycle.delete_for_session is not None):
        lifecycle = _ClaudeSettingsLifecycle(secrets.token_hex(16))
        _write_lifecycle(bridge_dir, lifecycle)
    return lifecycle.incarnation


def _require_incarnation(bridge_dir: Path, incarnation: str) -> None:
    lifecycle = _read_lifecycle(bridge_dir)
    if lifecycle is None or lifecycle.incarnation != incarnation:
        raise ClaudeSettingsRequestExpired()


def _session_matches(bridge_dir: Path, session_id: str) -> bool:
    from omnigent.harnesses.claude_native.bridge import _read_caller_settings

    try:
        config = json.loads(_read_caller_settings(bridge_dir / "bridge.json", nofollow=True))
    except FileNotFoundError:
        return True
    if not isinstance(config, dict):
        raise ValueError("Claude bridge session metadata is invalid")
    active = config.get("active_session_id") or config.get("conversation_id")
    return active is None or active == session_id


def _complete_bridge_deletion(bridge_dir: Path, incarnation: str) -> None:
    """Complete only the current intent while the caller holds the stable bridge lock."""
    lifecycle = _read_lifecycle(bridge_dir)
    if (
        lifecycle is None
        or lifecycle.incarnation != incarnation
        or lifecycle.delete_for_session is None
        or not _session_matches(bridge_dir, lifecycle.delete_for_session)
        or has_live_claude_settings_owners(bridge_dir)
    ):
        return
    shutil.rmtree(bridge_dir)


def request_claude_bridge_deletion(bridge_dir: Path, session_id: str) -> None:
    """Remove an unowned bridge, or let its final matching owner complete deletion."""
    with bridge_dir_preparation_lock(bridge_dir):
        if not bridge_dir.exists():
            return
        try:
            if not _session_matches(bridge_dir, session_id):
                return
            incarnation = prepare_claude_settings_lifecycle(bridge_dir)
            _write_lifecycle(bridge_dir, _ClaudeSettingsLifecycle(incarnation, session_id))
            _complete_bridge_deletion(bridge_dir, incarnation)
        except ValueError:
            _logger.warning("Claude bridge deletion metadata is invalid", exc_info=True)


def settings_generation_path(bridge_dir: Path, generation: str) -> Path:
    """Constrain generation resources to framework-owned bridge filenames."""
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise ValueError("Claude settings launch generation is invalid")
    return bridge_dir / f"claude-settings-{generation}.json"


def _open_lock(path: Path, *, create: bool = False) -> int:
    if fcntl is None:
        raise RuntimeError("Claude settings request ownership requires POSIX file locks")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Claude settings ownership requires regular lock files")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release(fd: int | None) -> None:
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def _live_request_incarnation(path: Path) -> tuple[bool, str | None]:
    """Read the binding from the same inode whose live sender lock was checked."""
    try:
        fd = _open_lock(path)
    except FileNotFoundError:
        return False, None
    try:
        assert fcntl is not None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            return False, None
        binding = os.pread(fd, 33, 0)
        if len(binding) != 32:
            return True, None
        incarnation = binding.decode("ascii", errors="replace")
        return True, incarnation if _GENERATION.fullmatch(incarnation) else None
    finally:
        _release(fd)


@contextlib.contextmanager
def _unused(path: Path) -> Iterator[bool]:
    """Hold an exclusive lock only when no process owns this resource."""
    fd: int | None = None
    try:
        try:
            fd = _open_lock(path)
        except FileNotFoundError:
            yield True
            return
        try:
            assert fcntl is not None
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        _release(fd)


def _retire_unused(bridge_dir: Path, generation: str) -> bool:
    """The stable bridge lock excludes new owners while lock inodes are removed."""
    baseline = settings_generation_path(bridge_dir, generation)
    request_lock = baseline.with_suffix(".request.lock")
    if not request_lock.exists():
        return False
    with _unused(request_lock) as request_unused:
        if not request_unused:
            return False
        with _unused(baseline.with_suffix(".lease.lock")) as leases_unused:
            if not leases_unused:
                return False
            if _has_unacknowledged_claims(bridge_dir, generation):
                return False
            for path in (
                baseline,
                baseline.with_suffix(".boundary.json"),
                request_lock,
                baseline.with_suffix(".lease.lock"),
            ):
                path.unlink(missing_ok=True)
    return True


def _has_unacknowledged_claims(bridge_dir: Path, generation: str) -> bool:
    """A lost sender or runner may leave an older receiver or detached terminal alive."""
    baseline = settings_generation_path(bridge_dir, generation)
    return os.path.lexists(baseline.with_suffix(".dispatch.owner")) or any(
        bridge_dir.glob(f"claude-settings-{generation}.terminal-*.owner")
    )


def _prune_unused(bridge_dir: Path) -> int:
    retired = 0
    for path in bridge_dir.glob("claude-settings-*.request.lock"):
        match = _REQUEST_FILE.fullmatch(path.name)
        if match is not None:
            retired += _retire_unused(bridge_dir, match[1])
    return retired


def prune_unused_claude_settings(bridge_dir: Path) -> int:
    """Reclaim protocol generations only after request and terminal owners release."""
    if not bridge_dir.exists():
        return 0
    with bridge_dir_preparation_lock(bridge_dir):
        return _prune_unused(bridge_dir)


def has_live_claude_settings_owners(bridge_dir: Path) -> bool:
    """Protect live or unacknowledged owners under the bridge cleanup lock."""
    for path in bridge_dir.iterdir():
        if not path.name.startswith("claude-settings-"):
            continue
        if path.name.endswith(".owner"):
            return True
        if path.name.endswith((".request.lock", ".lease.lock")):
            if _OWNERSHIP_LOCK.fullmatch(path.name) is None:
                return True
            with _unused(path) as unused:
                if not unused:
                    return True
    return False


@dataclass
class ClaudeSettingsLease:
    """An admitted reader that can transfer settings ownership to a terminal."""

    bridge_dir: Path
    generation: str
    _fd: int | None
    _incarnation: str
    _terminal_claim: Path | None = None

    def attach(self, instance: TerminalInstance) -> None:
        """Retain inputs until confirmed terminal termination, including after transfer."""
        with bridge_dir_preparation_lock(self.bridge_dir):
            if self._fd is None:
                raise ValueError("Claude settings request ownership has ended")
            _require_incarnation(self.bridge_dir, self._incarnation)
            path = settings_generation_path(self.bridge_dir, self.generation)
            fd = _open_lock(path.with_suffix(".lease.lock"))
            claim = path.with_suffix(f".terminal-{secrets.token_hex(16)}.owner")
            try:
                assert fcntl is not None
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                _release(_open_lock(claim, create=True))
            except BaseException:
                _release(fd)
                raise
        retained = ClaudeSettingsLease(
            self.bridge_dir, self.generation, fd, self._incarnation, claim
        )
        try:
            instance.add_resource_cleanup(retained.close)
        except BaseException:
            retained.close()
            raise

    def close(self) -> None:
        """Release this owner and retire files only after every owner has released."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            with bridge_dir_preparation_lock(self.bridge_dir):
                lifecycle = _read_lifecycle(self.bridge_dir)
                if lifecycle is None or lifecycle.incarnation != self._incarnation:
                    return
                if self._terminal_claim is not None:
                    self._terminal_claim.unlink(missing_ok=True)
                    self._terminal_claim = None
                _release(fd)
                fd = None
                _retire_unused(self.bridge_dir, self.generation)
                _complete_bridge_deletion(self.bridge_dir, self._incarnation)
        except (OSError, ValueError):
            _logger.warning("Claude settings resource cleanup failed", exc_info=True)
        finally:
            _release(fd)

    def __enter__(self) -> ClaudeSettingsLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass
class ClaudeSettingsRequest:
    """A serialized request is admissible only while its sender holds this owner."""

    bridge_dir: Path
    generation: str
    _request_fd: int | None
    _lease: ClaudeSettingsLease

    def settings_path(self, bridge_dir: Path) -> Path:
        """Reject argument construction after release or for another bridge."""
        if self._request_fd is None or bridge_dir != self.bridge_dir:
            raise ValueError("Claude settings request ownership does not match this launch")
        return settings_generation_path(bridge_dir, self.generation)

    def mark_dispatched(self) -> None:
        """Retain exposed operands until the actual receiver acknowledges admission."""
        with bridge_dir_preparation_lock(self.bridge_dir):
            _require_incarnation(self.bridge_dir, self._lease._incarnation)
            baseline = self.settings_path(self.bridge_dir)
            _release(_open_lock(baseline.with_suffix(".dispatch.owner"), create=True))

    def acknowledge(self, generation: object) -> bool:
        """Accept only the actual receiver's matching generation acknowledgement."""
        if generation != self.generation:
            return False
        with bridge_dir_preparation_lock(self.bridge_dir):
            _require_incarnation(self.bridge_dir, self._lease._incarnation)
            baseline = self.settings_path(self.bridge_dir)
            baseline.with_suffix(".dispatch.owner").unlink(missing_ok=True)
        return True

    @classmethod
    def create(cls, bridge_dir: Path) -> ClaudeSettingsRequest:
        """Own a generation before argument construction can expose its pathname."""
        from omnigent.harnesses.claude_native.bridge import _ensure_secure_dir

        request_fd: int | None = None
        lease_fd: int | None = None
        with bridge_dir_preparation_lock(bridge_dir):
            _ensure_secure_dir(bridge_dir)
            incarnation = prepare_claude_settings_lifecycle(bridge_dir)
            _prune_unused(bridge_dir)
            generation = secrets.token_hex(16)
            baseline = settings_generation_path(bridge_dir, generation)
            try:
                request_fd = _open_lock(baseline.with_suffix(".request.lock"), create=True)
                lease_fd = _open_lock(baseline.with_suffix(".lease.lock"), create=True)
                assert fcntl is not None
                fcntl.flock(request_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lease_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if os.write(request_fd, incarnation.encode("ascii")) != 32:
                    raise OSError("Claude settings request binding was not fully written")
            except BaseException:
                _release(request_fd)
                _release(lease_fd)
                _retire_unused(bridge_dir, generation)
                raise
        return cls(
            bridge_dir,
            generation,
            request_fd,
            ClaudeSettingsLease(bridge_dir, generation, lease_fd, incarnation),
        )

    def close(self) -> None:
        """Release sender ownership; later replays must rebuild the request."""
        fd, self._request_fd = self._request_fd, None
        if fd is None:
            return
        try:
            with bridge_dir_preparation_lock(self.bridge_dir):
                _release(fd)
                fd = None
        finally:
            _release(fd)
            self._lease.close()

    def __enter__(self) -> ClaudeSettingsRequest:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def acquire_claude_settings_request(bridge_dir: Path, generation: str) -> ClaudeSettingsLease:
    """Admit a live request before registry reuse, creation or any preparation await."""
    baseline = settings_generation_path(bridge_dir, generation)
    with bridge_dir_preparation_lock(bridge_dir):
        lifecycle = _read_lifecycle(bridge_dir)
        if lifecycle is None:
            raise ClaudeSettingsRequestExpired()
        live, incarnation = _live_request_incarnation(baseline.with_suffix(".request.lock"))
        if not live or not baseline.is_file():
            _retire_unused(bridge_dir, generation)
            raise ClaudeSettingsRequestExpired()
        if incarnation is None or incarnation != lifecycle.incarnation:
            raise ClaudeSettingsRequestExpired()
        fd = _open_lock(baseline.with_suffix(".lease.lock"))
        try:
            assert fcntl is not None
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            baseline.with_suffix(".dispatch.owner").unlink(missing_ok=True)
        except BaseException:
            _release(fd)
            raise
    return ClaudeSettingsLease(bridge_dir, generation, fd, incarnation)
