"""One isolated-session COM caller; Office is never force-terminated."""
from __future__ import annotations

import ctypes
import gc
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from scripts.diagnostics.ppt_capture.fixture import TEXT, TEXT_SHA256
from .ppt_quit_compare_fixture import SyntheticFixture, verify_fixture

ERROR_TYPES = frozenset({"ValueError", "OSError", "RuntimeError", "ImportError", "ModuleNotFoundError",
                         "com_error", "PermissionError", "FileNotFoundError", "ComScopeUnavailable",
                         "PptAddinUnavailable", "TimeoutError", "AttributeError", "TypeError", "OverflowError",
                         "OfficeOwnershipProbeError", "OfficeOwnershipCleanupError", "PptLaunchNotStarted",
                         "ArgumentError", "error"})


def error_fields(error: BaseException) -> dict[str, Any]:
    """Reduce errors to fixed reason/type codes and numeric HRESULT/Win32 metadata."""
    from .com_fault import clear_com_error_frames, outer_hresult
    names = ERROR_TYPES
    reasons = {"baseline_not_empty", "capture_busy", "launch_identity_unknown", "target_changed",
               "concurrent_application", "user_document_or_unknown", "visible_window_or_unknown",
               "window_owner_mismatch", "synthetic_read_mismatch", "fixture_binding", "duplicate_quit",
               "invalid_arm", "fixture_invalid", "fixture_members", "fixture_content", "fixture_digest",
               "office_bitness", "office_path", "office_path_expansion", "office_path_link", "office_image",
               "service_disabled", "service_unavailable", "service_incompatible", "windows_required",
               "evidence_bound", "evidence_rows", "evidence_partial", "evidence_order", "evidence_record",
               "evidence_status", "evidence_stage", "evidence_import_state"}
    reasons |= {"handoff_parent_changed", "handoff_record", "handoff_binding", "handoff_identity",
                "target_exited", "process_set_changed", "identity_mismatch", "launch_not_prepared"}
    code = outer_hresult(error)
    winerror = getattr(error, "winerror", None)
    reason = error.args[0] if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] in reasons else "unspecified"
    cause = error.__cause__
    result = dict(error_type=type(error).__name__ if type(error).__name__ in names else "unknown",
                  hresult=code if type(code) is int else None,
                  winerror=winerror if type(winerror) is int else None, reason=reason,
                  cause_error_type=(type(cause).__name__ if type(cause).__name__ in names else "unknown")
                  if cause is not None else None,
                  cause_hresult=outer_hresult(cause) if cause is not None else None)
    clear_com_error_frames(error)
    return result


class Session:
    """Keep every caller-owned COM reference in one disposable helper process."""

    def __init__(self, config: dict[str, Any], native: Any, parent: int | None = None) -> None:
        """Prepare a launch caller without opening any target process by PID."""
        self.config, self.native = config, native
        self.holds: dict[str, Any] = {}
        self.cache = SimpleNamespace(ppt_service=None)
        self.fixture = SyntheticFixture(Path(config["fixture_path"]), config["fixture_sha256"])
        self.target = None
        self.observer: Any = None
        self.bootstrap = True
        self.parent = parent
        self.emit: Callable[..., None] | None = None
        self.requested = False

    def launch_and_handoff(self) -> None:
        """Launch next to activation and transfer only its original observation capability."""
        from .ppt_capture_observer import processes
        from .ppt_launch import LaunchedPowerPoint, trusted_powerpoint_path
        from .ppt_quit_compare_handoff import publish_launch
        if self.parent is None or self.emit is None:
            raise RuntimeError("launch_not_prepared")
        rows = processes(self.native)
        if any(row["name"] == "powerpnt.exe" or (row["name"] == "aegisdesk.exe"
                and row["pid"] not in self.config["allowed_pids"]) for row in rows):
            raise RuntimeError("baseline_not_empty")
        path = trusted_powerpoint_path(self.config["office_bitness"])
        self.bind_step = "launch"
        launched = LaunchedPowerPoint(path)
        try:
            if (type(launched.identity) is not int or not 0 < launched.identity <= 0xffffffffffffffff
                    or type(launched.pid) is not int or not 0 < launched.pid <= 0xffffffff):
                raise RuntimeError("launch_identity_unknown")
            self.config.update(pid=launched.pid, creation=launched.identity)
            self.emit("launch_created")
            self.bind_step = "handoff"
            publish_launch(self.native, self.parent, launched, self.config)
            self.observer = launched.detach_observation()
            self.target = self.observer._handle
            self.emit("handle_transferred")
        finally:
            launched.close()

    def target_check(self) -> None:
        """Require the original live process and an otherwise empty PowerPoint baseline."""
        from .ppt_capture_observer import processes
        rows = processes(self.native)
        candidates = [row["pid"] for row in rows if row["name"] == "powerpnt.exe"]
        if any(row["name"] == "aegisdesk.exe" and row["pid"] not in self.config["allowed_pids"] for row in rows):
            raise RuntimeError("concurrent_application")
        if self.native.wait(self.target):
            raise RuntimeError("target_exited")
        if self.native.creation(self.target) != self.config["creation"]:
            raise RuntimeError("identity_mismatch")
        if candidates != [self.config["pid"]]:
            raise RuntimeError("process_set_changed")

    def counts(self, expected: int) -> None:
        """Reject visible windows or foreign documents; snapshots do not prove race safety."""
        self.target_check()
        app = self.holds["app"]
        for name, count in (("Presentations", expected), ("ProtectedViewWindows", 0),
                            ("SlideShowWindows", 0), ("Windows", 0)):
            collection = getattr(app, name)
            actual = collection.Count
            collection = None
            if type(actual) is not int or actual != count:
                raise RuntimeError("user_document_or_unknown")
        from ctypes import wintypes
        user = self.native.u
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        visible = False
        def visit(hwnd: int, parameter: int) -> bool:
            nonlocal visible
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self.config["pid"] and user.IsWindowVisible(hwnd):
                visible = True
            return True
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows.restype = wintypes.BOOL
        if not user.EnumWindows(callback_type(visit), 0) or visible:
            raise RuntimeError("visible_window_or_unknown")

    def bind(self) -> None:
        """Match production startup activation, then prove the original host before use."""
        if not getattr(self, "bootstrap", False):
            self.bind_step = "target_check"
            self.target_check()
        self.bind_step = "client_import"
        import win32com.client as client
        if getattr(self, "bootstrap", False):
            self.launch_and_handoff()
        # An /EMBEDDING server may expose its class factory before entering ROT.
        # Activation can hand off: a different host is never opened or quit.
        self.bind_step = "dispatch"
        if getattr(self, "emit", None) is not None:
            self.emit("dispatch_entered")
        self.holds["app"] = client.Dispatch("PowerPoint.Application")
        if getattr(self, "emit", None) is not None:
            self.emit("dispatch_returned")
        from .office_guard import _app_hwnd, _ppt_ownership_window
        self.bind_step = "app_hwnd"
        hwnd = _app_hwnd(self.holds["app"], "POWERPNT.EXE")
        if hwnd is None:
            # Match the production fallback only in an empty isolated host.
            self.bind_step = "probe_precheck"
            self.counts(0)
            self.bind_step = "caption_probe"
            with _ppt_ownership_window(self.holds["app"]) as read_hwnd:
                self.bind_step = "probe_owner"
                self.native.owner(read_hwnd(), self.config["pid"])
                self.target_check()
                self.bind_step = "probe_cleanup"
        else:
            self.bind_step = "hwnd_owner"
            self.native.owner(hwnd, self.config["pid"])
        self.bind_step = "idle_check"
        self.counts(0)
        self.bind_step = "service_select"
        from .ppt_addin_reader import select_service
        self.holds["service"] = select_service(self.holds["app"], self.cache)
        self.bind_step = "service_idle_check"
        self.counts(0)

    def read(self) -> None:
        """Read only the complete authenticated synthetic fixture via the installed Reader."""
        from .ppt_payload import decode_slide
        verify_fixture(self.fixture)
        self.counts(0)
        self.holds["document"] = self.holds["app"].Presentations.Open(str(self.fixture.path), -1, 0, 0)
        self.counts(1)
        self.document_check()
        reply = self.holds["service"].ExtractSlide(str(self.fixture.path), 1)
        texts, counts, _ = decode_slide(reply, 1, strict=True)
        text = "\n".join(texts)
        if counts["errors"] or len(text) != len(TEXT) or hashlib.sha256(text.encode()).hexdigest() != TEXT_SHA256:
            raise RuntimeError("synthetic_read_mismatch")

    def document_check(self) -> None:
        """Match the sole opened document to the authenticated built-in deck."""
        verify_fixture(self.fixture)
        document = self.holds["document"]
        if Path(document.FullName) != self.fixture.path:
            raise RuntimeError("fixture_binding")
        collection = self.holds["app"].Presentations
        actual = collection.Item(1)
        if Path(actual.FullName) != self.fixture.path:
            raise RuntimeError("fixture_binding")

    def close_document(self) -> None:
        """Close only the checked fixture; uncertain documents remain untouched."""
        self.counts(1)
        self.document_check()
        self.holds["document"].Close()
        self.holds.pop("document")
        self.counts(0)

    def quit(self, arm: str, emit: Callable[..., None]) -> str:
        """Make exactly one requested normal Quit in the explicitly isolated session."""
        self.counts(0)
        if self.requested:
            raise RuntimeError("duplicate_quit")
        self.requested = True
        emit("quit_requested")
        if arm == "internal_quit":
            status = self.holds["service"].TryQuitIfIdle()
            return status if type(status) is str and status in {
                "quit_requested", "protected", "not_supported", "failed"} else "unknown"
        if arm != "external_quit":
            raise ValueError("invalid_arm")
        self.holds["app"].Quit()
        return "returned"

    def drop(self) -> None:
        """Clear service cache and COM ownership after operation frames have unwound."""
        self.cache.ppt_service = None
        previous, self.holds = self.holds, {}
        previous.clear()
        gc.collect()


def operate(session: Any, arm: str, emit: Callable[..., None]) -> bool:
    """Unwind operation frames and opaque COM exceptions before releasing references."""
    stage = "bind"
    try:
        if getattr(session, "bootstrap", False):
            session.emit = emit
        session.bind()
        emit("host_bound")
        stage = "read"
        session.read()
        emit("synthetic_read")
        stage = "close"
        session.close_document()
        emit("document_closed")
        if arm != "reference_drop":
            stage = "quit"
            emit("quit_returned", status=session.quit(arm, emit))
        return True
    except Exception as error:
        details = error_fields(error)
        if stage == "bind":
            details["bind_step"] = getattr(session, "bind_step", "target_check")
        emit("operation_failed", stage=stage, **details)
        return False


def run_caller(session: Any, arm: str, emit: Callable[..., None], scope_factory: Callable[..., Any] | None = None) -> int:
    """Balance this caller's native initialization and let process exit end implicit imports."""
    from .com_scope import ComScope
    scope_factory = scope_factory or ComScope
    ok = False
    try:
        with scope_factory() as scope:
            scope.acquire()
            emit("native_scope_acquired", pythoncom_preimported="pythoncom" in sys.modules)
            try:
                ok = operate(session, arm, emit)
            finally:
                session.drop()
                emit("references_dropped")
        emit("native_pair_completed")
    except Exception as error:
        emit("operation_failed", stage="cleanup", **error_fields(error))
        ok = False
    emit("caller_complete")
    return 0 if ok else 2
