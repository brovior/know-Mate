"""Installation contracts and fake native boundaries, without Office/registry writes."""

import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowmate.secure import ppt_installation as installation


def result_json(**changes):
    value = dict(schema=1, operation="check", status="ready", reason="registration_ok",
                 required_version="1.0.5.13", installed_version="1.0.5.13", office_bitness=64,
                 scope="Machine", machine_safe=True, auto_install_validated=False, auto_install_mode="disabled")
    value.update(changes)
    return json.dumps(value)


def reservation(**changes):
    value = dict(schema=1, request_id="33e979c6-c5c4-40af-845a-64c80517d1ae",
                 required_version="1.0.5.13", helper_sid="S-1-5-21-1-2-3-1001", helper_pid=123,
                 helper_creation_ticks="638901000000000000", child_pid=None,
                 child_creation_ticks=None, state="complete")
    value.update(changes)
    return value


@pytest.mark.parametrize("changes,code", [({}, 0), ({"status": "missing", "reason": "not_installed",
    "installed_version": None}, 0)])
def test_valid_setup_contract(changes, code):
    assert installation.parse_setup_result(result_json(**changes), code).machine_safe


@pytest.mark.parametrize("changes,code", [({"schema": True}, 0), ({"operation": "prepare"}, 0),
    ({"required_version": "0.1.3.0"}, 0), ({"scope": "User"}, 0), ({"status": "ready"}, 13),
    ({"machine_safe": False}, 0), ({"office_bitness": True}, 0), ({"office_bitness": None}, 0),
    ({"reason": "body text"}, 0), ({"installed_version": "1.0.0"}, 0),
    ({"stdout_body": "confidential"}, 0), ({"machine_safe": 1}, 0), ({}, 42)])
def test_malformed_setup_never_ready(changes, code):
    value = installation.parse_setup_result(result_json(**changes), code)
    assert value.status == "unknown" and not value.machine_safe


def test_manifest_cannot_enable_auto_install():
    assert not installation.parse_setup_result(result_json(auto_install_validated=True), 0).auto_install_validated
    assert not installation.auto_install_enabled()


def test_optional_setup_diagnostic_bundle_preserves_observed_safety():
    value = installation.parse_setup_result(result_json(operation="prepare", status="failed",
        reason="acl_unavailable", installed_version="1.0.0.0", machine_safe=False,
        stage="parent_policy", error_type="policy", winerror=None), 12, "prepare")
    assert value.reason == "acl_unavailable" and value.stage == "parent_policy"
    assert value.error_type == "policy" and value.winerror is None
    assert value.installed_version == "1.0.0.0" and not value.machine_safe


@pytest.mark.parametrize("policy_reason", ["unknown_write", "unsupported_ace", "unsupported_scope",
    "unsupported_mask", "unsafe_owner", "missing_trusted_principal", "required_access_missing",
    "namespace_changed", "reservation_released_retry_required"])
@pytest.mark.parametrize("diagnostic", [False, True])
def test_fixed_policy_reason_is_optional_additive_diagnostic(policy_reason: str, diagnostic: bool) -> None:
    """Preserve fixed policy reasons without changing result safety or schema."""
    fields = dict(operation="prepare", status="busy", reason="reservation_unresolved",
        installed_version="1.0.0.0", machine_safe=False, policy_reason=policy_reason)
    if diagnostic:
        fields.update(stage="parent_repair_check", error_type="policy", winerror=None)
    value = installation.parse_setup_result(result_json(**fields), 12, "prepare")
    assert value.policy_reason == value.policy_diagnostic == policy_reason and not value.machine_safe
    assert value.reason == "reservation_unresolved" and value.status == "busy"


def test_reservation_release_policy_reason_preserves_outdated_result() -> None:
    """A released reservation requires retry and never asserts Reader readiness."""
    value = installation.parse_setup_result(result_json(operation="recover-parent", status="outdated",
        reason="version_old", installed_version="1.0.0.0", machine_safe=False,
        policy_reason="reservation_released_retry_required"), 0, "recover-parent")
    assert value.status == "outdated" and not value.machine_safe
    assert value.policy_reason == "reservation_released_retry_required"


def test_nullable_policy_reason_preserves_existing_contracts() -> None:
    """Both absent and null diagnostics remain compatible with existing helpers."""
    expected = installation.parse_setup_result(result_json(), 0)
    assert installation.parse_setup_result(result_json(policy_reason=None), 0) == expected
    assert expected.policy_reason is None and expected.status == "ready"


@pytest.mark.parametrize("policy_reason", ["confidential-marker", "UNKNOWN_WRITE", "", [], {}, True, 5])
def test_unknown_policy_reason_is_rejected_without_leaking_input(policy_reason: object) -> None:
    """Reject arbitrary reason values instead of reflecting helper text into logs."""
    value = installation.parse_setup_result(result_json(policy_reason=policy_reason), 0)
    assert value == installation.SetupResult(reason="diagnostic_unavailable")
    assert "confidential-marker" not in repr(value)


def test_policy_reason_preserves_native_group_and_rejects_schema_expansion() -> None:
    """Accept the bounded reason alongside native details without widening groups."""
    output = native_failure_json(policy_reason="required_access_missing")
    value = installation.parse_setup_result(output, 12, "prepare")
    assert value.policy_reason == "required_access_missing"
    assert value.native_diagnostic == ("commit_return", "C0000035", None)
    fields = json.loads(output)
    del fields["error_type"]
    assert installation.parse_setup_result(json.dumps(fields), 12, "prepare").reason == "diagnostic_unavailable"
    fields = json.loads(output)
    fields["extra"] = "confidential-marker"
    assert installation.parse_setup_result(json.dumps(fields), 12, "prepare").reason == "diagnostic_unavailable"
    duplicate = output[:-1] + ', "policy_reason":"unknown_write"}'
    assert installation.parse_setup_result(duplicate, 12, "prepare").reason == "diagnostic_unavailable"


@pytest.mark.parametrize("changes,code", [
    ({"required_version": "1.0.5.11"}, 0), ({"required_version": "1.0.5.12"}, 0),
    ({"status": "ready", "machine_safe": False}, 0), ({"status": "ready"}, 12),
    ({"installed_version": "1.0.0.0"}, 0)])
def test_policy_reason_cannot_bypass_existing_readiness_validation(changes: dict[str, object], code: int) -> None:
    """Keep version, actual safety and exit checks authoritative with diagnostics."""
    value = installation.parse_setup_result(result_json(policy_reason="namespace_changed", **changes), code)
    assert value == installation.SetupResult(reason="diagnostic_unavailable")


@pytest.mark.parametrize("stage", ["parent_repair_check", "parent_repair_backup",
    "parent_repair_write", "parent_repair_verify", "parent_repair_recover"])
@pytest.mark.parametrize("status,reason,code", [
    ("failed", "acl_unavailable", 12), ("busy", "reservation_unresolved", 12),
    ("failed", "permission_declined", 11), ("failed", "mutation_failed", 13)])
def test_parent_repair_diagnostic_preserves_failure_contract(
    stage: str, status: str, reason: str, code: int,
) -> None:
    """Keep fixed repair diagnostics observable without permitting a COM read."""
    value = installation.parse_setup_result(result_json(operation="prepare", status=status,
        reason=reason, installed_version="1.0.0.0", machine_safe=False,
        stage=stage, error_type="policy", winerror=None), code, "prepare")
    assert (value.status, value.reason, value.stage) == (status, reason, stage)
    assert value.error_type == "policy" and value.winerror is None
    assert not value.machine_safe and value.native_diagnostic == (None, None, None)


@pytest.mark.parametrize("changes", [
    {"stage": "parent_repair_unknown"}, {"reason": "parent_repair_succeeded"},
    {"extra": "confidential-marker"}, {"error_type": "policy", "winerror": 5},
    {"required_version": "1.0.5.10"}, {"status": "ready"},
    {"native_stage": "commit_verify", "ntstatus": "00000000", "io_status": "00000000"}])
def test_parent_repair_diagnostics_do_not_relax_output_schema(changes: dict[str, object]) -> None:
    """Reject unknown stages, stale payloads and unsupported diagnostic fields."""
    fields = dict(operation="prepare", status="busy", reason="reservation_unresolved",
        installed_version="1.0.0.0", machine_safe=False, stage="parent_repair_write",
        error_type="policy", winerror=None)
    fields.update(changes)
    value = installation.parse_setup_result(result_json(**fields), 12, "prepare")
    assert value == installation.SetupResult(reason="diagnostic_unavailable")
    assert "confidential-marker" not in repr(value)


def native_failure_json(**changes):
    value = {"operation": "prepare", "status": "failed", "reason": "acl_unavailable",
        "machine_safe": False, "stage": "request_commit", "error_type": "IOException", "winerror": None,
        "native_stage": "commit_return", "ntstatus": "C0000035", "io_status": None}
    value.update(changes)
    return result_json(**value)


@pytest.mark.parametrize("stage,ntstatus,io_status", [
    ("commit_return", "C0000035", None),
    ("commit_verify", "00000000", "00000000"),
    ("commit_close", "00000000", "00000000")])
def test_native_failure_preserves_separate_status_and_unknown_io(stage, ntstatus, io_status):
    value = installation.parse_setup_result(native_failure_json(
        native_stage=stage, ntstatus=ntstatus, io_status=io_status), 12, "prepare")
    assert value.native_diagnostic == (stage, ntstatus, io_status)
    assert value.error_type == "IOException" and value.winerror is None
    assert not value.machine_safe and value.status == "failed"


@pytest.mark.parametrize("code", [11, 12, 13])
def test_native_completion_failure_preserves_busy_and_original_exit(code):
    value = installation.parse_setup_result(native_failure_json(
        status="busy", reason="reservation_unresolved"), code, "prepare")
    assert value.status == "busy" and value.reason == "reservation_unresolved"
    assert value.native_diagnostic == ("commit_return", "C0000035", None)
    assert not value.machine_safe


@pytest.mark.parametrize("stage,error_type,winerror", [
    ("commit_verify", "Win32Exception", 5), ("commit_close", "Win32Exception", 6),
    ("commit_verify", "UnauthorizedAccessException", None)])
def test_native_success_followed_by_validation_or_close_failure_preserves_actual_error(stage, error_type, winerror):
    value = installation.parse_setup_result(native_failure_json(native_stage=stage,
        ntstatus="00000000", io_status="00000000", error_type=error_type, winerror=winerror), 12, "prepare")
    assert value.native_diagnostic == (stage, "00000000", "00000000")
    assert value.error_type == error_type and value.winerror == winerror
    assert not value.machine_safe


@pytest.mark.parametrize("changes", [
    {"native_stage": "confidential-marker"}, {"native_stage": []},
    {"ntstatus": "c0000035"}, {"ntstatus": "0xC0000035"}, {"ntstatus": "C000035"},
    {"ntstatus": None}, {"ntstatus": 3221225525}, {"ntstatus": True},
    {"io_status": "c0000035"}, {"io_status": "000000000"}, {"io_status": 0},
    {"stage": "parent_open"}, {"status": "unknown"}, {"winerror": 53},
    {"error_type": "Win32Exception", "winerror": 32}, {"machine_safe": True},
    {"native_stage": "commit_verify"}, {"native_stage": "commit_close"}])
def test_native_diagnostic_rejects_noncanonical_values(changes):
    value = installation.parse_setup_result(native_failure_json(**changes), 12, "prepare")
    assert value == installation.SetupResult(reason="diagnostic_unavailable")
    assert "confidential-marker" not in repr(value)


@pytest.mark.parametrize("omitted", ["native_stage", "ntstatus", "io_status", "stage", "error_type", "winerror"])
def test_native_diagnostic_requires_both_complete_groups(omitted):
    value = json.loads(native_failure_json())
    del value[omitted]
    assert installation.parse_setup_result(json.dumps(value), 12, "prepare").reason == "diagnostic_unavailable"


def test_native_diagnostic_cannot_be_added_without_existing_diagnostic_group():
    value = json.loads(native_failure_json())
    for key in ("stage", "error_type", "winerror"):
        del value[key]
    assert installation.parse_setup_result(json.dumps(value), 12, "prepare").reason == "diagnostic_unavailable"


def test_native_diagnostic_duplicate_oversize_and_success_rejected():
    duplicate = native_failure_json()[:-1] + ', "ntstatus":"00000000"}'
    for output, code in ((duplicate, 12), (native_failure_json() + " " * 16384, 12),
                         (native_failure_json(status="ready", reason="registration_ok", machine_safe=True), 0)):
        assert installation.parse_setup_result(output, code, "prepare").reason == "diagnostic_unavailable"


@pytest.mark.parametrize("changes", [{"stage": "parent_policy"},
    {"stage": "parent_policy", "error_type": "policy", "winerror": True},
    {"stage": "parent_policy", "error_type": "policy", "winerror": -1},
    {"stage": "parent_policy", "error_type": "IOException", "winerror": 5},
    {"stage": "parent_policy", "error_type": "policy", "winerror": 2**31},
    {"stage": "confidential-marker", "error_type": "policy", "winerror": None},
    {"stage": "parent_policy", "error_type": "confidential-marker", "winerror": None},
    {"stage": "parent_policy", "error_type": "policy", "winerror": None, "extra": "confidential-marker"},
    {"stage": [], "error_type": "policy", "winerror": None},
    {"stage": "parent_policy", "error_type": [], "winerror": None}])
def test_malformed_diagnostic_is_unavailable(changes):
    value = installation.parse_setup_result(result_json(**changes), 0)
    assert value == installation.SetupResult(reason="diagnostic_unavailable")
    assert "confidential-marker" not in repr(value)


def test_setup_output_size_is_bounded():
    value = installation.parse_setup_result(result_json() + " " * 16384, 0)
    assert value.reason == "diagnostic_unavailable" and not value.machine_safe


def test_setup_duplicate_fields_and_non_integer_exit_rejected():
    assert installation.parse_setup_result(result_json()[:-1] + ', "reason":"registration_ok"}', 0).reason == "diagnostic_unavailable"
    assert not installation.parse_setup_result(result_json(), False).machine_safe


@pytest.mark.parametrize("changes", [dict(schema=True), dict(helper_pid=True), dict(helper_creation_ticks="0"),
    dict(state="invalid"), dict(child_pid=5), dict(child_pid=True, child_creation_ticks="5"),
    dict(helper_sid="not-sid"), dict(required_version="evil"), dict(request_id="not-uuid"),
    dict(extra="secret"), dict(state="elevated")])
def test_corrupt_reservation_not_safe(changes):
    assert not installation._valid_reservation(reservation(**changes), reservation()["request_id"])


def test_old_completed_version_valid_but_filename_must_match():
    value = reservation(required_version="0.9.0.0")
    assert installation._valid_reservation(value, value["request_id"])
    assert not installation._valid_reservation(value, "a-different-uuid")


def fake_folders(monkeypatch, tmp_path):
    common = tmp_path / "common"
    program = tmp_path / "program"
    monkeypatch.setattr(installation, "_known_folder", lambda identifier:
        common if identifier.startswith("62AB") else program)
    return common / "AegisDesk" / "PptReaderRequests", program / "AegisDesk" / "PptReader"


def test_fresh_machine_has_no_pending_reservation(monkeypatch, tmp_path):
    fake_folders(monkeypatch, tmp_path)
    assert installation._machine_records_safe()


@pytest.mark.parametrize("kind", ["reserved", "unresolved", "elevated", "corrupt", "unexpected", "symlink", "journal"])
def test_pending_and_corrupt_records_never_expire(monkeypatch, tmp_path, kind):
    requests, installed = fake_folders(monkeypatch, tmp_path)
    requests.mkdir(parents=True)
    record = requests / (reservation()["request_id"] + ".json")
    if kind == "journal":
        installed.mkdir(parents=True)
        (installed / "journal.json").write_text("{}")
    elif kind == "unexpected":
        (requests / "foreign.tmp").write_text("{}")
    elif kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text(json.dumps(reservation()))
        try:
            record.symlink_to(target)
        except OSError as error:
            if getattr(error, "winerror", None) != 1314:
                raise
            pytest.skip("Required symlink rejection gate unexecuted: fixture privilege missing (WinError 1314)")
    elif kind == "corrupt":
        record.write_text(json.dumps({"schema": 1, "state": "complete"}))
    else:
        record.write_text(json.dumps(reservation(state=kind)))
    assert not installation._machine_records_safe()


def test_complete_records_safe_and_known_folder_failure_closed(monkeypatch, tmp_path):
    requests, _ = fake_folders(monkeypatch, tmp_path)
    requests.mkdir(parents=True)
    (requests / (reservation()["request_id"] + ".json")).write_text(json.dumps(reservation()))
    assert installation._machine_records_safe()
    monkeypatch.setattr(installation, "_known_folder", lambda _: (_ for _ in ()).throw(OSError()))
    assert not installation._machine_records_safe()


class NativeCall:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def fake_mutex(monkeypatch, wait=0, existing=55):
    calls = []
    kernel = SimpleNamespace(**{name: NativeCall(callback) for name, callback in {
        "OpenMutexW": lambda rights, inherit, name: calls.append(("open", rights)) or existing,
        "CreateMutexW": lambda security, inherit, name: calls.append(("create", security is not None)) or 55,
        "WaitForSingleObject": lambda *args: wait,
        "ReleaseMutex": lambda *args: calls.append(("release", args)),
        "CloseHandle": lambda *args: calls.append(("close", args)),
        "LocalFree": lambda *args: None,
    }.items()})
    advapi = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=NativeCall(lambda *args: 1))
    monkeypatch.setattr(installation.ctypes, "WinDLL", lambda name, **kwargs: advapi if name == "advapi32" else kernel, raising=False)
    monkeypatch.setattr(installation, "os", SimpleNamespace(name="nt"))
    return calls


def test_guard_rechecks_under_owned_mutex_without_helper(monkeypatch):
    calls = fake_mutex(monkeypatch)
    monkeypatch.setattr(installation, "check_installation", lambda: pytest.fail("self-contending helper"))
    monkeypatch.setattr(installation, "_machine_records_safe", lambda: True)
    with installation.MachineReadGuard():
        assert not any(call[0] == "release" for call in calls)
    assert calls[0] == ("open", 0x00100001)
    assert [call[0] for call in calls][-2:] == ["release", "close"]


@pytest.mark.parametrize("wait,safe", [(0x80, False), (0, False), (258, True)])
def test_guard_busy_abandoned_unresolved_release(monkeypatch, wait, safe):
    calls = fake_mutex(monkeypatch, wait)
    with pytest.raises(installation.PptMachineBusy):
        with installation.MachineReadGuard(lambda: installation.SetupResult(machine_safe=safe)):
            pytest.fail("unsafe read")
    assert any(call[0] == "close" for call in calls)


def test_cross_user_creation_uses_explicit_acl(monkeypatch):
    calls = fake_mutex(monkeypatch, existing=0)
    with installation.MachineReadGuard(lambda: installation.SetupResult(machine_safe=True)):
        pass
    assert ("create", True) in calls


def test_missing_payload_fresh_machine_uses_safe_com(monkeypatch, tmp_path):
    fake_mutex(monkeypatch)
    monkeypatch.setattr(installation, "_validate_helper", lambda _: False)
    monkeypatch.setattr(installation, "_machine_records_safe", lambda: True)
    state = installation.check_installation(tmp_path / "PptReaderSetup.exe")
    assert state.machine_safe and state.reason == "payload_invalid"
    monkeypatch.setattr(installation, "_machine_records_safe", lambda: False)
    assert not installation.check_installation(tmp_path / "PptReaderSetup.exe").machine_safe


def test_detached_setup_gate_and_close_does_not_kill(monkeypatch):
    fake_mutex(monkeypatch)
    process = installation.DetachedSetupProcess(Path("PptReaderSetup.exe"))
    with pytest.raises(installation.PptMachineBusy, match="prototype_gate"):
        process.start()
    closed = []
    process._process = SimpleNamespace(_handle=SimpleNamespace(Close=lambda: closed.append(True)),
        kill=lambda: pytest.fail("kill"), terminate=lambda: pytest.fail("terminate"), poll=lambda: None)
    assert process.poll() is None
    process.close()
    assert closed == [True]


def test_detached_finalizer_never_polls_a_closed_handle_or_fabricates_exit():
    process = object.__new__(installation._DetachedPopen)
    closed = []
    process._handle = SimpleNamespace(Close=lambda: closed.append(True))
    process._child_created = True
    process.pid = 12345
    process.returncode = None
    process._internal_poll = lambda **kwargs: pytest.fail("closed handle polled")
    process.close_observation()
    process.close_observation()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        process.__del__()
    assert not captured and closed == [True] and process.returncode is None


def test_detached_result_never_reads_a_live_helper():
    process = installation.DetachedSetupProcess()
    process._process = SimpleNamespace(poll=lambda: None)
    process._output = SimpleNamespace(seek=lambda _: pytest.fail("live output read"))
    assert process.read_result().reason == "diagnostic_unavailable"


@pytest.mark.parametrize("output", [b"broken", b"x" * 16385, b"\xff"])
def test_detached_invalid_output_is_unavailable(output):
    process = installation.DetachedSetupProcess()
    process._process = SimpleNamespace(poll=lambda: 12)
    process._output = io.BytesIO(output)
    assert process.read_result() == installation.SetupResult(reason="diagnostic_unavailable")
    process.close()


def test_detached_actual_exit_reads_failure_detail():
    process = installation.DetachedSetupProcess()
    process._process = SimpleNamespace(poll=lambda: 12)
    process._output = io.BytesIO(result_json(operation="prepare", status="failed",
        reason="acl_unavailable", machine_safe=False, stage="parent_open",
        error_type="Win32Exception", winerror=32).encode())
    result = process.read_result()
    assert result.stage == "parent_open" and result.winerror == 32
    assert not result.machine_safe
    process.close()


def test_detached_actual_exit_reads_native_diagnostics_without_promoting_safety():
    process = installation.DetachedSetupProcess()
    process._process = SimpleNamespace(poll=lambda: 12)
    process._output = io.BytesIO(native_failure_json().encode())
    result = process.read_result()
    assert result.native_diagnostic == ("commit_return", "C0000035", None)
    assert not result.machine_safe and result.winerror is None
    process.close()


def test_detached_read_failure_remains_unknown():
    process = installation.DetachedSetupProcess()
    process._process = SimpleNamespace(poll=lambda: 12)
    process._output = SimpleNamespace(seek=lambda _: (_ for _ in ()).throw(OSError("confidential-marker")))
    assert process.read_result().reason == "diagnostic_unavailable"


def test_detached_closes_output_even_when_process_handle_close_fails(monkeypatch):
    fake_mutex(monkeypatch)
    process = installation.DetachedSetupProcess()
    output = io.BytesIO()
    process._output = output
    process._process = SimpleNamespace(_handle=SimpleNamespace(
        Close=lambda: (_ for _ in ()).throw(OSError("handle_close"))))
    with pytest.raises(OSError):
        process.close()
    assert output.closed and process._process is None and process._output is None


def test_detached_launch_uses_file_and_cleans_failed_launch(monkeypatch):
    fake_mutex(monkeypatch)
    monkeypatch.setattr(installation, "auto_install_enabled", lambda: True)
    monkeypatch.setattr(installation, "_validate_helper", lambda _: True)
    monkeypatch.setattr(installation, "create_setup_output", lambda: tempfile.TemporaryFile(mode="w+b"))
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        monkeypatch.setattr(installation.subprocess, name, 0, raising=False)
    outputs = []
    def launch(*args, **kwargs):
        outputs.append(kwargs["stdout"])
        assert kwargs["stdout"] != subprocess.PIPE
        assert kwargs["close_fds"] and kwargs["stderr"] == subprocess.DEVNULL
        raise OSError("launch")
    monkeypatch.setattr(installation, "_DetachedPopen", launch)
    process = installation.DetachedSetupProcess()
    with pytest.raises(OSError):
        process.start()
    assert outputs[0].closed and process._output is None


def test_private_output_failure_never_launches_helper(monkeypatch):
    fake_mutex(monkeypatch)
    monkeypatch.setattr(installation, "auto_install_enabled", lambda: True)
    monkeypatch.setattr(installation, "_validate_helper", lambda _: True)
    monkeypatch.setattr(installation, "create_setup_output", lambda:
        (_ for _ in ()).throw(OSError("private_output_failed")))
    monkeypatch.setattr(installation, "_DetachedPopen", lambda *args, **kwargs: pytest.fail("no helper"))
    with pytest.raises(installation.SetupLaunchNotStarted):
        installation.DetachedSetupProcess().start()


def _assert_output_open_denied(error: OSError) -> None:
    native_error = getattr(error, "winerror", None)
    if native_error is None:
        # CRT file opens can expose errno without a native Windows error.
        assert error.errno in (errno.EACCES, errno.EPERM)
    else:
        assert native_error in (5, 32, 303)


@pytest.mark.parametrize("number,native,accepted", [
    (errno.EACCES, None, True), (errno.EPERM, None, True),
    (errno.EACCES, 5, True), (errno.EACCES, 32, True),
    (errno.EACCES, 303, True), (errno.ENOENT, None, False),
    (errno.EIO, None, False), (errno.EACCES, 2, False),
])
def test_stdout_denial_accepts_crt_errno_but_rejects_unrelated_errors(number, native, accepted):
    error = OSError(number, "fixture")
    error.winerror = native
    if accepted:
        _assert_output_open_denied(error)
    else:
        with pytest.raises(AssertionError):
            _assert_output_open_denied(error)


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited temporary stdout lifetime required")
def test_windows_helper_stdout_survives_observer_close_and_deletes_on_last_close(monkeypatch, tmp_path, request):
    """Native handle inheritance, delayed write and last-close deletion without Office."""
    monkeypatch.setattr(installation, "auto_install_enabled", lambda: True)
    monkeypatch.setattr(installation, "_validate_helper", lambda _: True)
    import ctypes
    import msvcrt

    from knowmate.tests.ppt_stdout_lifecycle import (
        StdoutFailure,
        StdoutLifecycle,
        read_progress,
    )
    from knowmate.tests.ppt_stdout_native import NativeStdoutChild

    launch = installation._DetachedPopen
    progress, release, done = (tmp_path / name for name in ("progress", "release", "done"))
    child_script = Path(__file__).with_name("ppt_stdout_child.py")
    process = installation.DetachedSetupProcess()
    native = NativeStdoutChild(process)
    lifecycle = StdoutLifecycle(progress, done, release=release.touch,
        close_observation=native.close_observation, wait=native.wait,
        exit_code=native.exit_code,
        terminate=native.terminate, close_verifier=native.close_verifier)
    # This one finalizer owns all paths, including start/ready/ACL failures.
    request.addfinalizer(lifecycle.finish)

    def native_launch(command, **kwargs):
        child = launch([sys.executable, str(child_script), str(progress), str(release), str(done), "normal"], **kwargs)
        lifecycle.has_control = True
        native.attach(child)
        return child

    monkeypatch.setattr(installation, "_DetachedPopen", native_launch)
    try:
        process.start()
        if native.duplicate_error is not None:
            lifecycle.native_error = native.duplicate_error
            raise StdoutFailure("duplicate_handle_failed")
        lifecycle.await_stage("ready", "ready")
        lifecycle.require_alive()
        kernel = native.kernel
        kernel.GetFinalPathNameByHandleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
            ctypes.c_uint32, ctypes.c_uint32]
        name = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(process._output.fileno()), name, len(name), 0)
        assert 0 < length < len(name)
        filename = Path(name.value)
        with pytest.raises(OSError) as denied:
            filename.open("rb")
        _assert_output_open_denied(denied.value)
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        security.GetSecurityInfo.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        security.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(ctypes.c_uint32)]
        security.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        descriptor, owner, dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        assert security.GetSecurityInfo(msvcrt.get_osfhandle(process._output.fileno()), 1, 5,
            ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)) == 0
        try:
            control, revision = ctypes.c_uint16(), ctypes.c_uint32()
            assert security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
            assert control.value & 0x1004 == 0x1004  # DACL present and protected
            assert dacl.value
            assert ctypes.cast(dacl, ctypes.POINTER(ctypes.c_uint16))[2] == 3
            def sid_string(pointer):
                text = ctypes.c_void_p()
                assert security.ConvertSidToStringSidW(pointer, ctypes.byref(text))
                try:
                    return ctypes.wstring_at(text)
                finally:
                    kernel.LocalFree(text)
            owner_sid = sid_string(owner)
            actual_sids = set()
            for index in range(3):
                ace = ctypes.c_void_p()
                assert security.GetAce(dacl, index, ctypes.byref(ace))
                header = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
                assert header[0] == 0 and header[1] == 0  # Allow, no inheritance flags
                assert ctypes.cast(ace, ctypes.POINTER(ctypes.c_uint32))[1] in (0x1F01FF, 0x10000000)
                actual_sids.add(sid_string(ace.value + 8))
            private_principals_match = actual_sids == {owner_sid, "S-1-5-32-544", "S-1-5-18"}
            assert private_principals_match  # never print user SID in failure output
        finally:
            kernel.LocalFree(descriptor)
        lifecycle.phase = "close_observation"
        lifecycle.require_alive()
        process.close()  # The test Python still owns the real inherited stdout.
        lifecycle.require_alive()
        # File absence or unrelated I/O failure cannot masquerade as denial.
        with pytest.raises(OSError) as denied:
            filename.open("rb")
        _assert_output_open_denied(denied.value)
        lifecycle.phase = "release"
        lifecycle.require_alive()
        lifecycle.release_child()
        lifecycle.await_stage("release_seen", "release")
        lifecycle.await_exit()
        assert done.is_file(), "child_done_unconfirmed"
        observed = read_progress(progress)
        assert observed is not None and observed[0] == "before_return", "child_progress_unavailable"
        lifecycle.filename = filename
        lifecycle.body_complete = True
        lifecycle.phase = "complete"
    except BaseException:
        lifecycle.primary_failed = True
        raise


@pytest.mark.parametrize("mode", ["disabled", "candidate", "released"])
def test_setup_csharp_contract_without_installation(tmp_path, mode):
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows .NET compiler required; setup native operations not executed")
    root = Path(__file__).resolve().parents[2]
    sources = [root / "scripts/ppt_reader/PptReaderSetup.cs", *sorted((root / "scripts/ppt_reader").glob("PptSetup*.cs")),
               root / "knowmate/tests/ppt_setup_harness.cs"]
    policy = tmp_path / "PptBuildPolicy.cs"
    policy.write_text('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                      f'public const string AutoInstallMode = "{mode}"; }} }}')
    sources.append(policy)
    executable = tmp_path / "SetupHarness.exe"
    commands = [[str(compiler), "/nologo", "/main:SetupHarness", f"/out:{executable}",
                 "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll", "/reference:System.Security.dll",
                 *(str(path) for path in sources)], [str(executable), str(tmp_path)]]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, encoding="ascii", errors="replace", timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["disabled", "candidate", "released"])
def test_embedded_policy_and_result_must_match(monkeypatch, mode):
    monkeypatch.setattr(installation, "auto_install_mode", lambda: mode)
    result = installation.parse_setup_result(result_json(auto_install_mode=mode,
        auto_install_validated=mode == "released"), 0)
    assert result.status == "ready"
    assert result.auto_install_enabled == (mode != "disabled")
    wrong = installation.parse_setup_result(result_json(auto_install_mode=mode,
        auto_install_validated=mode != "released"), 0)
    assert not wrong.machine_safe
    for foreign in {"disabled", "candidate", "released"} - {mode}:
        assert not installation.parse_setup_result(result_json(auto_install_mode=foreign,
            auto_install_validated=foreign == "released"), 0).machine_safe


def test_source_cannot_enable_installation_with_generated_module(monkeypatch):
    from knowmate.secure import ppt_policy
    monkeypatch.setattr(ppt_policy.sys, "frozen", False, raising=False)
    monkeypatch.setattr(ppt_policy, "get_build_info", lambda: {"ppt_auto_install_mode": "candidate"})
    assert ppt_policy.auto_install_mode() == "disabled"
    assert not ppt_policy.auto_install_enabled()


@pytest.mark.parametrize("filename", ["journal.json.abc.tmp", "installation.json.abc.tmp"])
def test_metadata_atomic_write_residue_blocks_fallback_com(monkeypatch, tmp_path, filename):
    _, installed = fake_folders(monkeypatch, tmp_path)
    installed.mkdir(parents=True)
    (installed / filename).write_text("incomplete")
    assert not installation._machine_records_safe()
