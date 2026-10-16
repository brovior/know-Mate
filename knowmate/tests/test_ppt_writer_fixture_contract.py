"""Portable regressions for fixture provenance and primary/cleanup diagnostics."""
from __future__ import annotations

import subprocess
import uuid

import pytest

from knowmate.tests.test_ppt_request_writer import (
    _cleanup_contract_events,
    _cleanup_writer,
    _writer_outcome,
    _writer_run,
)

_ARCH = "PPT_WRITER_ARCH bits=64\n"
_ID = "PPT_WRITER_FIXTURE_ID volume=12 high=34 low=56\n"
_CLEAN = ("PPT_WRITER_CLEANUP result=PASS stage=cleanup_verify error_type=none code=none "
          "winerror=none ntstatus=none io_status=none\n")
_EXPECT = "PPT_WRITER_EXPECT expected=reject actual=reject verified=1\n"
_END = "PPT_WRITER_STAGE complete elapsed_ms=3\nPPT_WRITER_RESULT PASS\n"
_PRIMARY = ("PPT_WRITER_DIAGNOSTIC phase=primary stage=commit_return error_type=IOException "
            "code=exception winerror=none ntstatus=C0000043 io_status=C0000022\n")
_CLEAN_FAIL = ("PPT_WRITER_CLEANUP result=FAIL stage=cleanup_delete error_type=Win32Exception "
               "code=exception winerror=5 ntstatus=none io_status=none\n")


def test_primary_and_cleanup_failures_preserve_independent_evidence():
    """Cleanup never replaces the body's native status or stage."""
    output = _ARCH + _ID + _PRIMARY + _CLEAN_FAIL + _END.replace("RESULT PASS", "RESULT FAIL")
    result = _writer_outcome(output, 1)
    assert result.result == "FAIL"
    assert (result.stage, result.primary_stage, result.primary_error_type, result.primary_ntstatus,
            result.primary_io_status, result.primary_winerror) == (
                "commit_return", "commit_return", "IOException", "C0000043", "C0000022", "none")
    assert (result.cleanup_result, result.cleanup_stage, result.cleanup_winerror) == ("FAIL", "cleanup_delete", "5")


@pytest.mark.parametrize("cleanup", ["", _CLEAN_FAIL, _CLEAN + _CLEAN])
def test_expected_rejection_cannot_pass_without_one_successful_cleanup(cleanup):
    assert _writer_outcome(_ARCH + _ID + _EXPECT + cleanup + _END, 0).result == "FAIL"


def test_fixture_creation_unexecuted_retains_original_win32_and_cleanup_status():
    fixture = ("PPT_WRITER_DIAGNOSTIC phase=fixture stage=fixture_prepare error_type=Win32Exception "
               "code=fixture_creation_failed winerror=1314 ntstatus=none io_status=none\n"
               "PPT_WRITER_UNEXECUTED gate=reparse reason=fixture_creation_failed winerror=1314\n")
    output = _ARCH + _ID + fixture + _CLEAN + _END.replace("RESULT PASS", "RESULT FAIL")
    outcome = _writer_outcome(output, 1)
    assert (outcome.result, outcome.fixture_stage, outcome.fixture_winerror) == ("UNEXECUTED", "fixture_prepare", "1314")
    assert _writer_outcome(output.replace(_CLEAN, _CLEAN_FAIL), 1).result == "FAIL"
    assert _writer_outcome(output.replace(_CLEAN, _PRIMARY + _CLEAN), 1).result == "FAIL"
    # A privilege guess or fixture event alone cannot turn an ordinary failure into unexecuted.
    assert _writer_outcome(output.replace("phase=fixture", "phase=primary"), 1).result == "FAIL"


@pytest.mark.parametrize("ids", ["", _ID + _ID, _ID.replace("volume=12", "volume=4294967296")])
def test_missing_duplicate_or_out_of_range_identity_never_authorizes_cleanup(monkeypatch, tmp_path, ids):
    output = _ARCH + ids + "PPT_WRITER_STAGE native_unconfirmed elapsed_ms=1\n"
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 30, output=output.encode())
    monkeypatch.setattr(subprocess, "run", run)
    result = _writer_run(["fixture.exe", str(tmp_path / ("ppt-writer-" + uuid.uuid4().hex)), "pending", "1"], isolation=True)
    assert len(calls) == 1
    assert (result.result, result.cleanup_result, result.cleanup_code) == ("FAIL", "INCOMPLETE", "cleanup_identity_missing")


def test_cleanup_handoff_uses_only_emitted_id_and_preserves_primary(monkeypatch, tmp_path):
    command = ["fixture.exe", str(tmp_path / ("ppt-writer-" + uuid.uuid4().hex)), "pending", "2"]
    output = _ARCH + _ID + _PRIMARY
    primary = _writer_outcome(output, 1)
    def run(actual, **kwargs):
        assert actual == [command[0], command[1], "cleanup", "2", "12", "34", "56"]
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(actual, 0, _ARCH + _CLEAN + _END, "")
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _cleanup_writer(command, primary, root_absent=True)
    assert outcome.primary_ntstatus == "C0000043" and outcome.stage == "commit_return"
    assert outcome.cleanup_result == "PASS" and outcome.result == "FAIL"


def test_preexisting_root_is_never_cleaned_even_with_one_valid_child_id(monkeypatch, tmp_path):
    path = tmp_path / ("ppt-writer-" + uuid.uuid4().hex)
    path.mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 30, output=(_ARCH + _ID).encode())
    monkeypatch.setattr(subprocess, "run", run)
    result = _writer_run(["fixture.exe", str(path), "pending", "1"], isolation=True)
    assert len(calls) == 1 and path.is_dir()
    assert result.cleanup_result == "INCOMPLETE"


def test_cleanup_child_terminal_or_parse_failure_cannot_count_as_success(monkeypatch, tmp_path):
    command = ["fixture.exe", str(tmp_path / ("ppt-writer-" + uuid.uuid4().hex)), "pending", "1"]
    def run(actual, **kwargs):
        return subprocess.CompletedProcess(actual, 0, _ARCH + _CLEAN + _END + "unexpected\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    result = _cleanup_writer(command, _writer_outcome(_ARCH + _ID, 1), root_absent=True)
    assert result.result == "FAIL" and result.cleanup_result == "INCOMPLETE"


def test_unknown_diagnostic_text_is_filtered_and_duplicate_terminal_is_failure():
    output = (_ARCH + _ID + _EXPECT + _CLEAN
              + "PPT_WRITER_DIAGNOSTIC phase=primary stage=secret_marker error_type=IOException "
              "code=exception winerror=5 ntstatus=none io_status=none\n" + _END)
    assert "secret" not in repr(_writer_outcome(output, 0))
    assert _writer_outcome(output, 0).result == "FAIL"
    valid = _ARCH + _ID + _EXPECT + _CLEAN + _END
    assert _writer_outcome(valid, 0).result == "PASS"
    assert _writer_outcome(valid + "PPT_WRITER_RESULT PASS\n", 0).result == "FAIL"


def test_primary_error_marker_cannot_be_overridden_by_a_pass_terminal():
    """A forged or contradictory terminal cannot hide the preceding contract failure."""
    output = (_ARCH + _ID + _EXPECT + _CLEAN
              + "PPT_WRITER_ERROR stage=fixture_validate kind=contract code=fixture_link_count\n" + _END)
    assert _writer_outcome(output, 0).result == "FAIL"


def test_fixture_unexecuted_requires_matching_original_numeric_code():
    output = (_ARCH + _ID
              + "PPT_WRITER_DIAGNOSTIC phase=fixture stage=fixture_prepare error_type=Win32Exception "
              "code=fixture_creation_failed winerror=5 ntstatus=none io_status=none\n"
              + "PPT_WRITER_UNEXECUTED gate=hardlink reason=fixture_creation_failed winerror=1314\n"
              + _CLEAN + _END.replace("RESULT PASS", "RESULT FAIL"))
    assert _writer_outcome(output, 1).result == "FAIL"


@pytest.mark.parametrize("error_type", [
    "UnauthorizedAccessException", "IOException", "SecurityException", "InvalidOperationException",
    "ArgumentException", "NotSupportedException", "Exception", "Win32Exception", "policy",
])
def test_primary_diagnostics_preserve_every_bounded_product_error_type(error_type):
    output = (_ARCH + _ID + _PRIMARY.replace("error_type=IOException", "error_type=" + error_type)
              + _CLEAN + _END.replace("RESULT PASS", "RESULT FAIL"))
    result = _writer_outcome(output, 1)
    assert result.primary_error_type == error_type
    assert result.primary_ntstatus == "C0000043" and result.primary_io_status == "C0000022"


def test_cleanup_contract_relay_preserves_numeric_errors_and_filters_private_values():
    valid = "PPT_CLEANUP_DIAGNOSTIC phase=cleanup stage=cleanup_external error_type=Win32Exception code=exception winerror=5"
    invalid = [valid.replace("stage=cleanup_external", "stage=private_marker"),
               valid.replace("code=exception", "code=private_marker"),
               valid.replace("winerror=5", "winerror=4294967296"),
               "secret_prefix " + valid]
    assert _cleanup_contract_events("\n".join([valid, *invalid])) == (valid,)
