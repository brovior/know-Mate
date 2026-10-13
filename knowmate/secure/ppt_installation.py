"""Read-only setup contracts and Windows process/mutex boundaries for PPT."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import re
import uuid
from dataclasses import dataclass
from typing import Callable

from knowmate.secure.ppt_operation import PptPreparationPending

REQUIRED_VERSION = "1.0.0.0"
# This is a release gate, not a user configuration option. No Windows prototype
# has verified first-user reservation ACLs, UAC/restart and cross-session races.
AUTO_INSTALL_VALIDATED = False
MUTEX_NAME = r"Global\AegisDesk.PptReader.Machine.v1"
_STATUSES = frozenset({"ready", "missing", "outdated", "repairable", "disabled",
                       "conflict", "busy", "unknown", "failed"})
_REASONS = frozenset({"registration_ok", "not_installed", "version_old",
    "registration_missing", "disabled", "hkcu_shadow", "registration_foreign",
    "office_busy", "office_unknown", "mutex_busy", "mutex_unavailable",
    "reservation_unresolved", "journal_unresolved", "prototype_gate",
    "payload_invalid", "acl_unavailable", "permission_declined", "launch_failed",
    "child_unconfirmed", "mutation_failed", "removed", "check_failed"})


@dataclass(frozen=True)
class SetupResult:
    """Validated, body-free installation observation."""

    status: str = "unknown"
    reason: str = "check_failed"
    installed_version: str | None = None
    office_bitness: int | None = None
    machine_safe: bool = False
    auto_install_validated: bool = False


class PptMachineBusy(PptPreparationPending):
    """The shared installation state does not permit a PPT COM read."""


def parse_setup_result(stdout: str, exit_code: int, operation: str = "check") -> SetupResult:
    """Reject malformed output and mismatched operation/version/exit status."""
    try:
        if len(stdout) > 16384 or exit_code not in {0, 10, 11, 12, 13}:
            raise ValueError("setup_contract")
        value = json.loads(stdout)
        allowed = {"schema", "operation", "status", "reason", "required_version",
                   "installed_version", "office_bitness", "scope", "machine_safe",
                   "auto_install_validated"}
        if not isinstance(value, dict) or set(value) != allowed:
            raise ValueError("setup_fields")
        if type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError("setup_schema")
        if value["operation"] != operation or value["required_version"] != REQUIRED_VERSION:
            raise ValueError("setup_operation")
        if value["scope"] != "Machine" or value["status"] not in _STATUSES:
            raise ValueError("setup_scope")
        if value["reason"] not in _REASONS:
            raise ValueError("setup_reason")
        if type(value["machine_safe"]) is not bool or type(value["auto_install_validated"]) is not bool:
            raise ValueError("setup_flags")
        installed = value["installed_version"]
        if installed is not None and (not isinstance(installed, str) or len(installed) > 32
                or len(installed.split(".")) != 4 or not all(s.isdigit() for s in installed.split("."))):
            raise ValueError("setup_version")
        bitness = value["office_bitness"]
        if bitness is not None and (type(bitness) is not int or bitness not in (32, 64)):
            raise ValueError("setup_bitness")
        if value["status"] == "ready" and (exit_code != 0 or not value["machine_safe"]
                or installed != REQUIRED_VERSION or bitness is None):
            raise ValueError("setup_ready")
        if exit_code != 0 and value["status"] in {"ready", "missing", "outdated", "repairable"}:
            raise ValueError("setup_exit")
        return SetupResult(value["status"], value["reason"], installed, bitness,
                           value["machine_safe"], value["auto_install_validated"] and AUTO_INSTALL_VALIDATED)
    except (ValueError, TypeError, KeyError):
        return SetupResult()


def setup_helper_path() -> Path:
    """Resolve the fixed bundled helper location for source or frozen runs."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "ppt_reader" / "PptReaderSetup.exe"
    return Path(__file__).resolve().parents[2] / "scripts" / "ppt_reader" / "bin" / "PptReaderSetup.exe"


def _validate_helper(path: Path) -> bool:
    try:
        manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
        expected = manifest["files"]["PptReaderSetup.exe"]
        return (path.name == "PptReaderSetup.exe" and manifest["schema"] == 1
                and manifest["reader_version"] == REQUIRED_VERSION
                and isinstance(expected, str) and len(expected) == 64
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _known_folder(identifier: str) -> Path:
    """Read OS-known paths rather than caller-controlled environment variables."""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    guid = ctypes.create_string_buffer(uuid.UUID(identifier).bytes_le)
    pointer = ctypes.c_void_p()
    if shell.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(pointer)) != 0 or not pointer.value:
        raise OSError("known_folder_unavailable")
    try:
        return Path(ctypes.wstring_at(pointer))
    finally:
        ole.CoTaskMemFree(pointer)


def _valid_reservation(value: object, filename: str) -> bool:
    """Validate the complete reservation protocol, including identity and UUID."""
    try:
        fields = {"schema", "request_id", "required_version", "helper_sid", "helper_pid",
                  "helper_creation_ticks", "child_pid", "child_creation_ticks", "state"}
        if not isinstance(value, dict) or set(value) != fields:
            return False
        if type(value["schema"]) is not int or value["schema"] != 1:
            return False
        if (not isinstance(value["request_id"], str)
                or value["request_id"] != str(uuid.UUID(value["request_id"])) or value["request_id"] != filename):
            return False
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value["required_version"]):
            return False
        if not re.fullmatch(r"S-1-(?:\d+-)*\d+", value["helper_sid"]):
            return False
        if type(value["helper_pid"]) is not int or not 0 < value["helper_pid"] <= 2**31 - 1:
            return False
        ticks = value["helper_creation_ticks"]
        if not isinstance(ticks, str) or not ticks.isascii() or not ticks.isdecimal() or not 0 < int(ticks) <= 2**63 - 1:
            return False
        if value["state"] not in {"reserved", "elevated", "unresolved", "complete"}:
            return False
        if value["child_pid"] is None:
            return value["child_creation_ticks"] is None and value["state"] != "elevated"
        ticks = value["child_creation_ticks"]
        return (type(value["child_pid"]) is int and 0 < value["child_pid"] <= 2**31 - 1
                and isinstance(ticks, str) and ticks.isascii() and ticks.isdecimal() and 0 < int(ticks) <= 2**63 - 1)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _machine_records_safe() -> bool:
    """Treat absent records as fresh, while incomplete/foreign records block reads."""
    try:
        requests = _known_folder("62AB5D82-FDC1-4DC3-A9DD-070D1D495D97") / "AegisDesk" / "PptReaderRequests"
        roots = {_known_folder("905e63b6-c1bf-494e-b29c-65b732d3d21a") / "AegisDesk" / "PptReader",
                 _known_folder("7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e") / "AegisDesk" / "PptReader"}
        # ProgramFiles is architecture-relative for a 32-bit caller on 64-bit
        # Windows. The explicit X64 known folder covers the AnyCPU setup root.
        try:
            roots.add(_known_folder("6d809377-6af0-444b-8957-a3773f02200e") / "AegisDesk" / "PptReader")
        except OSError:
            import platform
            if platform.machine().lower() in {"amd64", "x86_64", "arm64", "aarch64"}:
                return False
        for directory in {requests, *roots}:
            for part in (directory, *directory.parents):
                if part.exists() and (part.is_symlink() or getattr(part.stat(), "st_file_attributes", 0) & 0x400):
                    return False
        if requests.exists():
            for path in requests.iterdir():
                if (not path.is_file() or path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400
                        or path.suffix != ".json" or path.stat().st_size > 8192):
                    return False
                value = json.loads(path.read_text(encoding="utf-8"))
                if not _valid_reservation(value, path.stem) or value["state"] != "complete":
                    return False
        return not any((root / "journal.json").exists() for root in roots)
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return False


def check_installation(helper_path: Path | None = None, timeout_sec: float = 10) -> SetupResult:
    """Run the verified helper's read-only check without elevation or Office."""
    if os.name != "nt":
        return SetupResult(reason="office_unknown", machine_safe=True)
    path = Path(helper_path) if helper_path is not None else setup_helper_path()
    if not _validate_helper(path):
        # Source/fresh machines still permit ordinary COM when records and the
        # machine mutex can be inspected; absence alone is not a pending install.
        try:
            with MachineReadGuard(checker=lambda: SetupResult(machine_safe=_machine_records_safe())):
                return SetupResult(reason="payload_invalid", machine_safe=True)
        except PptMachineBusy:
            return SetupResult(reason="payload_invalid")
    try:
        result = subprocess.run([str(path), "/check"], capture_output=True, text=True,
                                timeout=timeout_sec, creationflags=subprocess.CREATE_NO_WINDOW)
        return parse_setup_result(result.stdout, result.returncode)
    except (OSError, subprocess.SubprocessError):
        return SetupResult()


class DetachedSetupProcess:
    """Observe a detached helper; closing observation never terminates setup."""

    def __init__(self, helper_path: Path | None = None, operation: str = "prepare") -> None:
        self.path = Path(helper_path) if helper_path is not None else setup_helper_path()
        self.operation = operation
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        """Launch only a validated payload after the automatic-install release gate."""
        if os.name != "nt" or self.operation != "prepare" or not AUTO_INSTALL_VALIDATED:
            raise PptMachineBusy("prototype_gate")
        if not _validate_helper(self.path):
            raise PptMachineBusy("payload_invalid")
        self._process = subprocess.Popen([str(self.path), "/prepare"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)

    def poll(self) -> int | None:
        """Return actual helper termination, never a launch-success substitute."""
        return self._process.poll() if self._process is not None else None

    def close(self) -> None:
        """Release observation handles without kill/terminate/waiting for completion."""
        process, self._process = self._process, None
        if process is not None and os.name == "nt":
            # CPython's Windows process handle is independent of child lifetime.
            handle = getattr(process, "_handle", None)
            if handle is not None:
                handle.Close()


class MachineReadGuard:
    """Hold the machine mutex and recheck reservations/journal before PPT COM."""

    def __init__(self, checker: Callable[[], SetupResult] | None = None) -> None:
        self._checker = checker
        self._handle: int | None = None

    def __enter__(self) -> MachineReadGuard:
        """Acquire the cross-session mutex or fail closed before dispatch."""
        if os.name != "nt":
            return self
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.OpenMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        # Existing Users access is deliberately limited; CreateMutex attempts
        # ALL_ACCESS on existing objects and fails for another normal user.
        self._handle = kernel.OpenMutexW(0x00100001, False, MUTEX_NAME)
        if not self._handle:
            class SecurityAttributes(ctypes.Structure):
                _fields_ = [("length", ctypes.c_uint32), ("descriptor", ctypes.c_void_p), ("inherit", ctypes.c_int)]
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            descriptor = ctypes.c_void_p()
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    "D:P(A;;0x00100001;;;BU)(A;;GA;;;BA)(A;;GA;;;SY)", 1, ctypes.byref(descriptor), None):
                raise PptMachineBusy("mutex_unavailable")
            try:
                attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
                self._handle = kernel.CreateMutexW(ctypes.byref(attributes), False, MUTEX_NAME)
                if not self._handle:  # another user can win the creation race
                    self._handle = kernel.OpenMutexW(0x00100001, False, MUTEX_NAME)
            finally:
                kernel.LocalFree(descriptor)
        if not self._handle:
            raise PptMachineBusy("mutex_unavailable")
        wait = kernel.WaitForSingleObject(self._handle, 0)
        if wait not in (0, 0x80):
            kernel.CloseHandle(self._handle)
            self._handle = None
            raise PptMachineBusy("mutex_busy")
        try:
            # Recheck durable state under the held mutex. Starting a separate
            # /check helper here would contend with our own mutex and deadlock.
            state = self._checker() if self._checker else SetupResult(machine_safe=_machine_records_safe())
            if not state.machine_safe:
                raise PptMachineBusy(state.reason)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Release the same-thread mutex after read and close finish."""
        if self._handle is not None:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.ReleaseMutex(self._handle)
            kernel.CloseHandle(self._handle)
            self._handle = None
