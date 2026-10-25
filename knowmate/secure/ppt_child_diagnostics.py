"""Validate bounded child-helper telemetry without changing installation authority."""
from __future__ import annotations

from dataclasses import dataclass
import re


CHILD_DIAGNOSTICS_ABSENT = object()
CHECK_STATUSES = frozenset({"ready", "missing", "outdated", "repairable", "disabled",
    "conflict", "busy", "unknown", "failed"})
CHECK_REASONS = frozenset({"registration_ok", "not_installed", "version_old",
    "registration_missing", "disabled", "hkcu_shadow", "registration_foreign",
    "office_busy", "office_unknown", "mutex_busy", "mutex_unavailable",
    "reservation_unresolved", "journal_unresolved", "prototype_gate",
    "payload_invalid", "acl_unavailable", "permission_declined", "launch_failed",
    "child_unconfirmed", "mutation_failed", "removed", "check_failed"})
ERROR_TYPES = frozenset({"policy", "Win32Exception", "UnauthorizedAccessException",
    "IOException", "SecurityException", "InvalidOperationException", "ArgumentException",
    "NotSupportedException", "Exception"})
CHILD_STAGES = frozenset({"anchor_open", "parent_open", "parent_policy", "child_open",
    "child_policy", "request_create", "request_acl", "request_commit", "lease_recheck",
    "parent_repair_check", "parent_repair_backup", "parent_repair_write",
    "parent_repair_verify", "parent_repair_recover", "child_payload_check", "child_admin_check",
    "child_machine_lock", "child_lease_acquire", "child_reservation_load", "child_reservation_enter",
    "mutation_office_check", "mutation_reservations_check", "mutation_lease_check",
    "mutation_version_check", "mutation_recovery", "mutation_machine_check",
    "mutation_ownership_check", "install_root_create", "install_root_policy", "install_journal",
    "install_payload_copy", "install_file_acl", "install_registry", "install_record",
    "install_verify", "install_rollback", "child_complete", "machine_record_check",
    "machine_office_check", "machine_registry_check"})
CHILD_TARGETS = frozenset({"none", "request", "program_files_parent", "reader_root",
    "version_directory", "journal", "record", "payload", "registry"})
CHILD_POLICY_REASONS = frozenset({"unknown_write", "unsupported_ace", "unsupported_scope",
    "unsupported_mask", "unsafe_owner", "missing_trusted_principal", "required_access_missing",
    "namespace_changed", "reservation_released_retry_required", "mutation_blocked",
    "installation_unknown", "installation_view_changed", "root_owner_untrusted",
    "root_write_untrusted", "root_create_failed"})
_TRANSPORTS = frozenset({"received", "unavailable", "peer_mismatch", "malformed", "timeout"})
_SEQUENCES = frozenset({(), ("parent_repair",), ("install",), ("remove",), ("parent_repair", "install")})
_OPERATION_SEQUENCES = {
    "prepare": _SEQUENCES - {("remove",)},
    "remove": frozenset({(), ("remove",)}),
}
_RECORD_KEYS = frozenset({"phase", "transport", "child_exit_code", "last_stage", "last_target", "primary", "secondary"})
_ERROR_KEYS = frozenset({"stage", "target", "error_type", "policy_reason", "winerror", "hresult",
    "native_stage", "ntstatus", "io_status", "check_status", "check_reason"})


@dataclass(frozen=True)
class ChildDiagnosticError:
    """Store only validated, body-free error scalars."""

    stage: str
    target: str
    error_type: str
    policy_reason: str | None
    winerror: int | None
    hresult: str | None
    native_stage: str | None
    ntstatus: str | None
    io_status: str | None
    check_status: str | None
    check_reason: str | None


@dataclass(frozen=True)
class ChildDiagnostic:
    """Observe a single authenticated elevation attempt independently of the outer exit."""

    phase: str
    transport: str
    child_exit_code: int | None
    last_stage: str | None
    last_target: str | None
    primary: ChildDiagnosticError | None
    secondary: ChildDiagnosticError | None


@dataclass(frozen=True)
class ChildDiagnosticBundle:
    """Keep telemetry validation state separate from installation results."""

    state: str
    records: tuple[ChildDiagnostic, ...] = ()


def _member(value: object, allowed: frozenset[str]) -> bool:
    """Exclude arbitrary objects before using a fixed telemetry vocabulary."""
    return type(value) is str and value in allowed


def _hex(value: object) -> bool:
    """Accept only the unchanged eight-digit uppercase hexadecimal representation."""
    return type(value) is str and re.fullmatch(r"[0-9A-F]{8}", value) is not None


def _error(value: object) -> ChildDiagnosticError:
    """Copy one exact error tuple after checking native, policy and check fields."""
    if type(value) is not dict or set(value) != _ERROR_KEYS:
        raise ValueError("child_error_fields")
    if (not _member(value["stage"], CHILD_STAGES) or not _member(value["target"], CHILD_TARGETS)
            or not _member(value["error_type"], ERROR_TYPES)):
        raise ValueError("child_error_vocabulary")
    if value["policy_reason"] is not None and not _member(value["policy_reason"], CHILD_POLICY_REASONS):
        raise ValueError("child_policy_reason")
    winerror, hresult = value["winerror"], value["hresult"]
    if winerror is not None and (type(winerror) is not int or not 0 <= winerror <= 2**31 - 1
                                 or value["error_type"] != "Win32Exception"):
        raise ValueError("child_winerror")
    if hresult is not None and not _hex(hresult):
        raise ValueError("child_hresult")
    if value["error_type"] == "policy" and (winerror is not None or hresult is not None):
        raise ValueError("child_policy_os_error")
    native = value["native_stage"], value["ntstatus"], value["io_status"]
    if native != (None, None, None):
        stage, status, io_status = native
        if (value["stage"] != "request_commit" or not _hex(status)
                or (io_status is not None and not _hex(io_status))
                or not ((stage == "commit_return" and value["error_type"] == "IOException" and winerror is None)
                        or (stage in {"commit_verify", "commit_close"} and status == "00000000"))):
            raise ValueError("child_native")
    check = value["check_status"], value["check_reason"]
    if check != (None, None) and (not _member(check[0], CHECK_STATUSES) or not _member(check[1], CHECK_REASONS)):
        raise ValueError("child_check")
    return ChildDiagnosticError(**value)


def _record(value: object) -> ChildDiagnostic:
    """Reject inconsistent successful, unavailable and failed child observations."""
    if type(value) is not dict or set(value) != _RECORD_KEYS:
        raise ValueError("child_fields")
    if not _member(value["phase"], frozenset({"parent_repair", "install", "remove"})):
        raise ValueError("child_phase")
    if not _member(value["transport"], _TRANSPORTS):
        raise ValueError("child_transport")
    code = value["child_exit_code"]
    if code is not None and (type(code) is not int or not 0 <= code <= 2**32 - 1):
        raise ValueError("child_exit")
    if value["transport"] != "received":
        if any(value[name] is not None for name in ("last_stage", "last_target", "primary", "secondary")):
            raise ValueError("child_unavailable")
        return ChildDiagnostic(**value)
    if (code is None or not _member(value["last_stage"], CHILD_STAGES)
            or not _member(value["last_target"], CHILD_TARGETS)):
        raise ValueError("child_received")
    primary = _error(value["primary"]) if value["primary"] is not None else None
    secondary = _error(value["secondary"]) if value["secondary"] is not None else None
    if (code == 0 and (primary is not None or secondary is not None)) or (code != 0 and primary is None):
        raise ValueError("child_outcome")
    return ChildDiagnostic(**{**value, "primary": primary, "secondary": secondary})


def sanitize_child_diagnostics(value: object, *, operation: str | None = None) -> ChildDiagnosticBundle:
    """Drop an invalid optional group without changing the valid base installation result."""
    if value is CHILD_DIAGNOSTICS_ABSENT:
        return ChildDiagnosticBundle("absent")
    if type(value) is not list or len(value) > 2:
        return ChildDiagnosticBundle("malformed")
    try:
        records = tuple(_record(item) for item in value)
        phases = tuple(record.phase for record in records)
        if phases not in _SEQUENCES:
            raise ValueError("child_sequence")
        if operation is not None and phases not in _OPERATION_SEQUENCES.get(operation, {()}):
            raise ValueError("child_operation")
        return ChildDiagnosticBundle("valid", records)
    except (TypeError, ValueError, KeyError):
        return ChildDiagnosticBundle("malformed")
