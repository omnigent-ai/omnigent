"""Client-lifetime static-key files with native Windows access control."""

from __future__ import annotations

import ctypes
import logging
import os
import secrets
import tempfile
from collections.abc import Callable
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SECURITY_DESCRIPTOR_REVISION = 1
_FILE_OBJECT = 1
_OWNER_AND_DACL_INFORMATION = 0x00000005
_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE = 0
_FILE_ALL_ACCESS = 0x001F01FF
_OBJECT_AND_CONTAINER_INHERIT = 0x03
_GENERIC_WRITE = 0x40000000
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x80


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("descriptor", ctypes.c_void_p),
        ("inherit_handle", wintypes.BOOL),
    ]


class _AclSize(ctypes.Structure):
    _fields_ = [
        ("ace_count", wintypes.DWORD),
        ("bytes_used", wintypes.DWORD),
        ("bytes_free", wintypes.DWORD),
    ]


class _AllowedAce(ctypes.Structure):
    _fields_ = [
        ("kind", wintypes.BYTE),
        ("flags", wintypes.BYTE),
        ("size", wintypes.WORD),
        ("mask", wintypes.DWORD),
        ("sid_start", wintypes.DWORD),
    ]


class _WindowsOwnerAcl:
    """Create and verify a protected DACL granting only the current token's user."""

    def __init__(self) -> None:
        load_library = cast(Callable[..., ctypes.CDLL], vars(ctypes)["WinDLL"])
        self.kernel = load_library("kernel32", use_last_error=True)
        self.security = load_library("advapi32", use_last_error=True)
        pointer = ctypes.c_void_p
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.LocalFree.argtypes = [pointer]
        self.kernel.LocalFree.restype = pointer
        self.kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, pointer]
        self.kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            pointer,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.WriteFile.argtypes = [
            wintypes.HANDLE,
            pointer,
            wintypes.DWORD,
            pointer,
            pointer,
        ]
        self.security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, pointer]
        self.security.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            pointer,
            wintypes.DWORD,
            pointer,
        ]
        self.security.ConvertSidToStringSidW.argtypes = [pointer, pointer]
        self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            pointer,
            pointer,
        ]
        self.security.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            pointer,
            pointer,
            pointer,
            pointer,
            pointer,
        ]
        self.security.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.security.GetSecurityInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            pointer,
            pointer,
            pointer,
            pointer,
            pointer,
        ]
        self.security.GetSecurityInfo.restype = wintypes.DWORD
        self.security.GetSecurityDescriptorControl.argtypes = [pointer, pointer, pointer]
        self.security.GetAclInformation.argtypes = [pointer, pointer, wintypes.DWORD, ctypes.c_int]
        self.security.GetAce.argtypes = [pointer, wintypes.DWORD, pointer]
        self.security.EqualSid.argtypes = [pointer, pointer]

        token = wintypes.HANDLE()
        self._check(
            self.security.OpenProcessToken(
                self.kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
            )
        )
        try:
            length = wintypes.DWORD()
            self.security.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(length))
            self._token_info = ctypes.create_string_buffer(length.value)
            self._check(
                self.security.GetTokenInformation(
                    token, _TOKEN_USER, self._token_info, length, ctypes.byref(length)
                )
            )
            self.sid = ctypes.cast(self._token_info, ctypes.POINTER(pointer))[0]
            text = pointer()
            self._check(self.security.ConvertSidToStringSidW(self.sid, ctypes.byref(text)))
            try:
                self.sid_text = ctypes.wstring_at(text)
            finally:
                self.kernel.LocalFree(text)
        finally:
            self.kernel.CloseHandle(token)

    @staticmethod
    def _check(result: int) -> None:
        if not result:
            raise _windows_error()

    def create_directory(self, path: Path) -> None:
        """Install the protected, inheritable owner DACL during directory creation."""
        descriptor = ctypes.c_void_p()
        sddl = f"O:{self.sid_text}D:P(A;OICI;FA;;;{self.sid_text})"
        self._check(
            self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, _SECURITY_DESCRIPTOR_REVISION, ctypes.byref(descriptor), None
            )
        )
        try:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            self._check(self.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)))
        finally:
            self.kernel.LocalFree(descriptor)

    def verify(self, path: Path, *, directory: bool, handle: int | None = None) -> None:
        """Read back owner and ACL; unsupported or permissive filesystems fail closed."""
        descriptor, owner, acl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        query = (
            self.security.GetNamedSecurityInfoW
            if handle is None
            else self.security.GetSecurityInfo
        )
        result = query(
            str(path) if handle is None else handle,
            _FILE_OBJECT,
            _OWNER_AND_DACL_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(acl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise _windows_error(result)
        try:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            self._check(
                self.security.GetSecurityDescriptorControl(
                    descriptor, ctypes.byref(control), ctypes.byref(revision)
                )
            )
            if not owner or not acl or not self.security.EqualSid(owner, self.sid):
                raise OSError("Private key file has an unexpected owner or DACL")
            if not control.value & _DACL_PROTECTED:
                raise OSError("Private key storage inherits external permissions")
            size = _AclSize()
            self._check(
                self.security.GetAclInformation(
                    acl, ctypes.byref(size), ctypes.sizeof(size), _ACL_SIZE_INFORMATION
                )
            )
            if size.ace_count != 1:
                raise OSError("Private key file grants unexpected access")
            ace = ctypes.c_void_p()
            self._check(self.security.GetAce(acl, 0, ctypes.byref(ace)))
            allowed = ctypes.cast(ace, ctypes.POINTER(_AllowedAce)).contents
            assert ace.value is not None
            ace_sid = ace.value + _AllowedAce.sid_start.offset
            if (
                allowed.kind != _ACCESS_ALLOWED_ACE
                or allowed.mask != _FILE_ALL_ACCESS
                or not self.security.EqualSid(ace_sid, self.sid)
            ):
                raise OSError("Private key file grants unexpected access")
            if allowed.flags != (_OBJECT_AND_CONTAINER_INHERIT if directory else 0):
                raise OSError("Private key storage has unexpected inheritance flags")
        finally:
            self.kernel.LocalFree(descriptor)

    def write_file(self, path: Path, data: bytes) -> None:
        """Exclusively create and verify the owner-only file before writing the key."""
        descriptor = ctypes.c_void_p()
        sddl = f"O:{self.sid_text}D:P(A;;FA;;;{self.sid_text})"
        self._check(
            self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, _SECURITY_DESCRIPTOR_REVISION, ctypes.byref(descriptor), None
            )
        )
        try:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            handle = self.kernel.CreateFileW(
                str(path),
                _GENERIC_WRITE,
                0,
                ctypes.byref(attributes),
                _CREATE_NEW,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise _windows_error()
            try:
                self.verify(path, directory=False, handle=handle)
                written = wintypes.DWORD()
                self._check(
                    self.kernel.WriteFile(handle, data, len(data), ctypes.byref(written), None)
                )
                if written.value != len(data):
                    raise OSError("Private key file write was incomplete")
            finally:
                self.kernel.CloseHandle(handle)
        finally:
            self.kernel.LocalFree(descriptor)


def _windows_error(code: int | None = None) -> OSError:
    get_last_error = cast(Callable[[], int], vars(ctypes)["get_last_error"])
    win_error = cast(Callable[[int], OSError], vars(ctypes)["WinError"])
    return win_error(get_last_error() if code is None else code)


class PrivateStaticKeyFile:
    """Keep a client-owned key out of child arguments, settings, and environment."""

    def __init__(self, secret: str) -> None:
        self.directory = (
            Path(tempfile.gettempdir()) / f"omnigent-claude-key-{secrets.token_hex(16)}"
        )
        self.path = self.directory / "key"
        created = False
        try:
            acl = _WindowsOwnerAcl() if os.name == "nt" else None
            if acl is not None:
                acl.create_directory(self.directory)
            else:
                self.directory.mkdir(mode=0o700)
            created = True
            if acl is not None:
                acl.verify(self.directory, directory=True)
                acl.write_file(self.path, secret.encode("utf-8"))
            else:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(secret.encode("utf-8"))
        except (OSError, UnicodeError):
            if created:
                self.close()
            raise RuntimeError("Could not create a private Claude SDK credential file") from None
        except BaseException:
            if created:
                self.close()
            raise

    def close(self) -> None:
        """Try removal after CLI exit without interrupting independent cleanup."""
        try:
            self.path.unlink(missing_ok=True)
            with suppress(FileNotFoundError):
                self.directory.rmdir()
        except OSError:
            logger.warning(
                "Could not remove private Claude SDK credential storage at %s; storage may remain",
                self.directory,
            )
