"""Activate one managed host per Actor cold boot, without durable launch secrets."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

import psutil

from omnigent.host.identity_env import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
    HOST_TOKEN_FILE_ENV_VAR,
)
from omnigent.host.warm_bootstrap import _Signals
from omnigent.onboarding.sandboxes.types import GitCloneOptions, RepoWorkspace

MAX_ACTIVATION_BYTES = 1024 * 1024
ACTOR_UID_FILE = Path("/run/substrate/uid")
HOME = "/home/omnigent"
TOKEN_DIR = Path("/run/omnigent-bootstrap")


class ActivationError(Exception):
    """An activation error safe to expose without including payload contents."""


@dataclass(frozen=True, repr=False)
class Activation:
    actor_uid: str
    generation: str
    host_id: str
    host_name: str
    token: str
    server_url: str
    repos: tuple[RepoWorkspace, ...]
    host_config: dict[str, object] | None

    @classmethod
    def parse(cls, raw: object, actor_uid: str) -> Activation:
        names = {"version", *cls.__dataclass_fields__}
        if (
            not isinstance(raw, dict)
            or set(raw) != names
            or type(raw.get("version")) is not int
            or raw["version"] != 1
        ):
            raise ActivationError("Invalid activation payload")
        for name in ("actor_uid", "generation", "host_id", "host_name", "token", "server_url"):
            if not isinstance(raw[name], str) or not raw[name] or "\0" in raw[name]:
                raise ActivationError("Invalid activation payload")
        if raw["actor_uid"] != actor_uid:
            raise ActivationError("Activation targets a different Actor")
        if raw["generation"] != hashlib.sha256(raw["token"].encode()).hexdigest():
            raise ActivationError("Invalid activation generation")
        server = urlsplit(raw["server_url"])
        if (
            server.scheme != "https"
            or not server.hostname
            or server.username
            or server.password
            or server.path not in ("", "/")
            or server.query
            or server.fragment
        ):
            raise ActivationError("Invalid server origin")
        if raw["host_config"] is not None and not isinstance(raw["host_config"], dict):
            raise ActivationError("Invalid host configuration")
        if not isinstance(raw["repos"], list) or len(raw["repos"]) > 32:
            raise ActivationError("Invalid repositories")
        repos = []
        try:
            for value in raw["repos"]:
                if not isinstance(value, dict) or set(value) != {
                    "url",
                    "branch",
                    "repo_name",
                    "git_clone",
                }:
                    raise ValueError()
                if not isinstance(value["url"], str) or "\0" in value["url"]:
                    raise ValueError()
                url = urlsplit(value["url"])
                if (
                    url.scheme != "https"
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.fragment
                ):
                    raise ValueError()
                name = value["repo_name"]
                if (
                    not isinstance(name, str)
                    or name in {".", ".."}
                    or not re.fullmatch(r"[a-zA-Z0-9_.][a-zA-Z0-9_.-]*", name)
                ):
                    raise ValueError()
                branch = value["branch"]
                if branch is not None and (
                    not isinstance(branch, str) or not branch or "\0" in branch
                ):
                    raise ValueError()
                repos.append(
                    RepoWorkspace(
                        value["url"], branch, name, GitCloneOptions(**value["git_clone"])
                    )
                )
        except (ValueError, TypeError):
            raise ActivationError("Invalid repositories") from None
        return cls(
            **{name: raw[name] for name in cls.__dataclass_fields__ if name != "repos"},
            repos=tuple(repos),
        )

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "HOME": HOME,
            "IS_SANDBOX": "1",
            HOST_ID_ENV_VAR: self.host_id,
            HOST_NAME_ENV_VAR: self.host_name,
            HOST_TOKEN_ENV_VAR: self.token,
        }


def _token_directory() -> Path:
    directory_stat = TOKEN_DIR.stat()
    if (
        TOKEN_DIR.resolve() != TOKEN_DIR
        or not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise ActivationError("Bootstrap credential directory must be private and image-owned")
    return TOKEN_DIR


class ActorSignals(_Signals):
    """SIGTERM announces worker eviction; keep the Actor alive for snapshotting."""

    def __init__(self) -> None:
        super().__init__()
        self.draining = False

    def forward(self, signum: int, _frame: FrameType | None) -> None:
        if signum == signal.SIGTERM:
            self.draining = True
        else:
            super().forward(signum, _frame)


@contextlib.contextmanager
def actor_signals() -> Iterator[ActorSignals]:
    state = ActorSignals()
    previous = {sig: signal.signal(sig, state.forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield state
    finally:
        for sig, previous_handler in previous.items():
            signal.signal(sig, previous_handler)


def _cleanup_children(owner: psutil.Process) -> bool:
    """Reap the isolated bootstrap's descendants, including adopted session leaders."""
    deadline = time.monotonic() + 5
    try:
        while True:
            children = owner.children(recursive=True)
            if not children:
                return True
            for child in children:
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            _, alive = psutil.wait_procs(children, timeout=max(0, deadline - time.monotonic()))
            if alive or time.monotonic() >= deadline:
                return False
    except psutil.Error:
        return False


class Bootstrap:
    """One active host assignment, retained only for this Actor cold boot."""

    def __init__(self, actor_uid: str, signals: ActorSignals) -> None:
        uuid.UUID(actor_uid)
        self.actor_uid = actor_uid
        self.signals = signals
        self.activation: Activation | None = None
        self.stage = "waiting"
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.cleaned = True

    def activate(self, raw: object) -> None:
        activation = Activation.parse(raw, self.actor_uid)
        with self.lock:
            if self.signals.draining:
                raise ActivationError("Actor is draining")
            if self.activation is not None:
                same_host = (
                    self.activation.host_id == activation.host_id
                    and self.activation.server_url == activation.server_url
                )
                retryable = (
                    self.stage == "failed"
                    and self.cleaned
                    and (self.worker is None or not self.worker.is_alive())
                    and (self.signals.child is None or self.signals.child.poll() is not None)
                )
                if not same_host or (not retryable and self.activation != activation):
                    raise ActivationError("Actor is already bound to another activation")
                if not retryable:
                    return
            self.activation = activation
            self.stage = "preparing"
            self.cleaned = False
            self.worker = threading.Thread(target=self._launch, args=(activation,), daemon=True)
            self.worker.start()

    def status(self) -> dict[str, object]:
        with self.lock:
            return {
                "stage": self.stage,
                "generation": self.activation.generation if self.activation else None,
                "draining": self.signals.draining,
                "retryable": self.stage == "failed"
                and self.cleaned
                and (self.worker is None or not self.worker.is_alive()),
            }

    def _launch(self, activation: Activation) -> None:
        try:
            directory = _token_directory()
            token_file = directory / "host-token"
            descriptor, temporary = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(activation.token)
                os.replace(temporary, token_file)
                environment = {
                    **activation.environment(),
                    HOST_TOKEN_FILE_ENV_VAR: str(token_file),
                }
                self._run_host(activation, environment)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    Path(temporary).unlink()
                with contextlib.suppress(FileNotFoundError):
                    token_file.unlink()
        except (OSError, ValueError, ActivationError):
            pass
        finally:
            # The template runs bootstrap as PID 1 in an isolated actor. Never
            # sweep an ordinary developer process's unrelated children.
            cleaned = self.signals.child is None or (
                os.getpid() == 1 and _cleanup_children(psutil.Process())
            )
            with self.lock:
                self.cleaned = cleaned
                self.stage = "failed"

    def _run_host(self, activation: Activation, environment: dict[str, str]) -> None:
        from omnigent.onboarding.sandboxes.kubernetes import (
            _render_host_command,
            _render_workspace_prep_command,
        )

        command = _render_workspace_prep_command(
            f"{HOME}/workspace",
            activation.repos,
            activation.server_url,
            activation.host_id,
            activation.host_config,
        )
        self.signals.child = subprocess.Popen(
            command,
            env=environment,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if self.signals.signum is not None:
            self.signals.forward(self.signals.signum, None)
        if self.signals.child.wait() != 0:
            raise ActivationError("Workspace preparation failed")
        if self.signals.signum is not None:
            return
        self.signals.child = subprocess.Popen(
            _render_host_command(activation.server_url),
            env=environment,
            start_new_session=True,
        )
        if self.signals.signum is not None:
            self.signals.forward(self.signals.signum, None)
        with self.lock:
            self.stage = "prepared"
        self.signals.child.wait()


def handler(bootstrap: Bootstrap) -> type[BaseHTTPRequestHandler]:
    """Expose a fixed, bounded protocol; the Substrate router authenticates ingress."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def _reply(self, status: int, value: Mapping[str, object]) -> None:
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/readyz":
                self._reply(200, {"version": 1})
            elif self.path == "/status":
                self._reply(200, bootstrap.status())
            else:
                self._reply(404, {"error": "Unknown endpoint"})

        def do_POST(self) -> None:
            if self.path != "/activate":
                self._reply(404, {"error": "Unknown endpoint"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_ACTIVATION_BYTES or self.headers.get("Transfer-Encoding"):
                    raise ActivationError("Invalid activation size")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ActivationError("Incomplete activation")
                bootstrap.activate(json.loads(raw))
                self._reply(200, bootstrap.status())
            except (ValueError, ActivationError):
                self._reply(409, {"error": "Activation rejected"})

    return Handler


def main() -> None:
    _token_directory()
    with actor_signals() as signals:
        bootstrap = Bootstrap(ACTOR_UID_FILE.read_text().strip(), signals)
        with HTTPServer(("0.0.0.0", 80), handler(bootstrap)) as server:
            server.timeout = 0.2
            while signals.signum is None:
                server.handle_request()
        if signals.child is not None:
            try:
                signals.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(signals.child.pid, signal.SIGKILL)
                signals.child.wait()


if __name__ == "__main__":
    main()
