"""Win32 capture resources; no import-time Office activation or process termination."""
from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import time
from ctypes import wintypes as w
from pathlib import Path
from typing import Any
from uuid import UUID

from scripts.diagnostics.ppt_capture.layout import CAPACITY, HEADER_SIZE, MAGIC, MAPPING_SIZE, SLOT_SIZE


class Native:
    """Own exact handles and primitive Win32 observations on Windows only."""

    def __init__(self) -> None:
        """Load fixed system libraries and declare handle-sized signatures."""
        if os.name != "nt":
            raise OSError("windows_required")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        self.b = ctypes.WinDLL("kernelbase", use_last_error=True)
        signatures = {
            "GetCurrentProcess": ([], w.HANDLE),
            "CreateFileMappingW": ([w.HANDLE, ctypes.c_void_p, w.DWORD, w.DWORD, w.DWORD, w.LPCWSTR], w.HANDLE),
            "MapViewOfFile": ([w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.c_size_t], ctypes.c_void_p),
            "UnmapViewOfFile": ([ctypes.c_void_p], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "GetProcessId": ([w.HANDLE], w.DWORD),
            "CreateEventW": ([ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
            "CreateMutexW": ([ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
            "SetEvent": ([w.HANDLE], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "GetProcessTimes": ([w.HANDLE, *([ctypes.POINTER(ctypes.c_ulonglong)] * 4)], w.BOOL),
            "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            "DuplicateHandle": ([w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD], w.BOOL),
            "QueryPerformanceFrequency": ([ctypes.POINTER(ctypes.c_longlong)], w.BOOL),
            "LocalFree": ([ctypes.c_void_p], ctypes.c_void_p),
            "GetVolumeInformationW": ([w.LPCWSTR, w.LPWSTR, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, w.LPWSTR, w.DWORD], w.BOOL),
            "GetDriveTypeW": ([w.LPCWSTR], w.UINT),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.k, name);function.argtypes = arguments;function.restype = result
        self.b.CompareObjectHandles.argtypes = [w.HANDLE, w.HANDLE];self.b.CompareObjectHandles.restype = w.BOOL
        self.u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.u.GetWindowThreadProcessId.restype = w.DWORD
        self.handles: list[int] = []
        self.view: int | None = None

    def keep(self, handle: int) -> int:
        """Adopt only a successful API-created handle."""
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handles.append(int(handle));return int(handle)

    def detach(self, handle: int) -> int:
        """Transfer one known owned handle to a worker's independent lifetime."""
        self.handles.remove(handle)
        return handle

    def security(self) -> Any:
        """Create a protected current-user/System/Admin DACL for anonymous resources."""
        import win32api
        import win32security
        import win32con
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        finally:
            token.Close()
        class Attributes(ctypes.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]
        pointer = ctypes.c_void_p()
        self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR,w.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
        if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;;GA;;;"+sid+")(A;;GA;;;SY)(A;;GA;;;BA)",1,ctypes.byref(pointer),None):
            raise ctypes.WinError(ctypes.get_last_error())
        return Attributes(ctypes.sizeof(Attributes),pointer,False)

    def event(self) -> int:
        """Create one protected unnamed manual-reset event."""
        security = self.security()
        try:return self.keep(self.k.CreateEventW(ctypes.byref(security),True,False,None))
        finally:self.k.LocalFree(security.descriptor)

    def mutex(self) -> int:
        """Reject concurrent diagnostic controllers without removing another run's lock."""
        security = self.security()
        try:
            handle = self.keep(self.k.CreateMutexW(ctypes.byref(security),False,"Local\\AegisDesk.PptCapture.v1"))
            if ctypes.get_last_error() == 183:raise RuntimeError("capture_busy")
            return handle
        finally:self.k.LocalFree(security.descriptor)

    def duplicate(self, handle: int, *, source: int | None = None, target: int | None = None, inherit: bool = False, rights: int | None = None) -> int:
        """Duplicate a known owned capability without requesting termination rights."""
        result=w.HANDLE();current=self.k.GetCurrentProcess()
        if not self.k.DuplicateHandle(source or current,handle,target or current,ctypes.byref(result),rights or 0,inherit,2 if rights is None else 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return self.keep(result.value) if target is None else int(result.value)

    def same_in_child(self, child: int, number: int, original: int) -> bool:
        """Prove the actual child received the exact intended kernel object."""
        copy=self.duplicate(number,source=child)
        return bool(self.b.CompareObjectHandles(copy,original))

    def signal(self, handle: int) -> None:
        """Signal a known owned event, preserving the immediate native failure."""
        if not self.k.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, handle: int, milliseconds: int = 0) -> bool:
        """Wait for a known capability with an explicit bounded timeout."""
        if not 0 <= milliseconds <= 3000:
            raise ValueError("wait_bound")
        result = self.k.WaitForSingleObject(handle, milliseconds)
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def owner(self, hwnd: int, pid: int) -> int:
        """Resolve one exact HWND's native owner thread without reading its title."""
        observed = w.DWORD()
        thread = self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(observed))
        if not thread or observed.value != pid:
            raise RuntimeError("window_owner_mismatch")
        return int(thread)

    def creation(self, handle: int) -> int:
        """Read the creation identity through a retained process handle."""
        values=[ctypes.c_ulonglong() for _ in range(4)]
        if not self.k.GetProcessTimes(handle,*[ctypes.byref(value) for value in values]):
            raise ctypes.WinError(ctypes.get_last_error())
        return values[0].value

    def mapping(self, run_id: str) -> int:
        """Allocate one anonymous, zero-initialized pagefile-backed append buffer."""
        security=self.security()
        try:handle=self.keep(self.k.CreateFileMappingW(w.HANDLE(-1),ctypes.byref(security),4,0,MAPPING_SIZE,None))
        finally:self.k.LocalFree(security.descriptor)
        self.view=self.k.MapViewOfFile(handle,2,0,0,MAPPING_SIZE)
        if not self.view:raise ctypes.WinError(ctypes.get_last_error())
        raw=bytearray(HEADER_SIZE);raw[:8]=MAGIC
        struct.pack_into("<6I",raw,8,3,1,CAPACITY,SLOT_SIZE,HEADER_SIZE,1);raw[32:48]=UUID(run_id).bytes_le
        frequency=ctypes.c_longlong()
        if not self.k.QueryPerformanceFrequency(ctypes.byref(frequency)):raise ctypes.WinError(ctypes.get_last_error())
        struct.pack_into("<q",raw,64,frequency.value);ctypes.memmove(self.view,bytes(raw),len(raw))
        return handle

    def bind_mapping(self, handle: int, pid: int, creation: int, owner: int) -> None:
        """Write the target binding before the host's one-shot Bind adoption."""
        if self.view is None:
            self.view=self.k.MapViewOfFile(handle,2,0,0,MAPPING_SIZE)
        if not self.view:raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.string_at(self.view,8)!=MAGIC or struct.unpack("<I",ctypes.string_at(self.view+88,4))[0]:
            raise RuntimeError("mapping_already_adopted")
        ctypes.memmove(self.view+48,struct.pack("<IIq",pid,owner,creation),16)

    def process_state(self, handle: int, binding: dict[str, Any]) -> dict[str, Any]:
        """Observe only the retained target, never an arbitrary matching process name."""
        wait=int(self.k.WaitForSingleObject(handle,0))
        if wait not in (0,258):raise ctypes.WinError(ctypes.get_last_error())
        matches=self.creation(handle)==binding["creation"]
        code=w.DWORD();has_code=bool(self.k.GetExitCodeProcess(handle,ctypes.byref(code))) if wait==0 else False
        return dict(binding,event="target_state",qpc_ns=time.perf_counter_ns(),retained_handle=True,
                    identity_matches=matches,wait_result=wait,exit_code=int(code.value) if has_code else None)

    def close(self) -> None:
        """Close owned observations only, without terminating any child or Office."""
        if self.view:self.k.UnmapViewOfFile(self.view);self.view=None
        for handle in reversed(self.handles):self.k.CloseHandle(handle)
        self.handles.clear()


def start_child(arguments: list[str], inherited: list[int], **options: Any) -> subprocess.Popen:
    """Start an explicit helper with only the declared inherited handles."""
    startup=subprocess.STARTUPINFO();startup.lpAttributeList={"handle_list":inherited}
    return subprocess.Popen(arguments,startupinfo=startup,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW,**options)
