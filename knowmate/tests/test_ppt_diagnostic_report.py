"""Regression coverage for one-run PPT collection and final pytest evidence."""
from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts import ppt_diagnostic_report as report
from scripts.ppt_diagnostic_junit import NODE_IDS, TEST_IDS, junit_outcomes
from scripts.ppt_diagnostic_markers import WRITER_MODES, marker_line, valid_writer_case


def _xml(outcomes=None, *, duplicates=(), separate=False):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="pytest")
    for owner, (classname, name) in TEST_IDS.items():
        case = ET.SubElement(suite, "testcase", classname=classname, name=name)
        if outcomes and owner in outcomes:
            kind, message = outcomes[owner]
            ET.SubElement(case, kind, message=message)
    for owner, kind, message in duplicates:
        parent = ET.SubElement(root, "testsuite", name="other") if separate else suite
        classname, name = TEST_IDS[owner]
        case = ET.SubElement(parent, "testcase", classname=classname, name=name)
        if kind:
            ET.SubElement(case, kind, message=message)
    return ET.tostring(root)


def _case(mode="create", leases="1", result="PASS"):
    item = {"category": "actual_writer", "mode": mode, "leases": leases, "result": result, "expected": "success",
                "actual": "success", "bits": "64", "last_stage": "complete", "error_code": "none", "winerror": "none",
                "errno": "none", "ntstatus": "none", "io_status": "none", "setup_stage": "none", "cleanup_result": "PASS"}
    for phase in ("fixture", "primary", "cleanup"):
        for field in ("stage", "error_type", "code", "winerror"):
            item[phase + "_" + field] = "none"
        if phase != "fixture":
            item[phase + "_ntstatus"] = item[phase + "_io_status"] = "none"
    return item


def _stdout():
    return {"phase": "complete", "last_stage": "before_return", "stage_started_ms": "100", "actual_exit_before_cleanup": "true",
                "done": "true", "wait_result": "0", "winerror": "unavailable", "cleanup": "PASS", "cleanup_error": "none",
                "cleanup_wait_result": "0", "cleanup_winerror": "unavailable", "actual_exit_code": "0", "cleanup_error_winerror": "unavailable"}


def _log(*, prefix=False, problem=None):
    lines = []
    def add(owner, marker, item):
        line = marker_line(marker, item)
        lines.append((NODE_IDS[owner] + " " if prefix else "") + line)
    add("baseline", "PPT_RENAME_SUMMARY", {"category": "baseline", "comparison_failures": "5", "native_failures": "0",
                                               "required": "false", "product_approval": "false"})
    counts = dict.fromkeys(("pass", "fail", "timeout", "skip", "unexecuted"), 0)
    for mode in WRITER_MODES:
        for leases in ("1", "2"):
            case = problem if problem and (mode, leases) == (problem["mode"], problem["leases"]) else _case(mode, leases)
            counts[case["result"].lower()] += 1
            add("actual_writer", "PPT_WRITER_CASE", case)
    add("actual_writer", "PPT_WRITER_SUMMARY", {"category": "actual_writer", **{key: str(value) for key, value in counts.items()},
                                                 "product_approval": "false"})
    add("acl", "PPT_ACL_SUMMARY", {"result": "PASS", "actual_request_writer": "true"})
    add("stdout", "PPT_STDOUT_SUMMARY", {"result": "PASS"})
    add("stdout", "PPT_STDOUT_DIAGNOSTIC", _stdout())
    add("cleanup_contract", "PPT_CLEANUP_SUMMARY", {"result": "PASS"})
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("kind,result", [("failure", "FAIL"), ("error", "FAIL"), ("skipped", "UNEXECUTED")])
def test_final_single_pytest_outcomes(kind, result):
    assert junit_outcomes(_xml({"acl": (kind, "private-error")}))["acl"].result == result


def test_body_and_teardown_failure_pair_is_fail_with_both_phases():
    final = junit_outcomes(_xml({"acl": ("failure", "secret")},
                              duplicates=[("acl", "error", "failed on teardown with secret")]))["acl"]
    assert (final.result, final.body_failure, final.teardown_error) == ("FAIL", True, True)


@pytest.mark.parametrize("duplicates,outcome,separate", [
    ([("acl", None, "")], None, False),
    ([("acl", "failure", "")], ("failure", ""), False),
    ([("acl", "error", "unknown phase")], ("failure", ""), False),
    ([("acl", "error", "failed on setup with secret")], ("failure", ""), False),
    ([("acl", "error", "failed on teardown with secret")], ("failure", ""), True),
    ([("acl", "error", "failed on teardown with secret"), ("acl", None, "")], ("failure", ""), False),
])
def test_ambiguous_duplicates_never_merge(duplicates, outcome, separate):
    assert junit_outcomes(_xml({"acl": outcome} if outcome else None,
                              duplicates=duplicates, separate=separate))["acl"].result == "INCOMPLETE"


@pytest.mark.parametrize("xml", [b"", b"<broken", b"<testcase/>", b'<!DOCTYPE testsuites [<!ENTITY a "secret">]><testsuites/>'])
def test_missing_broken_and_entity_xml_is_incomplete(xml):
    assert all(item.result == "INCOMPLETE" for item in junit_outcomes(xml).values())


def test_missing_identity_and_multi_outcome_structure_is_incomplete():
    root = ET.fromstring(_xml())
    root[0].remove(root[0][0])
    assert junit_outcomes(ET.tostring(root))["baseline"].result == "INCOMPLETE"
    root = ET.fromstring(_xml({"acl": ("failure", "")}))
    ET.SubElement(root[0][2], "error", message="failed on teardown with secret")
    assert junit_outcomes(ET.tostring(root))["acl"].result == "INCOMPLETE"


@pytest.mark.parametrize("prefix", [False, True])
def test_exact_markers_allow_pure_or_actual_pytest_node_prefix(prefix):
    short = "\n".join(report.build_short_report(_log(prefix=prefix), _xml(), 0))
    assert "required_gate=PASS" in short
    assert "comparison_failures=5 native_failures=0" in short
    assert "category=actual_writer result=PASS pass=62" in short
    assert "distinct_accounts=UNEXECUTED" in short and "product_approval=false" in short


@pytest.mark.parametrize("prefix", ["arbitrary ", "> print('", "E ", "    ", NODE_IDS["stdout"] + " "])
def test_raw_source_tracebacks_arbitrary_and_wrong_owner_prefix_rejected(prefix):
    log = _log().replace("PPT_ACL_SUMMARY", prefix + "PPT_ACL_SUMMARY")
    assert "category=acl result=INCOMPLETE" in "\n".join(report.build_short_report(log, _xml(), 0))


@pytest.mark.parametrize("replacement", [
    "PPT_ACL_SUMMARY result=PASS actual_request_writer=true secret=S-1-5-21-123",
    "PPT_ACL_SUMMARY result=PASS actual_request_writer=true actual_request_writer=true",
    "PPT_ACL_SUMMARY result=PASS", "PPT_ACL_SUMMARY result=PASS actual_request_writer=true trailing",
])
def test_malformed_or_extended_markers_cannot_grant_pass(replacement):
    log = _log().replace("PPT_ACL_SUMMARY result=PASS actual_request_writer=true", replacement)
    short = "\n".join(report.build_short_report(log, _xml(), 0))
    assert "category=acl result=INCOMPLETE" in short and "S-1-5" not in short


def test_case_summary_and_junit_contradictions_incomplete():
    log = _log().replace("pass=62 fail=0", "pass=61 fail=1")
    assert "category=actual_writer result=INCOMPLETE" in "\n".join(report.build_short_report(log, _xml(), 0))
    log = _log().replace("cleanup_result=PASS", "cleanup_result=FAIL", 1)
    assert "category=actual_writer result=INCOMPLETE" in "\n".join(report.build_short_report(log, _xml(), 0))
    short = "\n".join(report.build_short_report(_log(), _xml({"acl": ("failure", "secret")}), 1))
    assert "category=acl result=INCOMPLETE" in short


def test_canonical_summary_never_reports_pass_after_teardown_error():
    short = "\n".join(report.build_short_report(_log(), _xml({"acl": ("error", "failed on teardown with private")}), 1))
    assert "PPT_ACL_SUMMARY category=acl result=FAIL actual_request_writer=true body_result=PASS teardown_error=true" in short
    assert "PPT_ACL_SUMMARY category=acl result=PASS" not in short


def test_validated_managed_security_booleans_and_control_flags_remain_visible():
    security = ("PPT_RENAME_SECURITY_SUMMARY mode=managed-replace leases=1 owner_equal=1 dacl_equal=0 "
                "protected_before=1 protected_after=1 canonical_before=1 canonical_after=1 control_before=32772 control_after=32788")
    short = "\n".join(report.build_short_report(_log() + security, _xml(), 0))
    assert security in short
    short = "\n".join(report.build_short_report(_log() + security + " SID=S-1-5-21", _xml(), 0))
    assert "category=baseline result=INCOMPLETE" in short and "S-1-5-21" not in short


def test_actual_writer_producer_roundtrip_covers_all_62_cases_and_timeout_modes(monkeypatch, tmp_path, capsys):
    from knowmate.tests import test_ppt_request_writer as producer

    def run(command, *, isolation=False):
        mode = command[2]
        if isolation:
            outcome = producer._writer_outcome(
                "PPT_WRITER_ARCH bits=64\nPPT_WRITER_STAGE native_unconfirmed elapsed_ms=1\n"
                "PPT_WRITER_HOLD verified=1 buffers=1 source=1 lease=1 mutex=1\n", None,
                timed_out=True, isolation=True)
            assert outcome.result == "PASS" and outcome.expected == outcome.actual == "isolate"
            from dataclasses import replace
            return replace(outcome, cleanup_result="PASS", cleanup_stage="cleanup_verify")
        kind = "interrupt" if mode.startswith("interrupt-") else "success"
        return producer._WriterOutcome("PASS", stage="complete", bits="64", expected=kind, actual=kind,
                                       cleanup_result="PASS", cleanup_stage="cleanup_verify")

    monkeypatch.setattr(producer, "_writer_run", run)
    producer._collect_writer(tmp_path / "writer.exe", tmp_path)
    actual = capsys.readouterr().out
    log = "\n".join(line for line in _log().splitlines() if not line.startswith("PPT_WRITER_")) + "\n" + actual
    short = "\n".join(report.build_short_report(log, _xml(), 0))
    assert "PPT_WRITER_SUMMARY category=actual_writer result=PASS pass=62" in short
    assert actual.count("PPT_WRITER_CASE ") == 62


def test_unexecuted_fixture_keeps_original_numeric_error_and_is_not_approval():
    case = _case("hardlink", "1", "UNEXECUTED")
    case.update(fixture_stage="fixture_prepare", fixture_code="fixture_creation_failed",
                fixture_error_type="Win32Exception", fixture_winerror="1314")
    short = "\n".join(report.build_short_report(_log(problem=case), _xml({"actual_writer": ("failure", "private")}), 1))
    assert "category=actual_writer result=FAIL pass=61 fail=0 timeout=0 skip=0 unexecuted=1" in short
    assert "result=UNEXECUTED fixture_stage=fixture_prepare" in short and "fixture_winerror=1314" in short


def test_invalid_numeric_fields_and_false_success_case_are_rejected():
    for key, value in (("winerror", "4294967296"), ("ntstatus", "private"), ("primary_code", "private"),
                       ("setup_stage", "private"), ("expected", "none"), ("cleanup_result", "UNCONFIRMED")):
        case = _case()
        case[key] = value
        assert not valid_writer_case(case)


@pytest.mark.parametrize("error_type", ["UnauthorizedAccessException", "SecurityException", "InvalidOperationException",
                                      "ArgumentException", "NotSupportedException", "Exception"])
def test_product_exception_types_survive_fixed_case_summary(error_type):
    case = _case("replace", "1", "FAIL")
    case.update(primary_stage="commit_return", primary_error_type=error_type, primary_code="exception", primary_winerror="5")
    short = "\n".join(report.build_short_report(_log(problem=case), _xml({"actual_writer": ("failure", "private")}), 1))
    assert "category=actual_writer result=FAIL" in short and "primary_error_type=" + error_type in short


def test_cleanup_contract_preserves_primary_and_both_cleanup_errors():
    events = [
        "PPT_CLEANUP_DIAGNOSTIC phase=primary stage=external_verify error_type=contract code=cleanup_external_changed winerror=none",
        "PPT_CLEANUP_DIAGNOSTIC phase=cleanup stage=cleanup_root error_type=Win32Exception code=cleanup_exception winerror=5",
        "PPT_CLEANUP_DIAGNOSTIC phase=cleanup stage=cleanup_external error_type=IOException code=cleanup_exception winerror=32",
    ]
    log = _log().replace("PPT_CLEANUP_SUMMARY result=PASS", "PPT_CLEANUP_SUMMARY result=FAIL") + "\n".join(events)
    xml = _xml({"cleanup_contract": ("failure", "private original exception")})
    short = "\n".join(report.build_short_report(log, xml, 1))
    assert "PPT_CLEANUP_SUMMARY category=cleanup_contract result=FAIL body_failure=true" in short
    assert short.count("PPT_CLEANUP_PROBLEM") == 3 and "winerror=5" in short and "winerror=32" in short
    duplicate = "\n".join(report.build_short_report(log + "\n" + events[1], xml, 1))
    assert "category=cleanup_contract result=INCOMPLETE" in duplicate


def test_cleanup_skip_marker_matches_final_junit():
    log = _log().replace("PPT_CLEANUP_SUMMARY result=PASS", "PPT_CLEANUP_SUMMARY result=UNEXECUTED")
    short = "\n".join(report.build_short_report(log, _xml({"cleanup_contract": ("skipped", "private")}), 0))
    assert "required_gate=UNEXECUTED" in short
    assert "PPT_CLEANUP_SUMMARY category=cleanup_contract result=UNEXECUTED" in short


def test_stdout_exit_code_wait_and_progress_must_match_pass_summary():
    for key, value in (("actual_exit_code", "1"), ("wait_result", "258"), ("last_stage", "stdout_before"),
                       ("cleanup", "FAIL"), ("done", "false"), ("cleanup_error_winerror", "4294967296")):
        original = marker_line("PPT_STDOUT_DIAGNOSTIC", _stdout())
        changed = _stdout()
        changed[key] = value
        log = _log().replace(original, marker_line("PPT_STDOUT_DIAGNOSTIC", changed))
        short = "\n".join(report.build_short_report(log, _xml(), 0))
        assert "category=stdout result=INCOMPLETE" in short


def test_failures_keep_first_cleanup_native_and_fixture_evidence_without_none_noise():
    case = _case("hardlink", "2", "FAIL")
    case.update(primary_stage="fixture_read", primary_code="fixture_read_accepted", primary_error_type="contract",
                primary_ntstatus="C0000043", cleanup_result="FAIL", cleanup_stage="cleanup_delete",
                cleanup_code="cleanup_exception", cleanup_error_type="Win32Exception", cleanup_winerror="5")
    short = "\n".join(report.build_short_report(_log(problem=case), _xml({"actual_writer": ("failure", "secret")}), 1))
    assert "category=actual_writer result=FAIL" in short
    problem = next(line for line in short.splitlines() if line.startswith("PPT_WRITER_PROBLEM"))
    assert "primary_code=fixture_read_accepted" in problem and "cleanup_winerror=5" in problem
    assert "primary_ntstatus=C0000043" in problem and "=none" not in problem


def test_skip_does_not_need_success_markers_and_nonzero_is_not_inferred_pass():
    xml = _xml({key: ("skipped", "private reason") for key in TEST_IDS})
    short = "\n".join(report.build_short_report("", xml, 0))
    assert short.count("result=UNEXECUTED") == 5 and "required_gate=UNEXECUTED" in short
    assert "required_gate=INCOMPLETE" in report.build_short_report(_log(), _xml(), 2)[0]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-be"])
def test_atomic_report_preserves_detailed_log_and_identical_end_blocks(tmp_path, encoding, capsys):
    run_id = "a" * 32
    log = tmp_path / f"AegisDesk-PptRename-{run_id}.txt"
    xml = log.with_suffix(".xml")
    original = _log() + "private original detail SID S-1-5-21 SDDL O:SY\n"
    if encoding == "utf-16-be":
        log.write_bytes(b"\xfe\xff" + original.encode(encoding))
    else:
        log.write_text(original, encoding=encoding)
    xml.write_bytes(_xml())
    assert report.finalize_report(log, xml, run_id, 0) == 0
    data = log.read_text(encoding=encoding if encoding != "utf-16-be" else "utf-16")
    first, remaining = data.split("PPT_SHORT_END\n", 1)
    assert remaining.endswith(first + "PPT_SHORT_END\n")
    assert original in remaining and not xml.exists()
    assert "S-1-5-21" not in capsys.readouterr().out


@pytest.mark.parametrize("which", ["log", "xml", "missing", "broken", "decode", "write"])
def test_collection_failure_keeps_original_and_emits_only_fixed_error(tmp_path, monkeypatch, capsys, which):
    run_id = "a" * 32
    log = tmp_path / f"AegisDesk-PptRename-{run_id}.txt"
    xml = log.with_suffix(".xml")
    log.write_bytes(b"private raw log\n")
    xml.write_bytes(_xml())
    if which == "log":
        monkeypatch.setattr(report, "MAX_BYTES", 2)
    elif which == "xml":
        xml.write_bytes(b"x" * 200)
        monkeypatch.setattr(report, "MAX_BYTES", 100)
    elif which == "missing":
        xml.unlink()
    elif which == "broken":
        xml.write_bytes(b"<broken private>")
    elif which == "decode":
        log.write_bytes(b"\xffprivate")
    else:
        def fail(*args):
            raise OSError("private raw exception")
        monkeypatch.setattr(report.os, "replace", fail)
    original = log.read_bytes()
    assert report.finalize_report(log, xml, run_id, 7) == 1
    assert log.read_bytes() == original
    assert xml.exists() == (which != "missing")
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out and "code=report_collection_failed" in out and "private" not in out


def test_wrong_run_paths_never_read_remove_or_replace_files(tmp_path):
    log, xml = tmp_path / "old.txt", tmp_path / "old.xml"
    log.write_text("old detail")
    xml.write_bytes(_xml())
    assert report.finalize_report(log, xml, "b" * 32, 0) == 1
    assert log.read_text() == "old detail" and xml.exists()


def test_preexisting_rewrite_file_is_never_deleted(tmp_path):
    run_id = "c" * 32
    log = tmp_path / f"AegisDesk-PptRename-{run_id}.txt"
    xml, rewrite = log.with_suffix(".xml"), log.with_name(log.name + ".rewrite")
    log.write_text(_log())
    xml.write_bytes(_xml())
    rewrite.write_text("unowned prior file")
    assert report.finalize_report(log, xml, run_id, 0) == 1
    assert rewrite.read_text() == "unowned prior file"


def test_input_limit_is_exactly_16_mib(tmp_path):
    assert report.MAX_BYTES == 16 * 1024 * 1024
    path = tmp_path / "bounded"
    path.write_bytes(b"x" * report.MAX_BYTES)
    assert len(report._read_bounded(path)) == report.MAX_BYTES
    with path.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(ValueError, match="input_limit"):
        report._read_bounded(path)


@pytest.mark.parametrize("body,teardown,kind", [(False, False, "PASS"), (True, False, "FAIL"),
    (False, True, "FAIL"), (True, True, "FAIL"), ("skip", False, "UNEXECUTED"), ("setup", False, "FAIL")])
def test_actual_pytest_s_v_and_final_junit(tmp_path, body, teardown, kind):
    target = tmp_path / "knowmate/tests/test_ppt_acl_native.py"
    target.parent.mkdir(parents=True)
    code = "import pytest\n@pytest.fixture\ndef fixture(request):\n"
    if body == "setup":
        code += "    pytest.fail('private setup')\n"
    elif teardown:
        code += "    request.addfinalizer(lambda: pytest.fail('private teardown'))\n"
    else:
        code += "    pass\n"
    code += "def test_ppt_native_legacy_directory_lease(fixture):\n"
    code += ("    pytest.skip('private skip')\n" if body == "skip" else
             "    pytest.fail('private body')\n" if body is True else
             "    print('PPT_ACL_SUMMARY result=PASS actual_request_writer=true', flush=True)\n")
    target.write_text(code)
    xml = tmp_path / "result.xml"
    env = {**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    completed = subprocess.run([sys.executable, "-m", "pytest", str(target.relative_to(tmp_path)), "-s", "-v", "--color=no",
                                "-o", "addopts=", f"--junitxml={xml}"], check=False, cwd=tmp_path, env=env,
                               capture_output=True, text=True, timeout=30)
    outcome = junit_outcomes(xml.read_bytes())["acl"]
    assert outcome.result == kind
    assert outcome.body_failure == (body is True)
    assert outcome.teardown_error == teardown
    short = "\n".join(report.build_short_report(completed.stdout, xml.read_bytes(), completed.returncode))
    assert f"category=acl result={kind}" in short
    assert "private" not in short
    if body is False:
        assert NODE_IDS["acl"] + " PPT_ACL_SUMMARY" in completed.stdout
