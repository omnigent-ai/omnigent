"""End-to-end regression: the runner and host daemon raise an inherited low soft fd limit.

On macOS the desktop app's host daemon and runner are launchd children and
inherit launchd's soft ``RLIMIT_NOFILE`` of 256 (hard unlimited). A long
orchestrator session holds far more descriptors than that, after which every
spawn fails with ``[Errno 24] Too many open files``.

Stand-in (environment fidelity)
-------------------------------
This CI host is Linux with an ambient soft limit of 65536, so the inherited
budget is recreated explicitly: each process is spawned with a ``-c`` bootstrap
that lowers its own soft limit to 256 (hard untouched) before the real
entrypoint runs, and its ``/proc/<pid>/limits`` is read once it is up. This
exercises the real startup paths, not launchd's inheritance mechanics, and the
hard ceiling is this host's 65536 rather than macOS's ``unlimited``.

Usage::

    pytest tests/e2e/test_process_startup_raises_soft_open_file_limit_e2e.py -v
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests._helpers.server_runner import server_runner

#: launchd's inherited soft fd limit on the reported macOS environment.
_INHERITED_SOFT_LIMIT = 256
_DAEMON_RAISE_DEADLINE_S = 60.0


def _bootstrap(entry_module: str) -> str:
    """Lower this process's soft ``RLIMIT_NOFILE`` to 256, then run *entry_module*'s ``main``."""
    return f"""
import resource
_soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, ({_INHERITED_SOFT_LIMIT}, _hard))
from {entry_module} import main

main()
"""


def _soft_nofile(pid: int) -> int:
    """Return the live process's soft ``Max open files`` from ``/proc/<pid>/limits``."""
    for line in Path(f"/proc/{pid}/limits").read_text().splitlines():
        if line.startswith("Max open files"):
            return int(line.split()[3])
    raise AssertionError(f"no 'Max open files' row in /proc/{pid}/limits")


_linux_only = pytest.mark.skipif(
    not Path("/proc/self/limits").exists(),
    reason="reads /proc/<pid>/limits; Linux stand-in for the macOS launchd limit",
)


@_linux_only
@pytest.mark.timeout(300)
def test_runner_raises_inherited_soft_open_file_limit(tmp_path: Path) -> None:
    """A runner that inherits a soft fd limit of 256 has raised it by the time it is online."""
    with server_runner(tmp_path) as stack:
        stack.start_runner(bootstrap=_bootstrap("omnigent.runner._entry"), python_args=[])
        assert stack.runner is not None
        soft = _soft_nofile(stack.runner.pid)

    assert soft > _INHERITED_SOFT_LIMIT, (
        f"the runner inherited a soft RLIMIT_NOFILE of {_INHERITED_SOFT_LIMIT} and never "
        f"raised it (soft is still {soft} after it came online)"
    )


@_linux_only
@pytest.mark.timeout(300)
def test_host_daemon_raises_inherited_soft_open_file_limit(tmp_path: Path) -> None:
    """A daemon that inherits a soft fd limit of 256 raises it during startup and stays up."""
    with server_runner(tmp_path) as stack:
        stack.start_host(bootstrap=_bootstrap("omnigent.host._daemon_entry"))
        host = stack.host
        assert host is not None

        def read_soft() -> int:
            try:
                return _soft_nofile(host.pid)
            except OSError:
                # /proc/<pid> vanishes once an exited daemon is reaped.
                assert host.poll() is None, (
                    f"host daemon exited with {host.returncode} before raising its "
                    f"soft open-file limit\n{stack.log_tail()}"
                )
                raise

        deadline = time.monotonic() + _DAEMON_RAISE_DEADLINE_S
        soft = read_soft()
        while soft <= _INHERITED_SOFT_LIMIT and time.monotonic() < deadline:
            assert host.poll() is None, (
                f"host daemon exited with {host.returncode} before raising its "
                f"soft open-file limit\n{stack.log_tail()}"
            )
            time.sleep(0.2)
            soft = read_soft()
        assert host.poll() is None, (
            f"host daemon exited with {host.returncode}\n{stack.log_tail()}"
        )

    assert soft > _INHERITED_SOFT_LIMIT, (
        f"the host daemon inherited a soft RLIMIT_NOFILE of {_INHERITED_SOFT_LIMIT} and "
        f"never raised it within {_DAEMON_RAISE_DEADLINE_S:.0f}s (soft is {soft})"
    )
