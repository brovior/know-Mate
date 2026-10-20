"""COM-free OS snapshots and separately bounded WCT observation workers."""
from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes as w
from pathlib import Path
from typing import Any, Callable


class ProcessEntry(ctypes.Structure):
    """Native Toolhelp process entry containing only executable names."""
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD), ("name", w.WCHAR * 260)]


def processes(native: Any) -> list[dict[str, Any]]:
    """Enumerate a bounded process snapshot without COM or document/window text."""
    kernel = native.k
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
        rows = []
        while ok:
            if len(rows) >= 65536:
                raise RuntimeError("process_snapshot_bound")
            rows.append(dict(pid=int(entry.pid), name=entry.name.lower(), threads=int(entry.threads)))
            ok = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
        return rows
    finally:
        kernel.CloseHandle(handle)


def snapshot(native: Any, handle: int, binding: dict[str, Any]) -> dict[str, Any]:
    """Read target CPU/thread and class/owner/visibility metadata, excluding titles."""
    pid = binding["pid"]
    if native.creation(handle) != binding["creation"]:
        raise RuntimeError("snapshot_foreign_target")
    rows = processes(native)
    own = next((row for row in rows if row["pid"] == pid), None)
    windows: list[dict[str, Any]] = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user = native.u
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetWindow.argtypes = [w.HWND, w.UINT]
    user.GetWindow.restype = w.HWND
    user.IsWindowVisible.argtypes = [w.HWND]
    user.IsWindowEnabled.argtypes = [w.HWND]
    def visit(hwnd: int, parameter: int) -> bool:
        observed = w.DWORD()
        thread = user.GetWindowThreadProcessId(hwnd, ctypes.byref(observed))
        if observed.value == pid and len(windows) < 256:
            name = ctypes.create_unicode_buffer(256)
            if user.GetClassNameW(hwnd, name, len(name)):
                windows.append(dict(class_name=name.value, owner_thread=int(thread),
                                    has_owner=bool(user.GetWindow(hwnd, 4)),
                                    visible=bool(user.IsWindowVisible(hwnd)), enabled=bool(user.IsWindowEnabled(hwnd))))
        return True
    callback = callback_type(visit)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    if not user.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    values = [ctypes.c_ulonglong() for _ in range(4)]
    if not native.k.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
        raise ctypes.WinError(ctypes.get_last_error())
    return dict(event="os_snapshot", threads=None if own is None else own["threads"],
                kernel_100ns=values[2].value, user_100ns=values[3].value, windows=windows,
                loaded_modules=modules(native, pid))


def modules(native: Any, pid: int) -> dict[str, Any]:
    """Collect only fixed module names/versions; managed loader absence stays unknown."""
    from knowmate.secure.ppt_capture_preflight import file_version
    class Entry(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("module", w.DWORD), ("pid", w.DWORD),
                    ("global_usage", w.DWORD), ("process_usage", w.DWORD),
                    ("base", ctypes.c_void_p), ("bytes", w.DWORD), ("handle", w.HMODULE),
                    ("name", w.WCHAR * 256), ("path", w.WCHAR * 260)]
    kernel = native.k
    kernel.Module32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Module32NextW.argtypes = kernel.Module32FirstW.argtypes
    handle = kernel.CreateToolhelp32Snapshot(0x18, pid)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    allow = {"powerpnt.exe", "clr.dll", "mscoree.dll", "mso.dll", "aegisdesk.pptreader.dll", "aegisdesk.pptreader.events.dll"}
    rows: list[dict[str, Any]] = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        ok, count = kernel.Module32FirstW(handle, ctypes.byref(entry)), 0
        while ok:
            count += 1
            if count > 4096:
                raise RuntimeError("module_snapshot_bound")
            name = entry.name.lower()
            if name in allow:
                try:
                    rows.append(dict(name=name, version=file_version(Path(entry.path))))
                except Exception as error:
                    rows.append(dict(name=name, version=None, error_type=type(error).__name__))
            ok = kernel.Module32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)
    return dict(scope="win32_toolhelp_allowlist", available=True, modules=rows, absent_module_interpretation="unknown")


def wait_chain(owner_thread: int) -> dict[str, Any]:
    """Copy bounded WCT numeric statuses; never expose lock object names."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    class Lock(ctypes.Structure):
        _fields_ = [("name", w.WCHAR * 128), ("timeout", ctypes.c_longlong), ("alertable", w.BOOL)]
    class Thread(ctypes.Structure):
        _fields_ = [("pid", w.DWORD), ("tid", w.DWORD), ("wait_ms", w.DWORD), ("switches", w.DWORD)]
    class Data(ctypes.Union):
        _fields_ = [("lock", Lock), ("thread", Thread)]
    class Node(ctypes.Structure):
        _fields_ = [("kind", ctypes.c_int), ("status", ctypes.c_int), ("data", Data)]
    advapi.OpenThreadWaitChainSession.argtypes = [w.DWORD, ctypes.c_void_p]
    advapi.OpenThreadWaitChainSession.restype = w.HANDLE
    advapi.GetThreadWaitChain.argtypes = [w.HANDLE, ctypes.c_size_t, w.DWORD, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Node), ctypes.POINTER(w.BOOL)]
    advapi.CloseThreadWaitChainSession.argtypes = [w.HANDLE]
    session = advapi.OpenThreadWaitChainSession(0, None)
    if not session:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        count, cycle = w.DWORD(16), w.BOOL()
        nodes = (Node * 16)()
        if not advapi.GetThreadWaitChain(session, 0, 1, owner_thread, ctypes.byref(count), nodes, ctypes.byref(cycle)):
            raise ctypes.WinError(ctypes.get_last_error())
        if count.value > 16:
            raise RuntimeError("wct_count")
        return dict(event="wct", available=True, cycle=bool(cycle.value),
                    nodes=[dict(kind=n.kind, status=n.status, **({"pid": int(n.data.thread.pid), "tid": int(n.data.thread.tid),
                        "wait_ms": int(n.data.thread.wait_ms), "context_switches": int(n.data.thread.switches)} if n.kind == 8 else {}))
                           for n in nodes[:count.value]])
    finally:
        advapi.CloseThreadWaitChainSession(session)


class Probe:
    """A daemon worker may hang, but never holds up the supervisor's target wait."""

    def __init__(self, kind: str, action: Callable[[], dict[str, Any]], now: int) -> None:
        """Start one bounded observation whose worker is never killed or joined."""
        self.kind, self.started, self.done = kind, now, False
        self.result: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        def work() -> None:
            try:
                value = action()
            except Exception as error:
                value = dict(event=kind, available=False, error_type=type(error).__name__, winerror=getattr(error, "winerror", None))
            self.result.put_nowait(dict(value, qpc_ns=time.perf_counter_ns()))
        threading.Thread(target=work, daemon=True, name="ppt-capture-" + kind).start()

    def poll(self, now: int) -> dict[str, Any] | None:
        """Return completion or a two-second unavailable record once only."""
        if self.done:
            return None
        try:
            row = self.result.get_nowait()
        except queue.Empty:
            if now - self.started < 2_000_000_000:
                return None
            row = dict(event=self.kind, available=False, reason="probe_deadline", qpc_ns=now)
        self.done = True
        return row
