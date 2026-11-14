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
from dataclasses import dataclass, replace
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
    admission_epoch: str | None = None
    session_id: str | None = None
    bridge_identity: tuple[int, int] | None = None

    @property
    def epoch(self) -> str:
        return self.admission_epoch or self.incarnation


def _read_lifecycle(bridge_dir: Path) -> _ClaudeSettingsLifecycle | None:
    from omnigent.harnesses.claude_native.bridge import _read_caller_settings

    try:
        payload = json.loads(_read_caller_settings(bridge_dir / _LIFECYCLE_FILE, nofollow=True))
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise ValueError("Claude settings lifecycle metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Claude settings lifecycle metadata is invalid")
    fields = {"schema", "incarnation", "delete_for_session"}
    if payload.get("schema") == 2:
        fields |= {"admission_epoch", "session_id", "bridge_identity"}
    if (
        set(payload) - fields
        or type(payload.get("schema")) is not int
        or payload["schema"] not in {1, 2}
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
    if payload["schema"] == 1:
        # Legacy request locks bind directly to the ownership incarnation.
        return _ClaudeSettingsLifecycle(payload["incarnation"], payload.get("delete_for_session"))
    identity = payload.get("bridge_identity")
    if (
        not isinstance(payload.get("admission_epoch"), str)
        or not _GENERATION.fullmatch(payload["admission_epoch"])
        or "session_id" not in payload
        or (
            payload["session_id"] is not None
            and (not isinstance(payload["session_id"], str) or not payload["session_id"])
        )
        or not isinstance(identity, list)
        or len(identity) != 2
        or any(type(value) is not int or value < 0 for value in identity)
        or identity[1] == 0
    ):
        raise ValueError("Claude settings lifecycle metadata is invalid")
    return _ClaudeSettingsLifecycle(
        payload["incarnation"],
        payload.get("delete_for_session"),
        payload["admission_epoch"],
        payload["session_id"],
        (identity[0], identity[1]),
    )


def _write_lifecycle(bridge_dir: Path, lifecycle: _ClaudeSettingsLifecycle) -> None:
    from omnigent.harnesses.claude_native.bridge import _write_json_file

    payload: dict[str, object] = {"schema": 1, "incarnation": lifecycle.incarnation}
    if lifecycle.admission_epoch is not None:
        assert lifecycle.bridge_identity is not None
        payload.update(
            schema=2,
            admission_epoch=lifecycle.admission_epoch,
            session_id=lifecycle.session_id,
            bridge_identity=list(lifecycle.bridge_identity),
        )
    if lifecycle.delete_for_session is not None:
        payload["delete_for_session"] = lifecycle.delete_for_session
    _write_json_file(bridge_dir / _LIFECYCLE_FILE, payload)


def _directory_identity(bridge_dir: Path) -> tuple[int, int]:
    info = bridge_dir.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Claude settings ownership requires a bridge directory")
    return info.st_dev, info.st_ino


def _bridge_session_id(bridge_dir: Path) -> str | None:
    from omnigent.harnesses.claude_native.bridge import _read_caller_settings

    try:
        config = json.loads(_read_caller_settings(bridge_dir / "bridge.json", nofollow=True))
    except FileNotFoundError:
        return None
    if not isinstance(config, dict):
        raise ValueError("Claude bridge session metadata is invalid")
    active = config.get("active_session_id")
    if active is None:
        active = config.get("conversation_id")
    return active if isinstance(active, str) and active else None


def _lifecycle_matches_bridge(bridge_dir: Path, lifecycle: _ClaudeSettingsLifecycle) -> bool:
    return (
        lifecycle.bridge_identity is None
        or lifecycle.bridge_identity == _directory_identity(bridge_dir)
    ) and (lifecycle.session_id is None or lifecycle.session_id == _bridge_session_id(bridge_dir))


def prepare_claude_settings_lifecycle(
    bridge_dir: Path, *, reactivate: bool = False, session_id: str | None = None
) -> str:
    """Keep verified same-session owners while a new epoch expires old admissions."""
    lifecycle = _read_lifecycle(bridge_dir)
    identity = _directory_identity(bridge_dir)
    same_session = (
        lifecycle is not None
        and lifecycle.admission_epoch is not None
        and session_id is not None
        and lifecycle.session_id == session_id
        and lifecycle.bridge_identity == identity
        and _bridge_session_id(bridge_dir) == session_id
    )
    if lifecycle is None or (reactivate and not same_session):
        incarnation = secrets.token_hex(16)
        lifecycle = _ClaudeSettingsLifecycle(
            incarnation,
            admission_epoch=incarnation,
            session_id=session_id,
            bridge_identity=identity,
        )
        _write_lifecycle(bridge_dir, lifecycle)
    elif reactivate and lifecycle.delete_for_session is not None:
        lifecycle = replace(
            lifecycle, admission_epoch=secrets.token_hex(16), delete_for_session=None
        )
        _write_lifecycle(bridge_dir, lifecycle)
    return lifecycle.incarnation


def _require_incarnation(
    bridge_dir: Path, incarnation: str, *, admission_epoch: str | None = None
) -> _ClaudeSettingsLifecycle:
    lifecycle = _read_lifecycle(bridge_dir)
    if (
        lifecycle is None
        or lifecycle.incarnation != incarnation
        or (admission_epoch is not None and lifecycle.epoch != admission_epoch)
    ):
        raise ClaudeSettingsRequestExpired()
    return lifecycle


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


def _complete_bridge_deletion(bridge_dir: Path, incarnation: str, *, admission_epoch: str) -> None:
    """Complete only the current intent while the caller holds the stable bridge lock."""
    lifecycle = _read_lifecycle(bridge_dir)
    if (
        lifecycle is None
        or lifecycle.incarnation != incarnation
        or lifecycle.epoch != admission_epoch
        or not _lifecycle_matches_bridge(bridge_dir, lifecycle)
        or lifecycle.delete_for_session is None
        or not _session_matches(bridge_dir, lifecycle.delete_for_session)
        or has_live_claude_settings_owners(bridge_dir)
    ):
        return
    shutil.rmtree(bridge_dir)


def request_claude_bridge_deletion(bridge_dir: Path, session_id: str) -> None:
    """Remove an unowned bridge, or let its final matching owner complete deletion."""
    if not bridge_dir.exists():
        return
    with bridge_dir_preparation_lock(bridge_dir):
        if not bridge_dir.exists():
            return
        try:
            if not _session_matches(bridge_dir, session_id):
                return
            incarnation = prepare_claude_settings_lifecycle(bridge_dir)
            lifecycle = _require_incarnation(bridge_dir, incarnation)
            _write_lifecycle(bridge_dir, replace(lifecycle, delete_for_session=session_id))
            _complete_bridge_deletion(bridge_dir, incarnation, admission_epoch=lifecycle.epoch)
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


def _open_bridge(bridge_dir: Path) -> int:
    return os.open(
        bridge_dir,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )


def _fd_identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _file_matches_fd(path: Path, fd: int) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == _fd_identity(fd)


def _dispatch_binding(
    lifecycle: _ClaudeSettingsLifecycle, admission_epoch: str, **owner_fds: int
) -> bytes:
    return json.dumps(
        {
            "schema": 1,
            "incarnation": lifecycle.incarnation,
            "admission_epoch": admission_epoch,
            "session_id": lifecycle.session_id,
            "owner_identities": {name: list(_fd_identity(fd)) for name, fd in owner_fds.items()},
        },
        sort_keys=True,
        ensure_ascii=True,
    ).encode("ascii")


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
    _admission_epoch: str | None = None
    _bridge_fd: int | None = None
    _claim_fd: int | None = None
    _session_id: str | None = None

    def _identity_matches(
        self, lifecycle: _ClaudeSettingsLifecycle, *, lease_fd: int | None = None
    ) -> bool:
        lease_fd = self._fd if lease_fd is None else lease_fd
        if (
            self._bridge_fd is None
            or _fd_identity(self._bridge_fd) != _directory_identity(self.bridge_dir)
            or lifecycle.session_id != self._session_id
            or not _lifecycle_matches_bridge(self.bridge_dir, lifecycle)
            or lease_fd is None
            or not _file_matches_fd(
                settings_generation_path(self.bridge_dir, self.generation).with_suffix(
                    ".lease.lock"
                ),
                lease_fd,
            )
        ):
            return False
        if self._terminal_claim is not None:
            if self._claim_fd is None or not _file_matches_fd(
                self._terminal_claim, self._claim_fd
            ):
                return False
        return True

    def _require_current(self) -> _ClaudeSettingsLifecycle:
        lifecycle = _require_incarnation(
            self.bridge_dir,
            self._incarnation,
            admission_epoch=self._admission_epoch or self._incarnation,
        )
        if not self._identity_matches(lifecycle):
            raise ClaudeSettingsRequestExpired()
        return lifecycle

    def _retain_for_terminal(self) -> ClaudeSettingsLease:
        with bridge_dir_preparation_lock(self.bridge_dir):
            if self._fd is None:
                raise ValueError("Claude settings request ownership has ended")
            self._require_current()
            path = settings_generation_path(self.bridge_dir, self.generation)
            fd = _open_lock(path.with_suffix(".lease.lock"))
            claim = path.with_suffix(f".terminal-{secrets.token_hex(16)}.owner")
            bridge_fd: int | None = None
            claim_fd: int | None = None
            try:
                assert fcntl is not None
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                assert self._bridge_fd is not None
                bridge_fd = os.dup(self._bridge_fd)
                claim_fd = _open_lock(claim, create=True)
            except BaseException:
                _release(fd)
                _release(bridge_fd)
                _release(claim_fd)
                raise
        return ClaudeSettingsLease(
            self.bridge_dir,
            self.generation,
            fd,
            self._incarnation,
            claim,
            _admission_epoch=self._admission_epoch,
            _bridge_fd=bridge_fd,
            _claim_fd=claim_fd,
            _session_id=self._session_id,
        )

    def attach(self, instance: TerminalInstance) -> None:
        """Retain inputs until confirmed terminal termination, including after transfer."""
        retained = self._retain_for_terminal()
        try:
            instance.add_resource_cleanup(retained.close)
        except BaseException:
            retained.close()
            raise

    async def attach_async(self, instance: TerminalInstance) -> None:
        """Drain retention before registering cleanup on the terminal's event loop."""
        from omnigent.harnesses.claude_native.bridge import _run_claude_settings_step

        retained = await _run_claude_settings_step(
            self._retain_for_terminal, dispose=ClaudeSettingsLease.close
        )
        try:
            instance.add_resource_cleanup(retained.close)
        except BaseException:
            await _run_claude_settings_step(retained.close)
            raise

    def close(self) -> None:
        """Release this owner and retire files only after every owner has released."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            with bridge_dir_preparation_lock(self.bridge_dir):
                if self._terminal_claim is not None:
                    if (
                        self._bridge_fd is None
                        or _fd_identity(self._bridge_fd) != _directory_identity(self.bridge_dir)
                        or self._claim_fd is None
                        or not _file_matches_fd(self._terminal_claim, self._claim_fd)
                    ):
                        return
                    self._terminal_claim.unlink(missing_ok=True)
                    self._terminal_claim = None
                lifecycle = _read_lifecycle(self.bridge_dir)
                if lifecycle is None or lifecycle.incarnation != self._incarnation:
                    return
                if not self._identity_matches(lifecycle, lease_fd=fd):
                    return
                same_epoch = lifecycle.epoch == (self._admission_epoch or self._incarnation)
                if not same_epoch and (
                    lifecycle.session_id is None or lifecycle.bridge_identity is None
                ):
                    return
                _release(fd)
                fd = None
                # Expired admissions may release claims without retiring their operand generation.
                if same_epoch:
                    _retire_unused(self.bridge_dir, self.generation)
                _complete_bridge_deletion(
                    self.bridge_dir,
                    self._incarnation,
                    admission_epoch=lifecycle.epoch,
                )
        except (OSError, ValueError):
            _logger.warning("Claude settings resource cleanup failed", exc_info=True)
        finally:
            _release(fd)
            _release(self._bridge_fd)
            self._bridge_fd = None
            _release(self._claim_fd)
            self._claim_fd = None

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
            lifecycle = self._lease._require_current()
            baseline = self.settings_path(self.bridge_dir)
            assert self._request_fd is not None
            assert self._lease._fd is not None and self._lease._bridge_fd is not None
            claim = baseline.with_suffix(".dispatch.owner")
            fd = _open_lock(claim, create=True)
            try:
                binding = _dispatch_binding(
                    lifecycle,
                    lifecycle.epoch,
                    bridge=self._lease._bridge_fd,
                    request=self._request_fd,
                    lease=self._lease._fd,
                    dispatch=fd,
                )
                if os.write(fd, binding) != len(binding):
                    raise OSError("Claude settings dispatch binding was not fully written")
            except BaseException:
                if _file_matches_fd(claim, fd):
                    claim.unlink(missing_ok=True)
                raise
            finally:
                _release(fd)

    def _clear_dispatch(self) -> None:
        with bridge_dir_preparation_lock(self.bridge_dir):
            self._lease._require_current()
            baseline = self.settings_path(self.bridge_dir)
            baseline.with_suffix(".dispatch.owner").unlink(missing_ok=True)

    def cancel_undelivered_dispatch(self) -> None:
        """Clear exposure only after the sender proves this request was never delivered."""
        self._clear_dispatch()

    def acknowledge(self, generation: object) -> bool:
        """Accept only the actual receiver's matching generation acknowledgement."""
        if generation != self.generation:
            return False
        # Completed admission stays acknowledged after bridge reactivation.
        with contextlib.suppress(ClaudeSettingsRequestExpired):
            self._clear_dispatch()
        return True

    @classmethod
    def create(cls, bridge_dir: Path) -> ClaudeSettingsRequest:
        """Own a generation before argument construction can expose its pathname."""
        from omnigent.harnesses.claude_native.bridge import _ensure_secure_dir

        request_fd: int | None = None
        lease_fd: int | None = None
        bridge_fd: int | None = None
        with bridge_dir_preparation_lock(bridge_dir):
            _ensure_secure_dir(bridge_dir)
            incarnation = prepare_claude_settings_lifecycle(bridge_dir)
            lifecycle = _require_incarnation(bridge_dir, incarnation)
            if not _lifecycle_matches_bridge(bridge_dir, lifecycle):
                raise ClaudeSettingsRequestExpired()
            _prune_unused(bridge_dir)
            generation = secrets.token_hex(16)
            baseline = settings_generation_path(bridge_dir, generation)
            try:
                bridge_fd = _open_bridge(bridge_dir)
                request_fd = _open_lock(baseline.with_suffix(".request.lock"), create=True)
                lease_fd = _open_lock(baseline.with_suffix(".lease.lock"), create=True)
                assert fcntl is not None
                fcntl.flock(request_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lease_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if os.write(request_fd, lifecycle.epoch.encode("ascii")) != 32:
                    raise OSError("Claude settings request binding was not fully written")
            except BaseException:
                _release(request_fd)
                _release(lease_fd)
                _release(bridge_fd)
                _retire_unused(bridge_dir, generation)
                raise
        return cls(
            bridge_dir,
            generation,
            request_fd,
            ClaudeSettingsLease(
                bridge_dir,
                generation,
                lease_fd,
                incarnation,
                _admission_epoch=lifecycle.epoch,
                _bridge_fd=bridge_fd,
                _session_id=lifecycle.session_id,
            ),
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


def _reject_expired_dispatch(
    bridge_dir: Path,
    generation: str,
    lifecycle: _ClaudeSettingsLifecycle,
    admission_epoch: str,
) -> None:
    """Release only this live request's verified exposure after definite rejection."""
    if lifecycle.session_id is None or lifecycle.bridge_identity is None:
        return
    baseline = settings_generation_path(bridge_dir, generation)
    paths = {
        "request": baseline.with_suffix(".request.lock"),
        "lease": baseline.with_suffix(".lease.lock"),
        "dispatch": baseline.with_suffix(".dispatch.owner"),
    }
    owner_fds: dict[str, int] = {}
    try:
        owner_fds["bridge"] = _open_bridge(bridge_dir)
        for name, path in paths.items():
            owner_fds[name] = _open_lock(path)
        if _live_request_incarnation(paths["request"]) != (True, admission_epoch):
            return
        observed = os.pread(owner_fds["dispatch"], 4097, 0)
        if observed != _dispatch_binding(lifecycle, admission_epoch, **owner_fds):
            return
        if (
            _fd_identity(owner_fds["bridge"]) != lifecycle.bridge_identity
            or not _lifecycle_matches_bridge(bridge_dir, lifecycle)
            or not all(_file_matches_fd(path, owner_fds[name]) for name, path in paths.items())
        ):
            return
        paths["dispatch"].unlink()
    except (OSError, ValueError):
        return
    finally:
        for fd in owner_fds.values():
            _release(fd)


def acquire_claude_settings_request(bridge_dir: Path, generation: str) -> ClaudeSettingsLease:
    """Admit a live request before registry reuse, creation or any preparation await."""
    baseline = settings_generation_path(bridge_dir, generation)
    with bridge_dir_preparation_lock(bridge_dir):
        lifecycle = _read_lifecycle(bridge_dir)
        if lifecycle is None or not _lifecycle_matches_bridge(bridge_dir, lifecycle):
            raise ClaudeSettingsRequestExpired()
        live, admission_epoch = _live_request_incarnation(baseline.with_suffix(".request.lock"))
        if not live or not baseline.is_file():
            _retire_unused(bridge_dir, generation)
            raise ClaudeSettingsRequestExpired()
        if admission_epoch is None:
            raise ClaudeSettingsRequestExpired()
        if admission_epoch != lifecycle.epoch:
            _reject_expired_dispatch(bridge_dir, generation, lifecycle, admission_epoch)
            raise ClaudeSettingsRequestExpired()
        fd: int | None = None
        bridge_fd: int | None = None
        try:
            bridge_fd = _open_bridge(bridge_dir)
            fd = _open_lock(baseline.with_suffix(".lease.lock"))
            assert fcntl is not None
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            baseline.with_suffix(".dispatch.owner").unlink(missing_ok=True)
        except BaseException:
            _release(fd)
            _release(bridge_fd)
            raise
    return ClaudeSettingsLease(
        bridge_dir,
        generation,
        fd,
        lifecycle.incarnation,
        _admission_epoch=admission_epoch,
        _bridge_fd=bridge_fd,
        _session_id=lifecycle.session_id,
    )
