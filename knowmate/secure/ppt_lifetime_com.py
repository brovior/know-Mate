"""One native-MTA caller arm; no old capture handshake or internal Quit path."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from scripts.diagnostics.ppt_lifetime.evidence import ARMS, ERROR_TYPES
from scripts.diagnostics.ppt_lifetime.fixture import TEXT, TEXT_SHA256, Fixture, verify_fixture
from scripts.diagnostics.ppt_lifetime.gate import READER_IID, VERSION, require_live_review


def parse_identity(reply: str) -> tuple[int, int]:
    """Accept only a bounded two-integer native host identity reply."""
    if type(reply) is not str or len(reply) > 160:
        raise ValueError("lifetime_host_identity")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("lifetime_host_identity_duplicate")
            value[key] = item
        return value
    value = json.loads(reply, object_pairs_hook=pairs)
    if (type(value) is not dict or set(value) != {"pid", "creation_filetime"}
            or type(value["pid"]) is not int or not 0 < value["pid"] <= 0xffffffff
            or type(value["creation_filetime"]) is not int or not 0 < value["creation_filetime"] <= 0xffffffffffffffff):
        raise ValueError("lifetime_host_identity")
    return value["pid"], value["creation_filetime"]


def _error_fields(error: BaseException) -> dict[str, Any]:
    from .com_fault import clear_com_error_frames, outer_hresult
    name = type(error).__name__
    try:
        code = outer_hresult(error)
    except Exception:
        code = None
    clear_com_error_frames(error)
    return dict(error_type=name if name in ERROR_TYPES else "unknown", hresult=code)


class _Emitter:
    """Latch persistence failure without letting its traceback retain COM frames."""

    def __init__(self, emit: Callable[..., None]) -> None:
        self.emit = emit
        self.failed = False

    def __call__(self, event: str, **fields: Any) -> bool:
        if self.failed:
            return False
        try:
            self.emit(event, **fields)
            return True
        except BaseException as error:
            _error_fields(error)
            self.failed = True
            return False


def _drop(session: Any) -> dict[str, Any] | None:
    """Unwind drop errors and detach the caller-owned dictionary before native teardown."""
    failure = None
    try:
        session.drop()
    except BaseException as error:
        failure = _error_fields(error)
    # This dictionary is the session's complete COM ownership container. A
    # failed callback must not leave it reachable while the native pair ends.
    previous, session.holds = session.holds, {}
    previous.clear()
    return failure


def _acquire(scope: Any) -> dict[str, Any] | None:
    """End initialization error frames before releasing any caller-owned dictionary."""
    try:
        scope.acquire()
        return None
    except BaseException as error:
        return _error_fields(error)


def _operate(session: Any, arm: str, emit: _Emitter) -> bool:
    """Unwind every operation/exception frame before dropping caller-held references."""
    stage = "bind"
    try:
        session.bind()
        if not emit("host_bound"):
            return False
        stage = "open"
        session.open_fixture()
        if not emit("document_opened"):
            return False
        stage = "read"
        text = session.read_fixture()
        length, sha = len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()
        text = None
        if not emit("synthetic_read", characters=length, sha256=sha):
            return False
        if length != len(TEXT) or sha != TEXT_SHA256:
            raise ValueError("lifetime_synthetic_read_mismatch")
        stage = "close"
        session.close_document()
        if not emit("document_closed"):
            return False
        stage = "idle"
        session.check_empty()
        if not emit("idle_checked"):
            return False
        if arm == "exact_app_external_quit":
            stage = "quit"
            if not emit("quit_requested"):
                return False
            session.quit_once()
            if not emit("quit_returned"):
                return False
        return True
    except BaseException as error:
        emit("caller_failed", stage=stage, **_error_fields(error))
        return False


def run_caller(session: Any, arm: str, emit: Callable[..., None]) -> int:
    """Pair own initialization after helper unwind; never force another thread's teardown."""
    if arm not in ARMS:
        raise ValueError("lifetime_arm")
    succeeded = False
    error_fields: dict[str, Any] | None = None
    record = _Emitter(emit)
    acquired = False
    error_stage = "native_initialize"
    try:
        with session.scope() as scope:
            error_fields = _acquire(scope)
            acquired = error_fields is None
            if acquired and record("native_scope_acquired"):
                succeeded = _operate(session, arm, record)
            drop_error = _drop(session)
            if drop_error is not None:
                record("cleanup_failed", stage="drop", **drop_error)
                succeeded = False
            record("references_dropped")
            if acquired:
                error_stage = "native_uninitialize"
        if acquired:
            record("native_pair_completed")
    except BaseException as error:
        error_fields = _error_fields(error)
    if error_fields is not None:
        record("caller_failed", stage=error_stage, **error_fields)
    record("caller_complete")
    return 0 if succeeded and error_fields is None and not record.failed else 2


class WindowsSession:
    """Hold only exact host/fixture COM references in the native-MTA caller process."""

    def __init__(self, config: dict[str, Any], native: Any) -> None:
        """Require the independent source lock before accepting any live session."""
        require_live_review()
        self.config, self.native = config, native
        self.holds: dict[str, Any] = {}
        self.requested = False
        fixture = config["fixture"]
        self.fixture = Fixture(Path(fixture["path"]), fixture["sha256"],
                               fixture["generator_sha256"], fixture["template_sha256"])
        self.target = native.keep(native.k.OpenProcess(0x100000 | 0x1000, False, config["binding"]["pid"]))

    def scope(self) -> Any:
        """Use the same lazy native scope as the indexing worker."""
        from .com_scope import ComScope
        return ComScope()

    def _check_target(self) -> None:
        from .ppt_capture_observer import processes
        binding = self.config["binding"]
        candidates = [row for row in processes(self.native) if row["name"] in {"powerpnt.exe", "aegisdesk.exe"}]
        if (len(candidates) != 1 or candidates[0]["pid"] != binding["pid"]
                or self.native.creation(self.target) != binding["creation"] or self.native.wait(self.target)):
            raise RuntimeError("lifetime_target_changed")

    def _counts(self, presentations: int) -> None:
        self._check_target()
        if self._visible_window():
            raise RuntimeError("lifetime_visible_user_window")
        app = self.holds["app"]
        for name, expected in (("Presentations", presentations), ("ProtectedViewWindows", 0),
                               ("SlideShowWindows", 0), ("Windows", 0)):
            collection = getattr(app, name)
            count = collection.Count
            collection = None
            if type(count) is not int or count != expected:
                raise RuntimeError("lifetime_user_document_or_unknown")

    def _visible_window(self) -> bool:
        """Read visibility and owner PID only, including an empty visible Backstage host."""
        import ctypes
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
            if pid.value == self.config["binding"]["pid"] and user.IsWindowVisible(hwnd):
                visible = True
            return True
        callback = callback_type(visit)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows.restype = wintypes.BOOL
        if not user.EnumWindows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return visible

    def bind(self) -> None:
        """Bind without activation, blank documents, caption changes or policy mutation."""
        self._check_target()
        import pythoncom
        import win32com.client as client
        self.client = client
        self.holds["app"] = client.GetActiveObject("PowerPoint.Application")
        self._counts(0)
        addins = self.holds["app"].COMAddIns
        addin = addins.Item("AegisDesk.PptReader")
        if addin.Connect is not True:
            raise RuntimeError("lifetime_reader_not_loaded")
        service = addin.Object
        # Query the diagnostic dual IID explicitly; normal default interfaces fail closed.
        interface = service._oleobj_.QueryInterface(pythoncom.MakeIID(READER_IID), pythoncom.IID_IDispatch)
        self.holds["service"] = client.Dispatch(interface)
        service = self.holds["service"]
        if service.ProtocolVersion() != 3 or service.ReaderVersion() != VERSION:
            raise RuntimeError("lifetime_reader_version")
        if parse_identity(service.GetHostProcessIdentity()) != (self.config["binding"]["pid"], self.config["binding"]["creation"]):
            raise RuntimeError("lifetime_host_identity_mismatch")
        self._check_target()

    def open_fixture(self) -> None:
        """Open only the supervisor-generated exact synthetic input without a window."""
        verify_fixture(self.fixture)
        self._counts(0)
        self.holds["document"] = self.holds["app"].Presentations.Open(str(self.fixture.path), -1, 0, 0)
        self._counts(1)
        if Path(self.holds["document"].FullName) != self.fixture.path:
            raise RuntimeError("lifetime_fixture_binding")

    def read_fixture(self) -> str:
        """Use the existing fast Reader protocol; return synthetic text in memory only."""
        from .ppt_payload import decode_slide
        self._counts(1)
        reply = self.holds["service"].ExtractSlide(str(self.fixture.path), 1)
        texts, counts, _ = decode_slide(reply, 1, strict=True)
        if counts["errors"]:
            raise RuntimeError("lifetime_incomplete_read")
        return "\n".join(texts)

    def close_document(self) -> None:
        """Close only the opened fixture after rechecking the controlled host state."""
        self._counts(1)
        verify_fixture(self.fixture)
        self.holds["document"].Close()
        self.holds.pop("document")

    def check_empty(self) -> None:
        """An empty snapshot is an experiment precondition, never a race-safety proof."""
        self._counts(0)

    def quit_once(self) -> None:
        """Issue one external normal Quit to the already-bound exact Application."""
        if self.requested:
            raise RuntimeError("lifetime_duplicate_quit")
        self.check_empty()
        self.requested = True
        self.holds["app"].Quit()

    def drop(self) -> None:
        """Release caller-held references after operation and exception frames unwind."""
        previous, self.holds = self.holds, {}
        previous.clear()
