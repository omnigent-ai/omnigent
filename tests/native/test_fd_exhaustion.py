"""``fd_exhaustion_errno`` classifies fd-table exhaustion and nothing else."""

from __future__ import annotations

import errno

import httpx

from omnigent.native.fd_exhaustion import fd_exhaustion_errno


def test_direct_emfile_and_enfile() -> None:
    assert fd_exhaustion_errno(OSError(errno.EMFILE, "too many")) == errno.EMFILE
    assert fd_exhaustion_errno(OSError(errno.ENFILE, "table full")) == errno.ENFILE


def test_wrapped_cause_chain_is_classified() -> None:
    # httpx wraps the socket-level OSError via ``raise ... from exc``.
    outer = httpx.ConnectError("all connection attempts failed")
    outer.__cause__ = OSError(errno.EMFILE, "too many open files")
    assert fd_exhaustion_errno(outer) == errno.EMFILE


def test_other_errors_are_not_classified() -> None:
    assert fd_exhaustion_errno(OSError(errno.EACCES, "denied")) is None
    assert fd_exhaustion_errno(ValueError("nope")) is None


def test_implicit_context_is_not_classified() -> None:
    # An unrelated error raised WHILE HANDLING an fd failure is a real bug.
    try:
        try:
            raise OSError(errno.EMFILE, "too many open files")
        except OSError:
            raise ValueError("unrelated failure during handling") from None
    except ValueError as exc:
        assert fd_exhaustion_errno(exc) is None


def test_cause_cycle_is_bounded() -> None:
    first, second = ValueError("a"), ValueError("b")
    first.__cause__, second.__cause__ = second, first
    assert fd_exhaustion_errno(first) is None
