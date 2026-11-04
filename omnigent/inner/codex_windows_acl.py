"""Experimental Windows CODEX_HOME ACL guard (R6B_R8 review candidate).

Security boundary is conservative and fail-closed. Not production approved.
Native Win32 API; no third-party dependency and no subprocesses.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


class WindowsHomeSecurityError(OSError):
    pass


def verify_private_windows_directory(path: Path) -> None:
    """Reject non-private ACLs on an opened real directory; no ACL mutations.

    IMPORTANT: A caller reopening this path later does NOT retain this proof.
    For full TOCTOU safety, subsequent operations must bind to the same object.
    """
    if os.name != "nt":
        raise WindowsHomeSecurityError("Windows ACL guard called outside Windows")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    SE_FILE_OBJECT = 1
    OWNER_SECURITY_INFORMATION = 0x00000001
    DACL_SECURITY_INFORMATION = 0x00000004
    SE_DACL_PROTECTED = 0x1000
    FILE_READ_ATTRIBUTES = 0x80
    READ_CONTROL = 0x00020000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    ACL_SIZE_INFORMATION_CLASS = 2
    ACCESS_ALLOWED_ACE_TYPE = 0
    INHERITED_ACE = 0x10
    DELETE = 0x00010000
    READ_CONTROL_MASK = 0x00020000
    FILE_LIST_DIRECTORY = 0x00000001
    FILE_ADD_FILE = 0x00000002
    FILE_ADD_SUBDIRECTORY = 0x00000004
    FILE_READ_DATA = 0x00000001
    FILE_WRITE_DATA = 0x00000002
    HIGH_GENERIC = 0xF0000000

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", FILETIME),
                    ("accessed", FILETIME), ("modified", FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [("ace_count", wintypes.DWORD),
                    ("acl_bytes_in_use", wintypes.DWORD),
                    ("acl_bytes_free", wintypes.DWORD)]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TOKEN_USER_STRUCT(ctypes.Structure):
        _fields_ = [("user", SID_AND_ATTRIBUTES)]

    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    advapi32.GetSecurityInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                      ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(str(path), READ_CONTROL | FILE_READ_ATTRIBUTES, 7, None, 3,
                                  FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise WindowsHomeSecurityError("Cannot open CODEX_HOME directory for security inspection")
    sd = ctypes.c_void_p()
    token = ctypes.c_void_p()
    allocated_sids: list[ctypes.c_void_p] = []
    try:
        info = BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise WindowsHomeSecurityError("Cannot inspect directory identity")
        if not (info.attributes & FILE_ATTRIBUTE_DIRECTORY) or (info.attributes & FILE_ATTRIBUTE_REPARSE_POINT):
            raise WindowsHomeSecurityError("CODEX_HOME is not a real directory")
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        rc = advapi32.GetSecurityInfo(handle, SE_FILE_OBJECT,
                                      OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                                      ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd))
        if rc != 0 or not sd.value or not owner.value or not dacl.value:
            raise WindowsHomeSecurityError("CODEX_HOME has missing or unreadable owner/DACL")
        ctrl = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(sd, ctypes.byref(ctrl), ctypes.byref(revision)):
            raise WindowsHomeSecurityError("Cannot read DACL control flags")
        if not (ctrl.value & SE_DACL_PROTECTED):
            raise WindowsHomeSecurityError("CODEX_HOME DACL inheritance is not protected")
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            raise WindowsHomeSecurityError("Cannot open current user token")
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(needed))
        if needed.value < ctypes.sizeof(TOKEN_USER_STRUCT) or needed.value > 65536:
            raise WindowsHomeSecurityError("Invalid token user information size")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer, needed, ctypes.byref(needed)):
            raise WindowsHomeSecurityError("Cannot inspect current user SID")
        user_sid = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER_STRUCT)).contents.user.sid
        if not advapi32.EqualSid(owner, user_sid):
            raise WindowsHomeSecurityError("CODEX_HOME owned by unexpected user")
        allowed = [user_sid]
        for sid_text in ("S-1-3-4", "S-1-5-18", "S-1-5-32-544"):
            sid = ctypes.c_void_p()
            if not advapi32.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)) or not sid.value:
                raise WindowsHomeSecurityError("Cannot resolve allowed Windows SID")
            allocated_sids.append(sid)
            allowed.append(sid.value)
        acl_info = ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(dacl, ctypes.byref(acl_info),
                                           ctypes.sizeof(acl_info), ACL_SIZE_INFORMATION_CLASS):
            raise WindowsHomeSecurityError("Cannot enumerate DACL")
        if not 0 < acl_info.ace_count <= 64:
            raise WindowsHomeSecurityError("Invalid or empty DACL")
        owner_read = False
        for n in range(acl_info.ace_count):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, n, ctypes.byref(ace)) or not ace.value:
                raise WindowsHomeSecurityError("Cannot read DACL ACE")
            # Supported simple ACCESS_ALLOWED_ACE only: ACE_HEADER (4), mask (4), SID.
            header = ctypes.string_at(ace, 4)
            kind, flags, ace_size = header[0], header[1], int.from_bytes(header[2:4], "little")
            if kind != ACCESS_ALLOWED_ACE_TYPE or (flags & INHERITED_ACE) or ace_size < 20:
                raise WindowsHomeSecurityError("Unsupported, denied or inherited ACE")
            mask = int.from_bytes(ctypes.string_at(ace.value + 4, 4), "little")
            if mask & HIGH_GENERIC:
                raise WindowsHomeSecurityError("Generic access bits require mapping")
            ace_sid = ctypes.c_void_p(ace.value + 8)
            if not any(advapi32.EqualSid(ace_sid, sid) for sid in allowed):
                raise WindowsHomeSecurityError("Unauthorized CODEX_HOME principal")
            if (advapi32.EqualSid(ace_sid, user_sid) or advapi32.EqualSid(ace_sid, allowed[1])) and (mask & READ_CONTROL_MASK):
                owner_read = True
        if not owner_read:
            raise WindowsHomeSecurityError("CODEX_HOME owner access not established")
    finally:
        for sid in allocated_sids:
            kernel32.LocalFree(sid)
        if sd.value:
            kernel32.LocalFree(sd)
        if token.value:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def harden_private_windows_directory(path: Path) -> None:
    """Set a protected DACL on a newly created per-session Windows directory.

    R10_R5 isolated candidate: DACL applied to the same open directory handle.
    Parent path trust and later pathname-based file operations remain unresolved.
    Not approved for credential-bearing execution.
    """
    if os.name != "nt":
        raise WindowsHomeSecurityError("Windows ACL hardener called outside Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    p = Path(path)
    if p.is_symlink() or p.is_junction() or not p.is_dir():
        raise WindowsHomeSecurityError("Unsafe session directory before hardening")
    # Explicitly include current user's SID. OWNER_RIGHTS is not assumed to
    # substitute for the user's direct permissions in every application.
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    # Open and secure the exact directory object, not a separately re-resolved path.
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    advapi32.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetSecurityInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.SetSecurityInfo.restype = wintypes.DWORD
    kernel32.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attrs", wintypes.DWORD)]
    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("user", SID_AND_ATTRIBUTES)]
    token = ctypes.c_void_p()
    string_sid = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    dir_handle = ctypes.c_void_p()
    try:
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise WindowsHomeSecurityError("Cannot read current user token")
        length = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not ctypes.sizeof(TOKEN_USER) <= length.value <= 65536:
            raise WindowsHomeSecurityError("Invalid token user length")
        buf = ctypes.create_string_buffer(length.value)
        if not advapi32.GetTokenInformation(token, 1, buf, length, ctypes.byref(length)):
            raise WindowsHomeSecurityError("Cannot read user SID")
        sid = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents.user.sid
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(string_sid)):
            raise WindowsHomeSecurityError("Cannot convert user SID")
        sid_text = ctypes.wstring_at(string_sid)
        sddl = f"D:P(A;OICI;FA;;;{sid_text})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise WindowsHomeSecurityError("Cannot create protected DACL")
        # Get the DACL from the trusted, locally constructed SDDL descriptor.
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        new_dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                ctypes.byref(new_dacl), ctypes.byref(defaulted)) or not present.value or not new_dacl.value:
            raise WindowsHomeSecurityError("Cannot read generated private DACL")
        # WRITE_DAC | READ_CONTROL | FILE_READ_ATTRIBUTES; open the leaf itself.
        # FILE_FLAG_OPEN_REPARSE_POINT prevents silently following a leaf junction.
        dir_handle = ctypes.c_void_p(kernel32.CreateFileW(
            str(p), 0x00040000 | 0x00020000 | 0x80, 7, None, 3,
            0x02000000 | 0x00200000, None))
        if dir_handle.value in (None, ctypes.c_void_p(-1).value):
            raise WindowsHomeSecurityError("Cannot open session directory with WRITE_DAC")
        # Keep the same handle through the mutation; no SetFileSecurityW(path).
        rc = advapi32.SetSecurityInfo(dir_handle, 1, 0x4 | 0x80000000,
                                     None, None, new_dacl, None)
        if rc:
            raise WindowsHomeSecurityError(f"SetSecurityInfo denied private DACL (winerror={rc})")
    finally:
        if dir_handle.value: kernel32.CloseHandle(dir_handle)
        if descriptor.value: kernel32.LocalFree(descriptor)
        if string_sid.value: kernel32.LocalFree(string_sid)
        if token.value: kernel32.CloseHandle(token)
