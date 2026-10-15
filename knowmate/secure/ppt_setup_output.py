"""Private, delete-on-last-close stdout storage for detached setup helpers."""
from __future__ import annotations

import ctypes
import os
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO


def create_setup_output() -> BinaryIO:
    """Create stdout atomically with a private Windows ACL and no sharing."""
    if os.name != "nt":
        return tempfile.TemporaryFile(mode="w+b")
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    security.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    security.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [ctypes.c_wchar_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]

    token, sid_text, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    handle = None
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = ctypes.c_uint32()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise OSError("setup_output_token_unavailable")
        user = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, user, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        user_sid = ctypes.wstring_at(sid_text)
        sddl = f"O:{user_sid}D:P(A;;GA;;;{user_sid})(A;;GA;;;BA)(A;;GA;;;SY)"
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1,
                ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("descriptor", ctypes.c_void_p), ("inherit", ctypes.c_int)]

        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(SecurityAttributes), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        for _ in range(3):
            path = Path(tempfile.gettempdir()) / ("aegis-ppt-setup-" + uuid.uuid4().hex + ".tmp")
            # GENERIC_READ|GENERIC_WRITE|DELETE, share=0, CREATE_NEW,
            # FILE_ATTRIBUTE_TEMPORARY|FILE_FLAG_DELETE_ON_CLOSE. DuplicateHandle
            # for helper stdout preserves this object without reopening its path.
            handle = kernel.CreateFileW(str(path), 0xC0010000, 0, ctypes.byref(attributes),
                1, 0x04000100, None)
            if handle != ctypes.c_void_p(-1).value:
                break
            handle = None
            error = ctypes.get_last_error()
            if error not in (80, 183):
                raise ctypes.WinError(error)
        if handle is None:
            raise OSError("setup_output_create_collision")
        descriptor_fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        handle = None  # the CRT descriptor now owns the native handle
        try:
            return os.fdopen(descriptor_fd, "w+b")
        except BaseException:
            os.close(descriptor_fd)
            raise
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_text.value:
            kernel.LocalFree(sid_text)
        if token.value:
            kernel.CloseHandle(token)
