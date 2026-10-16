"""Reconcile fixed PPT markers with known final pytest outcomes."""
from __future__ import annotations

import re
from dataclasses import replace

try:
    from .ppt_diagnostic_junit import NODE_IDS, junit_outcomes
    from .ppt_diagnostic_markers import (
        WRITER_MODES,
        fields,
        marker_line,
        valid_cleanup,
        valid_stdout,
        valid_writer_case,
    )
except ImportError:
    from ppt_diagnostic_junit import NODE_IDS, junit_outcomes
    from ppt_diagnostic_markers import (
        WRITER_MODES,
        fields,
        marker_line,
        valid_cleanup,
        valid_stdout,
        valid_writer_case,
    )

EXTERNAL = "PPT_REQUIRED_EXTERNAL distinct_accounts=UNEXECUTED uac=UNEXECUTED office=UNEXECUTED x86_x64=inspect_bits"
MARKERS = {
    "PPT_RENAME_SUMMARY": "baseline", "PPT_WRITER_CASE": "actual_writer",
    "PPT_WRITER_SUMMARY": "actual_writer", "PPT_ACL_SUMMARY": "acl",
    "PPT_STDOUT_SUMMARY": "stdout", "PPT_STDOUT_DIAGNOSTIC": "stdout",
    "PPT_RENAME_SECURITY_SUMMARY": "baseline",
    "PPT_CLEANUP_SUMMARY": "cleanup_contract", "PPT_CLEANUP_DIAGNOSTIC": "cleanup_contract",
}


def _problem(item: dict[str, str]) -> str:
    selected = {key: item[key] for key in ("mode", "leases", "result")}
    for phase in ("fixture", "primary", "cleanup"):
        if phase == "cleanup" and item["cleanup_result"] != "UNCONFIRMED":
            selected["cleanup_result"] = item["cleanup_result"]
        for suffix in ("stage", "error_type", "code", "winerror", "ntstatus", "io_status"):
            key = phase + "_" + suffix
            value = item.get(key, "none")
            if value != "none":
                selected[key] = value
    if not any(key.endswith("_code") for key in selected):
        for key in ("last_stage", "error_code", "winerror", "errno", "ntstatus", "io_status"):
            if item[key] not in {"none", "unavailable"}:
                selected[key] = item[key]
    return marker_line("PPT_WRITER_PROBLEM", selected)


def _markers(log: str) -> tuple[dict[str, list[dict[str, str]]], set[str]]:
    result: dict[str, list[dict[str, str]]] = {marker: [] for marker in MARKERS}
    invalid = set()
    for raw in log.splitlines():
        for marker, owner in MARKERS.items():
            prefix = NODE_IDS[owner] + " "
            line = raw.removeprefix(prefix)
            if line.startswith(marker + " "):
                item = fields(line, marker)
                if item is None:
                    invalid.add(owner)
                else:
                    result[marker].append(item)
    return result, invalid


def _counts(item: dict[str, str], keys: set[str], maximum: int) -> bool:
    return all(key in item and re.fullmatch(r"[0-9]{1,2}", item[key]) and int(item[key]) <= maximum
               for key in keys)


def build_short_report(log: str, xml: bytes, pytest_exit: int) -> list[str]:
    """Reconcile fixed markers with final known JUnit outcomes, never raw errors."""
    final = junit_outcomes(xml)
    junit_incomplete = any(value.result == "INCOMPLETE" for value in final.values())
    markers, invalid = _markers(log)
    details = []
    baseline = markers["PPT_RENAME_SUMMARY"]
    if baseline:
        item = baseline[0]
        good = (len(baseline) == 1 and set(item) == {
            "category", "comparison_failures", "native_failures", "required", "product_approval"} and
            item.get("category") == "baseline" and item.get("required") == "false" and
            item.get("product_approval") == "false" and _counts(item, {"comparison_failures"}, 6) and
            _counts(item, {"native_failures"}, 9))
        outcome = final["baseline"]
        if (not good or outcome.result == "PASS" and int(item["native_failures"]) != 0
                or outcome.result == "FAIL" and int(item["native_failures"]) == 0 and not outcome.teardown_error
                or outcome.result == "UNEXECUTED"):
            invalid.add("baseline")
        else:
            details.append(marker_line("PPT_RENAME_SUMMARY", item))
    elif final["baseline"].result == "PASS":
        invalid.add("baseline")
    security = markers["PPT_RENAME_SECURITY_SUMMARY"]
    seen = set()
    for item in security:
        bits = {"owner_equal", "dacl_equal", "protected_before", "protected_after", "canonical_before", "canonical_after"}
        controls = {"control_before", "control_after"}
        identity = (item.get("mode"), item.get("leases"))
        good = (set(item) == bits | controls | {"mode", "leases"} and item.get("mode") == "managed-replace"
                and item.get("leases") in {"0", "1", "2"} and identity not in seen
                and all(item[key] in {"0", "1"} for key in bits)
                and all(re.fullmatch(r"[0-9]{1,6}", item[key]) and int(item[key]) <= 65535 for key in controls))
        if not good or final["baseline"].result == "UNEXECUTED":
            invalid.add("baseline")
        else:
            seen.add(identity)
            details.append(marker_line("PPT_RENAME_SECURITY_SUMMARY", item))

    cases, summaries = markers["PPT_WRITER_CASE"], markers["PPT_WRITER_SUMMARY"]
    writer = final["actual_writer"].result
    if cases or summaries:
        keys = {"pass", "fail", "timeout", "skip", "unexecuted"}
        identities = {(item.get("mode"), item.get("leases")) for item in cases}
        expected = {(mode, str(leases)) for mode in WRITER_MODES for leases in (1, 2)}
        good = (len(cases) == 62 and identities == expected and all(valid_writer_case(item) for item in cases)
                and len(summaries) == 1)
        summary = summaries[0] if summaries else {}
        good = (good and set(summary) == keys | {"category", "product_approval"} and
                summary.get("category") == "actual_writer" and summary.get("product_approval") == "false" and
                _counts(summary, keys, 62))
        if good:
            counts = {key: sum(item["result"].lower() == key for item in cases) for key in keys}
            good = all(int(summary[key]) == counts[key] for key in keys)
            good = good and all(item["result"] != "PASS" or item["cleanup_result"] == "PASS" for item in cases)
            expected_final = "PASS" if counts["pass"] == 62 else "UNEXECUTED" if counts["skip"] == 62 else "FAIL"
            # Final teardown failure can follow a successful body/matrix.
            good = good and (writer == expected_final or writer == "FAIL" and final["actual_writer"].teardown_error)
        if not good:
            invalid.add("actual_writer")
        else:
            details.append(marker_line("PPT_WRITER_SUMMARY", summary))
            details.extend(_problem(item) for item in cases if item["result"] != "PASS")
    elif writer == "PASS":
        invalid.add("actual_writer")

    for owner, marker, expected in (
        ("acl", "PPT_ACL_SUMMARY", {"result": "PASS", "actual_request_writer": "true"}),
        ("stdout", "PPT_STDOUT_SUMMARY", None),
        ("cleanup_contract", "PPT_CLEANUP_SUMMARY", None),
    ):
        values = markers[marker]
        if values:
            good = len(values) == 1
            item = values[0]
            allowed = ({"result": "PASS"}, {"result": "FAIL"}) + (({"result": "UNEXECUTED"},) if owner == "cleanup_contract" else ())
            good = good and (item == expected if expected else item in allowed)
            if good:
                result = final[owner]
                contradiction = (result.result == "UNEXECUTED" and item["result"] != "UNEXECUTED"
                                 or item["result"] == "UNEXECUTED" and result.result != "UNEXECUTED"
                                 or result.result == "PASS" and item["result"] != "PASS"
                                 or result.result == "FAIL" and item["result"] == "PASS" and not result.teardown_error)
                if contradiction:
                    invalid.add(owner)
                else:
                    details.append(marker_line(marker, item))
            else:
                invalid.add(owner)
        elif final[owner].result == "PASS":
            invalid.add(owner)
    cleanup = markers["PPT_CLEANUP_DIAGNOSTIC"]
    identities = {(item.get("phase"), item.get("stage")) for item in cleanup}
    if (len(cleanup) > 3 or len(identities) != len(cleanup) or not all(valid_cleanup(item) for item in cleanup)
            or sum(item.get("phase") == "primary" for item in cleanup) > 1
            or sum(item.get("phase") == "cleanup" for item in cleanup) > 2
            or cleanup and final["cleanup_contract"].result == "PASS"):
        invalid.add("cleanup_contract")
    else:
        details.extend(marker_line("PPT_CLEANUP_PROBLEM", {key: value for key, value in item.items() if value != "none"})
                       for item in cleanup)
    stdout = markers["PPT_STDOUT_DIAGNOSTIC"]
    if stdout:
        if len(stdout) != 1 or not valid_stdout(stdout[0]):
            invalid.add("stdout")
        else:
            item = stdout[0]
            if final["stdout"].result == "PASS" and (item["phase"] != "complete" or item["last_stage"] != "before_return"
                    or item["actual_exit_before_cleanup"] != "true" or item["actual_exit_code"] != "0" or item["done"] != "true"
                    or item["wait_result"] != "0" or item["cleanup_wait_result"] != "0"
                    or item["cleanup"] != "PASS" or item["cleanup_error"] != "none"):
                invalid.add("stdout")
            else:
                selected = {key: value for key, value in item.items() if value not in {"unavailable", "none", "not_needed"}}
                details.append(marker_line("PPT_STDOUT_PROGRESS", selected))
    elif final["stdout"].result == "PASS":
        invalid.add("stdout")
    for owner in invalid:
        final[owner] = replace(final[owner], result="INCOMPLETE")
    problem_owners = {"PPT_WRITER_PROBLEM": "actual_writer", "PPT_STDOUT_PROGRESS": "stdout", "PPT_CLEANUP_PROBLEM": "cleanup_contract"}
    details = [line for line in details if (MARKERS | problem_owners)[line.split(" ", 1)[0]] not in invalid]
    # Canonical summaries always carry the final JUnit-reconciled result.
    for marker, owner in (("PPT_RENAME_SUMMARY", "baseline"), ("PPT_WRITER_SUMMARY", "actual_writer"),
                          ("PPT_ACL_SUMMARY", "acl"), ("PPT_STDOUT_SUMMARY", "stdout"),
                          ("PPT_CLEANUP_SUMMARY", "cleanup_contract")):
        existing = next((line for line in details if line.startswith(marker + " ")), None)
        item = fields(existing, marker) if existing else {}
        if existing:
            details.remove(existing)
        body_result = item.get("result")
        canonical = {"category": owner, "result": final[owner].result}
        canonical.update({key: value for key, value in item.items() if key not in {"category", "result"}})
        if body_result and body_result != canonical["result"]:
            canonical["body_result"] = body_result
        if final[owner].body_failure:
            canonical["body_failure"] = "true"
        if final[owner].teardown_error:
            canonical["teardown_error"] = "true"
        if owner in {"baseline", "actual_writer"}:
            canonical["product_approval"] = "false"
        details.append(marker_line(marker, canonical))
    required = [final[key].result for key in ("actual_writer", "acl", "stdout", "cleanup_contract")]
    gate = next((value for value in ("INCOMPLETE", "FAIL", "UNEXECUTED") if value in required), "PASS")
    # A nonzero run exit is retained even if unrelated collected tests failed.
    if pytest_exit != 0 and gate == "PASS":
        gate = "INCOMPLETE"
    status = "INCOMPLETE" if invalid or junit_incomplete else "complete"
    header = f"PPT_SHORT_REPORT status={status} required_gate={gate} product_approval=false"
    errors = (["PPT_COLLECTION_ERROR code=junit_incomplete"] if junit_incomplete else []) + (
        ["PPT_COLLECTION_ERROR code=marker_protocol_incomplete"] if invalid else [])
    return [header, *details, *errors, EXTERNAL,
            f"PPT_RENAME_REPORT pytest_exit_code={pytest_exit} product_approval=false", "PPT_SHORT_END"]
