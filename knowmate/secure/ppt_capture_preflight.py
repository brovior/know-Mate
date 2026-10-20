"""Bounded, read-only installation and runtime metadata for capture packages."""
from __future__ import annotations

import ctypes
import importlib.metadata
import hashlib
import json
import os
import platform
import struct
import subprocess
import tempfile
import time
from ctypes import wintypes as w
from pathlib import Path
from typing import Any


def helper_check(package: Path, pins: dict[str, Any]) -> dict[str, Any]:
    """Run the pinned helper's read-only /check with a ten-second no-kill bound."""
    from knowmate.secure.ppt_installation import _unique_json_fields
    with tempfile.TemporaryFile(mode="w+b") as output:
        child = subprocess.Popen([str(package / "PptReaderSetup.exe"), "/check"], stdout=output,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if child.poll() is None:
            raise RuntimeError("read_only_helper_deadline_no_kill")
        output.seek(0)
        raw = output.read(16385)
    if len(raw) > 16384:
        raise ValueError("capture_helper_output_size")
    value = json.loads(raw, object_pairs_hook=_unique_json_fields)
    expected = dict(schema=1, operation="check", status="ready", reason="registration_ok",
                    required_version="1.0.6.0", installed_version="1.0.6.0", scope="Machine",
                    machine_safe=True, auto_install_validated=False, auto_install_mode="candidate",
                    office_bitness=pins["office_bitness"])
    if child.returncode != 0 or any(type(value.get(k)) is not type(v) or value[k] != v for k, v in expected.items()):
        raise ValueError("capture_fresh_registration_not_ready")
    return expected


def file_version(path: Path) -> list[int]:
    """Read fixed numeric VS_FIXEDFILEINFO without logging a path or localized strings."""
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
    version.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.UINT)]
    unused = w.DWORD()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    if not size or size > 1024 * 1024:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = ctypes.c_void_p(), w.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        raise RuntimeError("file_version_unavailable")
    fixed = (w.DWORD * 13).from_address(pointer.value)
    if fixed[0] != 0xFEEF04BD:
        raise RuntimeError("file_version_signature")
    return [fixed[2] >> 16, fixed[2] & 0xffff, fixed[3] >> 16, fixed[3] & 0xffff]


def environment(native: Any, pins: dict[str, Any]) -> dict[str, Any]:
    """Record actual interpreter/Windows/Office identity before the Quit interval."""
    from scripts.diagnostics.ppt_capture.gate import digest
    from knowmate.secure.ppt_launch import trusted_powerpoint_path
    executable = trusted_powerpoint_path(pins["office_bitness"])
    sha = digest(executable)
    if sha != pins.get("office_exe_sha256"):
        raise ValueError("capture_office_exe_pin")
    session = w.DWORD()
    native.k.ProcessIdToSessionId.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
    if not native.k.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    return dict(python_version=platform.python_version(), python_bitness=struct.calcsize("P") * 8,
                pywin32_version=importlib.metadata.version("pywin32"), windows_version=list(platform.win32_ver()[:2]),
                office_bitness=pins["office_bitness"], office_exe_version=file_version(executable),
                office_exe_sha256=sha, session=int(session.value), elevated=False,
                clr_version={"available": False, "reason": "runtime_module_snapshot_pending"},
                aegis_registration="fresh_helper_registration_ok", registered_addins=registered_addins())


def registered_addins() -> dict[str, Any]:
    """Read bounded ProgID/LoadBehavior only, excluding friendly names and paths."""
    import re
    import winreg
    rows: list[dict[str, Any]] = []
    try:
        for hive_name, hive in (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE)):
            for bits, view in ((32, winreg.KEY_WOW64_32KEY), (64, winreg.KEY_WOW64_64KEY)):
                try:
                    opened = winreg.OpenKey(hive, r"Software\Microsoft\Office\PowerPoint\Addins", 0, winreg.KEY_READ | view)
                except FileNotFoundError:
                    continue
                with opened as key:
                    count = winreg.QueryInfoKey(key)[0]
                    if count > 128:
                        raise ValueError("registered_addin_bound")
                    for index in range(count):
                        prog_id = winreg.EnumKey(key, index)
                        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", prog_id):
                            rows.append(dict(hive=hive_name, bitness=bits, available=False, reason="invalid_progid"))
                            continue
                        with winreg.OpenKey(key, prog_id) as addin:
                            try:
                                value, kind = winreg.QueryValueEx(addin, "LoadBehavior")
                            except FileNotFoundError:
                                value, kind = None, None
                        rows.append(dict(hive=hive_name, bitness=bits, prog_id=prog_id,
                                         load_behavior=value if type(value) is int and kind == winreg.REG_DWORD else None))
        return dict(available=True, rows=rows)
    except Exception as error:
        return dict(available=False, rows=rows, error_type=type(error).__name__)


def registration_fingerprint() -> str:
    """Hash only fixed Aegis registration trees, keeping their strings out of evidence."""
    import winreg
    keys = (r"Software\Classes\CLSID\{287A80D2-804A-46D3-A0E9-CA31C6FC3D09}",
            r"Software\Classes\AegisDesk.PptReader",
            r"Software\Microsoft\Office\PowerPoint\Addins\AegisDesk.PptReader")
    def read(hive: int, path: str, view: int, depth: int = 0) -> Any:
        try:
            opened = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
        except FileNotFoundError:
            return None
        with opened as key:
            children, values, _ = winreg.QueryInfoKey(key)
            if depth > 4 or children > 32 or values > 32:
                raise ValueError("capture_registration_bound")
            data = sorted(winreg.EnumValue(key, index) for index in range(values))
            nested = [(name, read(hive, path + "\\" + name, view, depth + 1))
                      for name in sorted(winreg.EnumKey(key, index) for index in range(children))]
            return data, nested
    rows = [read(hive, key, view) for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY) for key in keys]
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def lease_directories(native: Any, root: Path, streams: dict[str, Any]) -> None:
    """Hold verified nonreparse ancestors against write/delete/rename replacement."""
    kernel = native.k
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    class Attribute(ctypes.Structure):
        _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]
    for path in reversed([root, *root.parents]):
        handle = kernel.CreateFileW(str(path), 0x81, 1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        native.keep(handle)
        attributes = Attribute()
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        final = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, final, len(final), 0)
        if (attributes.attributes & 0x400 or not attributes.attributes & 0x10 or not 0 < length < len(final)
                or os.path.normcase(final.value.removeprefix("\\\\?\\").rstrip("\\")) != os.path.normcase(str(path).rstrip("\\"))):
            raise RuntimeError("capture_directory_lease_identity")
    for name, stream in streams.items():
        if not stream.closed:
            current, held = (root / name).stat(), os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise RuntimeError("capture_output_replaced_before_lease")
