"""Verified OS cleanup releases a real collector QThread before forced stop."""
import sys
import threading
from types import SimpleNamespace

import pytest

from knowmate.app import lifecycle
from knowmate.collector.scheduler import CollectorWorker
from knowmate.secure import office_exit, office_guard


def test_owned_excel_exit_releases_collector_and_runs_com_finally(tmp_path, monkeypatch, native_com_backend):
    office_guard.clear_owned_pids()
    entered, release = threading.Event(), threading.Event()
    events = []
    controller_thread = threading.get_ident()
    from knowmate.secure.com_scope import acquire_current_scope
    native_com_backend.initialize = lambda: (events.append(("com_init", threading.get_ident())), 0)[1]
    native_com_backend.uninitialize = lambda: events.append(("com_uninit", threading.get_ident()))
    monkeypatch.setattr(lifecycle, "_WAIT_GRACEFUL_MS", 10)

    def com_cleanup(**_kwargs):
        events.append(("com_finally", threading.get_ident()))
        return office_guard.OfficeCleanupResult()

    worker = CollectorWorker(
        {"collector": {}, "mail": {"enabled": False}},
        SimpleNamespace(maintenance_in_progress=False), None,
        state_file=tmp_path / "state.json", com_cleanup_fn=com_cleanup,
    )

    def blocked_cycle():
        acquire_current_scope()
        worker._work_phase = "com_cell_read"
        entered.set()
        assert release.wait(3), "fake Office read was not released"
        events.append(("read_released", threading.get_ident()))

    monkeypatch.setattr(worker, "_run_cycle", blocked_cycle)
    monkeypatch.setattr(worker, "terminate", lambda: (_ for _ in ()).throw(
        AssertionError("Office cleanup should release this worker without terminate")))
    monkeypatch.setattr(office_guard.sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 111)])
    from knowmate.secure import office_resiliency
    monkeypatch.setattr(office_resiliency, "clear_resiliency_markers", lambda _exe: None)
    with office_guard._owned_lock:
        office_guard._ownership_generation += 1
        office_guard._owned_pids[111] = office_guard.OwnedOfficeProcess(
            "EXCEL.EXE", 777, generation=office_guard._ownership_generation,
        )

    def terminate_office(pid, record, _timeout):
        assert pid == 111 and record.creation_identity == 777
        events.append(("office_exit", threading.get_ident()))
        release.set()
        return "terminated"

    monkeypatch.setattr(office_guard, "_terminate_and_confirm", terminate_office)
    worker.start()
    try:
        assert entered.wait(2)
        office_guard.begin_application_shutdown()
        unsafe = lifecycle.stop_worker(
            worker, hard_exit=lambda _code: (_ for _ in ()).throw(AssertionError("hard exit")),
            office_cleanup=lambda item: office_exit.cleanup_before_worker_termination(item, budget_sec=1),
        )
        assert unsafe is False
        assert not worker.isRunning()
        names = [name for name, _thread in events]
        assert names.index("office_exit") < names.index("read_released") < names.index("com_finally")
        assert names.index("com_finally") < names.index("com_uninit")
        worker_thread = dict(events)["com_init"]
        assert worker_thread != controller_thread
        assert dict(events)["com_finally"] == dict(events)["com_uninit"] == worker_thread
        assert dict(events)["office_exit"] not in {controller_thread, worker_thread}
        assert not office_guard.take_owned_processes()
    finally:
        release.set()
        worker.wait(3000)
        office_guard.clear_owned_pids()


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_main_window_exit_wires_idle_cleanup_and_preserves_unsafe_failure(monkeypatch, cleanup_error):
    from knowmate.app import main

    office_guard.clear_owned_pids()
    calls = []
    window = SimpleNamespace(
        _shutdown_done=False, _bridge=SimpleNamespace(_worker=None),
        _tray=None, _idle_scheduler=None,
    )

    def stop(worker, *, office_cleanup):
        assert worker is None
        assert office_guard._application_closing.is_set()
        assert office_cleanup is office_exit.cleanup_before_worker_termination
        calls.append("cleanup")
        if cleanup_error:
            raise RuntimeError("cleanup completion unknown")
        return False

    monkeypatch.setattr(lifecycle, "stop_worker", stop)
    monkeypatch.setattr(lifecycle, "finalize_shutdown", lambda worker, quit_fn, force_hard_exit:
                        calls.append(("finalize", force_hard_exit)))
    monkeypatch.setattr(main, "QApplication", SimpleNamespace(instance=lambda:
                        SimpleNamespace(quit=lambda: None)))
    monkeypatch.setattr(main.logger, "warning", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("unsafe cleanup must not log synchronously")))
    try:
        main.MainWindow._shutdown(window)
        main.MainWindow._shutdown(window)
        assert calls == ["cleanup", ("finalize", cleanup_error)]
    finally:
        office_guard.clear_owned_pids()
