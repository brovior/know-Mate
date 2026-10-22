"""Unsupported application HWND must use the production ownership proof safely."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from knowmate.secure import office_guard, ppt_quit_compare as compare, ppt_quit_compare_com as com


class BindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = com.Session.__new__(com.Session)
        self.session.config = {"pid": 123}
        self.session.holds = {}
        self.session.cache = Mock()
        self.session.native = Mock()
        self.session.target_check = Mock()
        self.session.counts = Mock()
        self.application = object()  # Hwnd and HWND genuinely unsupported.
        client = ModuleType("win32com.client")
        client.Dispatch = Mock(return_value=self.application)
        package = ModuleType("win32com")
        package.client = client
        context = patch.dict(sys.modules, {"win32com": package, "win32com.client": client})
        context.start()
        self.addCleanup(context.stop)
        self.cleanup = []

    @contextmanager
    def probe(self, app):
        self.assertIs(app, self.application)
        try:
            yield lambda: 777
        finally:
            self.cleanup.append("closed")

    def test_missing_hwnd_uses_production_probe_and_cleans_before_service(self) -> None:
        def service(*_):
            self.assertEqual(self.cleanup, ["closed"])
            return "compatible_service"
        with patch.object(office_guard, "_ppt_ownership_window", self.probe), patch(
                "knowmate.secure.ppt_addin_reader.select_service", side_effect=service):
            self.session.bind()
        self.session.native.owner.assert_called_once_with(777, 123)
        self.assertEqual(self.session.holds["service"], "compatible_service")
        self.assertEqual(self.session.counts.call_count, 3)

    def test_nonempty_host_blocks_probe_and_service(self) -> None:
        self.session.counts.side_effect = RuntimeError("user_document_or_unknown")
        with patch.object(office_guard, "_ppt_ownership_window") as probe, patch(
                "knowmate.secure.ppt_addin_reader.select_service") as service, self.assertRaises(RuntimeError):
            self.session.bind()
        probe.assert_not_called()
        service.assert_not_called()
        self.session.native.owner.assert_not_called()

    def test_probe_pid_mismatch_cleans_probe_and_never_selects_service(self) -> None:
        self.session.native.owner.side_effect = RuntimeError("window_owner_mismatch")
        with patch.object(office_guard, "_ppt_ownership_window", self.probe), patch(
                "knowmate.secure.ppt_addin_reader.select_service") as service, self.assertRaises(RuntimeError):
            self.session.bind()
        self.assertEqual(self.cleanup, ["closed"])
        service.assert_not_called()
        self.assertNotIn("document", self.session.holds)

    def test_failure_records_fixed_substep_and_original_error_type_without_text(self) -> None:
        class FailedSession:
            bind_step = "app_hwnd"
            def bind(self):
                raise AttributeError("PRIVATE_ATTRIBUTE_PATH")
        records = []
        self.assertFalse(com.operate(FailedSession(), "external_quit",
                                     lambda event, **fields: records.append(dict(event=event, **fields))))
        self.assertEqual(records[0]["bind_step"], "app_hwnd")
        self.assertEqual(records[0]["error_type"], "AttributeError")
        self.assertNotIn("PRIVATE", json.dumps(records))
        base = dict(run_id="a" * 32, pid=123, creation=456, qpc_ns=1)
        compare.validate_row(dict(base, **records[0]), base)

    def test_wrapped_probe_error_preserves_only_fixed_cause_type(self) -> None:
        try:
            raise office_guard.OfficeOwnershipProbeError("PRIVATE_WRAPPER") from AttributeError("PRIVATE_CAUSE")
        except office_guard.OfficeOwnershipProbeError as error:
            fields = com.error_fields(error)
        self.assertEqual(fields["cause_error_type"], "AttributeError")
        self.assertNotIn("PRIVATE", json.dumps(fields))
        base = dict(run_id="a" * 32, pid=123, creation=456, qpc_ns=1)
        row = dict(base, event="operation_failed", stage="bind", bind_step="caption_probe", **fields)
        compare.validate_row(row, base)
        for updates in ({"cause_error_type": []}, {"cause_hresult": "PRIVATE"}, {"bind_step": "PRIVATE"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                compare.validate_row(dict(row, **updates), base)


if __name__ == "__main__":
    unittest.main()
