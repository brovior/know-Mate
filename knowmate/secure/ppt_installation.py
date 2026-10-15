"""Read-only setup contracts and Windows process/mutex boundaries for PPT."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import re
import uuid
from dataclasses import dataclass
from typing import BinaryIO, Callable

from knowmate.secure.ppt_operation import PptPreparationPending
from knowmate.secure.ppt_policy import auto_install_mode, auto_install_enabled, REQUIRED_VERSION
from knowmate.secure.ppt_setup_output import create_setup_output

MUTEX_NAME = r"Global\AegisDesk.PptReader.Machine.v1"
_STATUSES = frozenset({"ready", "missing", "outdated", "repairable", "disabled",
                       "conflict", "busy", "unknown", "failed"})
_REASONS = frozenset({"registration_ok", "not_installed", "version_old",
    "registration_missing", "disabled", "hkcu_shadow", "registration_foreign",
    "office_busy", "office_unknown", "mutex_busy", "mutex_unavailable",
    "reservation_unresolved", "journal_unresolved", "prototype_gate",
    "payload_invalid", "acl_unavailable", "permission_declined", "launch_failed",
    "child_unconfirmed", "mutation_failed", "removed", "check_failed"})
_DIAGNOSTIC_STAGES = frozenset({"anchor_open", "parent_open", "parent_policy", "child_open",
    "child_policy", "request_create", "request_acl", "request_commit", "lease_recheck"})
_DIAGNOSTIC_ERRORS = frozenset({"policy", "Win32Exception", "UnauthorizedAccessException",
    "IOException", "SecurityException", "InvalidOperationException", "ArgumentException", "NotSupportedException", "Exception"})
_MAX_SETUP_OUTPUT = 16384


@dataclass(frozen=True)
class SetupResult:
    """Validated, body-free installation observation."""

    status: str = "unknown"
    reason: str = "check_failed"
    installed_version: str | None = None
    office_bitness: int | None = None
    machine_safe: bool = False
    auto_install_validated: bool = False
    auto_install_mode: str = "disabled"
    stage: str | None = None
    error_type: str | None = None
    winerror: int | None = None

    @property
    def auto_install_enabled(self) -> bool:
        """Permit candidates without claiming Windows validation passed."""
        return (self.auto_install_mode == auto_install_mode()
                and self.auto_install_mode in {"candidate", "released"}
                and self.auto_install_validated == (self.auto_install_mode == "released"))


class PptMachineBusy(PptPreparationPending):
    """The shared installation state does not permit a PPT COM read."""


class SetupLaunchNotStarted(PptMachineBusy):
    """A prelaunch check failed before any setup process was created."""


def _unique_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("setup_duplicate_field")
        value[key] = item
    return value


def parse_setup_result(stdout: str, exit_code: int, operation: str = "check") -> SetupResult:
    """Reject malformed output and mismatched operation/version/exit status."""
    try:
        if (not isinstance(stdout, str) or len(stdout.encode("utf-8")) > _MAX_SETUP_OUTPUT
                or type(exit_code) is not int or exit_code not in {0, 10, 11, 12, 13}):
            raise ValueError("setup_contract")
        value = json.loads(stdout, object_pairs_hook=_unique_json_fields)
        allowed = {"schema", "operation", "status", "reason", "required_version",
                   "installed_version", "office_bitness", "scope", "machine_safe",
                   "auto_install_validated", "auto_install_mode"}
        diagnostic = {"stage", "error_type", "winerror"}
        if not isinstance(value, dict) or set(value) not in (allowed, allowed | diagnostic):
            raise ValueError("setup_fields")
        if diagnostic <= value.keys():
            if value["stage"] not in _DIAGNOSTIC_STAGES or value["error_type"] not in _DIAGNOSTIC_ERRORS:
                raise ValueError("setup_diagnostic")
            if value["winerror"] is not None and (type(value["winerror"]) is not int
                    or not 0 <= value["winerror"] <= 2**31 - 1
                    or value["error_type"] != "Win32Exception"):
                raise ValueError("setup_winerror")
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
        if (value["auto_install_mode"] != auto_install_mode()
                or value["auto_install_validated"] != (value["auto_install_mode"] == "released")):
            raise ValueError("setup_policy")
        installed = value["installed_version"]
        if installed is not None and (not isinstance(installed, str) or len(installed) > 32
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", installed)):
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
                           value["machine_safe"], value["auto_install_validated"], value["auto_install_mode"],
                           value.get("stage"), value.get("error_type"), value.get("winerror"))
    except (ValueError, TypeError, KeyError, UnicodeError):
        return SetupResult(reason="diagnostic_unavailable")


def setup_helper_path() -> Path:
    """Resolve the fixed bundled helper location for source or frozen runs."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "ppt_reader" / "PptReaderSetup.exe"
    return Path(__file__).resolve().parents[2] / "scripts" / "ppt_reader" / "bin" / "PptReaderSetup.exe"


def _validate_helper(path: Path) -> bool:
    from knowmate.build_info import get_build_info
    from knowmate.secure.ppt_bundle import verify_payload

    try:
        if path.name != "PptReaderSetup.exe":
            return False
        # Source mode remains disabled; frozen payloads must match the embedded
        # app build, not merely a caller-editable manifest's helper hash.
        info = get_build_info() if getattr(sys, "frozen", False) else None
        verify_payload(path.parent, info)
        return True
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
        for root in roots:
            if (root / "journal.json").exists():
                return False
            if root.exists():
                for pattern in ("journal.json.*.tmp", "installation.json.*.tmp"):
                    if any(root.glob(pattern)):
                        return False
        return True
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
        self._output: BinaryIO | None = None

    def start(self) -> None:
        """Launch only a validated payload after the automatic-install release gate."""
        if os.name != "nt" or self.operation != "prepare" or not auto_install_enabled():
            raise SetupLaunchNotStarted("prototype_gate")
        if not _validate_helper(self.path):
            raise SetupLaunchNotStarted("payload_invalid")
        if self._process is not None:
            raise RuntimeError("setup_already_started")
        # The private file survives until the inherited stdout handle's final
        # close. A pipe would break helper writes when the application exits.
        try:
            self._output = create_setup_output()
        except (OSError, ValueError) as exc:
            raise SetupLaunchNotStarted("diagnostic_unavailable") from exc
        try:
            self._process = subprocess.Popen([str(self.path), "/prepare"],
                stdin=subprocess.DEVNULL, stdout=self._output, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        except BaseException:
            self.close()
            raise

    def poll(self) -> int | None:
        """Return actual helper termination, never a launch-success substitute."""
        return self._process.poll() if self._process is not None else None

    def read_result(self) -> SetupResult:
        """Read bounded output only after actual exit; observation failures stay unknown."""
        if self._process is None or self._output is None:
            return SetupResult(reason="diagnostic_unavailable")
        try:
            code = self._process.poll()
            if code is None:
                return SetupResult(reason="diagnostic_unavailable")
            self._output.seek(0)
            output = self._output.read(_MAX_SETUP_OUTPUT + 1)
            if len(output) > _MAX_SETUP_OUTPUT:
                return SetupResult(reason="diagnostic_unavailable")
            return parse_setup_result(output.decode("utf-8-sig"), code, self.operation)
        except (OSError, ValueError, UnicodeError):
            return SetupResult(reason="diagnostic_unavailable")

    def close(self) -> None:
        """Release observation handles without kill/terminate/waiting for completion."""
        process, self._process = self._process, None
        output, self._output = self._output, None
        try:
            if process is not None and os.name == "nt":
                # CPython's Windows process handle is independent of child lifetime.
                handle = getattr(process, "_handle", None)
                if handle is not None:
                    handle.Close()
        finally:
            if output is not None:
                output.close()


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
