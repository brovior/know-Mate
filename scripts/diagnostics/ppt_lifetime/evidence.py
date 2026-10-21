"""Strict content-free evidence and conservative lifetime result interpretation."""
from __future__ import annotations

from typing import Any

ARMS = ("natural_drop", "exact_app_external_quit")
EVENTS = frozenset({"native_scope_acquired", "host_bound", "document_opened", "synthetic_read",
                    "document_closed", "idle_checked", "quit_requested", "quit_returned",
                    "references_dropped", "native_pair_completed", "caller_failed", "cleanup_failed", "caller_complete",
                    "os_observed", "observer_failed"})
FIELDS = {"event", "arm", "run_id", "pid", "creation", "qpc_ns", "status", "error_type",
          "hresult", "error_code", "characters", "sha256", "stage"}
ERROR_TYPES = {"RuntimeError", "OSError", "ValueError", "TypeError", "com_error", "unknown"}


def validate_binding(binding: dict[str, Any]) -> None:
    """Require one positive native identity and one arm-specific run identifier."""
    import re
    if (type(binding) is not dict or set(binding) != {"run_id", "arm", "pid", "creation"}
            or type(binding["arm"]) is not str or binding["arm"] not in ARMS
            or type(binding["run_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", binding["run_id"])
            or type(binding["pid"]) is not int or not 0 < binding["pid"] <= 0xffffffff
            or type(binding["creation"]) is not int or not 0 < binding["creation"] <= 0xffffffffffffffff):
        raise ValueError("lifetime_binding")


def validate_record(row: dict[str, Any], binding: dict[str, Any]) -> None:
    """Reject foreign bindings, arbitrary strings, body fields and malformed primitives."""
    import re
    validate_binding(binding)
    if (type(row) is not dict or not set(row) <= FIELDS or type(row.get("event")) is not str or row["event"] not in EVENTS
            or any(type(row.get(key)) is not type(value) or row.get(key) != value
                   for key, value in binding.items())
            or type(row.get("qpc_ns")) is not int or not 0 <= row["qpc_ns"] <= 0x7fffffffffffffff):
        raise ValueError("lifetime_evidence_binding")
    for key in ("hresult", "error_code", "characters"):
        if key in row and row[key] is not None and (type(row[key]) is not int or not -0x80000000 <= row[key] <= 0xffffffff):
            raise ValueError("lifetime_evidence_integer")
    if "error_type" in row and row["error_type"] not in ERROR_TYPES:
        raise ValueError("lifetime_evidence_error_type")
    if "status" in row and row["status"] not in {"exited", "alive", "observation_failed", "target_unconfirmed"}:
        raise ValueError("lifetime_evidence_status")
    if "sha256" in row and (type(row["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
        raise ValueError("lifetime_evidence_hash")
    if "stage" in row and row["stage"] not in {"bind", "open", "read", "close", "idle", "quit", "drop",
                                               "native_initialize", "native_uninitialize"}:
        raise ValueError("lifetime_evidence_stage")
    if row["event"] == "os_observed" and "status" not in row:
        raise ValueError("lifetime_evidence_missing_status")


def summarize(manifest: dict[str, Any], controller: list[dict[str, Any]],
              observations: list[dict[str, Any]], caller_alive: bool | None,
              incomplete_transport: bool = False) -> dict[str, Any]:
    """Keep request return, reference scopes, OS exit and user impact separate."""
    binding = manifest["binding"]
    validate_binding(binding)
    if binding.get("arm") not in ARMS or manifest.get("mode") not in {"mock", "live"}:
        raise ValueError("lifetime_evidence_manifest")
    for row in controller + observations:
        validate_record(row, binding)
    events = [row["event"] for row in controller]
    if any(events.count(event) > 1 for event in set(events)):
        raise ValueError("lifetime_duplicate_controller_event")
    expected = ["native_scope_acquired", "host_bound", "document_opened", "synthetic_read",
                "document_closed", "idle_checked", "quit_requested", "quit_returned",
                "references_dropped", "native_pair_completed", "caller_complete"]
    ordered = [expected.index(event) for event in events if event in expected]
    if ordered != sorted(ordered):
        raise ValueError("lifetime_controller_order")
    times = [row["qpc_ns"] for row in controller]
    if times != sorted(times):
        raise ValueError("lifetime_controller_clock")
    prerequisites = {"host_bound": "native_scope_acquired", "document_opened": "host_bound",
                     "synthetic_read": "document_opened", "document_closed": "synthetic_read",
                     "idle_checked": "document_closed", "quit_requested": "idle_checked",
                     "native_pair_completed": "references_dropped"}
    if any(event in events and prerequisite not in events for event, prerequisite in prerequisites.items()):
        raise ValueError("lifetime_controller_missing_prerequisite")
    if ("quit_returned" in events and "quit_requested" not in events
            or binding["arm"] == "natural_drop" and "quit_requested" in events):
        raise ValueError("lifetime_quit_contract")
    statuses = [row.get("status") for row in observations if row["event"] == "os_observed"]
    bound = "host_bound" in events
    failed = (incomplete_transport or "observation_failed" in statuses
              or any(row["event"] == "observer_failed" for row in observations))
    bound_at = next((row["qpc_ns"] for row in controller if row["event"] == "host_bound"), None)
    exited = bound and any(row.get("status") == "exited" and row["qpc_ns"] >= bound_at for row in observations)
    unconfirmed = not bound or "target_unconfirmed" in statuses or "exited" in statuses and not exited
    from .fixture import TEXT, TEXT_SHA256
    reads = [row for row in controller if row["event"] == "synthetic_read"]
    read_verified = len(reads) == 1 and reads[0].get("characters") == len(TEXT) and reads[0].get("sha256") == TEXT_SHA256
    required = {"native_scope_acquired", "host_bound", "document_opened", "synthetic_read", "document_closed",
                "idle_checked", "references_dropped", "native_pair_completed", "caller_complete"}
    if binding["arm"] == "exact_app_external_quit":
        required.update(("quit_requested", "quit_returned"))
    complete = (required <= set(events) and read_verified and not {"caller_failed", "cleanup_failed"} & set(events)
                and not failed and exited and caller_alive is False)
    return dict(schema=1, binding=binding, mode=manifest["mode"], document_closed="document_closed" in events,
                own_references_scope_ended="references_dropped" in events,
                own_native_pair_completed="native_pair_completed" in events,
                whole_apartment_teardown="UNKNOWN", quit_requested="quit_requested" in events,
                quit_returned="quit_returned" in events,
                os_exit=("target_unconfirmed" if unconfirmed else "exited" if exited else
                         "observation_failed" if failed else "remaining"),
                actual_os_exit_verified=manifest["mode"] == "live" and exited and not failed,
                normal_exit_cause="UNKNOWN", user_document_impact="UNVERIFIED",
                caller_alive=caller_alive, caller_completed="caller_complete" in events,
                caller_failed="caller_failed" in events, evidence_incomplete=failed,
                cleanup_failed="cleanup_failed" in events,
                synthetic_read_verified=read_verified, protocol_complete=complete,
                experiment_complete=manifest["mode"] == "live" and complete,
                live_approved=False)
