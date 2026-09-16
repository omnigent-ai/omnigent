"""Environment-owned Linux overlay mounts shared by sandboxed consumers."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from omnigent.inner._proc import kill_tree

if TYPE_CHECKING:
    from omnigent.inner.datamodel import OSEnvSpec
    from omnigent.inner.sandbox import SandboxPolicy


SHARED_ENVIRONMENT_VAR = "OMNIGENT_COPY_ON_WRITE_ENVIRONMENT"


def has_copy_on_write(spec: OSEnvSpec | None) -> bool:
    return bool(
        spec and spec.sandbox and any(p.copy_on_write for p in spec.sandbox.write_path_specs)
    )


def validate_copy_on_write_harness(spec: OSEnvSpec | None, harness: str | None) -> None:
    """Reject harnesses whose native tools do not share the environment."""
    from omnigent.harness_aliases import canonicalize_harness

    if has_copy_on_write(spec) and canonicalize_harness(harness) != "openai-agents":
        raise ValueError(
            "copy_on_write currently requires executor.harness=openai-agents; "
            "other harnesses' native tools do not share its disposable filesystem"
        )


def copy_on_write_startup_error(detail: str) -> OSError:
    """Explain mount failures without guessing support from a kernel version."""
    return OSError(
        f"Cannot initialize copy_on_write mounts (kernel {os.uname().release}). "
        "Requires Bubblewrap 0.11+ (--tmp-overlay), unprivileged user/mount namespaces, "
        "OverlayFS with userxattr, and tmpfs user.* extended attributes "
        "(upstream Linux 6.6+ for both, or equivalent distro backports). "
        "Host namespace limits and AppArmor/SELinux/seccomp/container policy must permit "
        "these operations. Original directories will not be used for writes. "
        "See docs/AGENT_YAML_SPEC.md#copy-on-write-requirements. "
        f"Details: {detail}"
    )


def export_shared_environment(policy: SandboxPolicy) -> str:
    """Transport a prepared runtime handle to a framework harness process."""
    if not policy.copy_on_write_namespace:
        raise RuntimeError("Copy-on-write environment is not prepared")
    return json.dumps(
        {
            "roots": [str(root) for root in policy.copy_on_write_roots or []],
            "namespace": list(policy.copy_on_write_namespace),
        }
    )


def attach_shared_environment(policy: SandboxPolicy) -> None:
    """Adopt the runner's mounts without changing the declared sandbox policy."""
    raw = os.environ.get(SHARED_ENVIRONMENT_VAR)
    if not raw or not policy.copy_on_write_roots:
        return
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("roots") != [
        str(root) for root in policy.copy_on_write_roots
    ]:
        raise ValueError("Inherited copy-on-write environment does not match configured paths")
    policy.copy_on_write_namespace = parse_namespace(value.get("namespace"))


class CopyOnWriteEnvironment:
    """Keep one mounted view alive independently of file helpers and terminals."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = tuple(roots)
        self._process: subprocess.Popen[str] | None = None
        self._namespace: tuple[int, int, int] | None = None
        self._closed = False
        self._lock = threading.Lock()

    def prepare(self, policy: SandboxPolicy) -> None:
        """Attach the shared namespace, failing if an existing mount was lost."""
        if tuple(policy.copy_on_write_roots or []) != self.roots:
            raise ValueError("Inherited copy-on-write paths must match their environment")
        with self._lock:
            if self._closed:
                raise RuntimeError("Copy-on-write environment is closed")
            if self._process is None:
                self._start()
            assert self._process is not None
            if self._process.poll() is not None:
                raise RuntimeError("Copy-on-write environment was lost; start a new environment")
            policy.copy_on_write_namespace = self._namespace

    def _start(self) -> None:
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise copy_on_write_startup_error(
                "bwrap was not found on PATH; install Bubblewrap 0.11+"
            )
        # The stdin pipe owns the lifetime. PDEATHSIG would kill the keeper
        # when a short-lived tool worker thread exits, even while the runner lives.
        argv = [bwrap, "--unshare-user", "--new-session", "--bind", "/", "/"]
        for root in self.roots:
            argv.extend(["--overlay-src", str(root), "--tmp-overlay", str(root)])
        # Only this trusted keeper sees the host tree; consumers build their
        # normal restricted bwrap view inside its mount namespace.
        with (
            tempfile.TemporaryDirectory(prefix="omnigent-cow-probe-") as probe_dir,
            tempfile.TemporaryFile(mode="w+") as errors,
        ):
            # Older tmpfs can mount as an upper layer but fail directory operations
            # later. Check user xattrs on a separate tmpfs before publishing readiness.
            argv.extend(
                [
                    "--tmpfs",
                    probe_dir,
                    "--",
                    sys.executable,
                    "-u",
                    "-m",
                    "omnigent.sandbox.copy_on_write",
                    "--keeper",
                    probe_dir,
                ]
            )
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                    env={"PATH": os.defpath},
                )
            except OSError as exc:
                raise copy_on_write_startup_error(f"Could not execute {bwrap}: {exc}") from exc
            try:
                assert process.stdout is not None
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=15):
                        raise copy_on_write_startup_error(
                            "Timed out creating copy-on-write mounts"
                        )
                line = process.stdout.readline().strip()
                if not line:
                    errors.seek(0)
                    raise copy_on_write_startup_error(
                        errors.read().strip() or "bwrap exited without reporting a mount namespace"
                    )
                pid = int(line)
                self._namespace = (
                    pid,
                    os.stat(f"/proc/{pid}/ns/user").st_ino,
                    os.stat(f"/proc/{pid}/ns/mnt").st_ino,
                )
                self._process = process
            except BaseException:
                self._stop(process)
                raise

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_tree(process)
            process.wait()
        if process.stdout is not None:
            process.stdout.close()

    def close(self) -> None:
        """Discard the overlay once its owning environment is torn down."""
        with self._lock:
            self._closed = True
            if self._process is not None:
                self._stop(self._process)
                self._process = None


def wrap_shared_namespace(argv: list[str], policy: SandboxPolicy) -> list[str]:
    """Enter the environment namespace before constructing a restricted sandbox."""
    namespace = policy.copy_on_write_namespace
    if namespace is None:
        raise RuntimeError("copy_on_write requires an owned, prepared OS environment")
    return [
        sys.executable,
        "-m",
        "omnigent.sandbox.copy_on_write",
        json.dumps(namespace),
        *argv,
    ]


def _run_keeper(probe_dir: str) -> None:
    """Publish readiness only after tmpfs xattrs work, then wait for owner EOF."""
    with tempfile.TemporaryFile(dir=probe_dir) as probe:
        os.setxattr(probe.fileno(), "user.omnigent_cow_probe", b"1")
    print(os.getpid(), flush=True)
    sys.stdin.buffer.read()


def parse_namespace(namespace: object) -> tuple[int, int, int]:
    """Validate the runtime handle at every serialization boundary."""
    if (
        not isinstance(namespace, list)
        or len(namespace) != 3
        or any(type(value) is not int or value <= 0 for value in namespace)
    ):
        raise ValueError("Invalid copy-on-write namespace")
    return cast(tuple[int, int, int], tuple(namespace))


def _enter(namespace: object, argv: list[str]) -> None:
    """Pin namespace descriptors before entry, then close them before exec."""
    pid, user_inode, mount_inode = parse_namespace(namespace)
    if not argv:
        raise ValueError("Missing copy-on-write command")
    descriptors: list[int] = []
    try:
        user = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY)
        descriptors.append(user)
        mount = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
        descriptors.append(mount)
        root = os.open(f"/proc/{pid}/root", os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(root)
        if os.fstat(user).st_ino != user_inode or os.fstat(mount).st_ino != mount_inode:
            raise RuntimeError("Copy-on-write environment was replaced")
        setns = cast(Callable[[int, int], None], getattr(os, "setns", None))
        setns(user, 0)
        setns(mount, 0)
        os.fchdir(root)
        os.chroot(".")
        os.chdir("/")
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    if sys.argv[1] == "--keeper":
        _run_keeper(sys.argv[2])
    else:
        _enter(json.loads(sys.argv[1]), sys.argv[2:])
