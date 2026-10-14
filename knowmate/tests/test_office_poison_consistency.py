"""Production guard and getter regressions for COM recovery state races."""
from __future__ import annotations

import sys
import threading
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from knowmate.secure import com_reader, com_stage, office_guard as guard


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    monkeypatch.setattr(guard, "_owned_lock", threading.Lock())
    monkeypatch.setattr(guard, "_owned_pids", {})
    monkeypatch.setattr(guard, "_cleanup_inflight", {})
    monkeypatch.setattr(guard, "_unverified_cleanup", {})
    monkeypatch.setattr(guard, "_cleanup_retry_at", {})
    monkeypatch.setattr(guard, "_application_closing", threading.Event())
    monkeypatch.setattr(guard, "_cache", {"ts": 0.0, "procs": None, "epoch": 0})
    monkeypatch.setattr(guard, "_guard_epoch", 0)
    monkeypatch.setattr(com_reader, "_tls", threading.local())
    com_stage.clear()
    yield
    com_stage.clear()


def _record(exe="EXCEL.EXE", generation=7):
    return guard.OwnedOfficeProcess(exe, 2020, generation=generation)


def _finish_exit(exe, pid, record):
    """Perform a real registry cleanup ABA without a mocked state transition."""
    guard.begin_shutdown_cleanup(exe)
    try:
        pending = guard._mark_owned_cleanup_pending(pid, record)
        assert pending is not None
        assert guard._remove_owned_if_matches(pid, pending)
    finally:
        guard.finish_shutdown_cleanup(exe)


def test_snapshot_cannot_republish_enumeration_started_before_cleanup(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    result, errors = [], []

    def enumerate_blocked():
        entered.set()
        assert release.wait(3)
        return [("EXCEL.EXE", 20)]

    def read_snapshot():
        try:
            result.append(guard._cached_process_snapshot())
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(guard, "_enumerate_processes", enumerate_blocked)
    worker = threading.Thread(target=read_snapshot, daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        guard.begin_shutdown_cleanup("EXCEL.EXE")
        guard.finish_shutdown_cleanup("EXCEL.EXE")
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors
    assert result == [None]
    assert guard._cache["procs"] is None


def test_guard_retries_stale_handed_snapshot_after_identity_lookup(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    record = _record()
    guard._owned_pids[20] = record
    enumerations = []

    def enumerate_live():
        enumerations.append(1)
        return [("EXCEL.EXE", 20)] if len(enumerations) == 1 else []

    def identity(pid):
        _finish_exit("EXCEL.EXE", pid, record)
        return 2020

    monkeypatch.setattr(guard, "_enumerate_processes", enumerate_live)
    monkeypatch.setattr(guard, "_process_creation_identity", identity)
    decision = guard.inspect_office_for_ext(".xls")
    assert decision.status == "available"
    assert decision.listed_pids == ()
    assert len(enumerations) == 2


def test_guard_final_gate_recheck_catches_cleanup_during_identity_lookup(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    guard._owned_pids[20] = _record()
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 20)])

    def identity(pid):
        guard.begin_shutdown_cleanup("EXCEL.EXE")
        return 2020

    monkeypatch.setattr(guard, "_process_creation_identity", identity)
    assert guard.inspect_office_for_ext(".xlsx").status == "deferred"


def test_guard_churn_is_bounded_and_deferred_instead_of_fail_open(monkeypatch):
    calls = []

    def always_stale():
        calls.append(1)
        guard.begin_shutdown_cleanup("EXCEL.EXE")
        guard.finish_shutdown_cleanup("EXCEL.EXE")
        return []

    monkeypatch.setattr(guard, "_enumerate_processes", always_stale)
    assert guard.inspect_office_for_ext(".xls").status == "deferred"
    assert len(calls) == 2


def test_cached_process_list_expires_two_seconds_after_enumeration_start(monkeypatch):
    now = [10.0]
    calls = []

    def enumerate_slow():
        calls.append(1)
        now[0] += 2.1
        return []

    monkeypatch.setattr(guard.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(guard, "_enumerate_processes", enumerate_slow)
    assert guard._cached_process_snapshot().procs == ()
    assert guard._cache["procs"] is None
    guard._cached_process_snapshot()
    assert len(calls) == 2
    assert guard._CACHE_TTL_SEC == 2.0


def test_mutations_invalidate_cache_but_noops_preserve_epoch():
    record = _record()
    guard._owned_pids[20] = record
    guard._cache.update(procs=(("EXCEL.EXE", 20),), ts=1.0)
    pending = guard._mark_owned_cleanup_pending(20, record)
    assert pending is not None
    assert guard._cache["procs"] is None
    epoch = guard._guard_epoch
    assert guard._mark_owned_cleanup_pending(20, pending) is pending
    assert not guard._remove_owned_if_matches(20, record)
    guard.finish_unverified_cleanup("EXCEL.EXE", -1)
    guard.finish_shutdown_cleanup("EXCEL.EXE")
    assert guard._guard_epoch == epoch
    assert guard._remove_owned_if_matches(20, pending)
    assert guard._guard_epoch > epoch


def test_older_parallel_enumeration_cannot_overwrite_newer_cache(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    now, result, errors = [10.0], [], []

    def enumerate_live():
        if threading.current_thread() is worker:
            entered.set()
            assert release.wait(3)
            return [("EXCEL.EXE", 20)]
        return [("EXCEL.EXE", 21)]

    def read_snapshot():
        try:
            result.append(guard._cached_process_snapshot())
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(guard.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(guard, "_enumerate_processes", enumerate_live)
    worker = threading.Thread(target=read_snapshot, daemon=True)
    worker.start()
    try:
        assert entered.wait(3)
        now[0] = 10.5
        assert guard._cached_process_snapshot().procs == (("EXCEL.EXE", 21),)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors
    assert result[0].procs == (("EXCEL.EXE", 20),)
    assert guard._cache["procs"] == (("EXCEL.EXE", 21),)


def test_failed_enumeration_is_uncached_and_never_unlocks_an_active_gate(monkeypatch):
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: None)
    decision = guard.inspect_office_for_ext(".xls")
    assert decision.status == "available"
    assert decision.reason == "enumeration_unavailable"
    assert guard._cache["procs"] is None
    guard.begin_unverified_cleanup("EXCEL.EXE")
    assert guard.inspect_office_for_ext(".xls").status == "deferred"


@pytest.mark.parametrize("gate", ["pending", "inflight", "token", "closing"])
def test_all_gates_remain_deferred_when_enumeration_is_unavailable(monkeypatch, gate):
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: None)
    if gate == "pending":
        record = _record()
        guard._owned_pids[20] = record
        guard._mark_owned_cleanup_pending(20, record)
    elif gate == "inflight":
        guard.begin_shutdown_cleanup("EXCEL.EXE")
    elif gate == "token":
        guard.begin_unverified_cleanup("EXCEL.EXE")
    else:
        guard.begin_application_shutdown()
    assert guard.inspect_office_for_ext(".xls").status == "deferred"


def test_powerpoint_preflight_retries_epoch_change_and_protects_new_external_process(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []

    def enumerate_live():
        calls.append(1)
        if len(calls) == 1:
            guard.begin_shutdown_cleanup("POWERPNT.EXE")
            guard.finish_shutdown_cleanup("POWERPNT.EXE")
            return []
        return [("POWERPNT.EXE", 30)]

    monkeypatch.setattr(guard, "_enumerate_processes", enumerate_live)
    with pytest.raises(guard.OfficeBusyError):
        guard.prepare_powerpoint_dispatch()
    assert len(calls) == 2


def test_listed_owned_pid_with_unknown_identity_still_protects_user_office(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    guard._owned_pids[20] = _record()
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 20)])
    monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: None)
    decision = guard.inspect_office_for_ext(".xls")
    assert decision.status == "busy"
    assert decision.external_pids == (20,)
    assert decision.identity_unknown_pids == (20,)


class _App:
    """Fake COM app rejects implicit COM calls and exposes one real HWND."""

    Hwnd = 111

    def __init__(self, final_callback=None):
        object.__setattr__(self, "_final_callback", final_callback)
        object.__setattr__(self, "Options", _Options(self))

    def __bool__(self):
        raise AssertionError("COM truthiness must not be evaluated")

    def __eq__(self, other):
        raise AssertionError("COM equality must not be evaluated")

    def __repr__(self):
        raise AssertionError("COM repr must not be evaluated")

    def __setattr__(self, name, value):
        assert not guard._owned_lock.locked(), "COM configuration under ownership lock"
        object.__setattr__(self, name, value)
        if name == "AskToUpdateLinks":
            self._finish_configuration()

    def _finish_configuration(self):
        callback = self._final_callback
        if callback is not None:
            object.__setattr__(self, "_final_callback", None)
            callback()


class _Options:
    def __init__(self, app):
        object.__setattr__(self, "app", app)

    def __setattr__(self, name, value):
        assert not guard._owned_lock.locked()
        object.__setattr__(self, name, value)
        if name == "ConfirmConversions":
            self.app._finish_configuration()


def _real_getter_environment(monkeypatch, slot, final_callback=None):
    exe = "WINWORD.EXE" if slot == "word" else "EXCEL.EXE"
    live, apps = [], []

    def dispatch(prog_id):
        pid = 20 + len(apps)
        app = _App(final_callback if not apps else None)
        apps.append(app)
        live[:] = [(exe, pid)]
        return app

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: list(live))
    monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: 2020)
    monkeypatch.setattr(guard, "_pid_from_hwnd", lambda hwnd: live[0][1] if live else None)
    monkeypatch.setattr(com_reader, "_ensure_com_initialized", lambda: False)
    monkeypatch.setattr(com_reader, "_require_win32com", lambda: SimpleNamespace(DispatchEx=dispatch, Dispatch=dispatch))
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    getter = com_reader._get_word_app if slot == "word" else com_reader._get_excel_app
    return exe, live, apps, getter


@pytest.mark.parametrize("slot", ["word", "excel"])
def test_actual_getter_rejects_configuration_cleanup_aba(monkeypatch, slot):
    def cleanup_after_final_setter():
        record = guard._owned_pids[20]
        _finish_exit(exe, 20, record)
        live.clear()

    exe, live, apps, getter = _real_getter_environment(monkeypatch, slot, cleanup_after_final_setter)
    with pytest.raises(guard.OfficeCleanupPendingError):
        getter()
    assert getattr(com_reader._tls, slot) is None
    assert getattr(com_reader._tls, slot + "_proof") is None
    assert not guard._owned_pids
    assert not guard.unverified_cleanup_pending()
    assert getter() is apps[1]
    assert len(apps) == 2


@pytest.mark.parametrize("slot", ["word", "excel"])
def test_actual_getter_does_not_reuse_app_after_ensure_finishes_pending_cleanup(monkeypatch, slot):
    exe, live, apps, getter = _real_getter_environment(monkeypatch, slot)
    assert getter() is apps[0]
    pending = guard._mark_owned_cleanup_pending(20, guard._owned_pids[20])
    assert pending is not None

    def cleanup(snapshot, timeout_sec):
        guard.finish_owned_cleanup(snapshot, set())
        live.clear()
        return guard.OfficeCleanupResult(confirmed_exited=frozenset(snapshot))

    monkeypatch.setattr(guard, "cleanup_owned_processes", cleanup)
    with pytest.raises(guard.OfficeCleanupPendingError):
        getter()
    assert len(apps) == 1
    assert getattr(com_reader._tls, slot) is None
    assert getattr(com_reader._tls, slot + "_proof") is None
    assert not guard.unverified_cleanup_pending()
    assert getter() is apps[1]


@pytest.mark.parametrize("slot", ["word", "excel"])
@pytest.mark.parametrize("field", ["generation", "cleanup_generation"])
def test_reusable_consume_checks_generations_ignored_by_dataclass_equality(monkeypatch, slot, field):
    exe = "WINWORD.EXE" if slot == "word" else "EXCEL.EXE"
    monkeypatch.setattr(sys, "platform", "win32")
    record = _record(exe)
    changed = replace(record, **{field: getattr(record, field) + 1})
    assert record == changed
    guard._owned_pids[20] = record
    app = _App()
    com_reader._publish_word_excel_app(slot, com_reader.DispatchedOfficeApp(app, (20, record)))
    with guard._owned_lock:
        guard._owned_pids[20] = changed
        guard._state_changed_unlocked()
    with pytest.raises(guard.OfficeCleanupPendingError):
        com_reader._consume_word_excel_app(slot)
    assert guard._owned_pids[20] is changed
    assert getattr(com_reader._tls, slot) is None
    assert not guard.unverified_cleanup_pending()


@pytest.mark.parametrize("slot", ["word", "excel"])
def test_windows_missing_proof_defers_without_guessing_ownership(monkeypatch, slot):
    monkeypatch.setattr(sys, "platform", "win32")
    setattr(com_reader._tls, slot, _App())
    setattr(com_reader._tls, slot + "_proof", None)
    with pytest.raises(guard.OfficeCleanupPendingError):
        com_reader._consume_word_excel_app(slot)
    assert getattr(com_reader._tls, slot) is None
    assert not guard._owned_pids
    exe = "WINWORD.EXE" if slot == "word" else "EXCEL.EXE"
    assert guard.unverified_cleanup_pending() == frozenset({exe})


@pytest.mark.parametrize("slot", ["word", "excel"])
def test_verified_ref_release_preserves_existing_unverified_gate_and_pending_record(monkeypatch, slot):
    monkeypatch.setattr(sys, "platform", "win32")
    exe = "WINWORD.EXE" if slot == "word" else "EXCEL.EXE"
    record = _record(exe)
    guard._owned_pids[20] = record
    com_reader._publish_word_excel_app(slot, com_reader.DispatchedOfficeApp(_App(), (20, record)))
    token = guard.begin_unverified_cleanup(exe)
    with pytest.raises(guard.OfficeCleanupPendingError):
        com_reader._consume_word_excel_app(slot)
    pending = guard._owned_pids[20]
    epoch = guard.guard_epoch()
    assert pending.cleanup_pending
    assert guard._unverified_cleanup == {exe: {token}}
    com_reader.release_verified_pending_com_refs(exe)
    assert guard._owned_pids[20] is pending
    assert guard.guard_epoch() == epoch
    assert guard._unverified_cleanup == {exe: {token}}


@pytest.mark.parametrize("slot", ["word", "excel"])
def test_partial_pair_drops_orphaned_proof_and_fake_pair_uses_no_com_truthiness(monkeypatch, slot):
    monkeypatch.setattr(sys, "platform", "linux")
    setattr(com_reader._tls, slot, None)
    setattr(com_reader._tls, slot + "_proof", (20, _record()))
    assert com_reader._consume_word_excel_app(slot) is None
    assert getattr(com_reader._tls, slot + "_proof") is None
    app = _App()
    com_reader._publish_word_excel_app(slot, com_reader.DispatchedOfficeApp(app, None))
    assert com_reader._consume_word_excel_app(slot) is app


@pytest.mark.parametrize("operation", ["clear", "replace"])
def test_pair_release_occurs_after_ownership_lock_is_released(monkeypatch, operation):
    monkeypatch.setattr(sys, "platform", "linux")
    lock_states = []

    class ReleaseSpy:
        def __del__(self):
            lock_states.append(guard._owned_lock.locked())

    com_reader._tls.word = ReleaseSpy()
    com_reader._tls.word_proof = None
    if operation == "clear":
        com_reader._clear_reusable_word_excel_app("word")
    else:
        com_reader._publish_word_excel_app("word", com_reader.DispatchedOfficeApp(_App(), None))
    assert lock_states == [False]


class _ComError(Exception):
    def __init__(self, code, secret):
        self.hresult = code
        super().__init__(secret)


def _parse_failing_read(monkeypatch, slot, primary, close_error):
    close_calls = []

    class Document:
        @property
        def Content(self):
            raise primary

        def Close(self, save):
            close_calls.append(save)
            raise close_error

    class Sheet:
        Name = "DO_NOT_LOG_SHEET"
        UsedRange = SimpleNamespace(Row=1, Column=1, Rows=SimpleNamespace(Count=1), Columns=SimpleNamespace(Count=1))

        def Cells(self, row, col):
            return (row, col)

        def Range(self, first, last):
            raise primary

    document = Document()
    document.Sheets = [Sheet()]
    collection = SimpleNamespace(Open=lambda *args: document)
    app = SimpleNamespace(Documents=collection, Workbooks=collection)
    setattr(com_reader._tls, slot, app)
    setattr(com_reader._tls, slot + "_proof", (20, _record()))
    monkeypatch.setattr(com_reader, "_get_" + slot + "_app", lambda: app)
    reader = com_reader.WordComReader() if slot == "word" else com_reader.ExcelComReader()
    return reader, close_calls


@pytest.mark.parametrize("slot,read_stage", [("word", "read"), ("excel", "cell_read")])
@pytest.mark.parametrize("primary_code", [0x80004005, 0x80010001])
def test_actual_parse_keeps_primary_and_close_poison_separate(monkeypatch, caplog, slot, read_stage, primary_code):
    from knowmate.collector import failure_state, scheduler

    primary = _ComError(primary_code, "DO_NOT_LOG_DOCUMENT")
    close_error = _ComError(0x800706BA, "DO_NOT_LOG_CAPTION")
    reader, close_calls = _parse_failing_read(monkeypatch, slot, primary, close_error)
    with caplog.at_level(logging.DEBUG), pytest.raises(com_reader.OfficeComPoisonError) as raised:
        reader.parse("sample.doc" if slot == "word" else "sample.xls")
    poison = raised.value
    assert poison.hresult == 0x800706BA
    assert poison.__cause__ is close_error
    evidence = poison.evidence
    assert evidence.primary.original is primary
    assert evidence.primary.stage == read_stage
    assert evidence.primary.outer_hresult == primary_code
    assert evidence.poison_fault.original is close_error
    assert evidence.poison_fault.stage == "close"
    assert len(evidence.cleanup) == 1
    assert evidence.cleanup[0].original is close_error
    assert close_calls == [False]
    assert getattr(com_reader._tls, slot) is None
    assert getattr(com_reader._tls, slot + "_proof") is None
    assert com_stage.take_last_failed_stage() == read_stage
    assert com_stage.snapshot()[0] is None
    expected_kind = (failure_state.KIND_TEMPORARY_BUSY if primary_code == 0x80010001
                     else failure_state.KIND_READ_ERROR)
    assert scheduler._classify_poison_evidence(poison, stage_slot="close", watchdog_stage=None) == (
        expected_kind, f"0x{primary_code:08X}", read_stage)
    assert scheduler._classify_poison_evidence(poison, stage_slot="close", watchdog_stage="open") == (
        failure_state.KIND_OPEN_TIMEOUT, f"0x{primary_code:08X}", read_stage)
    scheduler._log_com_poison(poison, attempt_id=123)
    assert "DO_NOT_LOG" not in caplog.text


@pytest.mark.parametrize("slot,read_stage", [("word", "read"), ("excel", "cell_read")])
def test_actual_parse_recognizes_structured_inner_poison_and_skips_close(monkeypatch, slot, read_stage):
    from knowmate.collector import failure_state, scheduler

    primary = _ComError(0x80020009, "DO_NOT_LOG_DOCUMENT")
    primary.excepinfo = (0, "DO_NOT_LOG_SOURCE", "DO_NOT_LOG_CAPTION", None, 0, -2147023174)
    reader, close_calls = _parse_failing_read(monkeypatch, slot, primary, RuntimeError("close must be skipped"))
    with pytest.raises(com_reader.OfficeComPoisonError) as raised:
        reader.parse("sample.doc" if slot == "word" else "sample.xls")
    poison = raised.value
    assert poison.hresult == 0x800706BA
    assert poison.__cause__ is primary
    assert poison.evidence.primary.original is primary
    assert poison.evidence.primary.stage == read_stage
    assert poison.evidence.primary.outer_hresult == 0x80020009
    assert poison.evidence.primary.inner_hresults == (0x800706BA,)
    assert not close_calls
    assert getattr(com_reader._tls, slot + "_proof") is None
    assert scheduler._classify_poison_evidence(poison, stage_slot="close", watchdog_stage=None) == (
        failure_state.KIND_READ_ERROR, "0x80020009", read_stage)


@pytest.mark.parametrize("fatal_property", ["Hwnd", "HWND"])
def test_hwnd_attribute_error_with_fatal_cause_stops_probe_rpc_and_preserves_token(monkeypatch, fatal_property):
    monkeypatch.setattr(sys, "platform", "win32")
    fatal = _ComError(0x800706BA, "DO_NOT_LOG_CAPTION")
    attribute_error = AttributeError("DO_NOT_LOG_HWND")
    accesses, dispatch_calls = [], []

    class ProbeApp:
        def __getattribute__(self, name):
            if name in {"Hwnd", "HWND"}:
                accesses.append(name)
                if name == fatal_property:
                    raise attribute_error from fatal
                raise AttributeError(name)
            if name in {"Documents", "Close", "Quit", "AutomationSecurity"}:
                accesses.append(name)
                raise AssertionError("no probe RPC after fatal HWND")
            return object.__getattribute__(self, name)

    app = ProbeApp()

    def dispatch(prog_id):
        dispatch_calls.append(prog_id)
        return app

    monkeypatch.setattr(guard, "_enumerate_processes", lambda: [])
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    client = SimpleNamespace(DispatchEx=dispatch, Dispatch=dispatch)
    with pytest.raises(com_reader.OfficeComPoisonError) as raised:
        com_reader._dispatch_owned_app(client, "Word.Application", "WINWORD.EXE")
    poison = raised.value
    assert poison.hresult == 0x800706BA
    assert poison.__cause__ is attribute_error
    assert attribute_error.__cause__ is fatal
    assert poison.evidence.primary.original is attribute_error
    assert poison.evidence.primary.stage == "dispatch"
    assert poison.evidence.primary.phase == "window_verify"
    assert accesses == (["Hwnd"] if fatal_property == "Hwnd" else ["Hwnd", "HWND"])
    assert dispatch_calls == ["Word.Application"]
    assert guard.unverified_cleanup_pending() == frozenset({"WINWORD.EXE"})
    assert com_reader._tls.ownership_cleanup["WINWORD.EXE"].app is app
    assert not guard._owned_pids


@pytest.mark.parametrize("miss_value", [0, "unsupported"])
def test_nonfatal_hwnd_misses_still_check_alternate_property(monkeypatch, miss_value):
    accesses = []

    class ProbeApp:
        def __getattribute__(self, name):
            accesses.append(name)
            if name == "Hwnd":
                return miss_value
            raise AttributeError(name)

    assert guard._app_hwnd(ProbeApp(), "WINWORD.EXE") is None
    assert accesses == ["Hwnd", "HWND"]
