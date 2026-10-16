"""Strict fixed-field protocol for the test-only PPT short report."""
from __future__ import annotations

import re

WRITER_MODES = (
    "create", "replace", "collision", "missing", "malformed", "identity", "state", "size",
    "readonly", "hardlink", "reparse", "reparse-directory", "policy", "external-reader", "native-failure",
    "native-failure-io", "native-failure-close", "fake-success", "fault-source_open", "fault-source_acl",
    "fault-source_write", "fault-source_flush", "fault-source_written", "fault-target_close", "fault-commit_call",
    "fault-source_close", "pending", "io-incomplete", "io-unwritten", "interrupt-before", "interrupt-after",
)
WRITER_STAGES = frozenset({
    "none", "unavailable", "preflight", "reservation_create", "reservation_load", "target_open", "target_validated",
    "target_close", "target_closed", "source_open", "source_written", "commit_call", "commit_return", "commit_verify",
    "source_close", "source_closed", "source_close_failed", "source_acl", "source_write", "source_flush",
    "native_unconfirmed", "preservation_verify", "verify", "cleanup", "complete", "process_start",
    "fixture_prepare", "fixture_validate", "fixture_read", "fixture_write", "cleanup_validate", "cleanup_enumerate",
    "cleanup_acl", "cleanup_delete", "cleanup_verify",
})
WRITER_CODES = frozenset({
    "none", "exception", "target_not_closed", "source_missing", "target_outside_fixture", "initial_state", "final_state",
    "hardlink_fixture_failed", "external_reader_open_failed", "expected_rejection_missing", "missing_target_recreated",
    "rejected_target_content_changed", "rejected_target_identity_changed", "invalid_target_reached_commit",
    "rejected_source_changed", "cwd_decoy_changed", "source_identity_not_preserved", "source_security_not_preserved",
    "target_security_policy", "argument_count", "fixture_invalid", "programdata_fixture_forbidden", "lease_count_invalid",
    "junction_fixture_failed", "junction_not_preserved", "committed_close_fault_state", "fault_reached_native",
    "source_handle_leaked", "target_handle_leaked", "primary_native_failure_changed", "fake_success_accepted",
    "close_failure_not_classified", "process_start_failed", "fixture_identity", "fixture_link_count",
    "fixture_creation_failed", "fixture_read_accepted", "fixture_write_accepted", "fixture_preservation",
    "cleanup_identity_missing", "cleanup_identity_mismatch", "cleanup_owner_mismatch", "cleanup_path_invalid",
    "cleanup_exception", "cleanup_external_changed", "cleanup_contract", "cleanup_not_refused", "compiler_failed",
})
ERROR_TYPES = frozenset({"none", "IOException", "Win32Exception", "policy", "access", "other", "contract",
    "UnauthorizedAccessException", "SecurityException", "InvalidOperationException", "ArgumentException",
    "NotSupportedException", "Exception"})
RESULTS = frozenset({"PASS", "FAIL", "TIMEOUT", "SKIP", "UNEXECUTED"})


def _number(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,10}", value)) and int(value) <= 0xFFFFFFFF


def _native(value: str) -> bool:
    return value == "none" or bool(re.fullmatch(r"[0-9A-F]{8}", value))


def fields(line: str, marker: str) -> dict[str, str] | None:
    """Read one exact ASCII marker with unique complete key=value fields."""
    if not line.startswith(marker + " "):
        return None
    parts = line[len(marker) + 1:].split(" ")
    result = {}
    for part in parts:
        if not re.fullmatch(r"[a-z_]+=[A-Za-z0-9_-]+", part):
            return None
        key, value = part.split("=")
        if key in result:
            return None
        result[key] = value
    return result


def valid_writer_case(item: dict[str, str]) -> bool:
    """Accept only the complete additive writer case schema and bounded values."""
    enums = {
        "category": {"actual_writer"}, "mode": set(WRITER_MODES), "leases": {"1", "2"},
        "result": RESULTS, "expected": {"none", "reject", "success", "isolate", "interrupt"},
        "actual": {"none", "reject", "success", "isolate", "interrupt", "unverified"},
        "bits": {"unavailable", "32", "64"}, "last_stage": WRITER_STAGES, "error_code": WRITER_CODES,
        "setup_stage": {"none", "anchor_open", "parent_open", "parent_policy", "child_open", "child_policy",
                        "request_create", "request_acl", "request_commit", "lease_recheck"},
        "cleanup_result": {"UNCONFIRMED", "PASS", "FAIL", "INCOMPLETE"},
    }
    numbers = {"winerror", "errno"}
    native = {"ntstatus", "io_status"}
    for phase in ("primary", "fixture", "cleanup"):
        enums[phase + "_stage"] = WRITER_STAGES
        enums[phase + "_error_type"] = ERROR_TYPES
        enums[phase + "_code"] = WRITER_CODES
        numbers.add(phase + "_winerror")
        if phase != "fixture":
            native.update({phase + "_ntstatus", phase + "_io_status"})
    valid = (set(item) == set(enums) | numbers | native and
            all(item[key] in allowed for key, allowed in enums.items()) and
            all(item[key] == "none" or _number(item[key]) for key in numbers) and
            all(_native(item[key]) for key in native))
    if not valid:
        return False
    if item["result"] == "PASS":
        return (item["expected"] == item["actual"] != "none" and item["bits"] in {"32", "64"}
                and item["cleanup_result"] == "PASS" and item["error_code"] == "none"
                and all(item[phase + "_code"] == "none" for phase in ("primary", "fixture", "cleanup")))
    if item["result"] == "UNEXECUTED":
        return (item["fixture_stage"] == "fixture_prepare" and item["fixture_code"] == "fixture_creation_failed"
                and _number(item["fixture_winerror"]))
    return True


def valid_stdout(item: dict[str, str]) -> bool:
    """Validate the stdout progress/finalizer marker without exception messages."""
    enums = {
        "phase": {"start", "ready", "close_observation", "release", "exit", "complete"},
        "last_stage": {"unavailable", "ready", "release_seen", "stdout_before", "stdout_after", "done", "before_return"},
        "actual_exit_before_cleanup": {"true", "false"}, "done": {"true", "false"},
        "cleanup": {"PASS", "FAIL"},
        "cleanup_error": {"none", "release", "observation_close", "wait", "terminate", "exit_unconfirmed", "verifier_close", "delete"},
    }
    numbers = {"wait_result", "winerror", "cleanup_wait_result", "cleanup_winerror", "actual_exit_code", "cleanup_error_winerror"}
    return (set(item) == set(enums) | numbers | {"stage_started_ms"} and
            all(item[key] in allowed for key, allowed in enums.items()) and
            all(item[key] == "unavailable" or _number(item[key]) for key in numbers) and
            (item["stage_started_ms"] == "unavailable" or
             bool(re.fullmatch(r"[0-9]{1,12}", item["stage_started_ms"]))))


def marker_line(marker: str, item: dict[str, str]) -> str:
    """Reconstruct a validated marker without copying untrusted source text."""
    return marker + " " + " ".join(f"{key}={value}" for key, value in item.items())


def valid_cleanup(item: dict[str, str]) -> bool:
    """Validate the independent cleanup safety gate's fixed error evidence."""
    return (set(item) == {"phase", "stage", "error_type", "code", "winerror"}
            and item["phase"] in {"primary", "cleanup"}
            and item["stage"] in {"preflight", "compile", "fixture_create", "refusal_missing_id", "refusal_wrong_id",
                "preservation_verify", "fixture_junction", "cleanup_regular", "external_verify", "refusal_reparse_root",
                "cleanup_root", "cleanup_external"}
            and item["error_type"] in {"contract", "Win32Exception", "IOException", "access", "other"}
            and item["code"] in WRITER_CODES - {"none"}
            and (item["winerror"] == "none" or _number(item["winerror"])))
