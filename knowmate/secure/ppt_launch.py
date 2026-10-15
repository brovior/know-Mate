"""Restricted PowerPoint process creation; observation handles never terminate Office."""
from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path
from typing import Any


def trusted_powerpoint_path(bitness: int) -> Path:
    """Resolve the machine Office registration within an OS-known install root."""
    import winreg

    from knowmate.secure.ppt_installation import _known_folder

    if bitness not in {32, 64}:
        raise ValueError("office_bitness")
    access = winreg.KEY_READ | (winreg.KEY_WOW64_64KEY if bitness == 64 else winreg.KEY_WOW64_32KEY)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                       r"Software\Microsoft\Windows\CurrentVersion\App Paths\POWERPNT.EXE", 0, access) as key:
        raw, kind = winreg.QueryValueEx(key, "")
    if kind != winreg.REG_SZ or not isinstance(raw, str):
        raise ValueError("office_path")
    path = Path(raw.strip('"'))
    roots = {_known_folder("905e63b6-c1bf-494e-b29c-65b732d3d21a"),
             _known_folder("7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e")}
    if (not path.is_absolute() or path.name.upper() != "POWERPNT.EXE" or not path.is_file()
            or not any(path.is_relative_to(root) for root in roots)):
        raise ValueError("office_path")
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part.stat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("office_path_link")
    # Match the PE machine to the registration view, never infer from Python.
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("office_image")
        stream.seek(0x3C)
        offset = int.from_bytes(stream.read(4), "little")
        if not 64 <= offset <= path.stat().st_size - 24:
            raise ValueError("office_image")
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError("office_image")
        if int.from_bytes(stream.read(2), "little") != (0x8664 if bitness == 64 else 0x14C):
            raise ValueError("office_bitness")
    return path


class PptLaunchNotStarted(OSError):
    """CreateProcess returned failure before creating a PowerPoint child."""


class LaunchedPowerPoint:
    """Own one CreateProcess observation handle and close it exactly once."""

    def __init__(self, path: Path) -> None:
        from ctypes import wintypes

        from knowmate.secure.office_guard import _process_creation_identity_from_handle

        class StartupInfo(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                        ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                        ("xsize", wintypes.DWORD), ("ysize", wintypes.DWORD),
                        ("xchars", wintypes.DWORD), ("ychars", wintypes.DWORD),
                        ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("show", wintypes.WORD), ("reserved2size", wintypes.WORD),
                        ("reserved2", ctypes.POINTER(ctypes.c_byte)),
                        ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                        ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR, ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)]
        kernel.CreateProcessW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        startup = StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        info = ProcessInfo()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(path), "/EMBEDDING"]))
        if not kernel.CreateProcessW(str(path), command, None, None, False, 0, None,
                                     str(path.parent), ctypes.byref(startup), ctypes.byref(info)):
            raise PptLaunchNotStarted(ctypes.get_last_error(), "powerpoint_create_failed")
        self._kernel: Any = kernel
        self._handle = info.process
        self.pid = int(info.pid)
        self.identity: int | None = None
        try:
            self.identity = _process_creation_identity_from_handle(kernel, info.process)
        except BaseException:
            self.close()
            raise
        finally:
            kernel.CloseHandle(info.thread)

    def close(self) -> None:
        """Release observation only; never send a process termination request."""
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel.CloseHandle(handle)


def installation_stamp() -> tuple[Any, ...] | None:
    """Snapshot fixed registration/payload bytes around the helper's ready check."""
    import hashlib
    import json
    import winreg

    from knowmate.secure.ppt_installation import _known_folder
    from knowmate.secure.ppt_policy import REQUIRED_VERSION

    keys = (r"Software\Classes\CLSID\{287A80D2-804A-46D3-A0E9-CA31C6FC3D09}",
            r"Software\Classes\AegisDesk.PptReader",
            r"Software\Microsoft\Office\PowerPoint\Addins\AegisDesk.PptReader")
    def snapshot(hive: int, path: str, access: int, depth: int = 0) -> Any:
        try:
            opened = winreg.OpenKey(hive, path, 0, access)
        except FileNotFoundError:
            return None
        with opened as key:
            children, values, _ = winreg.QueryInfoKey(key)
            if depth > 4 or children > 32 or values > 32:
                raise ValueError("registration_size")
            data = tuple(sorted(winreg.EnumValue(key, i) for i in range(values)))
            nested = tuple((name, snapshot(hive, path + "\\" + name, access, depth + 1))
                           for name in sorted(winreg.EnumKey(key, i) for i in range(children)))
            return data, nested
    try:
        state = []
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key in keys:
                    state.append(snapshot(hive, key, winreg.KEY_READ | view))
        roots = {_known_folder("905e63b6-c1bf-494e-b29c-65b732d3d21a"),
                 _known_folder("7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e")}
        for root in sorted(roots):
            record = root / "AegisDesk" / "PptReader" / "installation.json"
            if not record.exists():
                state.append(None)
                continue
            dll = record.parent / REQUIRED_VERSION / "AegisDesk.PptReader.dll"
            for part in (record, dll, *dll.parents):
                if part.is_symlink() or getattr(part.stat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("installation_link")
            if record.stat().st_size > 16384:
                raise ValueError("installation_size")
            contents = record.read_bytes()
            metadata = json.loads(contents)
            digest = hashlib.sha256(dll.read_bytes()).hexdigest()
            if metadata.get("version") != REQUIRED_VERSION or metadata.get("sha256") != digest:
                raise ValueError("installation_hash")
            state.append((contents, digest))
        return tuple(state)
    except (OSError, ValueError, TypeError, KeyError):
        return None
