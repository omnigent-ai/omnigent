"""Single-use controller/editor handoff on a private local socket."""

from __future__ import annotations

import contextlib
import json
import math
import os
import secrets
import socket
import stat
import struct
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from omnigent.harnesses.claude_native.editor_handoff import (
    NativeEditorBinding,
    read_controller_editor_binding,
    read_editor_bridge_binding,
)
from omnigent.harnesses.claude_native.editor_owner import NativeDraftOwner
from omnigent.harnesses.claude_native.editor_transaction import (
    NativeDraftEdit,
    NativeDraftEditRefused,
)

_CLAIM_FILE = "editor-operation.json"
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_POLL_S = 0.05


class _Channel:
    def __init__(self, connection: socket.socket, token: str, check: Callable[[], None]) -> None:
        self.connection = connection
        self.token = token
        self.check = check
        connection.settimeout(_POLL_S)

    def send(self, phase: str, **fields: object) -> None:
        self.check()
        encoded = json.dumps({**fields, "token": self.token, "phase": phase}).encode()
        if len(encoded) > _MAX_FRAME_BYTES:
            raise NativeDraftEditRefused("The editor operation exceeds the handoff limit.")
        frame = memoryview(struct.pack("!I", len(encoded)) + encoded)
        offset = 0
        while offset < len(frame):
            self.check()
            try:
                sent = self.connection.send(frame[offset:])
            except TimeoutError:
                continue
            if sent <= 0:
                raise NativeDraftEditRefused("The editor operation's peer disconnected.")
            offset += sent

    def _read(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            self.check()
            try:
                chunk = self.connection.recv(size - len(result))
            except TimeoutError:
                continue
            if not chunk:
                raise NativeDraftEditRefused("The editor operation's peer disconnected.")
            result.extend(chunk)
        return bytes(result)

    def receive(self, phase: str) -> dict[str, object]:
        size = struct.unpack("!I", self._read(4))[0]
        if not 0 < size <= _MAX_FRAME_BYTES:
            raise NativeDraftEditRefused("The editor operation received an invalid frame.")
        payload = json.loads(self._read(size))
        if (
            not isinstance(payload, dict)
            or payload.get("token") != self.token
            or payload.get("phase") != phase
        ):
            raise NativeDraftEditRefused("The editor operation was refused or cancelled.")
        self.check()
        return payload


@dataclass(frozen=True)
class NativeDraftImport:
    """A permitted import; later recovery must observe this state in a new edit."""

    binding: NativeEditorBinding
    original_text: str
    replacement_text: str


class NativeEditorControl:
    """Own one editor claim under the caller's bridge injection/model locks."""

    def __init__(
        self,
        bridge_dir: Path,
        *,
        binding: NativeEditorBinding,
        expected_text: str,
        replacement_text: str,
        cancelled: Callable[[], bool],
        deadline: float,
    ) -> None:
        NativeDraftEdit._encode(expected_text)
        NativeDraftEdit._encode(replacement_text)
        self.bridge_dir = bridge_dir
        self.binding = binding
        self.expected_text = expected_text
        self.replacement_text = replacement_text
        self.cancelled = cancelled
        if not math.isfinite(deadline):
            raise NativeDraftEditRefused("The editor operation needs a finite deadline.")
        self.deadline = min(deadline, time.monotonic() + 15.0)
        self.import_receipt: NativeDraftImport | None = None
        self._started = False

    def _check(self, *, accepting: bool = False) -> None:
        if (
            (not accepting and self.cancelled())
            or time.monotonic() >= self.deadline
            or read_controller_editor_binding(self.bridge_dir) != self.binding
        ):
            raise NativeDraftEditRefused(
                "The editor operation was cancelled or its owner changed."
            )

    def run(self, open_editor: Callable[[], None]) -> NativeDraftImport:
        """Prepare and commit while parked, then permit one native import."""
        if self._started:
            raise NativeDraftEditRefused("An editor operation can only be claimed once.")
        self._started = True
        self._check()
        claim_path = self.bridge_dir / _CLAIM_FILE
        token = secrets.token_hex(32)
        connection: socket.socket | None = None
        triggered = False
        with (
            tempfile.TemporaryDirectory(prefix="omnigent-editor-") as private,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
        ):
            endpoint = Path(private) / "editor.sock"
            listener.bind(str(endpoint))
            endpoint.chmod(0o600)
            listener.listen(1)
            listener.settimeout(_POLL_S)
            claim = json.dumps(
                {
                    "version": 1,
                    "token": token,
                    "socket_path": str(endpoint),
                    "deadline": self.deadline,
                    "binding": asdict(self.binding),
                }
            ).encode()
            descriptor = os.open(
                claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            info = os.fstat(descriptor)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(claim)
                self._check()
                triggered = True
                open_editor()
                while connection is None:
                    # A cancelled caller still owns the child it asked to open.
                    self._check(accepting=True)
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                listener.close()
                channel = _Channel(connection, token, self._check)
                hello = channel.receive("hello")
                if hello.get("binding") != asdict(self.binding):
                    raise NativeDraftEditRefused("The editor belongs to a different native owner.")
                channel.send(
                    "prepare", expected=self.expected_text, replacement=self.replacement_text
                )
                channel.receive("prepared")
                channel.send("commit")
                channel.receive("committed")
                self._check()
                self.import_receipt = NativeDraftImport(
                    self.binding, self.expected_text, self.replacement_text
                )
                channel.send("release")
                channel.receive("released")
                return self.import_receipt
            finally:
                retired = connection is not None and self._retire(connection)
                if not triggered or retired:
                    with contextlib.suppress(FileNotFoundError):
                        current = claim_path.lstat()
                        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                            claim_path.unlink()
                os.close(descriptor)
                if connection is not None and not retired:
                    raise NativeDraftEditRefused(
                        "Editor cleanup is unconfirmed. Inspect the terminal before retrying."
                    )

    @staticmethod
    def _retire(connection: socket.socket) -> bool:
        """Wait for editor cleanup before relinquishing this operation's claim."""
        until = time.monotonic() + 2.0
        try:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_WR)
            while time.monotonic() < until:
                try:
                    if not connection.recv(65536):
                        return True
                except TimeoutError:
                    continue
                except OSError:
                    return True
            return False
        finally:
            connection.close()


def _read_claim(bridge_dir: Path) -> dict[str, object] | None:
    try:
        descriptor = os.open(bridge_dir / _CLAIM_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 0 < info.st_size <= 65536
        ):
            raise NativeDraftEditRefused("The editor operation claim is not private.")
        raw = os.read(descriptor, 65537)
    finally:
        os.close(descriptor)
    claim = json.loads(raw)
    if (
        not isinstance(claim, dict)
        or set(claim) != {"version", "token", "socket_path", "deadline", "binding"}
        or type(claim["version"]) is not int
        or claim["version"] != 1
        or not isinstance(claim["token"], str)
        or len(claim["token"]) != 64
        or any(char not in "0123456789abcdef" for char in claim["token"])
        or type(claim["deadline"]) not in {float, int}
        or not math.isfinite(claim["deadline"])
        or not time.monotonic() < claim["deadline"] <= time.monotonic() + 15.0
        or not isinstance(claim["socket_path"], str)
        or not Path(claim["socket_path"]).is_absolute()
    ):
        raise NativeDraftEditRefused("The editor operation claim is invalid or expired.")
    endpoint = Path(claim["socket_path"])
    for info, directory in ((endpoint.parent.lstat(), True), (endpoint.lstat(), False)):
        if (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISSOCK(info.st_mode))
        ):
            raise NativeDraftEditRefused("The editor operation endpoint is not private.")
    return claim


def run_editor_operation(path: Path, *, bridge_dir: Path, executable: Path) -> int | None:
    """Handle an armed operation, or preserve normal delegation when unarmed."""
    try:
        claim = _read_claim(bridge_dir)
        if claim is None:
            return None
        binding = read_editor_bridge_binding(bridge_dir, executable)
        if binding is None or asdict(binding) != claim["binding"]:
            raise NativeDraftEditRefused("The editor operation belongs to another native owner.")
        token, endpoint, deadline = claim["token"], claim["socket_path"], claim["deadline"]
        assert isinstance(token, str) and isinstance(endpoint, str)
        assert isinstance(deadline, float | int)

        def check() -> None:
            if (
                time.monotonic() >= deadline
                or read_editor_bridge_binding(bridge_dir, executable) != binding
            ):
                raise NativeDraftEditRefused("The editor operation expired or its owner changed.")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_POLL_S)
            connection.connect(endpoint)
            channel = _Channel(connection, token, check)
            channel.send("hello", binding=asdict(binding))
            request = channel.receive("prepare")
            expected, replacement = request.get("expected"), request.get("replacement")
            if not isinstance(expected, str) or not isinstance(replacement, str):
                raise NativeDraftEditRefused("The editor operation has no exact draft.")

            def current_owner() -> NativeDraftOwner | None:
                current = read_editor_bridge_binding(bridge_dir, executable)
                return current.owner if current is not None and current == binding else None

            with NativeDraftEdit(
                path,
                expected_text=expected,
                expected_owner=binding.owner,
                current_owner=current_owner,
                cancelled=lambda: time.monotonic() >= deadline,
            ) as edit:
                channel.send("prepared")
                channel.receive("commit")
                edit.commit(replacement)
                channel.send("committed")
                channel.receive("release")
                edit.release()
                channel.send("released")
            return 0
    except (OSError, RuntimeError, ValueError, OverflowError):
        return 1
