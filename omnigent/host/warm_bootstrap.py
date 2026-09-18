"""Bind an already-running sandbox Pod to one managed host.

The preparation container owns a private activation file in a memory-backed
volume. The host container reads that volume and starts the normal host only
after workspace preparation succeeds. Credentials never enter command argv
or bootstrap status output.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Literal

from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)

ACTIVATION_DIR_ENV_VAR = "OMNIGENT_ACTIVATION_DIR"
POD_UID_ENV_VAR = "OMNIGENT_POD_UID"
_DEFAULT_ACTIVATION_DIR = "/run/omnigent-activation"
_MAX_ACTIVATION_BYTES = 1024 * 1024
_POLL_INTERVAL = 0.2
_GENERATION_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
_STAGES = frozenset({"preparing", "prepared", "failed"})


class BootstrapError(Exception):
    """A bootstrap failure whose message is safe to expose to the caller."""


def _pod_uid() -> str:
    value = os.environ.get(POD_UID_ENV_VAR)
    if not value:
        raise BootstrapError("The sandbox Pod UID is missing.")
    return value


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or "\0" in value:
        raise BootstrapError("Invalid activation payload.")
    return value


@dataclass(frozen=True, repr=False)
class Activation:
    """Private, immutable assignment for one Pod lifetime."""

    version: int
    pod_uid: str
    host_id: str
    host_name: str
    token: str
    server_url: str
    prepare_command: tuple[str, ...]
    generation: str

    @classmethod
    def parse(cls, value: object) -> Activation:
        if not isinstance(value, dict):
            raise BootstrapError("Invalid activation payload.")
        if _string(value, "pod_uid") != _pod_uid():
            raise BootstrapError("Activation targets a different Pod.")
        if set(value) != set(cls.__dataclass_fields__):
            raise BootstrapError("Invalid activation payload.")
        if type(value.get("version")) is not int or value["version"] != 1:
            raise BootstrapError("Unsupported activation version.")
        command = value.get("prepare_command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or "\0" in arg for arg in command)
            or not command[0]
        ):
            raise BootstrapError("Invalid preparation command.")
        generation = _string(value, "generation")
        if not _GENERATION_RE.fullmatch(generation):
            raise BootstrapError("Invalid activation generation.")
        return cls(
            version=1,
            pod_uid=_string(value, "pod_uid"),
            host_id=_string(value, "host_id"),
            host_name=_string(value, "host_name"),
            token=_string(value, "token"),
            server_url=_string(value, "server_url"),
            prepare_command=tuple(command),
            generation=generation,
        )

    def environment(self) -> dict[str, str]:
        if self.pod_uid != _pod_uid():
            raise BootstrapError("Activation targets a different Pod.")
        return {
            **os.environ,
            HOST_ID_ENV_VAR: self.host_id,
            HOST_NAME_ENV_VAR: self.host_name,
            HOST_TOKEN_ENV_VAR: self.token,
        }


def _state_dir(*, create: bool = False) -> Path:
    directory = Path(os.environ.get(ACTIVATION_DIR_ENV_VAR, _DEFAULT_ACTIVATION_DIR))
    # The fsGroup-writable emptyDir root is owned by root, so create our own dir.
    directory /= "private"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return directory


def _write_json(path: Path, value: object) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_json(path: Path) -> object | None:
    try:
        with path.open() as stream:
            raw = stream.read(_MAX_ACTIVATION_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw.encode("utf-8")) > _MAX_ACTIVATION_BYTES:
        raise BootstrapError("Invalid bootstrap state.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise BootstrapError("Invalid bootstrap state.") from None


def _load_activation(directory: Path) -> Activation | None:
    value = _read_json(directory / "activation.json")
    return Activation.parse(value) if value is not None else None


@contextlib.contextmanager
def _lock(path: Path, *, nonblocking: bool = False) -> Iterator[None]:
    import fcntl

    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(descriptor, flags)
        except BlockingIOError:
            raise BootstrapError("Workspace preparation is already running.") from None
        yield
    finally:
        os.close(descriptor)


def activate(value: object) -> None:
    """Atomically accept one assignment, allowing identical delivery retries."""
    activation = Activation.parse(value)
    directory = _state_dir(create=True)
    with _lock(directory / "activation.lock"):
        existing = _load_activation(directory)
        if existing is not None:
            if existing != activation:
                raise BootstrapError("This Pod is already bound to another activation.")
            return
        _write_json(directory / "activation.json", asdict(activation))


def status() -> dict[str, str | None]:
    """Return only nonsecret preparation metadata."""
    directory = _state_dir()
    activation = _load_activation(directory)
    if activation is None:
        return {"stage": "waiting", "generation": None}
    value = _read_json(directory / "status.json")
    stage = "bound"
    if value is not None:
        if (
            not isinstance(value, dict)
            or value.get("pod_uid") != activation.pod_uid
            or value.get("generation") != activation.generation
            or not isinstance(value.get("stage"), str)
            or value.get("stage") not in _STAGES
        ):
            raise BootstrapError("Invalid bootstrap status.")
        stage = value["stage"]
    return {"stage": stage, "generation": activation.generation}


def _set_stage(
    directory: Path,
    activation: Activation,
    stage: Literal["preparing", "prepared", "failed"],
) -> None:
    _write_json(
        directory / "status.json",
        {"pod_uid": activation.pod_uid, "generation": activation.generation, "stage": stage},
    )


class _Signals:
    def __init__(self) -> None:
        self.signum: int | None = None
        self.child: subprocess.Popen[bytes] | None = None

    def forward(self, signum: int, _frame: FrameType | None) -> None:
        self.signum = signum
        if self.child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.child.pid, signum)


@contextlib.contextmanager
def _signals() -> Iterator[_Signals]:
    state = _Signals()
    previous = {sig: signal.signal(sig, state.forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield state
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _prepare_once(directory: Path, activation: Activation, signals: _Signals) -> None:
    _set_stage(directory, activation, "preparing")
    try:
        signals.child = subprocess.Popen(
            activation.prepare_command,
            env=activation.environment(),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if signals.signum is not None:
            signals.forward(signals.signum, None)
        returncode = signals.child.wait()
    except (OSError, subprocess.SubprocessError):
        returncode = 1
    finally:
        signals.child = None
    if signals.signum is None:
        _set_stage(directory, activation, "prepared" if returncode == 0 else "failed")


def prepare() -> int:
    """Wait for assignment, prepare once, then stay alive for status requests."""
    directory = _state_dir(create=True)
    with _signals() as signals, _lock(directory / "prepare.lock", nonblocking=True):
        _write_json(directory / "ready.json", {"pod_uid": _pod_uid()})
        while signals.signum is None:
            activation = _load_activation(directory)
            if (
                activation is not None
                and signals.signum is None
                and status()["stage"] in {"bound", "preparing"}
            ):
                _prepare_once(directory, activation, signals)
            with contextlib.suppress(ChildProcessError):
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            if signals.signum is None:
                time.sleep(_POLL_INTERVAL)
    return 128 + signals.signum if signals.signum is not None else 0


def host() -> int:
    """Start the ordinary managed host only after this assignment is prepared."""
    with _signals() as signals:
        while signals.signum is None:
            activation = _load_activation(_state_dir())
            if activation is not None:
                stage = status()["stage"]
                if stage == "failed":
                    raise BootstrapError("Sandbox workspace preparation failed.")
                if stage == "prepared":
                    from omnigent.onboarding.sandboxes.kubernetes import _render_host_command

                    command = _render_host_command(activation.server_url)
                    if signals.signum is None:
                        os.execvpe(command[0], command, activation.environment())
            time.sleep(_POLL_INTERVAL)
    return 128 + signals.signum if signals.signum is not None else 0


def ready() -> bool:
    value = _read_json(_state_dir() / "ready.json")
    return (
        isinstance(value, dict)
        and value.get("pod_uid") == _pod_uid()
        and status()["stage"] != "failed"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "host", "activate", "status", "ready"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "activate":
            raw = sys.stdin.buffer.readline(_MAX_ACTIVATION_BYTES + 1)
            if len(raw) > _MAX_ACTIVATION_BYTES:
                raise BootstrapError("Activation payload is too large.")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BootstrapError("Invalid activation payload.") from None
            activate(value)
            return 0
        if args.mode == "status":
            print(json.dumps(status(), separators=(",", ":")))
            return 0
        if args.mode == "ready":
            return 0 if ready() else 1
        if args.mode == "prepare":
            return prepare()
        return host()
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Warm sandbox bootstrap failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
