"""Offline safety and evidence tests; these never activate or terminate Office."""
from __future__ import annotations

import copy
import gc
import json
import sys
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref
from zipfile import ZipFile

from knowmate.secure import ppt_quit_compare as compare
from knowmate.secure import ppt_quit_compare_com as com
from knowmate.secure import ppt_quit_compare_fixture as fixture


def binding(arm="external_quit"):
    return dict(run_id="a" * 32, pid=123, creation=456, arm=arm)


def rows(arm="external_quit"):
    events = ["native_scope_acquired", "host_bound", "synthetic_read", "document_closed"]
    if arm != "reference_drop":
        events += ["quit_requested", "quit_returned"]
    events += ["references_dropped", "native_pair_completed", "caller_complete"]
    result = []
    for index, event in enumerate(events):
        row = dict(run_id="a" * 32, pid=123, creation=456, event=event, qpc_ns=index + 1)
        if event == "quit_returned":
            row["status"] = "returned" if arm == "external_quit" else "quit_requested"
        if event == "native_scope_acquired":
            row["pythoncom_preimported"] = False
        result.append(row)
    return result


class EvidenceTests(unittest.TestCase):
    def test_quit_return_and_caller_exit_never_prove_office_exit(self):
        result = compare.summarize_arm(binding(), "timeout", False, rows())
        self.assertTrue(result["experiment_complete"])
        self.assertFalse(result["actual_os_exit_verified"])
        self.assertEqual(result["outcome"], "remaining")

    def test_actual_exit_is_separate_from_incomplete_experiment(self):
        result = compare.summarize_arm(binding(), "exited", False, rows()[:2])
        self.assertTrue(result["actual_os_exit_verified"])
        self.assertFalse(result["experiment_complete"])
        self.assertFalse(compare.summarize_arm(binding(), "exited", False, [])['actual_os_exit_verified'])

    def test_hung_or_unknown_caller_never_completes(self):
        for alive in (True, None):
            result = compare.summarize_arm(binding(), "exited", alive, rows())
            self.assertFalse(result["experiment_complete"])
            self.assertFalse(result["caller_exited"])

    def test_missing_quit_reordered_duplicate_and_refused_results_do_not_pass(self):
        for changed in (rows()[:4] + rows()[6:], rows()[::-1]):
            try:
                self.assertFalse(compare.summarize_arm(binding(), "exited", False, changed)["experiment_complete"])
            except ValueError:
                pass
        with self.assertRaises(ValueError):
            compare.summarize_arm(binding(), "exited", False, rows() + [rows()[-1]])
        changed = rows("internal_quit")
        changed[5]["status"] = "protected"
        self.assertFalse(compare.summarize_arm(binding("internal_quit"), "exited", False, changed)["experiment_complete"])

    def test_foreign_identity_arbitrary_text_bool_and_container_fields_rejected(self):
        for updates in ({"pid": True}, {"creation": 457}, {"run_id": "other"},
                        {"document_text": "PRIVATE"}, {"event": []}, {"status": []},
                        {"qpc_ns": True}, {"pythoncom_preimported": "PRIVATE"}):
            value = dict(rows()[0], **updates)
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                compare.validate_row(value, binding())

    def test_error_metadata_is_numeric_or_fixed_and_does_not_echo_private_text(self):
        error = OSError("PRIVATE_DOCUMENT_PATH")
        error.winerror = 5
        fields = com.error_fields(error)
        self.assertEqual(fields["winerror"], 5)
        self.assertEqual(fields["reason"], "unspecified")
        self.assertNotIn("PRIVATE", json.dumps(fields))
        compare.validate_row(dict(run_id="a" * 32, pid=123, creation=456, qpc_ns=1,
                                  event="operation_failed", stage="bind", **fields), binding())

    def test_partial_record_at_caller_exit_invalidates_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = compare.WindowsBackend.__new__(compare.WindowsBackend)
            backend.directory = Path(temporary)
            backend.config = binding()
            backend.target = SimpleNamespace(poll=lambda: SimpleNamespace(status="exited", error_code=None))
            backend.caller = SimpleNamespace(poll=lambda: 0)
            (backend.directory / "caller.jsonl").write_text(json.dumps(rows()[0]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evidence_partial"):
                backend.poll()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "new-run"

    def backend(self, *, state="exited", alive=False, fail=False, close_fail=False):
        calls = []
        class Backend:
            target = None
            caller = None
            def __init__(self, *_):
                pass
            def start(self, arm):
                self.arm = arm
                calls.append(arm)
                if fail:
                    raise RuntimeError("PRIVATE_STARTUP_DATA")
                return binding(arm)
            def poll(self):
                return state, alive, rows(self.arm)
            def close(self):
                if close_fail:
                    raise OSError("PRIVATE_CLOSE_DATA")
        return Backend, calls

    def run_comparison(self, backend):
        tick = iter(range(0, 100, 5))
        with patch.object(compare.time, "monotonic", side_effect=lambda: next(tick)), patch.object(compare.time, "sleep"):
            return compare.run_compare(self.output, confirm_isolated_session=True, timeout=5, backend_factory=backend)

    def test_remaining_host_stops_and_logs_all_skipped_arms(self):
        backend, calls = self.backend(state="timeout")
        report = self.run_comparison(backend)
        self.assertEqual(calls, ["external_quit"])
        self.assertEqual(report["arms"]["internal_quit"]["outcome"], "UNEXECUTED")
        self.assertIn("reference_drop", (self.output / "summary.log").read_text())
        self.assertIn("UNEXECUTED", (self.output / "summary.log").read_text())

    def test_hung_caller_stops_even_when_original_office_exits(self):
        backend, calls = self.backend(alive=True)
        report = self.run_comparison(backend)
        self.assertEqual(calls, ["external_quit"])
        self.assertTrue(report["stopped"])

    def test_nonzero_caller_exit_stops_despite_complete_stage_records(self):
        backend, calls = self.backend()
        backend.caller = SimpleNamespace(poll=lambda: 2, pid=321)
        report = self.run_comparison(backend)
        self.assertEqual(calls, ["external_quit"])
        self.assertFalse(report["arms"]["external_quit"]["experiment_complete"])
        self.assertEqual(report["arms"]["external_quit"]["caller_exit_code"], 2)

    def test_bool_binding_identity_is_rejected_before_native_resources(self):
        config = dict(arm="external_quit", pid=True, creation=456, run_id="a" * 32,
                      token="b" * 64, parent_pid=1, parent_creation=2,
                      fixture_path=str(self.output / "fixture" / "synthetic.pptx"),
                      fixture_sha256="c" * 64, evidence=str(self.output / "caller.jsonl"),
                      timeout=30, allowed_pids=[1])
        with self.assertRaisesRegex(ValueError, "caller_identity"):
            compare.validate_binding(config)

    def test_independent_arms_run_in_order_but_mock_never_claims_actual_exit(self):
        backend, calls = self.backend()
        report = self.run_comparison(backend)
        self.assertEqual(calls, list(compare.ARMS))
        self.assertFalse(report["stopped"])
        self.assertTrue(report["simulated"])
        self.assertTrue(all(not arm["actual_os_exit_verified"] for arm in report["arms"].values()))

    def test_startup_and_close_errors_leave_fixed_summary_and_stop(self):
        for fail, close in ((True, False), (False, True)):
            with self.subTest(fail=fail, close=close):
                self.output = Path(self.temporary.name) / ("fail" if fail else "close")
                backend, calls = self.backend(fail=fail, close_fail=close)
                report = self.run_comparison(backend)
                self.assertTrue(report["stopped"])
                self.assertEqual(len(calls), 1)
                self.assertTrue((self.output / "result.json").exists())
                self.assertNotIn("PRIVATE", (self.output / "summary.log").read_text())

    def test_no_opt_in_bool_nan_or_invalid_timeout_has_side_effects(self):
        for kwargs in ({}, {"confirm_isolated_session": True, "timeout": float("nan")},
                       {"confirm_isolated_session": True, "timeout": True},
                       {"confirm_isolated_session": True, "timeout": 301}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                compare.run_compare(self.output, **kwargs)
            self.assertFalse(self.output.exists())


class FixtureTests(unittest.TestCase):
    def test_whole_package_verification_rejects_hidden_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            value = fixture.create_fixture(Path(temporary) / "fixture")
            fixture.verify_fixture(value)
            with ZipFile(value.path, "a") as archive:
                archive.writestr("customXml/item1.xml", b"PRIVATE_TAMPER")
            with self.assertRaises(ValueError):
                fixture.verify_fixture(value)

    def test_verifier_does_not_require_generator_source_file(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "scripts.diagnostics.ppt_lifetime.fixture.generator_sha256", side_effect=AssertionError("source unavailable")):
            fixture.verify_fixture(fixture.create_fixture(Path(temporary) / "fixture"))


class CallerTests(unittest.TestCase):
    def test_startup_activation_rejects_foreign_host_before_documents_or_service(self):
        session = com.Session.__new__(com.Session)
        session.config = dict(pid=123)
        session.holds = {}
        session.target_check = Mock()
        session.counts = Mock()
        session.native = Mock()
        session.native.owner.side_effect = RuntimeError("window_owner_mismatch")
        application = Mock(HWND=42)
        client = ModuleType("win32com.client")
        client.Dispatch = Mock(return_value=application)
        package = ModuleType("win32com")
        package.client = client
        with patch.dict(sys.modules, {"win32com": package, "win32com.client": client}), self.assertRaises(
                RuntimeError):
            session.bind()
        client.Dispatch.assert_called_once_with("PowerPoint.Application")
        session.counts.assert_not_called()
        application.Presentations.Open.assert_not_called()
        application.COMAddIns.Item.assert_not_called()
        application.Quit.assert_not_called()

    def test_private_caller_on_unsupported_platform_does_not_construct_native(self):
        with patch.object(compare.os, "name", "posix"), patch(
                "knowmate.secure.ppt_capture_native.Native") as native:
            self.assertEqual(compare.caller_entry(), 2)
            native.assert_not_called()

    def test_failure_frames_unwind_and_references_drop_before_native_pair(self):
        class Payload:
            pass
        value = Payload()
        reference = weakref.ref(value)
        events = []
        class Scope:
            def __enter__(self):
                return self
            def acquire(self):
                events.append("acquire")
            def __exit__(self, *_):
                gc.collect()
                self_test.assertIsNone(reference())
                events.append("uninitialize")
        class Session:
            holds = {"app": value}
            def bind(self):
                pass
            def read(self):
                error = RuntimeError("PRIVATE_BODY")
                error.reference = self.holds["app"]
                raise error
            def drop(self):
                self.holds.clear()
                events.append("drop")
        self_test = self
        del value
        rows_out = []
        result = com.run_caller(Session(), "external_quit", lambda event, **fields: rows_out.append(dict(event=event, **fields)), Scope)
        self.assertEqual(result, 2)
        self.assertLess(events.index("drop"), events.index("uninitialize"))
        self.assertNotIn("PRIVATE", json.dumps(rows_out))

    def test_count_guard_refusal_does_not_emit_quit_requested_or_call_quit(self):
        session = com.Session.__new__(com.Session)
        session.requested = False
        session.counts = Mock(side_effect=RuntimeError("user_document_or_unknown"))
        session.holds = {"app": Mock()}
        emit = Mock()
        with self.assertRaises(RuntimeError):
            session.quit("external_quit", emit)
        emit.assert_not_called()
        session.holds["app"].Quit.assert_not_called()

    def test_foreign_aegisdesk_stops_before_any_com_access(self):
        session = com.Session.__new__(com.Session)
        session.config = dict(pid=123, creation=456, allowed_pids=[10, 11])
        session.native = Mock()
        session.target = 99
        with patch("knowmate.secure.ppt_capture_observer.processes", return_value=[
                dict(pid=123, name="powerpnt.exe"), dict(pid=12, name="aegisdesk.exe")]):
            with self.assertRaisesRegex(RuntimeError, "concurrent_application"):
                session.target_check()


if __name__ == "__main__":
    unittest.main()
