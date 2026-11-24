"""Filter construction and cBPF export without installing filters."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import struct
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

from omnigent.inner import _seccomp as seccomp

_CONTEXT = 421
_RULES = (
    seccomp.SeccompRule("unshare", seccomp.scmp_act_errno(errno.EPERM)),
    seccomp.SeccompRule(
        "clone",
        seccomp.scmp_act_errno(errno.ENOSYS),
        (seccomp.SeccompArgFilter(0, seccomp.SCMP_CMP_MASKED_EQ, 8, 8),),
    ),
    seccomp.SeccompRule("unknown_syscall", seccomp.scmp_act_errno(errno.EPERM)),
)


@pytest.fixture
def filter_library(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    library = MagicMock(
        spec=[
            "seccomp_init",
            "seccomp_arch_resolve_name",
            "seccomp_arch_add",
            "seccomp_syscall_resolve_name",
            "seccomp_rule_add",
            "seccomp_rule_add_array",
            "seccomp_load",
            "seccomp_export_bpf",
            "seccomp_release",
        ]
    )
    library.seccomp_init.return_value = _CONTEXT
    library.seccomp_arch_resolve_name.side_effect = {
        b"x86": 29,
        b"x32": 30,
        b"arm": 31,
    }.get
    library.seccomp_syscall_resolve_name.side_effect = lambda name: {
        b"unshare": 11,
        b"clone": 12,
    }.get(name, -1)
    for name in (
        "seccomp_arch_add",
        "seccomp_rule_add",
        "seccomp_rule_add_array",
        "seccomp_load",
        "seccomp_export_bpf",
    ):
        getattr(library, name).return_value = 0
    monkeypatch.setattr(seccomp, "_load_libseccomp", lambda: library)
    monkeypatch.setattr(seccomp.platform, "machine", lambda: "x86_64")
    return library


@pytest.mark.parametrize(
    ("machine", "architectures"),
    [("x86_64", [29, 30]), ("aarch64", [31]), ("riscv64", [])],
)
@pytest.mark.parametrize(
    "default_action", [seccomp.SCMP_ACT_ALLOW, seccomp.scmp_act_errno(errno.EACCES)]
)
def test_export_preserves_filter_rules_without_loading_the_parent(
    monkeypatch: pytest.MonkeyPatch,
    filter_library: MagicMock,
    machine: str,
    architectures: list[int],
    default_action: int,
) -> None:
    monkeypatch.setattr(seccomp.platform, "machine", lambda: machine)
    library = filter_library
    for operation in ("load", "export"):
        library.reset_mock()
        if operation == "load":
            seccomp.apply_seccomp_filter(_RULES, default_action=default_action)
            library.seccomp_load.assert_called_once_with(_CONTEXT)
            library.seccomp_export_bpf.assert_not_called()
        else:
            seccomp.export_seccomp_filter(_RULES, 37, default_action=default_action)
            library.seccomp_load.assert_not_called()
            library.seccomp_export_bpf.assert_called_once()
            exported_context, descriptor = library.seccomp_export_bpf.call_args.args
            assert exported_context == _CONTEXT and descriptor.value == 37
            assert library.seccomp_export_bpf.argtypes == [ctypes.c_void_p, ctypes.c_int]
            assert library.seccomp_export_bpf.restype is ctypes.c_int

        assert library.seccomp_init.call_args.args[0].value == default_action
        assert [
            call.args[1].value for call in library.seccomp_arch_add.call_args_list
        ] == architectures
        library.seccomp_rule_add.assert_called_once()
        context, action, number, count = library.seccomp_rule_add.call_args.args
        assert (context, action.value, number.value, count.value) == (
            _CONTEXT,
            seccomp.scmp_act_errno(errno.EPERM),
            11,
            0,
        )
        library.seccomp_rule_add_array.assert_called_once()
        context, action, number, count, arguments = library.seccomp_rule_add_array.call_args.args
        assert (context, action.value, number.value, count.value) == (
            _CONTEXT,
            seccomp.scmp_act_errno(errno.ENOSYS),
            12,
            1,
        )
        assert [(item.arg, item.op, item.datum_a, item.datum_b) for item in arguments] == [
            (0, seccomp.SCMP_CMP_MASKED_EQ, 8, 8)
        ]
        library.seccomp_release.assert_called_once_with(_CONTEXT)


@pytest.mark.parametrize(
    ("failure", "expected_errno"),
    [
        ("init", errno.ENOMEM),
        ("architecture-name", errno.ENOTSUP),
        ("architecture-add", errno.EINVAL),
        ("rule", errno.EINVAL),
        ("argument-rule", errno.EINVAL),
        ("export", errno.EBADF),
        ("export-symbol", errno.ENOSYS),
    ],
)
def test_export_failure_releases_its_context_without_loading_a_filter(
    filter_library: MagicMock, failure: str, expected_errno: int
) -> None:
    library = filter_library
    if failure == "init":
        library.seccomp_init.return_value = None
    elif failure == "architecture-name":
        library.seccomp_arch_resolve_name.side_effect = None
        library.seccomp_arch_resolve_name.return_value = 0
    elif failure == "architecture-add":
        library.seccomp_arch_add.return_value = -errno.EINVAL
    elif failure == "rule":
        library.seccomp_rule_add.return_value = -errno.EINVAL
    elif failure == "argument-rule":
        library.seccomp_rule_add_array.return_value = -errno.EINVAL
    elif failure == "export":
        library.seccomp_export_bpf.return_value = -errno.EBADF
    else:
        del library.seccomp_export_bpf

    with pytest.raises(OSError) as caught:
        seccomp.export_seccomp_filter(_RULES, 37)
    assert caught.value.errno == expected_errno
    library.seccomp_load.assert_not_called()
    if failure == "init":
        library.seccomp_release.assert_not_called()
    else:
        library.seccomp_release.assert_called_once_with(_CONTEXT)


def test_existing_filter_load_needs_no_export_symbol(filter_library: MagicMock) -> None:
    del filter_library.seccomp_export_bpf
    seccomp.apply_seccomp_filter(_RULES)
    filter_library.seccomp_load.assert_called_once_with(_CONTEXT)
    filter_library.seccomp_release.assert_called_once_with(_CONTEXT)


def test_filter_load_failure_still_releases_the_context(filter_library: MagicMock) -> None:
    filter_library.seccomp_load.return_value = -errno.EPERM
    with pytest.raises(OSError) as caught:
        seccomp.apply_seccomp_filter(_RULES)
    assert caught.value.errno == errno.EPERM
    filter_library.seccomp_release.assert_called_once_with(_CONTEXT)


@pytest.mark.parametrize("descriptor", [-1, 2_147_483_648])
def test_export_rejects_a_descriptor_that_cannot_reach_the_c_abi(
    filter_library: MagicMock, descriptor: int
) -> None:
    with pytest.raises(ValueError, match="file descriptor"):
        seccomp.export_seccomp_filter(_RULES, descriptor)
    filter_library.seccomp_init.assert_not_called()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="libseccomp is Linux-only")
def test_export_writes_real_bpf_to_a_caller_owned_file(monkeypatch: pytest.MonkeyPatch) -> None:
    if ctypes.util.find_library("seccomp") is None:
        pytest.skip("libseccomp is not installed")
    library = seccomp._load_libseccomp()
    load = MagicMock(side_effect=AssertionError("An export must never install a filter"))
    monkeypatch.setattr(library, "seccomp_load", load)
    monkeypatch.setattr(seccomp, "_load_libseccomp", lambda: library)

    with tempfile.TemporaryFile() as output:
        descriptor = output.fileno()
        seccomp.export_seccomp_filter(_RULES, descriptor)
        written = output.tell()
        assert written > 0 and written % struct.calcsize("=HBBI") == 0
        output.seek(0)
        assert len(output.read()) == written
        assert output.fileno() == descriptor
    load.assert_not_called()
