"""One-apartment COM child adapter; primitives alone cross the process boundary."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from scripts.diagnostics.ppt_capture.controller import OneShotRequest, observe_for_capture
from scripts.diagnostics.ppt_capture.identity import error_fields
from scripts.ppt_reader.ppt_identity_observation import Pywin32Backend


class Session:
    """Own every caller COM wrapper until one ordered reference-drop operation."""

    def __init__(self, native: Any, config: dict[str, Any], emit: Callable[..., None]) -> None:
        """Keep only explicitly named holds; never force Release or garbage collection."""
        self.native, self.config, self.emit = native, config, emit
        self.holds: dict[str, Any] = {}
        self.failed = False

    def check(self) -> None:
        """Stop all new COM calls on cancellation, unknown or changed exact ownership."""
        if self.failed or self.config.get("persist_failed") or self.native.wait(self.config["cancel"]):
            raise RuntimeError("capture_no_further_com")
        from knowmate.secure.ppt_capture_observer import processes
        names = [p for p in processes(self.native) if p["name"] in {"powerpnt.exe", "aegisdesk.exe"}]
        if len(names) != 1 or names[0]["pid"] != self.config["pid"]:
            self.failed = True
            raise RuntimeError("target_set_changed")
        if self.native.creation(self.config["target_handle"]) != self.config["creation"] or self.native.wait(self.config["target_handle"]):
            self.failed = True
            raise RuntimeError("target_identity_changed")
        if "hwnd" in self.config and self.native.owner(self.config["hwnd"], self.config["pid"]) != self.config["owner_thread"]:
            self.failed = True
            raise RuntimeError("target_window_owner_changed")

    def boundary(self, member: str, operation: str, action: Callable[[], Any], **fields: Any) -> Any:
        """Record one independent GetIDsOfNames/Invoke boundary and latch first failure."""
        self.check()
        self.emit("com_begin", member=member, operation=operation, **fields)
        try:
            if member == "Quit" and operation == "Invoke":
                self.emit("quit_begin", path="external", **fields)
            result = action()
        except Exception as error:
            self.failed = True
            self.emit("com_fail", member=member, operation=operation, error=error_fields(error), **fields)
            raise
        self.emit("com_return", member=member, operation=operation, **fields)
        return result

    def call(self, value: Any, member: str, flags: int, *arguments: Any) -> Any:
        """Resolve once and invoke once, retaining no bound method or exception."""
        raw = Pywin32Backend.raw(value)
        dispid = self.boundary(member, "GetIDsOfNames", lambda: raw.GetIDsOfNames(0, member), flags=flags)
        if type(dispid) is not int:
            self.failed = True
            raise RuntimeError("dispid_unknown")
        return self.boundary(member, "Invoke", lambda: raw.Invoke(dispid, 0, flags, True, *arguments), dispid=dispid, flags=flags)

    def counts(self) -> None:
        """Apply the existing three empty-collection guards without enumerating documents."""
        for member in ("Presentations", "ProtectedViewWindows", "SlideShowWindows"):
            collection = self.call(self.holds["app"], member, 2)
            count = self.call(collection, "Count", 2)
            collection = None
            if type(count) is not int or count != 0:
                self.failed = True
                raise RuntimeError("nonempty_or_unknown_no_quit")

    def drop(self) -> None:
        """Drop same-apartment app/service/intermediates/prepared args before uninitializing."""
        try:
            self.emit("controller_refs_drop_begin")
        finally:
            self.holds.clear()
        self.emit("controller_refs_drop_end")


def _operate(session: Session, pythoncom: Any, client: Any) -> str:
    """Run one linear operation; unwinding this frame releases temporary COM holds."""
    from knowmate.secure import office_guard
    from knowmate.secure.ppt_installation import MachineReadGuard
    from knowmate.secure.ppt_capture_preflight import registration_fingerprint
    config, native, emit, holds = session.config, session.native, session.emit, session.holds
    try:
        with MachineReadGuard():
            if registration_fingerprint() != config["registration_sha256"]:
                raise RuntimeError("registration_changed_under_machine_lock")
            session.check()
            # No activation fallback and no RPC retries: unavailable ROT is unknown.
            holds["app"] = session.boundary("PowerPoint.Application", "GetActiveObject", lambda: client.GetActiveObject("PowerPoint.Application"))
            session.counts()
            holds["original_caption"] = session.call(holds["app"], "Caption", 2)
            holds["presentations"] = session.call(holds["app"], "Presentations", 2)
            holds["blank"] = session.call(holds["presentations"], "Add", 1, -1)
            marker = "AegisDesk-capture-" + config["run_id"]
            session.call(holds["app"], "Caption", 4, marker)
            if session.call(holds["app"], "Caption", 2) != marker:
                raise RuntimeError("ownership_marker_changed")
            hwnd = office_guard._hwnd_for_caption_marker(marker)
            if not hwnd:
                raise RuntimeError("ownership_hwnd_missing")
            owner = native.owner(hwnd, config["pid"])
            config["hwnd"], config["owner_thread"] = hwnd, owner
            emit("ownership_proved", native_owner_thread=owner, launching_pid=os.getpid(), caller_pid=os.getpid(), apartment="STA")
            session.call(holds["app"], "Caption", 4, holds.pop("original_caption"))
            session.call(holds["blank"], "Close", 1)
            holds.pop("blank")
            session.counts()
            holds["addins"] = session.call(holds["app"], "COMAddIns", 2)
            holds["addin"] = session.call(holds["addins"], "Item", 1, "AegisDesk.PptReader")
            if session.call(holds["addin"], "Connect", 2) is not True:
                raise RuntimeError("addin_disabled")
            holds["service"] = session.call(holds["addin"], "Object", 2)
            service = holds["service"]
            if (session.call(service, "ReaderVersion", 1) != config["pins"]["version"]
                    or session.call(service, "ProtocolVersion", 1) != 3):
                raise RuntimeError("service_incompatible")
            source = Path(config["pins"]["fixture_path"])
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            if source_hash != config["pins"]["fixture_sha256"]:
                raise RuntimeError("fixture_changed")
            holds["fixture"] = session.call(holds["presentations"], "Open", 1, str(source), -1, 0, 0)
            # The product reader only returns body in memory. Its callback checks
            # ownership/cancellation; the diagnostic records length/hash only.
            text = _synthetic_read(session, str(source))
            length, sha = len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()
            text = None
            emit("synthetic_read", characters=length, sha256=sha)
            if length != config["pins"]["fixture_text_length"] or sha != config["pins"]["fixture_text_sha256"]:
                raise RuntimeError("synthetic_read_mismatch")
            session.call(holds["fixture"], "Close", 1)
            holds.pop("fixture")
            holds.pop("slides")
            if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
                raise RuntimeError("synthetic_source_changed")
            holds.pop("addin"); holds.pop("addins"); holds.pop("presentations")
            session.counts()
            if native.owner(hwnd, config["pid"]) != owner:
                raise RuntimeError("owner_thread_changed")
            native.bind_mapping(config["mapping"], config["pid"], config["creation"], owner)
            if session.call(service, "BindExternalBootstrap", 1, config["pid"], str(config["creation"]), config["nonce"], hwnd) != "bound":
                emit("request_not_run", reason="bind_refused")
                return "blocked"
            backend = Pywin32Backend(pythoncom, client.VARIANT)
            observation = observe_for_capture(holds["app"], holds["app"], config["nonce"], backend,
                lambda candidate: json.loads(session.call(service, "ObserveExternalIdentity", 1, config["nonce"], candidate)),
                lambda value: emit("identity_evidence", observation=value), lambda row: emit(**row))
            if not observation["exported"] or not observation["eligible"] or observation["primary"]:
                emit("request_not_run", reason="identity_or_export_refused")
                return "blocked"
            emit("observation_saved")
            if not native.wait(config["admit"], 3000):
                emit("request_not_run", reason="collector_admission_missing")
                return "blocked"
            request = OneShotRequest(config["path"], lambda row: emit(**row))
            def match() -> None:
                session.check()
                if native.owner(hwnd, config["pid"]) != owner:
                    raise RuntimeError("owner_thread_changed")
            return request.run(observation,
                lambda: session.call(service, "TryQuitIfIdle", 1),
                lambda: session.call(service, "PrepareExternalBootstrap", 1, config["nonce"], holds["app"]),
                lambda: session.call(service, "ExternalBootstrapStatus", 1),
                lambda: native.signal(config["start"]), lambda ms: native.wait(config["ack"], ms),
                lambda: _external_quit(session), lambda: native.signal(config["complete"]), match)
    except Exception as error:
        session.failed = True
        emit("controller_failed", error=error_fields(error))
        return "unknown"


def _synthetic_read(session: Session, source: str) -> str:
    """Use the existing strict slide protocol with separately recorded COM calls."""
    from knowmate.secure.ppt_payload import decode_slide
    session.holds["slides"] = session.call(session.holds["fixture"], "Slides", 2)
    count = session.call(session.holds["slides"], "Count", 2)
    if type(count) is not int or count != 1:
        raise RuntimeError("synthetic_slide_count")
    reply = session.call(session.holds["service"], "ExtractSlide", 1, source, 1)
    texts, counts, seconds = decode_slide(reply, 1, strict=True)
    if counts["errors"]:
        raise RuntimeError("synthetic_partial_read")
    return "\n".join(texts)


def _external_quit(session: Session) -> Any:
    """Preserve PowerPoint Quit's known DISPID/flags contract with split boundaries."""
    raw = Pywin32Backend.raw(session.holds["app"])
    dispid = session.boundary("Quit", "GetIDsOfNames", lambda: raw.GetIDsOfNames(0, "Quit"), flags=3)
    if type(dispid) is not int or dispid != 2021:
        raise RuntimeError("quit_dispid_changed")
    return session.boundary("Quit", "Invoke", lambda: raw.Invoke(dispid, 0, 3, 1), dispid=dispid, flags=3)


def run_apartment(native: Any, config: dict[str, Any], emit: Callable[..., None]) -> int:
    """Balance a successful STA initialization only after all operation frames unwind."""
    import pythoncom
    import win32com.client
    initialized = False
    session = Session(native, config, emit)
    failed = False
    def safe_emit(event: str, **fields: Any) -> None:
        nonlocal failed
        try:
            emit(event, **fields)
        except Exception:
            failed = True
    try:
        emit("CoInitializeEx_begin", caller_pid=os.getpid(), apartment="STA")
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
        emit("CoInitializeEx_end", caller_pid=os.getpid(), apartment="STA")
        outcome = _operate(session, pythoncom, win32com.client)
        if outcome == "unknown":
            failed = True
        emit("controller_outcome", status=outcome)
    except Exception as error:
        safe_emit("controller_failed", error=error_fields(error))
    # No exception/traceback from _operate remains active in this cleanup scope.
    try:
        try:
            session.drop()
        except Exception as error:
            failed = True
            safe_emit("cleanup_failed", operation="reference_drop", error=error_fields(error))
    finally:
        if initialized:
            safe_emit("CoUninitialize_begin")
            try:
                pythoncom.CoUninitialize()
            except Exception as error:
                failed = True
                safe_emit("cleanup_failed", operation="CoUninitialize", error=error_fields(error))
            else:
                safe_emit("CoUninitialize_end")
    return 2 if failed else 0
