"""Offline lifetime candidate regressions; no Office, DLL build or native resources."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import weakref
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from scripts.diagnostics.ppt_lifetime import evidence, fixture, gate, runner, supervisor
from knowmate.secure.ppt_lifetime_com import parse_identity, run_caller


class FixtureTests(unittest.TestCase):
    """Mutable provenance cannot authorize other content hidden inside a synthetic deck."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = fixture.create_fixture(Path(self.temporary.name) / "fixture")
        self.original = self.fixture.path.read_bytes()

    def rewrite(self, transform) -> fixture.Fixture:
        with ZipFile(self.fixture.path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members = transform(members)
        with ZipFile(self.fixture.path, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return replace(self.fixture, sha256=hashlib.sha256(self.fixture.path.read_bytes()).hexdigest())

    def test_normal_deck_and_reordered_zip_are_accepted(self) -> None:
        fixture.verify_fixture(self.fixture)
        reordered = self.rewrite(lambda members: dict(reversed(list(members.items()))))
        fixture.verify_fixture(reordered)

    def test_hidden_parts_are_rejected_even_after_hash_recalculation(self) -> None:
        for name in ("ppt/notesSlides/notesSlide1.xml", "customXml/item1.xml", "unknown.xml"):
            with self.subTest(name=name):
                self.fixture.path.write_bytes(self.original)
                changed = self.rewrite(lambda members: dict(members, **{name: b"SYNTHETIC_TAMPER"}))
                with self.assertRaises(ValueError):
                    fixture.verify_fixture(changed)

    def test_document_properties_replacement_is_rejected(self) -> None:
        changed = self.rewrite(lambda members: dict(members, **{"docProps/core.xml": b"<tamper/>"}))
        with self.assertRaises(ValueError):
            fixture.verify_fixture(changed)

    def test_unknown_or_missing_members_are_rejected(self) -> None:
        changed = self.rewrite(lambda members: {name: data for name, data in members.items() if name != "docProps/core.xml"})
        with self.assertRaises(ValueError):
            fixture.verify_fixture(changed)

    def test_duplicate_members_and_expanded_content_are_rejected(self) -> None:
        with self.assertWarns(UserWarning):
            with ZipFile(self.fixture.path, "a") as archive:
                archive.writestr("ppt/slides/slide1.xml", b"x" * (1024 * 1024))
        changed = replace(self.fixture, sha256=hashlib.sha256(self.fixture.path.read_bytes()).hexdigest())
        with self.assertRaises(ValueError):
            fixture.verify_fixture(changed)

    def test_large_compressed_existing_member_is_rejected_before_decompression(self) -> None:
        changed = self.rewrite(lambda members: dict(members, **{"ppt/slides/slide1.xml": b"x" * (8 * 1024 * 1024)}))
        self.assertLess(changed.path.stat().st_size, 4 * 1024 * 1024)
        with self.assertRaises(ValueError):
            fixture.verify_fixture(changed)

    def test_archive_symlink_member_is_rejected(self) -> None:
        with ZipFile(self.fixture.path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        with ZipFile(self.fixture.path, "w") as archive:
            for name, data in members.items():
                info = ZipInfo(name)
                if name == "docProps/core.xml":
                    info.external_attr = 0o120777 << 16
                archive.writestr(info, data)
        changed = replace(self.fixture, sha256=hashlib.sha256(self.fixture.path.read_bytes()).hexdigest())
        with self.assertRaises(ValueError):
            fixture.verify_fixture(changed)

    def test_path_symlink_and_template_pin_changes_are_rejected(self) -> None:
        link = self.fixture.path.parent / "alias"
        link.symlink_to(self.fixture.path)
        with self.assertRaises(ValueError):
            fixture.verify_fixture(replace(self.fixture, path=link))
        with self.assertRaises(ValueError):
            fixture.verify_fixture(replace(self.fixture, template_sha256="0" * 64))

    def test_generator_dependency_version_is_pinned_even_when_template_matches(self) -> None:
        import pptx
        with patch.object(pptx, "__version__", "synthetic-different-version"):
            with self.assertRaises(ValueError):
                fixture.verify_fixture(self.fixture)


class FakeScope:
    def __init__(self, session, acquire_failure=False) -> None:
        self.session, self.acquire_failure = session, acquire_failure

    def __enter__(self):
        return self

    def acquire(self) -> None:
        if self.acquire_failure:
            raise RuntimeError("synthetic_acquire_failure")
        self.session.actions.append("acquire")

    def __exit__(self, *_):
        if self.session.holds:
            raise AssertionError("COM references outlive own native pair")
        self.session.actions.append("uninitialize")


class FakeSession:
    def __init__(self, failure=None) -> None:
        self.failure = failure
        self.actions = []
        self.holds = {}

    def scope(self):
        return FakeScope(self, self.failure == "acquire")

    def bind(self) -> None:
        self.holds["app"] = object()
        self.actions.append("bind")

    def open_fixture(self) -> None:
        self.holds["document"] = object()
        self.actions.append("open")

    def read_fixture(self) -> str:
        self.actions.append("read")
        return "synthetic mismatch" if self.failure == "read" else fixture.TEXT

    def close_document(self) -> None:
        self.actions.append("close")
        if self.failure == "close":
            raise RuntimeError("synthetic_close_failure")
        self.holds.pop("document")

    def check_empty(self) -> None:
        self.actions.append("idle")

    def quit_once(self) -> None:
        self.actions.append("quit")
        if self.failure == "quit":
            raise RuntimeError("synthetic_quit_failure")

    def drop(self) -> None:
        self.actions.append("drop")
        if self.failure == "drop":
            raise RuntimeError("synthetic_drop_failure")
        self.holds.clear()


class CallerTests(unittest.TestCase):
    """Persistence failures cannot bypass reference cleanup or authorize further Quit."""

    def test_natural_drop_never_requests_quit_and_external_requests_once(self) -> None:
        for arm, count in zip(evidence.ARMS, (0, 1)):
            session = FakeSession()
            rows = []
            self.assertEqual(run_caller(session, arm, lambda event, **fields: rows.append((event, fields))), 0)
            self.assertEqual(session.actions.count("quit"), count)
            self.assertEqual(session.actions[-2:], ["drop", "uninitialize"])
            self.assertEqual(rows[-2][0], "native_pair_completed")

    def test_emit_failure_at_every_boundary_still_drops_before_uninitialize(self) -> None:
        for boundary in ("native_scope_acquired", "host_bound", "document_opened", "synthetic_read",
                         "document_closed", "idle_checked", "quit_requested", "quit_returned",
                         "references_dropped", "native_pair_completed", "caller_complete"):
            with self.subTest(boundary=boundary):
                session = FakeSession()
                def emit(event, **fields):
                    if event == boundary:
                        raise OSError("synthetic_persist_failure")
                self.assertEqual(run_caller(session, evidence.ARMS[1], emit), 2)
                self.assertFalse(session.holds)
                self.assertEqual(session.actions[-1], "uninitialize")
                if boundary in {"native_scope_acquired", "host_bound", "document_opened", "synthetic_read",
                                "document_closed", "idle_checked", "quit_requested"}:
                    self.assertNotIn("quit", session.actions)

    def test_acquire_read_close_quit_and_drop_failures_do_not_keep_references(self) -> None:
        for failure in ("acquire", "read", "close", "quit", "drop"):
            with self.subTest(failure=failure):
                session, rows = FakeSession(failure), []
                self.assertEqual(run_caller(session, evidence.ARMS[1], lambda event, **fields: rows.append(event)), 2)
                self.assertFalse(session.holds)
                self.assertEqual(session.actions[-1], "uninitialize")
                if failure == "acquire":
                    self.assertNotIn("native_pair_completed", rows)

    def test_identity_rejects_extra_duplicate_boolean_or_out_of_range_fields(self) -> None:
        self.assertEqual(parse_identity('{"pid":12,"creation_filetime":34}'), (12, 34))
        for reply in ('{"pid":true,"creation_filetime":34}', '{"pid":12,"pid":12,"creation_filetime":34}',
                      '{"pid":12,"creation_filetime":34,"body":"synthetic"}',
                      '{"pid":0,"creation_filetime":34}', '{"pid":12,"creation_filetime":-1}'):
            with self.assertRaises(ValueError):
                parse_identity(reply)

    def test_operation_drop_and_emit_error_frames_end_before_uninitialize(self) -> None:
        references = []
        class Marker:
            pass
        class Scope(FakeScope):
            def __exit__(scope, *_):
                self.assertTrue(all(reference() is None for reference in references))
                super().__exit__()
        class Session(FakeSession):
            def scope(session):
                return Scope(session)
            def read_fixture(session):
                marker = Marker()
                references.append(weakref.ref(marker))
                raise RuntimeError("synthetic_read_frame")
            def drop(session):
                marker = Marker()
                references.append(weakref.ref(marker))
                raise RuntimeError("synthetic_drop_frame")
        def emit(event, **fields):
            if event == "caller_failed":
                marker = Marker()
                references.append(weakref.ref(marker))
                raise OSError("synthetic_emit_frame")
        session = Session()
        self.assertEqual(run_caller(session, evidence.ARMS[1], emit), 2)
        self.assertEqual(session.actions[-1], "uninitialize")
        self.assertTrue(all(reference() is None for reference in references))

    def test_retained_operation_and_emit_errors_do_not_keep_com_frames(self) -> None:
        references, retained_errors = [], []
        class Marker:
            pass
        class Scope(FakeScope):
            def __exit__(scope, *_):
                self.assertTrue(all(reference() is None for reference in references))
                super().__exit__()
        class Session(FakeSession):
            def scope(session):
                return Scope(session)
            def read_fixture(session):
                marker = Marker()
                references.append(weakref.ref(marker))
                error = RuntimeError("synthetic_retained_read_error")
                retained_errors.append(error)
                raise error
        def emit(event, **fields):
            if event == "caller_failed":
                marker = Marker()
                references.append(weakref.ref(marker))
                error = OSError("synthetic_retained_emit_error")
                retained_errors.append(error)
                raise error
        self.assertEqual(run_caller(Session(), evidence.ARMS[1], emit), 2)
        self.assertEqual(len(retained_errors), 2)
        self.assertTrue(all(error.__traceback__ is None for error in retained_errors))


class EvidenceTests(unittest.TestCase):
    """OS exit is a fact separate from a complete, successful experiment."""

    def sample(self, arm=evidence.ARMS[1]):
        backend = runner.MockBackend(arm)
        manifest = dict(schema=1, variant="lifetime_evidence", mode="live", binding=backend.binding)
        return manifest, backend.controller(), [backend.observe()]

    def test_failed_or_incomplete_experiment_keeps_exit_fact_but_never_completes(self) -> None:
        for missing in ("document_closed", "references_dropped", "native_pair_completed", "caller_complete", "quit_returned"):
            manifest, control, observed = self.sample()
            # Removal of a prerequisite is malformed evidence, which must not pass either.
            try:
                result = evidence.summarize(manifest, [r for r in control if r["event"] != missing], observed, False)
            except ValueError:
                continue
            self.assertTrue(result["actual_os_exit_verified"])
            self.assertFalse(result["experiment_complete"])
        manifest, control, observed = self.sample()
        read = next(row for row in control if row["event"] == "synthetic_read")
        read["sha256"] = "0" * 64
        result = evidence.summarize(manifest, control, observed, False)
        self.assertTrue(result["actual_os_exit_verified"])
        self.assertFalse(result["experiment_complete"])
        failed = dict(control[-1], event="caller_failed", error_type="RuntimeError")
        result = evidence.summarize(manifest, control + [failed], observed, False)
        self.assertTrue(result["actual_os_exit_verified"])
        self.assertFalse(result["experiment_complete"])

    def test_foreign_unknown_body_fields_and_bad_clock_are_rejected(self) -> None:
        manifest, control, observed = self.sample()
        for changes in ({"pid":True}, {"event":"unknown"}, {"body":"synthetic"}, {"qpc_ns":-1}):
            with self.assertRaises(ValueError):
                evidence.summarize(manifest, [dict(control[0], **changes)] + control[1:], observed, False)
        control[-1]["qpc_ns"] = 0
        with self.assertRaises(ValueError):
            evidence.summarize(manifest, control, observed, False)

    def test_unbound_child_exit_and_quit_return_only_never_prove_host_exit(self) -> None:
        manifest, control, observed = self.sample()
        result = evidence.summarize(manifest, [], observed, False)
        self.assertEqual(result["os_exit"], "target_unconfirmed")
        result = evidence.summarize(manifest, control, [], False)
        self.assertFalse(result["actual_os_exit_verified"])
        self.assertFalse(result["experiment_complete"])

    def test_closed_gate_precedes_private_runtime_and_output_side_effects(self) -> None:
        for mode in ("--live", "--caller"):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "never-created"
                with contextlib.redirect_stdout(io.StringIO()) as stream:
                    code = runner.main([mode, "--output", str(output)])
                self.assertEqual(code, 2)
                self.assertIn(gate.LIVE_LOCK_REASON, stream.getvalue())
                self.assertFalse(output.exists())

    def test_mock_and_replay_never_claim_actual_os_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mock"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--mock", "--output", str(output)]), 0)
            for arm in evidence.ARMS:
                summary = json.loads((output / arm / "summary.json").read_text())
                self.assertFalse(summary["actual_os_exit_verified"])
                self.assertTrue(summary["protocol_complete"])
            replayed = runner.replay(output / evidence.ARMS[0], Path(directory) / "replay")
            self.assertFalse(replayed["actual_os_exit_verified"])
            self.assertFalse(replayed["experiment_complete"])

    def test_second_arm_is_not_started_when_first_target_remains(self) -> None:
        created = []
        class Pending(runner.MockBackend):
            def observe(self):
                return dict(super().observe(), status="alive")
        def factory(arm):
            created.append(arm)
            return Pending(arm)
        with tempfile.TemporaryDirectory() as directory:
            result = supervisor.compare(Path(directory) / "pending", factory, mode="mock", duration_ms=1)
        self.assertEqual(created, [evidence.ARMS[0]])
        self.assertEqual(result["unexecuted"], [evidence.ARMS[1]])

    def test_live_exit_code_uses_complete_experiment_instead_of_os_exit_only(self) -> None:
        from types import SimpleNamespace
        from knowmate.secure import ppt_lifetime_runtime as runtime
        args = SimpleNamespace(package=Path("package"), source=Path("source"), output=Path("output"),
                               pins=Path("pins"), office_bitness=64)
        with patch.object(runtime, "require_live_review"), patch.object(runtime, "read_json", return_value={}):
            for complete, expected in ((False, 2), (True, 0)):
                result = {"arms": {arm: dict(actual_os_exit_verified=True, experiment_complete=complete)
                                   for arm in evidence.ARMS}}
                with patch.object(supervisor, "compare", return_value=result):
                    self.assertEqual(runtime.live_entry(args), expected)


class PackageTests(unittest.TestCase):
    """A diagnostic artifact never falls through to normal installation contracts."""

    def test_package_pins_and_disabled_policy_are_independently_enforced(self) -> None:
        import shutil
        from scripts.ppt_reader import build_lifetime as builder
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            package = Path(directory) / "package"
            package.mkdir()
            for relative in (*builder.CORE, "scripts/ppt_reader/build_lifetime.py"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(builder.ROOT / relative, target)
            (package / builder.DLL).write_bytes(b"synthetic artifact; never compiled or executed")
            generated = builder.copy_sources(package / "generated", root)
            manifest = builder.create_manifest(package, generated, root)
            pins = dict(package_manifest_sha256=gate.digest(package / builder.MANIFEST),
                        installed_reader=str(package / builder.DLL))
            self.assertEqual(gate.validate_package(package, root, pins), manifest)
            with self.assertRaises(ValueError):
                gate.validate_package(package, root, dict(pins, package_manifest_sha256="0" * 64))
            manifest["effective_internal_quit"] = True
            (package / builder.MANIFEST).write_text(json.dumps(manifest))
            pins["package_manifest_sha256"] = gate.digest(package / builder.MANIFEST)
            with self.assertRaises(ValueError):
                gate.validate_package(package, root, pins)
            self.assertFalse((package / "manifest.json").exists())
            self.assertFalse((package / "PptReaderSetup.exe").exists())


if __name__ == "__main__":
    unittest.main()
