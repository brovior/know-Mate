"""Collect isolated tests of the production request writer; Windows gates stay explicit."""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from knowmate.secure.ppt_installation import _DIAGNOSTIC_STAGES
from knowmate.tests.test_ppt_rename_probe import _compile, _os_code

_MODES = (
    "create", "replace", "collision", "missing", "malformed", "identity", "state", "size",
    "readonly", "hardlink", "reparse", "reparse-directory", "policy", "external-reader", "native-failure", "native-failure-io", "native-failure-close", "fake-success",
    "fault-source_open", "fault-source_acl", "fault-source_write", "fault-source_flush", "fault-source_written",
    "fault-target_close", "fault-commit_call", "fault-source_close", "pending", "io-incomplete", "io-unwritten", "interrupt-before", "interrupt-after",
)
_CASES = tuple((mode, leases) for mode in _MODES for leases in (1, 2))
_ERROR_CODES = frozenset({
    "exception", "target_not_closed", "source_missing", "target_outside_fixture", "initial_state", "final_state",
    "hardlink_fixture_failed", "external_reader_open_failed", "expected_rejection_missing", "missing_target_recreated",
    "rejected_target_content_changed", "rejected_target_identity_changed", "invalid_target_reached_commit",
    "rejected_source_changed", "cwd_decoy_changed", "source_identity_not_preserved", "source_security_not_preserved",
    "target_security_policy", "argument_count", "fixture_invalid", "programdata_fixture_forbidden", "lease_count_invalid",
    "junction_fixture_failed", "junction_not_preserved", "committed_close_fault_state", "fault_reached_native",
    "source_handle_leaked", "target_handle_leaked", "primary_native_failure_changed", "fake_success_accepted", "close_failure_not_classified",
})
_STAGES = frozenset({
    "preflight", "reservation_create", "reservation_load", "target_open", "target_validated", "target_close",
    "target_closed", "source_open", "source_written", "commit_call", "commit_return", "commit_verify",
    "source_close", "source_closed", "source_close_failed", "source_acl", "source_write", "source_flush", "native_unconfirmed", "preservation_verify", "verify", "cleanup", "complete",
})


@dataclass(frozen=True)
class _WriterOutcome:
    result: str
    stage: str = "unavailable"
    expected: str = "none"
    actual: str = "none"
    bits: str = "unavailable"
    events: tuple[str, ...] = ()
    winerror: str = "none"
    errno: str = "none"
    error_code: str = "none"
    ntstatus: str = "none"
    io_status: str = "none"
    setup_stage: str = "none"


def _writer_outcome(output: str | bytes | None, code: int | None, *, timed_out: bool = False,
                    isolation: bool = False) -> _WriterOutcome:
    if isinstance(output, bytes):
        output = output.decode("ascii", errors="replace")
    stage, bits, expected, actual = "unavailable", "unavailable", "none", "none"
    events, terminal = [], []
    held = False
    interruption = None
    error_stage, error_code = None, "none"
    ntstatus, io_status, winerror, setup_stage = "none", "none", "none", "none"
    unexecuted = False
    lines = (output or "").splitlines()
    for line in lines:
        match = re.fullmatch(r"PPT_WRITER_STAGE ([a-z_]+) elapsed_ms=([0-9]{1,12})", line)
        if match and match[1] in _STAGES:
            stage = match[1]
            events.append(line)
        match = re.fullmatch(r"PPT_WRITER_ARCH bits=(32|64)", line)
        if match:
            bits = match[1]
            events.append(line)
        match = re.fullmatch(r"PPT_WRITER_EXPECT expected=(reject|success) actual=(reject|success) verified=1", line)
        if match:
            expected, actual = match[1], match[2]
            events.append(line)
        match = re.fullmatch(r"PPT_WRITER_ERROR stage=([a-z_]+) kind=(contract|setup) code=([a-z_]+)", line)
        if match and match[1] in _STAGES and match[3] in _ERROR_CODES:
            error_stage, error_code = match[1], match[3]
            events.append(line)
        match = re.fullmatch(r"PPT_WRITER_INTERRUPTION point=(before|after) ready=1", line)
        if match:
            interruption = match[1]
            events.append(line)
        if line == "PPT_WRITER_HOLD verified=1 buffers=1 source=1 lease=1 mutex=1":
            held = True
            events.append(line)
        match = re.fullmatch(r"PPT_WRITER_REJECT stage=([a-z_]{1,32}) error_type=(IOException|Win32Exception|policy|access|other) winerror=(none|[0-9]{1,10})", line)
        if match and match[1] in _DIAGNOSTIC_STAGES | {"policy"}:
            setup_stage, winerror = match[1], match[3]
            events.append(line)
        match = re.fullmatch(r"PPT_WRITER_NATIVE stage=(commit_return|commit_verify|commit_close) ntstatus=([0-9A-F]{8}) io_status=(none|[0-9A-F]{8})", line)
        if match:
            ntstatus, io_status = match[2], match[3]
            events.append(line)
        if re.fullmatch(r"PPT_WRITER_UNEXECUTED gate=reparse reason=symlink_privilege winerror=[0-9]{1,10}", line):
            unexecuted = True
            events.append(line)
        if line in ("PPT_WRITER_RESULT PASS", "PPT_WRITER_RESULT FAIL", "PPT_WRITER_RESULT SKIP reason=elevated",
                    "PPT_WRITER_RESULT SKIP reason=nonlocal_ntfs"):
            terminal.append(line)
            events.append(line)
    result = "TIMEOUT" if timed_out else "FAIL"
    if isolation and timed_out and held and stage == "native_unconfirmed" and not terminal and bits != "unavailable":
        result, expected, actual = "PASS", "isolate", "isolate"
    elif timed_out and interruption is not None and stage == {"before": "commit_call", "after": "commit_return"}[interruption] and not terminal and bits != "unavailable":
        result, expected, actual = "INTERRUPTED", "interrupt", interruption
    elif not timed_out and len(terminal) == 1 and lines and lines[-1] == terminal[0] and bits != "unavailable":
        if unexecuted:
            result = "UNEXECUTED"
        elif terminal[0] == "PPT_WRITER_RESULT PASS" and code == 0 and stage == "complete" and expected == actual != "none":
            result = "PASS"
        elif terminal[0].startswith("PPT_WRITER_RESULT SKIP") and code == 77:
            result = "SKIP"
    if error_stage is not None and not timed_out:
        stage = error_stage
    return _WriterOutcome(result, stage, expected, actual, bits, tuple(events[:256]), error_code=error_code,
                          ntstatus=ntstatus, io_status=io_status, winerror=winerror, setup_stage=setup_stage)


def _fixture_record(path: Path) -> dict:
    if path.stat().st_size > 8192:
        raise ValueError("fixture_size")
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    required = {"schema", "request_id", "required_version", "helper_sid", "helper_pid", "helper_creation_ticks",
                "child_pid", "child_creation_ticks", "state"}
    if (not isinstance(record, dict) or set(record) != required or type(record["schema"]) is not int or record["schema"] != 1
            or type(record["helper_pid"]) is not int or record["helper_pid"] <= 0
            or not isinstance(record["helper_creation_ticks"], str) or not record["helper_creation_ticks"].isdigit()
            or int(record["helper_creation_ticks"]) <= 0 or not isinstance(record["required_version"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", record["required_version"])
            or not isinstance(record["helper_sid"], str) or not re.fullmatch(r"S-[0-9]+(?:-[0-9]+)+", record["helper_sid"])
            or record["child_pid"] is not None or record["child_creation_ticks"] is not None
            or record["state"] not in ("reserved", "complete") or not isinstance(record["request_id"], str)
            or str(uuid.UUID(record["request_id"])) != record["request_id"]):
        raise ValueError("fixture_shape")
    return record


def _cleanup_writer(command: list[str], outcome: _WriterOutcome) -> _WriterOutcome:
    if len(command) != 4 or not Path(command[1]).exists():
        return outcome
    try:
        result = subprocess.run([command[0], command[1], "cleanup", command[3]], check=False,
                                capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        success = result.returncode == 0 and "PPT_WRITER_CLEANUP PASS" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        success = False
    event = "PPT_WRITER_FIXTURE_CLEANUP result=" + ("PASS" if success else "FAIL")
    return replace(outcome, result=outcome.result if success else "FAIL", stage=outcome.stage if success else "cleanup",
                   events=outcome.events + (event,))


def _writer_run(command: list[str], *, isolation: bool = False) -> _WriterOutcome:
    # subprocess.run kills only the freshly launched test-owned harness on timeout.
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        outcome = _writer_outcome(error.stdout, None, timed_out=True, isolation=isolation)
        if outcome.result == "INTERRUPTED":
            anchor = Path(command[1])
            files = list((anchor / "AegisDesk" / "PptReaderRequests").glob("*.json"))
            try:
                if len(files) != 1:
                    raise ValueError("fixture_shape")
                record = _fixture_record(files[0])
                expected = "reserved" if outcome.actual == "before" else "complete"
                if record["state"] != expected or files[0].name != record["request_id"] + ".json":
                    raise ValueError("fixture_state")
                temps = list(files[0].parent.glob("*.tmp"))
                if outcome.actual == "before":
                    if len(temps) != 1:
                        raise ValueError("fixture_temp")
                    pending = _fixture_record(temps[0])
                    if pending != {**record, "state": "complete"}:
                        raise ValueError("fixture_temp_state")
                elif temps:
                    raise ValueError("fixture_old_temp")
            except (OSError, ValueError, KeyError, TypeError):
                return _cleanup_writer(command, replace(outcome, result="FAIL", expected="interrupt", actual="unverified"))
            return _cleanup_writer(command, replace(outcome, result="PASS", expected="interrupt", actual="interrupt"))
        return _cleanup_writer(command, outcome)
    except OSError as error:
        return _WriterOutcome("FAIL", "process_start", winerror=_os_code(error, "winerror"), errno=_os_code(error, "errno"))
    return _writer_outcome(result.stdout, result.returncode, isolation=isolation)


def _collect_writer(executable: Path, tmp_path: Path) -> list[_WriterOutcome]:
    outcomes = []
    for mode, leases in _CASES:
        print(f"PPT_WRITER_BEGIN category=actual_writer mode={mode} leases={leases}", flush=True)
        outcome = _writer_run([str(executable), str(tmp_path / ("ppt-writer-" + uuid.uuid4().hex)), mode, str(leases)],
                              isolation=mode in ("pending", "io-incomplete", "io-unwritten"))
        outcomes.append(outcome)
        for event in outcome.events:
            print(event, flush=True)
        print(f"PPT_WRITER_CASE category=actual_writer mode={mode} leases={leases} result={outcome.result} "
              f"expected={outcome.expected} actual={outcome.actual} bits={outcome.bits} last_stage={outcome.stage} error_code={outcome.error_code} winerror={outcome.winerror} errno={outcome.errno} ntstatus={outcome.ntstatus} io_status={outcome.io_status} "
              f"setup_stage={outcome.setup_stage}", flush=True)
    counts = {result: sum(outcome.result == result for outcome in outcomes)
              for result in ("PASS", "FAIL", "TIMEOUT", "SKIP", "UNEXECUTED")}
    print("PPT_WRITER_SUMMARY category=actual_writer " + " ".join(f"{key.lower()}={value}" for key, value in counts.items())
          + " product_approval=false", flush=True)
    return outcomes


def test_ppt_production_request_writer_matrix(tmp_path: Path) -> None:
    """Required product path; baseline probe success is never substituted for this gate."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Required production request writer native gate unexecuted: Windows .NET compiler required")
    root = Path(__file__).resolve().parents[2]
    policy = tmp_path / "PptBuildPolicy.cs"
    policy.write_text('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                      'public const string AutoInstallMode = "candidate"; } }', encoding="utf-8")
    sources = [root / "scripts/ppt_reader/PptReaderSetup.cs",
               *sorted((root / "scripts/ppt_reader").glob("PptSetup*.cs")),
               root / "knowmate/tests/ppt_rename_probe_native.cs",
               root / "knowmate/tests/ppt_request_writer_harness.cs", policy]
    executable = tmp_path / "RequestWriterHarness.exe"
    assert _compile([str(compiler), "/nologo", "/main:RequestWriterHarness", f"/out:{executable}",
                     "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll", "/reference:System.Security.dll",
                     *(str(source) for source in sources)], sources), "PPT request writer harness compile failed"
    outcomes = _collect_writer(executable, tmp_path)
    if all(outcome.result == "SKIP" for outcome in outcomes):
        pytest.skip("Required production request writer gate unexecuted: ordinary local NTFS account required")
    assert all(outcome.result == "PASS" for outcome in outcomes), (
        "Production request writer required gate incomplete; inspect PPT_WRITER_SUMMARY/CASE. "
        "UNEXECUTED is not approval. Distinct-account/UAC/Office gates are separate.")


def test_writer_matrix_collects_all_cases_after_failure(monkeypatch, tmp_path, capsys):
    calls = []
    def run(command, *, isolation=False):
        calls.append((command, isolation))
        return _WriterOutcome("TIMEOUT" if len(calls) == 1 else "PASS")
    monkeypatch.setattr("knowmate.tests.test_ppt_request_writer._writer_run", run)
    result = _collect_writer(tmp_path / "fixture.exe", tmp_path)
    assert len(result) == len(_CASES) == len(calls)
    assert len({call[0][1] for call in calls}) == len(_CASES)
    assert sum(call[1] for call in calls) == 6
    assert "timeout=1" in capsys.readouterr().out


def test_writer_fixed_output_requires_expectation_and_terminal():
    prefix = "secret-marker\nPPT_WRITER_ARCH bits=64\nPPT_WRITER_EXPECT expected=reject actual=reject verified=1\n"
    valid = prefix + "PPT_WRITER_STAGE complete elapsed_ms=1\nPPT_WRITER_RESULT PASS\n"
    assert _writer_outcome(valid, 0).result == "PASS"
    assert "secret" not in repr(_writer_outcome(valid, 0))
    assert _writer_outcome(valid + "extra\n", 0).result == "FAIL"
    assert _writer_outcome(valid, 1).result == "FAIL"
    assert _writer_outcome("PPT_WRITER_ARCH bits=64\nPPT_WRITER_STAGE complete elapsed_ms=1\nPPT_WRITER_RESULT PASS\n", 0).result == "FAIL"


@pytest.mark.parametrize("output", [str, lambda value: value.encode()])
def test_writer_isolation_needs_live_buffers_handle_lease_mutex_and_no_return(output):
    held = ("PPT_WRITER_ARCH bits=32\nPPT_WRITER_STAGE native_unconfirmed elapsed_ms=1\n"
            "PPT_WRITER_HOLD verified=1 buffers=1 source=1 lease=1 mutex=1\n")
    assert _writer_outcome(output(held), None, timed_out=True, isolation=True).result == "PASS"
    assert _writer_outcome(output(held), None, timed_out=True).result == "TIMEOUT"
    assert _writer_outcome(output(held.replace("source=1", "source=0")), None, timed_out=True, isolation=True).result == "TIMEOUT"
    assert _writer_outcome(output(held + "PPT_WRITER_STAGE source_closed elapsed_ms=2\n"), None,
                           timed_out=True, isolation=True).result == "TIMEOUT"


def test_writer_subprocess_timeout_is_bounded_and_test_owned(monkeypatch):
    def run(command, **kwargs):
        assert command == ["test-owned.exe"] and kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(command, 30, output=b"PPT_WRITER_ARCH bits=64\n")
    monkeypatch.setattr(subprocess, "run", run)
    assert _writer_run(["test-owned.exe"], isolation=True).result == "TIMEOUT"


@pytest.mark.parametrize("point,state,passes", [("before", "reserved", True), ("after", "complete", True),
                                                ("before", "complete", False), ("after", "reserved", False)])
def test_writer_interruption_requires_complete_expected_target_json(monkeypatch, tmp_path, point, state, passes):
    anchor = tmp_path / ("ppt-writer-" + uuid.uuid4().hex)
    child = anchor / "AegisDesk" / "PptReaderRequests"
    child.mkdir(parents=True)
    request_id = str(uuid.uuid4())
    record = {"schema": 1, "request_id": request_id, "required_version": "1.0.4.0", "helper_sid": "S-1-5-21-1001",
              "helper_pid": 123, "helper_creation_ticks": "123456", "child_pid": None, "child_creation_ticks": None, "state": state}
    (child / (request_id + ".json")).write_text(json.dumps(record))
    if point == "before":
        (child / (request_id + ".json." + uuid.uuid4().hex + ".tmp")).write_text(json.dumps({**record, "state": "complete"}))
    def run(command, **kwargs):
        if command[2] == "cleanup":
            return subprocess.CompletedProcess(command, 0, "PPT_WRITER_CLEANUP PASS\n", "")
        raise subprocess.TimeoutExpired(command, 30, output=("PPT_WRITER_ARCH bits=64\n"
            + "PPT_WRITER_STAGE " + ("commit_call" if point == "before" else "commit_return")
            + " elapsed_ms=1\nPPT_WRITER_INTERRUPTION point=" + point + " ready=1\n"))
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _writer_run(["test-owned.exe", str(anchor), "interrupt-" + point, "1"])
    assert outcome.result == ("PASS" if passes else "FAIL")


def test_writer_rejection_records_native_and_win32_separately():
    output = ("PPT_WRITER_ARCH bits=64\n"
              "PPT_WRITER_NATIVE stage=commit_return ntstatus=C0000043 io_status=none\n"
              "PPT_WRITER_REJECT stage=request_commit error_type=IOException winerror=none\n"
              "PPT_WRITER_REJECT stage=confidential_marker error_type=IOException winerror=5\n"
              "PPT_WRITER_ERROR stage=commit_return kind=contract code=exception\n"
              "PPT_WRITER_STAGE complete elapsed_ms=2\nPPT_WRITER_RESULT FAIL\n")
    outcome = _writer_outcome(output, 1)
    assert (outcome.stage, outcome.ntstatus, outcome.io_status, outcome.winerror, outcome.setup_stage) == (
        "commit_return", "C0000043", "none", "none", "request_commit")
    assert "confidential" not in repr(outcome)


def test_writer_blocked_process_retains_fixed_os_codes(monkeypatch):
    error = OSError(13, "confidential_marker")
    error.winerror = 1260
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _writer_run(["test-owned.exe"])
    assert (outcome.stage, outcome.winerror, outcome.errno) == ("process_start", "1260", "13")
    assert "confidential" not in repr(outcome)
