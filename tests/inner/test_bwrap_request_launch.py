"""Request launcher construction with native exports and activation replaced."""

from __future__ import annotations

import errno
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omnigent.inner import _seccomp, bwrap_sandbox
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.sandbox import copy_on_write

pytestmark = pytest.mark.posix_only


@pytest.fixture
def request_policy() -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=False,
        cwd_allow_hidden=[],
        bind_host_proc=False,
    )


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    def write_program(_rules: object, descriptor: int) -> None:
        os.write(descriptor, b"\0" * 8)

    export = MagicMock(side_effect=write_program)
    monkeypatch.setattr(bwrap_sandbox, "export_seccomp_filter", export)
    monkeypatch.setattr(
        _seccomp,
        "_load_libseccomp",
        lambda: pytest.fail("Launcher component tests must not load libseccomp"),
    )
    return export


@pytest.fixture
def request_fds() -> Iterator[bwrap_sandbox.BwrapRequestFds]:
    with ExitStack() as resources:
        info = resources.enter_context(tempfile.TemporaryFile())
        baseline = resources.enter_context(tempfile.TemporaryFile())
        hardening = resources.enter_context(tempfile.TemporaryFile())
        yield bwrap_sandbox.BwrapRequestFds(info.fileno(), baseline.fileno(), hardening.fileno())


@pytest.mark.parametrize("shared_mounts", [False, True])
def test_request_launch_preserves_existing_mounts_and_the_exact_active_filter_rules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request_policy: SandboxPolicy,
    request_fds: bwrap_sandbox.BwrapRequestFds,
    exporter: MagicMock,
    shared_mounts: bool,
) -> None:
    backend = bwrap_sandbox.BwrapSandboxBackend()
    active_filters: list[list[_seccomp.SeccompRule]] = []
    monkeypatch.setattr(bwrap_sandbox, "_set_no_new_privs", lambda: None)
    monkeypatch.setattr(
        _seccomp, "apply_seccomp_filter", lambda rules: active_filters.append(rules)
    )
    monkeypatch.setattr(
        bwrap_sandbox, "apply_seccomp_filter", lambda rules: active_filters.append(rules)
    )
    backend.activate(request_policy)
    assert len(active_filters) == 2
    assert tuple(rule.syscall for rule in active_filters[0]) == _seccomp.BASELINE_DENYLIST_SYSCALLS
    assert all(
        rule.action == _seccomp.scmp_act_errno(errno.EPERM) and not rule.arg_filters
        for rule in active_filters[0]
    )

    mounted: list[list[str]] = []

    def shared(command: list[str], policy: SandboxPolicy) -> list[str]:
        assert policy is request_policy
        mounted.append(command)
        return ["shared-mounts", *command]

    if shared_mounts:
        request_policy.copy_on_write_roots = [tmp_path]
        monkeypatch.setattr(copy_on_write, "wrap_shared_namespace", shared)
    init_argv = [sys.executable, "-I", "-S", "-c", "pass"]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    old = backend.wrap_launcher_argv(
        init_argv, request_policy, tmp_path, chdir=scratch, target="/bin/sh"
    )
    exporter.assert_not_called()
    actual = backend.wrap_launcher_argv(
        init_argv,
        request_policy,
        tmp_path,
        chdir=scratch,
        target="/bin/sh",
        _request_fds=request_fds,
    )
    assert [(call.args[0], call.args[1]) for call in exporter.call_args_list] == [
        (active_filters[0], request_fds.baseline_fd),
        (active_filters[1], request_fds.hardening_fd),
    ]
    for descriptor in (request_fds.baseline_fd, request_fds.hardening_fd):
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        assert os.fstat(descriptor).st_size == 8
    options = [
        "--as-pid-1",
        "--info-fd",
        str(request_fds.info_fd),
        "--add-seccomp-fd",
        str(request_fds.baseline_fd),
        "--add-seccomp-fd",
        str(request_fds.hardening_fd),
    ]
    start = actual.index("--as-pid-1")
    assert actual[start : start + len(options)] == options
    assert actual[:start] + actual[start + len(options) :] == old
    assert "--as-pid-1" not in old and "--add-seccomp-fd" not in old
    if shared_mounts:
        assert len(mounted) == 2 and "--as-pid-1" in mounted[-1]


@pytest.mark.parametrize(
    ("failure", "expected_exports"), [("baseline", 1), ("hardening", 2), ("rewind", 2)]
)
def test_filter_export_failure_cannot_return_a_partially_hardened_launcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request_policy: SandboxPolicy,
    request_fds: bwrap_sandbox.BwrapRequestFds,
    exporter: MagicMock,
    failure: str,
    expected_exports: int,
) -> None:
    if failure == "rewind":
        seek = os.lseek

        def rewind(descriptor: int, offset: int, whence: int) -> int:
            if descriptor == request_fds.hardening_fd and whence == os.SEEK_SET:
                raise OSError("Program rewind failed")
            return seek(descriptor, offset, whence)

        monkeypatch.setattr(bwrap_sandbox.os, "lseek", rewind)
    else:
        failure_fd = getattr(request_fds, failure + "_fd")

        def export(_rules: object, descriptor: int) -> None:
            if descriptor == failure_fd:
                raise OSError("Filter export failed")

        exporter.side_effect = export
    shared = MagicMock()
    monkeypatch.setattr(copy_on_write, "wrap_shared_namespace", shared)
    request_policy.copy_on_write_roots = [tmp_path]
    message = "Program rewind failed" if failure == "rewind" else "Filter export failed"
    with pytest.raises(OSError, match=message):
        bwrap_sandbox.BwrapSandboxBackend().wrap_launcher_argv(
            [sys.executable, "-c", "pass"], request_policy, tmp_path, _request_fds=request_fds
        )
    assert exporter.call_count == expected_exports
    shared.assert_not_called()


@pytest.mark.parametrize("change", [{"active": False}, {"backend_type": "none"}])
def test_request_mode_requires_the_concrete_active_backend(
    tmp_path: Path,
    request_policy: SandboxPolicy,
    request_fds: bwrap_sandbox.BwrapRequestFds,
    exporter: MagicMock,
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="active linux_bwrap"):
        bwrap_sandbox.BwrapSandboxBackend().wrap_launcher_argv(
            [sys.executable, "-c", "pass"],
            replace(request_policy, **change),
            tmp_path,
            _request_fds=request_fds,
        )
    exporter.assert_not_called()


@pytest.mark.parametrize("problem", ["prefix", "offset", "aliased-file", "non-file"])
def test_program_files_must_be_empty_and_independently_owned(
    tmp_path: Path,
    request_policy: SandboxPolicy,
    request_fds: bwrap_sandbox.BwrapRequestFds,
    exporter: MagicMock,
    problem: str,
) -> None:
    with ExitStack() as resources:
        if problem == "prefix":
            os.write(request_fds.baseline_fd, b"prefix")
            os.lseek(request_fds.baseline_fd, 0, os.SEEK_SET)
        elif problem == "offset":
            os.lseek(request_fds.baseline_fd, 1, os.SEEK_SET)
        elif problem == "aliased-file":
            duplicate = resources.enter_context(os.fdopen(os.dup(request_fds.baseline_fd), "r+b"))
            request_fds = replace(request_fds, hardening_fd=duplicate.fileno())
        else:
            directory = os.open(tmp_path, os.O_RDONLY)
            resources.callback(os.close, directory)
            request_fds = replace(request_fds, baseline_fd=directory)
        with pytest.raises(ValueError, match="Request filters require"):
            bwrap_sandbox.BwrapSandboxBackend().wrap_launcher_argv(
                [sys.executable, "-c", "pass"], request_policy, tmp_path, _request_fds=request_fds
            )
    exporter.assert_not_called()


@pytest.mark.parametrize(
    "descriptors", [(101, 101, 103), (-1, 102, 103), (101, 102, 2_147_483_648)]
)
def test_request_descriptors_cannot_alias_or_overflow_the_launcher(
    descriptors: tuple[int, int, int],
) -> None:
    with pytest.raises(ValueError, match="distinct C file descriptors"):
        bwrap_sandbox.BwrapRequestFds(*descriptors)
