"""Typed HRESULT boundaries and scheduler correlation use production helpers."""
import logging
import threading
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from knowmate.secure import com_fault, com_stage, ppt_payload


def _error(outer=0x80020009, inner=None, description=""):
    error = RuntimeError("DOCUMENT_SENTINEL")
    error.hresult = outer
    error.excepinfo = (0, "SOURCE_SENTINEL", description, None, 0, inner)
    return error


@pytest.mark.parametrize("code", sorted(com_fault.FATAL_HRESULTS))
@pytest.mark.parametrize("signed", [False, True])
def test_inner_fatal_codes_accept_only_signed_or_unsigned_32_bit(code, signed):
    error = _error(inner=code - (1 << 32) if signed else code)
    assert com_fault.fatal_hresult(error) == code
    assert com_fault.exception_hresult_codes(error) == (0x80020009, code)


@pytest.mark.parametrize("invalid", [True, False, 1.0, "2147944122", 1 << 32, -(1 << 31) - 1, None])
def test_invalid_outer_and_inner_cannot_become_transport_failure(invalid):
    error = _error(invalid, invalid)
    assert com_fault.fatal_hresult(error) is None
    assert com_fault.outer_hresult(error) is None


def test_invalid_excepinfo_uses_only_standard_args_inner_slot():
    error = _error(inner=None)
    error.args = (0x80020009, "SECRET", (0, None, None, None, 0, 0x800706BA), 0x80010108)
    assert com_fault.fatal_hresult(error) == 0x800706BA
    error.args = (0x80020009, 0x800706BA)
    error.excepinfo = (0, None, None, None, 0, 0x800706BA, None)
    assert com_fault.fatal_hresult(error) is None


def test_causes_are_bounded_and_context_is_not_inspected():
    errors = [RuntimeError("SECRET") for _ in range(9)]
    for current, cause in zip(errors, errors[1:]):
        current.__cause__ = cause
    errors[-1].hresult = 0x800706BA
    assert com_fault.fatal_hresult(errors[0]) is None
    errors[-2].hresult = 0x800706BE
    assert com_fault.fatal_hresult(errors[0]) == 0x800706BE
    errors[-1].__cause__ = errors[0]
    assert com_fault.fatal_hresult(errors[0]) == 0x800706BE
    error = RuntimeError("RPC_S_SERVER_UNAVAILABLE: 0x800706BA")
    error.__context__ = errors[-1]
    assert com_fault.fatal_hresult(error) is None


def test_throwing_attributes_leave_original_error_and_valid_fallback_intact():
    class Error(Exception):
        @property
        def hresult(self):
            raise ValueError("DOCUMENT_SENTINEL")

        @property
        def excepinfo(self):
            raise ValueError("DOCUMENT_SENTINEL")

    error = Error(0x80020009, "SECRET", (0, None, None, None, 0, 0x800706BA))
    assert com_fault.outer_hresult(error) == 0x80020009
    assert com_fault.fatal_hresult(error) == 0x800706BA
    fault = com_fault.make_fault(error, "read")
    assert fault.original is error
    assert "DOCUMENT_SENTINEL" not in repr(fault)


@pytest.mark.parametrize("field", ["hresult", "excepinfo", "args", "__cause__"])
def test_each_throwing_introspection_field_is_unavailable_without_shadowing_primary(field):
    class Error(Exception):
        def __getattribute__(self, name):
            if name == field:
                raise ValueError("DOCUMENT_SENTINEL")
            return super().__getattribute__(name)

    error = Error(0x800706BA, "DOCUMENT_SENTINEL")
    error.hresult = 0x800706BA
    assert com_fault.fatal_hresult(error) == 0x800706BA


@pytest.mark.parametrize("code", [0x80010001, 0x8001010A, 0x80004005, 0x80020003])
def test_busy_and_application_errors_are_not_transport_failures(code):
    error = _error(code, code)
    assert com_fault.fatal_hresult(error) is None


@pytest.mark.parametrize("description,expected", [
    ("probe_extract_failed:cell_text:com:800706BA", 0x800706BA),
    ("probe_extract_failed:cell_text:800706BA", 0x800706BA),
    ("probe_extract_failed:arbitrary:com:800706BA", None),
    ("probe_extract_failed:cell_text:arbitrary:800706BA", None),
    ("probe_extract_failed:cell_text:com:800706ba", None),
    ("probe_extract_failed:cell_text:com:800706BA" + " " * 257, None),
])
def test_protocol_marker_is_strict_and_isolated_to_powerpoint(description, expected):
    error = _error(inner=0, description=description)
    assert com_fault.fatal_hresult(error) is None
    assert com_fault.fatal_hresult(error, app_exe="WINWORD.EXE") is None
    assert com_fault.fatal_hresult(error, app_exe="EXCEL.EXE") is None
    assert com_fault.fatal_hresult(error, app_exe="POWERPNT.EXE") == expected
    assert ppt_payload.fatal_hresult(error) == expected


def test_attempt_context_is_worker_local_and_stage_clear_does_not_end_recovery():
    com_stage.set_attempt_context(21, 31, "EXCEL.EXE")
    try:
        timer = com_stage.StageTimer("C:/x.xls")
        daemon_context = []
        thread = threading.Thread(target=lambda: daemon_context.append(com_stage.get_attempt_context()))
        thread.start()
        thread.join()
        com_stage.clear()
        assert com_stage.get_attempt_context().attempt_id == 31
        assert timer._attempt_context == com_stage.get_attempt_context()
        assert daemon_context == [None]
    finally:
        com_stage.clear_attempt_context()


def test_safe_poison_fields_keep_primary_close_codes_and_previous_recovery(caplog):
    from knowmate.collector.scheduler import _classify_poison_evidence, _log_com_poison, _log_office_guard
    from knowmate.secure.com_reader import OfficeComPoisonError
    from knowmate.secure.office_guard import OfficeBusyError, OfficeGuardDecision
    primary = com_fault.make_fault(_error(0x80004005), "cell_read", app_exe="EXCEL.EXE")
    close = com_fault.make_fault(_error(0x800706BA), "close", app_exe="EXCEL.EXE")
    poison = OfficeComPoisonError("EXCEL.EXE", 0x800706BA,
                                 evidence=com_fault.ComFailureEvidence(primary, (close,), close, 0x800706BA))
    assert _classify_poison_evidence(poison, stage_slot="close", watchdog_stage=None) == (
        "READ_ERROR", "0x80004005", "cell_read")
    assert _classify_poison_evidence(poison, stage_slot="close", watchdog_stage="open") == (
        "OPEN_TIMEOUT", "0x80004005", "cell_read")
    decision = OfficeGuardDecision("busy", "EXCEL.EXE", "external_process", 12, "live", 0.1, (40,), (40,))
    with caplog.at_level(logging.WARNING):
        _log_com_poison(poison, attempt_id=31)
        _log_office_guard(OfficeBusyError("DOCUMENT_SENTINEL", decision=decision),
                          attempt_id=32, previous_recovery=(31, True, 11), path="C:/x.xls")
    assert "DOCUMENT_SENTINEL" not in caplog.text and "SOURCE_SENTINEL" not in caplog.text
    assert "previous_recovery_attempt_id=31" in caplog.text
    assert "primary_stage=cell_read" in caplog.text
    assert "poison_stage=close" in caplog.text


def test_scheduler_throwing_primary_attribute_recovers_and_releases_attempt_before_next_file(tmp_path, caplog):
    from knowmate.collector.scheduler import CollectorWorker
    from knowmate.collector.failure_state import load_failures
    from knowmate.collector.state import load_state
    from knowmate.rag.embedding import EmbeddingClient
    from knowmate.rag.indexer import Indexer
    from knowmate.secure.com_reader import OfficeComPoisonError

    class PrimaryError(Exception):
        @property
        def hresult(self):
            raise ValueError("DOCUMENT_SENTINEL")

    folder = tmp_path / "docs"
    folder.mkdir()
    paths = [folder / "a.doc", folder / "b.doc"]
    for path in paths:
        path.write_bytes(b"legacy")
    attempt_contexts = []
    errors = []

    class Extractor:
        def extract(self, path):
            attempt_contexts.append(com_stage.get_attempt_context())
            if len(attempt_contexts) == 1:
                primary = com_fault.make_fault(PrimaryError("DOCUMENT_SENTINEL"), "read")
                close = com_fault.make_fault(_error(0x800706BA), "close")
                raise OfficeComPoisonError("WINWORD.EXE", 0x800706BA,
                                          evidence=com_fault.ComFailureEvidence(primary, (close,), close, 0x800706BA))
            return "정상 문서 내용"

    recovery_contexts = []

    def recover(exe):
        assert sys.exception() is None  # No raw parser exception is live during recovery.
        assert exe == "WINWORD.EXE"
        recovery_contexts.append(com_stage.get_attempt_context())
        return True

    embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
    indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
    worker = CollectorWorker(
        get_idle_seconds=lambda: 0.0,
        config={"collector": {"watch_folders": [str(folder)], "idle_seconds": 60,
                              "com_restart_every_n_files": 1, "com_timeout_base_sec": 0},
                "cleanup": {"dry_run": True, "max_delete_ratio": 0.3},
                "chunking": {"chunk_size": 400, "overlap": 80}},
        indexer=indexer, extractor=Extractor(), state_file=tmp_path / "state.json",
        failure_file=tmp_path / "failures.json", com_poison_recovery_fn=recover,
        com_restart_fn=MagicMock(),
    )
    worker.error.connect(errors.append)
    com_stage.clear_attempt_context()
    with caplog.at_level(logging.DEBUG):
        worker.run()
    assert not errors
    assert len(attempt_contexts) == 2
    assert recovery_contexts == [attempt_contexts[0]]
    assert attempt_contexts[1].attempt_id > attempt_contexts[0].attempt_id
    assert com_stage.get_attempt_context() is None
    state = load_state(tmp_path / "state.json")
    assert sum(Path(path).suffix == ".doc" for path in state) == 1
    failures = load_failures(tmp_path / "failures.json")
    assert len(failures) == 1
    record = next(iter(failures.values()))
    assert record.kind == "READ_ERROR" and record.last_error_code is None
    assert "DOCUMENT_SENTINEL" not in caplog.text
    assert "SOURCE_SENTINEL" not in caplog.text
    worker._com_restart_fn.assert_called_once()


@pytest.mark.parametrize("initialized", [False, True])
@pytest.mark.parametrize("unverified_release_fails", [False, True])
def test_worker_finally_releases_verified_refs_despite_apartment_or_release_error(
    monkeypatch, tmp_path, initialized, unverified_release_fails, native_com_backend,
):
    from knowmate.collector.scheduler import CollectorWorker
    from knowmate.secure import com_reader, office_guard

    from knowmate.secure.com_scope import acquire_current_scope, ComScopeUnavailable
    uninitialize = MagicMock()
    native_com_backend.initialize = lambda: 0 if initialized else -2147417850
    native_com_backend.uninitialize = uninitialize
    com_reader.release_verified_pending_com_refs()
    worker = CollectorWorker(config={"collector": {}}, indexer=MagicMock(), extractor=MagicMock(),
                             state_file=tmp_path / "state.json", com_cleanup_fn=MagicMock())

    def cycle():
        record = office_guard.OwnedOfficeProcess("EXCEL.EXE", 123, generation=2)
        com_reader._retain_verified_pending_app("EXCEL.EXE", object(), 20, record)
        try:
            acquire_current_scope()
        except ComScopeUnavailable:
            pass  # A failed native init must still release existing reference slots.

    monkeypatch.setattr(worker, "_run_cycle", cycle)
    if unverified_release_fails:
        def fail_release():
            raise RuntimeError("release failed")
        monkeypatch.setattr(com_reader, "release_unverified_com_refs", fail_release)
        with pytest.raises(RuntimeError, match="release failed"):
            worker.run()
    else:
        worker.run()
    assert not com_reader._tls.verified_cleanup
    assert uninitialize.call_count == int(initialized)
