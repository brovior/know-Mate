"""Validate additive child diagnostics while preserving the original setup contract."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import re

import pytest

from knowmate.secure import ppt_child_diagnostics as child
from knowmate.secure import ppt_installation as installation


def error(**changes: object) -> dict[str, object]:
    """Build one body-free synthetic installation error."""
    value = dict(stage="install_root_policy", target="reader_root", error_type="policy",
                 policy_reason="root_owner_untrusted", winerror=None, hresult=None,
                 native_stage=None, ntstatus=None, io_status=None, check_status=None, check_reason=None)
    value.update(changes)
    return value


def observation(**changes: object) -> dict[str, object]:
    """Build one failed elevation observation with an independent child exit."""
    value = dict(phase="install", transport="received", child_exit_code=13,
                 last_stage="install_root_policy", last_target="reader_root", primary=error(), secondary=None)
    value.update(changes)
    return value


def base_json(**changes: object) -> str:
    """Build the existing, valid base result without relying on optional telemetry."""
    value = dict(schema=1, operation="prepare", status="failed", reason="mutation_failed",
                 required_version=installation.REQUIRED_VERSION, installed_version="1.0.0.0", office_bitness=64,
                 scope="Machine", machine_safe=False, auto_install_validated=False, auto_install_mode="candidate")
    value.update(changes)
    return json.dumps(value)


@pytest.fixture(autouse=True)
def candidate_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep all contract tests independent of frozen build metadata."""
    monkeypatch.setattr(installation, "auto_install_mode", lambda: "candidate")


@pytest.mark.parametrize("outer,child_code", [(12, 13), (13, 0), (13, 12), (10, 10)])
def test_child_exit_is_not_compared_to_outer_exit(outer: int, child_code: int) -> None:
    """Retain authentic child outcomes even when the final helper outcome differs."""
    record = observation(child_exit_code=child_code)
    if child_code == 0:
        record.update(primary=None, last_stage="child_complete", last_target="none")
    value = installation.parse_setup_result(base_json(child_diagnostics=[record]), outer, "prepare")
    assert value.status == "failed" and not value.machine_safe and value.reason == "mutation_failed"
    assert value.child_diagnostic.state == "valid"
    assert value.child_diagnostic.records[0].child_exit_code == child_code


@pytest.mark.parametrize("phases", [[], ["parent_repair"], ["install"], ["remove"], ["parent_repair", "install"]])
def test_allowed_phase_sequences_preserve_each_observation(phases: list[str]) -> None:
    """Keep a repair failure or a later install failure attached to its own phase."""
    values = [observation(phase=phase) for phase in phases]
    bundle = child.sanitize_child_diagnostics(values)
    assert bundle.state == "valid" and [item.phase for item in bundle.records] == phases


@pytest.mark.parametrize("phases", [["install", "parent_repair"], ["install", "install"], ["remove", "install"],
                                   ["parent_repair", "remove"], ["parent_repair", "install", "remove"]])
def test_invalid_phase_sequences_drop_entire_group(phases: list[str]) -> None:
    """Reject stale or duplicated attempts rather than retaining a misleading subset."""
    bundle = child.sanitize_child_diagnostics([observation(phase=phase) for phase in phases])
    assert bundle.state == "malformed" and not bundle.records


@pytest.mark.parametrize("operation,phase", [("prepare", "remove"), ("remove", "install"),
    ("remove", "parent_repair"), ("check", "install"), ("recover", "install"), ("recover-parent", "parent_repair")])
def test_stale_phase_for_another_operation_drops_only_optional(operation: str, phase: str) -> None:
    """Do not attribute a previous or unrelated elevation to the current helper action."""
    value = installation.parse_setup_result(base_json(operation=operation,
        child_diagnostics=[observation(phase=phase)]), 13, operation)
    assert value.reason == "mutation_failed" and value.status == "failed" and not value.machine_safe
    assert value.child_diagnostic.state == "malformed" and not value.child_diagnostic.records


@pytest.mark.parametrize("operation,phase", [("prepare", "parent_repair"), ("prepare", "install"), ("remove", "remove")])
def test_matching_operation_keeps_child_observation(operation: str, phase: str) -> None:
    """Retain supported action/phase pairs without comparing child and outer codes."""
    value = installation.parse_setup_result(base_json(operation=operation,
        child_diagnostics=[observation(phase=phase)]), 12, operation)
    assert value.child_diagnostic.state == "valid" and value.child_diagnostic.records[0].phase == phase


@pytest.mark.parametrize("transport", ["unavailable", "peer_mismatch", "malformed", "timeout"])
@pytest.mark.parametrize("code", [None, 0, 13, 0xC0000005])
def test_unreceived_telemetry_keeps_only_actual_child_exit(transport: str, code: int | None) -> None:
    """Represent cancellation or crash without fabricating an error frame."""
    record = observation(transport=transport, child_exit_code=code, last_stage=None,
                         last_target=None, primary=None, secondary=None)
    bundle = child.sanitize_child_diagnostics([record])
    assert bundle.state == "valid" and bundle.records[0].child_exit_code == code


@pytest.mark.parametrize("invalid", [None, True, {}, "confidential-marker", [None], [True]])
def test_invalid_optional_group_does_not_replace_valid_base(invalid: object) -> None:
    """Drop malformed optional data and preserve original installation failure fields."""
    value = installation.parse_setup_result(base_json(child_diagnostics=invalid), 13, "prepare")
    assert (value.status, value.reason, value.machine_safe) == ("failed", "mutation_failed", False)
    assert value.child_diagnostic.state == "malformed" and not value.child_diagnostic.records
    assert "confidential-marker" not in repr(value)


@pytest.mark.parametrize("changes", [{"phase": "confidential-marker"}, {"transport": []},
    {"child_exit_code": True}, {"child_exit_code": -1}, {"child_exit_code": 2**32},
    {"child_exit_code": None}, {"last_stage": "confidential-marker"}, {"last_target": "C:/private"},
    {"primary": None}, {"child_exit_code": 0}, {"stdout_body": "confidential-marker"},
    {"transport": "unavailable"}])
def test_record_field_validation_is_atomic(changes: dict[str, object]) -> None:
    """Reject invalid scalars, unknown keys and contradictory outcomes."""
    record = observation(**changes)
    value = installation.parse_setup_result(base_json(child_diagnostics=[record]), 13, "prepare")
    assert value.child_diagnostic.state == "malformed" and value.reason == "mutation_failed"


@pytest.mark.parametrize("changes", [{"stage": "confidential-marker"}, {"target": "C:/private"},
    {"error_type": "confidential-marker"}, {"policy_reason": ["unknown_write"]},
    {"policy_reason": "confidential-marker"}, {"winerror": 5}, {"hresult": "80070005"},
    {"hresult": "0x80070005"}, {"message": "confidential-marker"}, {"check_status": "failed"},
    {"check_status": "failed", "check_reason": "confidential-marker"}, {"native_stage": "commit_return"}])
def test_error_tuple_rejects_unsafe_and_incomplete_fields(changes: dict[str, object]) -> None:
    """Never log raw paths/messages or synthetic OS codes for policy failures."""
    bundle = child.sanitize_child_diagnostics([observation(primary=error(**changes))])
    assert bundle.state == "malformed" and not bundle.records


@pytest.mark.parametrize("hresult", ["80070005", "80070497", "80131620"])
def test_hresult_is_kept_separate_from_win32_error(hresult: str) -> None:
    """Preserve an original managed exception HRESULT without truncating its bits."""
    bundle = child.sanitize_child_diagnostics([observation(primary=error(error_type="IOException",
        policy_reason=None, hresult=hresult))])
    actual = bundle.records[0].primary
    assert actual.hresult == hresult and actual.winerror is None


@pytest.mark.parametrize("winerror", [0, 5, 183, 2**31 - 1])
def test_real_win32_code_may_coexist_with_original_hresult(winerror: int) -> None:
    """Accept a bounded native error only when its exception category states the source."""
    bundle = child.sanitize_child_diagnostics([observation(primary=error(error_type="Win32Exception",
        policy_reason=None, winerror=winerror, hresult="80070005"))])
    assert bundle.records[0].primary.winerror == winerror


@pytest.mark.parametrize("changes", [{"winerror": True}, {"winerror": -1}, {"winerror": 2**31},
    {"hresult": "8007000a"}, {"hresult": 0x80070005}])
def test_original_error_codes_are_strict(changes: dict[str, object]) -> None:
    """Exclude booleans, overflow and noncanonical HRESULT values."""
    value = error(error_type="Win32Exception", policy_reason=None, **changes)
    assert child.sanitize_child_diagnostics([observation(primary=value)]).state == "malformed"


def test_primary_and_rollback_failure_remain_separate() -> None:
    """Keep the root policy cause even if a later rollback API also fails."""
    rollback = error(stage="install_rollback", target="journal", error_type="IOException",
                     policy_reason=None, hresult="80070497")
    record = observation(last_stage="install_rollback", last_target="journal", secondary=rollback)
    bundle = child.sanitize_child_diagnostics([record])
    assert bundle.records[0].primary.policy_reason == "root_owner_untrusted"
    assert bundle.records[0].secondary.hresult == "80070497"


@pytest.mark.parametrize("native_stage,status", [("commit_return", "C0000035"),
                                                 ("commit_verify", "00000000"), ("commit_close", "00000000")])
def test_existing_native_commit_fields_are_preserved(native_stage: str, status: str) -> None:
    """Keep bounded NTSTATUS diagnostics through the new child envelope."""
    value = error(stage="request_commit", target="request", error_type="IOException", policy_reason=None,
                  native_stage=native_stage, ntstatus=status, io_status="00000000")
    bundle = child.sanitize_child_diagnostics([observation(primary=value)])
    assert bundle.records[0].primary.ntstatus == status


def test_machine_check_rejection_keeps_status_and_reason() -> None:
    """Expose the original check observation without making it installation authority."""
    value = error(stage="mutation_machine_check", error_type="IOException", policy_reason="mutation_blocked",
                  hresult="80131620", check_status="unknown", check_reason="office_unknown")
    bundle = child.sanitize_child_diagnostics([observation(primary=value)])
    assert bundle.records[0].primary.check_reason == "office_unknown"


def test_absent_and_explicit_null_have_distinct_diagnostic_states() -> None:
    """Keep legacy helper output valid while refusing an explicitly malformed new group."""
    old = installation.parse_setup_result(base_json(), 13, "prepare")
    null = installation.parse_setup_result(base_json(child_diagnostics=None), 13, "prepare")
    assert old.child_diagnostic.state == "absent" and null.child_diagnostic.state == "malformed"
    assert old.reason == null.reason == "mutation_failed"


def test_direct_injection_and_later_mutation_are_revalidated() -> None:
    """Return independent immutable safe scalars and recheck mutable injected input."""
    raw = [observation()]
    result = installation.SetupResult(child_diagnostics=raw)
    copied = result.child_diagnostic
    raw[0]["primary"]["stage"] = "confidential-marker"
    assert result.child_diagnostic.state == "malformed"
    assert copied.records[0].primary.stage == "install_root_policy"
    with pytest.raises(FrozenInstanceError):
        copied.records[0].phase = "confidential-marker"


def test_optional_duplicate_keys_and_total_oversize_keep_base_rejection() -> None:
    """Retain the preexisting whole-output duplicate-key and size boundaries."""
    duplicate = base_json(child_diagnostics=[observation()]).replace('"phase": "install"',
        '"phase": "install", "phase": "remove"')
    oversized = base_json(child_diagnostics=[observation(last_stage="x" * 17000)])
    for text in (duplicate, oversized):
        result = installation.parse_setup_result(text, 13, "prepare")
        assert result.reason == "diagnostic_unavailable" and not result.machine_safe


def test_unknown_top_level_key_is_not_treated_as_optional_telemetry() -> None:
    """Do not weaken the existing exact base schema while adding a single optional key."""
    result = installation.parse_setup_result(base_json(child_diagnostic_state="valid"), 13, "prepare")
    assert result.reason == "diagnostic_unavailable"


def test_csharp_and_python_child_vocabularies_match() -> None:
    """Catch drift in the shared contract before a real child frame reaches Python."""
    source = (Path(__file__).resolve().parents[2] / "scripts/ppt_reader/PptSetupChildContract.cs").read_text(encoding="utf-8")
    for name, expected in (("Stages", child.CHILD_STAGES), ("Targets", child.CHILD_TARGETS),
            ("Policies", child.CHILD_POLICY_REASONS), ("Errors", child.ERROR_TYPES),
            ("Statuses", child.CHECK_STATUSES), ("Reasons", child.CHECK_REASONS)):
        match = re.search(rf'\b{name}\s*=\s*"([^"]+)"\.Split\(', source)
        assert match is not None, name
        assert frozenset(match[1].split()) == expected, name
