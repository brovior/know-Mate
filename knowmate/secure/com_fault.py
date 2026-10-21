"""Bounded, content-free COM error decoding and per-file failure evidence."""
from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field
from typing import Any

FATAL_HRESULTS = frozenset({0x800706BA, 0x800706BE, 0x800706BF, 0x80010006,
                           0x80010007, 0x80010012, 0x80010108, 0x800401FD})
_PROBE_KINDS = frozenset({"com", "binding", "argument", "null_reference", "invalid_operation", "other"})
_PROBE_STAGES = frozenset({
    "application", "application_get", "host_identity", "document_identity", "application_compare",
    "slide_count", "slide_get", "shapes", "shapes_get", "read_shapes", "shape_iter", "shape_read", "serialize",
    "shape_type", "group_items", "group_iter", "has_table", "table_get", "table_rows", "table_columns",
    "cell_get", "cell_shape", "cell_frame", "cell_range", "cell_text",
    "has_text_frame", "text_frame", "text_range", "text_read",
    "path_validate", "host_presentations", "host_document_count", "host_document_get", "host_document_path",
})


def _attribute(exc: BaseException, name: str) -> Any:
    try:
        return getattr(exc, name, None)
    except Exception:
        return None


def _code(value: Any) -> int | None:
    if type(value) is int and -(1 << 31) <= value <= 0xFFFFFFFF:
        return value & 0xFFFFFFFF
    return None


def outer_hresult(exc: BaseException) -> int | None:
    """Decode only the root exception's strict outer 32-bit HRESULT."""
    code = _code(_attribute(exc, "hresult"))
    if code is not None:
        return code
    args = _attribute(exc, "args")
    return _code(args[0]) if isinstance(args, (tuple, list)) and args else None


def _root_inner_codes(exc: BaseException, app_exe: str | None) -> tuple[int, ...]:
    info = _attribute(exc, "excepinfo")
    valid_info = isinstance(info, (tuple, list)) and len(info) == 6
    code = _code(info[5]) if valid_info else None
    if code is None:
        args = _attribute(exc, "args")
        fallback = args[2] if isinstance(args, (tuple, list)) and len(args) > 2 else None
        if isinstance(fallback, (tuple, list)) and len(fallback) == 6:
            code = _code(fallback[5])
            if not valid_info:
                info, valid_info = fallback, True
    codes = [] if code is None else [code]
    if app_exe == "POWERPNT.EXE" and valid_info:
        description = info[2]
        if isinstance(description, str) and len(description) <= 256:
            match = re.fullmatch(r"probe_extract_failed:([a-z_]+):(?:([a-z_]+):)?([0-9A-F]{8})", description)
            if match and match[1] in _PROBE_STAGES and (match[2] is None or match[2] in _PROBE_KINDS):
                codes.append(int(match[3], 16))
    return tuple(dict.fromkeys(codes))


def exception_hresult_codes(exc: BaseException, *, app_exe: str | None = None) -> tuple[int, ...]:
    """Decode structured codes from at most eight explicitly caused exceptions."""
    codes: list[int] = []
    seen: set[int] = set()
    current = exc
    for _ in range(8):
        if not isinstance(current, BaseException) or id(current) in seen:
            break
        seen.add(id(current))
        outer = outer_hresult(current)
        if outer is not None:
            codes.append(outer)
        codes.extend(_root_inner_codes(current, app_exe))
        current = _attribute(current, "__cause__")
    return tuple(dict.fromkeys(codes))


def fatal_hresult(exc: BaseException, *, app_exe: str | None = None) -> int | None:
    """Return the first structured transport failure from the bounded chain."""
    return next((code for code in exception_hresult_codes(exc, app_exe=app_exe)
                 if code in FATAL_HRESULTS), None)


@dataclass(frozen=True)
class ComFault:
    """Retain an original error in memory while exposing safe root diagnostics."""

    stage: str
    phase: str | None
    error_type: str
    outer_hresult: int | None
    inner_hresults: tuple[int, ...]
    original: BaseException = field(repr=False, compare=False)


@dataclass(frozen=True)
class ComFailureEvidence:
    """Keep the primary failure separate from all cleanup failures and poison."""

    primary: ComFault | None
    cleanup: tuple[ComFault, ...]
    poison_fault: ComFault
    poison_hresult: int | None


def make_com_fault(stage: str, exc: BaseException, *, app_exe: str | None = None,
                   phase: str | None = None) -> ComFault:
    """Capture root diagnostics without formatting the exception or traceback."""
    return ComFault(stage, phase, type(exc).__name__, outer_hresult(exc),
                    _root_inner_codes(exc, app_exe), exc)


def make_fault(exc: BaseException, stage: str, *, app_exe: str | None = None,
               phase: str | None = None) -> ComFault:
    """Capture an explicit operation fault using exception-first call syntax."""
    return make_com_fault(stage, exc, app_exe=app_exe, phase=phase)


def clear_com_error_frames(exc: BaseException) -> None:
    """Drop finished parser frames after scalar classification, before recovery."""
    from knowmate.secure.office_guard import OfficeOwnershipProbeError, OfficeOwnershipCleanupError
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        evidence = _attribute(current, "evidence")
        if isinstance(evidence, ComFailureEvidence):
            faults = (evidence.primary, *evidence.cleanup, evidence.poison_fault)
            pending.extend(fault.original for fault in faults if fault is not None)
        if isinstance(current, OfficeOwnershipProbeError):
            current.document = None
            if isinstance(current, OfficeOwnershipCleanupError):
                for name in ("probe_error", "close_error", "restore_error"):
                    original = getattr(current, name)
                    if isinstance(original, BaseException):
                        pending.append(original)
                    setattr(current, name, None)
        pending.extend(link for link in (current.__cause__, current.__context__)
                       if isinstance(link, BaseException))
        if current.__traceback__ is not None:
            # clear_frames skips executing frames; it never mutates live COM work.
            traceback.clear_frames(current.__traceback__)
        current.__traceback__ = None
        current.__cause__ = None
        current.__context__ = None


def fault_fatal_hresult(fault: ComFault, *, app_exe: str | None = None) -> int | None:
    """Find transport failure in the explicitly retained original error."""
    return fatal_hresult(fault.original, app_exe=app_exe)
