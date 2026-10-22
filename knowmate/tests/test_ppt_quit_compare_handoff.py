"""Original-handle handoff contracts; no real process or Office activation."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from knowmate.secure import ppt_quit_compare as compare
from knowmate.secure import ppt_quit_compare_com as com
from knowmate.secure import ppt_quit_compare_handoff as handoff


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "launch-handoff.json"
        self.config = dict(kind=handoff.KIND, version=handoff.VERSION, arm="external_quit",
                           run_id="a" * 32, token="b" * 64, parent_pid=11, parent_creation=77,
                           fixture_path=str(self.directory / "fixture" / "synthetic.pptx"),
                           fixture_sha256="c" * 64, timeout=30,
                           evidence=str(self.directory / "caller.jsonl"), allowed_pids=[11],
                           office_bitness=64, handoff=str(self.path))
        self.native = SimpleNamespace(
            wait=Mock(return_value=False),
            creation=Mock(side_effect=lambda handle: 77 if handle == 999 else 456),
            duplicate=Mock(return_value=222),
            k=SimpleNamespace(GetProcessId=Mock(return_value=123),
                              WaitForSingleObject=Mock(return_value=258), CloseHandle=Mock(return_value=True)))
        self.launched = SimpleNamespace(_handle=111, pid=123, identity=456)

    def publish(self):
        handoff.publish_launch(self.native, 999, self.launched, self.config)

    def test_duplicate_uses_only_observation_rights_and_parent_adopts_original_capability(self):
        handoff.validate_bootstrap(self.config)
        self.publish()
        self.native.duplicate.assert_called_once_with(111, target=999, rights=0x101000)
        observer = handoff.receive_launch(self.native, self.path, self.config, os.getpid())
        self.assertEqual((observer.pid, observer.identity), (123, 456))
        self.native.k.GetProcessId.assert_called_once_with(222)
        self.assertEqual(observer.poll().status, "timeout")
        observer.close()
        self.native.k.CloseHandle.assert_called_once_with(222)
        self.assertFalse(hasattr(self.native.k, "OpenProcess"))

    def test_foreign_nonce_caller_bool_or_oversized_handle_are_not_adopted_or_closed(self):
        self.publish()
        original = json.loads(self.path.read_bytes())
        for updates in ({"token": "d" * 64}, {"caller_pid": os.getpid() + 1},
                        {"handle": True}, {"handle": 1 << (ctypes.sizeof(ctypes.c_void_p) * 8)},
                        {"pid": True}, {"creation": 457}):
            with self.subTest(updates=updates):
                self.path.write_text(json.dumps(dict(original, **updates)), encoding="utf-8")
                with self.assertRaises(ValueError):
                    handoff.receive_launch(self.native, self.path, self.config, os.getpid())
        self.native.k.CloseHandle.assert_not_called()

    def test_missing_partial_or_duplicate_key_handoff_never_becomes_os_exit(self):
        self.assertIsNone(handoff.receive_launch(self.native, self.path, self.config, os.getpid()))
        for content in (b'{"kind":', b'{"kind":"a","kind":"b"}'):
            self.path.write_bytes(content)
            with self.assertRaisesRegex(ValueError, "handoff_record"):
                handoff.receive_launch(self.native, self.path, self.config, os.getpid())
        self.native.k.GetProcessId.assert_not_called()

    def test_changed_parent_prevents_duplicate_and_publication(self):
        self.native.creation.side_effect = None
        self.native.creation.return_value = 78
        with self.assertRaisesRegex(RuntimeError, "handoff_parent_changed"):
            self.publish()
        self.native.duplicate.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_missing_creation_identity_fails_before_publication_and_preserves_bootstrap(self):
        session = com.Session.__new__(com.Session)
        session.config = dict(self.config)
        session.native = self.native
        session.parent = 999
        session.emit = Mock()
        launched = SimpleNamespace(pid=123, identity=None, close=Mock())
        with patch("knowmate.secure.ppt_capture_observer.processes", return_value=[]), patch(
                "knowmate.secure.ppt_launch.trusted_powerpoint_path", return_value=Path("POWERPNT.EXE")), patch(
                "knowmate.secure.ppt_launch.LaunchedPowerPoint", return_value=launched), self.assertRaisesRegex(
                RuntimeError, "launch_identity_unknown"):
            session.launch_and_handoff()
        self.assertNotIn("pid", session.config)
        session.emit.assert_not_called()
        self.native.duplicate.assert_not_called()
        launched.close.assert_called_once_with()

    def test_bootstrap_rejects_existing_target_identity_and_malformed_parent(self):
        for updates in ({"pid": 123}, {"parent_pid": True}, {"version": True},
                        {"office_bitness": True}, {"allowed_pids": [99]}, {"handoff": "relative.json"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                handoff.validate_bootstrap(dict(self.config, **updates))

    def test_prelaunch_records_have_no_target_identity_and_never_prove_exit(self):
        row = dict(event="native_scope_acquired", qpc_ns=1, run_id=self.config["run_id"],
                   pid=None, creation=None, pythoncom_preimported=False)
        compare.validate_row(row, self.config)
        result = compare.summarize_arm(dict(run_id=self.config["run_id"], arm="external_quit", pid=None,
                                            creation=None, launch_handoff_required=True),
                                      "target_unconfirmed", False, [row])
        self.assertFalse(result["actual_os_exit_verified"])
        self.assertFalse(result["handoff_verified"])
        self.assertFalse(result["experiment_complete"])
        with self.assertRaises(ValueError):
            compare.validate_row(dict(row, event="host_bound"), self.config)

    def test_launch_handoff_then_dispatch_are_adjacent_after_client_preparation(self):
        session = com.Session.__new__(com.Session)
        session.bootstrap = True
        session.holds = {}
        session.config = dict(pid=123, creation=456)
        session.cache = SimpleNamespace(ppt_service=None)
        session.emit = Mock()
        session.native = SimpleNamespace(owner=Mock())
        session.counts = Mock()
        order = []
        session.launch_and_handoff = lambda: order.append("launch_and_handoff")
        app = Mock()
        client = SimpleNamespace(Dispatch=Mock(side_effect=lambda *_: order.append("dispatch") or app))
        import sys
        win32com = SimpleNamespace(client=client)
        with patch.dict(sys.modules, {"win32com": win32com, "win32com.client": client}), patch(
                "knowmate.secure.office_guard._app_hwnd", return_value=12), patch(
                "knowmate.secure.ppt_addin_reader.select_service", return_value=Mock()):
            session.bind()
        self.assertEqual(order, ["launch_and_handoff", "dispatch"])
        client.Dispatch.assert_called_once_with("PowerPoint.Application")

    def test_real_handoff_mode_needs_startup_stages_in_their_exact_position(self):
        from knowmate.tests.test_ppt_quit_compare import rows, binding
        original = rows()
        startup = [dict(original[0], event=event) for event in (
            "launch_created", "handle_transferred", "dispatch_entered", "dispatch_returned")]
        for record in startup:
            record.pop("pythoncom_preimported")
        for sequence in (original, original + startup, original[:1] + startup[::-1] + original[1:]):
            for index, record in enumerate(sequence):
                record["qpc_ns"] = index + 1
            result = compare.summarize_arm(dict(binding(), launch_handoff_required=True), "exited", False, sequence)
            self.assertFalse(result["experiment_complete"])

    def test_native_scope_and_client_import_precede_launch_then_single_dispatch(self):
        import sys
        session = com.Session.__new__(com.Session)
        session.bootstrap = True
        session.holds = {}
        session.config = dict(pid=123, creation=456)
        session.cache = SimpleNamespace(ppt_service=None)
        session.native = SimpleNamespace(owner=Mock())
        session.counts = Mock()
        session.read = Mock()
        session.close_document = Mock()
        session.quit = lambda arm, emit: emit("quit_requested") or "returned"
        state = {"scope": False}
        order = []
        test = self
        class Scope:
            def __enter__(self):
                return self
            def acquire(self):
                state["scope"] = True
                order.append("native_initialized")
            def __exit__(self, *_):
                order.append("native_uninitialized")
        def launch():
            test.assertTrue(state["scope"])
            test.assertIn("win32com.client", sys.modules)
            order.append("launch")
            session.emit("launch_created")
            session.emit("handle_transferred")
        session.launch_and_handoff = launch
        client = SimpleNamespace(Dispatch=lambda *_: order.append("dispatch") or Mock())
        with patch.dict(sys.modules, {"win32com": SimpleNamespace(client=client), "win32com.client": client}), patch(
                "knowmate.secure.office_guard._app_hwnd", return_value=12), patch(
                "knowmate.secure.ppt_addin_reader.select_service", return_value=Mock()):
            code = com.run_caller(session, "external_quit", lambda *_args, **_fields: None, Scope)
        self.assertEqual(code, 0)
        self.assertEqual(order, ["native_initialized", "launch", "dispatch", "native_uninitialized"])


if __name__ == "__main__":
    unittest.main()
