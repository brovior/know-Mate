"""App exit cleans verified Office before terminating the COM worker."""
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from knowmate.app.lifecycle import finalize_shutdown, stop_worker
from knowmate.secure import com_reader, office_exit, office_guard as guard
from knowmate.tests.test_office_shutdown_native import install_kernel


@pytest.fixture(autouse=True)
def clean_state():
    guard.clear_owned_pids()
    yield
    guard.clear_owned_pids()


def register(pid=111, exe="EXCEL.EXE", identity=777):
    with guard._owned_lock:
        guard._ownership_generation += 1
        guard._owned_pids[pid] = guard.OwnedOfficeProcess(
            exe, identity, exe != "POWERPNT.EXE", guard._ownership_generation,
        )


class Worker:
    work_stage = "com_cell_read"

    def __init__(self, running=True):
        self.running = running
        self.events = []

    def isRunning(self):
        return self.running

    def cancel(self):
        self.events.append("cancel")

    def wait(self, ms):
        self.events.append(("wait", ms))
        return not self.running

    def terminate(self):
        self.events.append("terminate")
        self.running = False


def cleanup(worker, budget=1.0):
    return office_exit.cleanup_before_worker_termination(worker, budget_sec=budget)


def native_environment(monkeypatch, processes):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: processes)
    from knowmate.secure import office_resiliency
    monkeypatch.setattr(office_resiliency, "clear_resiliency_markers", lambda _exe: None)


def test_stuck_read_terminates_owned_excel_then_worker_finishes_without_qthread_terminate(monkeypatch):
    register()
    worker = Worker()
    native_environment(monkeypatch, [("EXCEL.EXE", 111)])

    def terminate(pid, record, timeout):
        assert pid == 111 and record.creation_identity == 777
        worker.events.append("office_exit")
        worker.events.append("worker_finally")
        worker.running = False
        return "terminated"

    monkeypatch.setattr(guard, "_terminate_and_confirm", terminate)
    assert stop_worker(worker, office_cleanup=cleanup) is False
    quit_calls = []
    finalize_shutdown(worker, lambda: quit_calls.append(True))
    assert quit_calls == [True]
    assert worker.events == ["cancel", ("wait", 8000), "office_exit", "worker_finally"]
    assert not guard.take_owned_processes()


def test_unresponsive_worker_cleanup_precedes_terminate_and_no_logging_follows(monkeypatch):
    worker = Worker()
    events = worker.events

    def prepare(_worker):
        events.append("office_cleanup")
        return True

    def terminate():
        events.append("terminate")
        worker.running = False
        def fail(*_args, **_kwargs):
            pytest.fail("logging after terminate")
        from knowmate.app import lifecycle
        monkeypatch.setattr(lifecycle.logger, "error", fail)
        monkeypatch.setattr(lifecycle.logger, "warning", fail)

    worker.terminate = terminate
    unsafe = stop_worker(worker, office_cleanup=prepare)
    hard = []
    finalize_shutdown(worker, lambda: pytest.fail("normal quit"), hard_exit=hard.append, force_hard_exit=unsafe)
    assert hard == [0]
    assert events.index("office_cleanup") < events.index("terminate")


@pytest.mark.parametrize("kind", ["idle", "none", "graceful"])
def test_cleanup_runs_when_worker_already_stopped_or_absent(monkeypatch, kind):
    register()
    worker = None if kind == "none" else Worker(running=kind == "graceful")
    if kind == "graceful":
        def wait(_ms):
            worker.running = False
            return True
        worker.wait = wait
    native_environment(monkeypatch, [("EXCEL.EXE", 111)])
    calls = []
    monkeypatch.setattr(guard, "_terminate_and_confirm", lambda pid, *_args: calls.append(pid) or "terminated")
    assert stop_worker(worker, office_cleanup=cleanup) is False
    assert calls == [111]


@pytest.mark.parametrize("identity", [None, 778])
def test_unknown_or_reused_identity_never_kills_replacement_or_user_or_ppt(monkeypatch, identity):
    register(identity=identity)
    register(222, "POWERPNT.EXE")
    native_environment(monkeypatch, [("EXCEL.EXE", 111), ("EXCEL.EXE", 999), ("POWERPNT.EXE", 222)])
    calls = install_kernel(monkeypatch, identity=777)
    assert cleanup(None) is False
    assert not any(call[0] == "terminate" for call in calls)
    assert 222 in guard.take_owned_processes()


def test_cleanup_generations_are_stable_across_watchdog_and_app_exit(monkeypatch):
    register()
    earlier = guard.begin_owned_cleanup()
    later = guard.begin_owned_cleanup()
    assert earlier[111].cleanup_generation == later[111].cleanup_generation
    native_environment(monkeypatch, [("EXCEL.EXE", 111)])
    monkeypatch.setattr(guard, "_terminate_and_confirm", lambda *_args: "terminated")
    assert guard.cleanup_owned_processes(earlier).forced_exited == frozenset({111})


def test_multiple_pid_waits_share_one_deadline(monkeypatch):
    for pid in range(1, 5):
        register(pid)
    native_environment(monkeypatch, [("EXCEL.EXE", pid) for pid in range(1, 5)])
    calls = []
    def terminate(pid, record, timeout):
        calls.append((pid, timeout))
        time.sleep(timeout)
        return "remaining"
    monkeypatch.setattr(guard, "_terminate_and_confirm", terminate)
    started = time.monotonic()
    result = guard.cleanup_owned_processes(guard.begin_owned_cleanup(), deadline=started + 0.03)
    assert time.monotonic() - started < 0.15
    assert len(calls) == 1
    assert result.remaining == frozenset(range(1, 5))


@pytest.mark.parametrize("blocked", ["registry", "logger", "resiliency"])
def test_blocked_cleanup_does_not_block_controller_and_never_allows_normal_quit(monkeypatch, blocked):
    release = threading.Event()
    entered = threading.Event()
    helpers = []
    def block(*_args, **_kwargs):
        helpers.append(threading.current_thread())
        entered.set()
        release.wait(2)
    if blocked == "registry":
        monkeypatch.setattr(guard, "begin_owned_cleanup", lambda: block() or {})
    elif blocked == "logger":
        monkeypatch.setattr(office_exit.logger, "info", block)
    else:
        register()
        native_environment(monkeypatch, [("EXCEL.EXE", 111)])
        monkeypatch.setattr(guard, "_terminate_and_confirm", lambda *_args: "terminated")
        from knowmate.secure import office_resiliency
        monkeypatch.setattr(office_resiliency, "clear_resiliency_markers", block)
    try:
        started = time.monotonic()
        unsafe = cleanup(None, 0.03)
        assert entered.is_set() and unsafe
        assert time.monotonic() - started < 1.0
        hard = []
        finalize_shutdown(None, lambda: pytest.fail("normal quit"), hard_exit=hard.append, force_hard_exit=unsafe)
        assert hard == [0]
    finally:
        release.set()
        for helper in helpers:
            helper.join(1)


def test_finished_worker_with_existing_callback_inflight_requires_hard_exit():
    guard.begin_shutdown_cleanup("EXCEL.EXE")
    try:
        assert cleanup(Worker(False), 0.02) is True
    finally:
        guard.finish_shutdown_cleanup("EXCEL.EXE")


def test_zero_targets_log_is_explicit(caplog):
    caplog.set_level("INFO")
    assert cleanup(None) is False
    assert "phase=app_exit targets=[] count=0" in caplog.text


def test_closing_blocks_dispatch_but_late_verified_registration_remains_pending(monkeypatch):
    guard.begin_application_shutdown()
    with pytest.raises(guard.OfficeCleanupPendingError, match="application_closing"):
        guard.ensure_office_available("EXCEL.EXE")
    calls = []
    fake = SimpleNamespace(Dispatch=lambda _name: calls.append("dispatch"), DispatchEx=lambda _name: calls.append("dispatch"))
    with pytest.raises(guard.OfficeCleanupPendingError):
        com_reader._dispatch_and_own(fake, "Excel.Application", "EXCEL.EXE")
    assert calls == []
    native_environment(monkeypatch, [("EXCEL.EXE", 111)])
    monkeypatch.setattr(guard, "_pid_from_hwnd", lambda _hwnd: 111)
    monkeypatch.setattr(guard, "_process_creation_identity", lambda _pid: 777)
    registered = guard._register_owned_window("EXCEL.EXE", set(), lambda: 123)
    assert registered is not None
    assert registered[1].cleanup_pending
    assert 111 in guard.begin_owned_cleanup()
    # PowerPoint policy is deliberately unaffected by the application-closing flag.
    guard.ensure_application_open("POWERPNT.EXE")


def test_late_verified_registration_is_swept_after_initial_empty_snapshot(monkeypatch):
    worker = Worker()
    original = guard.begin_owned_cleanup
    snapshots = []
    def snapshot():
        result = original()
        snapshots.append(result)
        if len(snapshots) == 1:
            register()
        return result
    monkeypatch.setattr(guard, "begin_owned_cleanup", snapshot)
    native_environment(monkeypatch, [("EXCEL.EXE", 111)])
    def terminate(*_args):
        worker.running = False
        return "terminated"
    monkeypatch.setattr(guard, "_terminate_and_confirm", terminate)
    assert cleanup(worker) is False
    assert snapshots[0] == {}
    assert not guard.take_owned_processes()


@pytest.mark.parametrize("failure", ["status", "cancel", "wait"])
def test_qthread_errors_still_run_bounded_cleanup_and_force_hard_exit(failure):
    worker = Worker()
    def fail(*_args):
        raise RuntimeError("synthetic Qt failure")
    setattr(worker, {"status": "isRunning", "cancel": "cancel", "wait": "wait"}[failure], fail)
    calls = []
    assert stop_worker(worker, office_cleanup=lambda _worker: calls.append("cleanup") or False) is True
    assert calls == ["cleanup"]


def test_status_error_after_live_helper_report_cannot_be_lost():
    worker = Worker()
    count = []
    def status():
        count.append(True)
        if len(count) == 2:
            raise RuntimeError("status unavailable")
        return len(count) == 1
    worker.isRunning = status
    hard = []
    assert stop_worker(worker, office_cleanup=lambda _worker: True, hard_exit=hard.append) is True
    assert hard == [0]


def test_dispatch_started_before_closing_registers_owner_without_reading(monkeypatch):
    native_environment(monkeypatch, [])
    app = SimpleNamespace(Hwnd=123)
    monkeypatch.setattr(guard, "_pid_from_hwnd", lambda _hwnd: 111)
    monkeypatch.setattr(guard, "_process_creation_identity", lambda _pid: 777)
    def dispatch(_prog_id):
        guard.begin_application_shutdown()
        monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 111)])
        return app
    fake = SimpleNamespace(Dispatch=dispatch, DispatchEx=dispatch)
    try:
        with pytest.raises(guard.OfficeCleanupPendingError, match="application_closing"):
            com_reader._dispatch_and_own(fake, "Excel.Application", "EXCEL.EXE")
        assert guard.take_owned_processes()[111].cleanup_pending
        assert com_reader._tls.excel is app
    finally:
        com_reader._tls.excel = None


def test_unresolved_first_pid_is_not_hidden_by_later_success(monkeypatch, caplog):
    register()
    worker = Worker()
    native_environment(monkeypatch, [("EXCEL.EXE", 111), ("WINWORD.EXE", 222)])
    calls = []
    def terminate(pid, *_args):
        calls.append(pid)
        if pid == 111:
            if len(calls) == 1:
                register(222, "WINWORD.EXE")
            return "identity_unknown"
        worker.running = False
        return "terminated"
    monkeypatch.setattr(guard, "_terminate_and_confirm", terminate)
    caplog.set_level("INFO")
    assert cleanup(worker) is False
    assert "identity_unknown=[111]" in caplog.text
    assert "forced_exited=[222]" in caplog.text
