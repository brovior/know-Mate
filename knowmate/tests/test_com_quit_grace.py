"""COM shutdown ownership and timeout race regression tests."""
import sys
import threading

import pytest

from knowmate.secure import com_reader, office_guard


class _FakeApp:
    def __init__(self, on_quit=None, raises=False):
        self.quit_called = False
        self.on_quit = on_quit
        self.raises = raises

    def Quit(self):
        self.quit_called = True
        if self.on_quit:
            self.on_quit()
        if self.raises:
            raise RuntimeError("fake Quit failure")


def _register(pid=111, exe="EXCEL.EXE"):
    with office_guard._owned_lock:
        office_guard._ownership_generation += 1
        office_guard._owned_pids[pid] = office_guard.OwnedOfficeProcess(
            exe, pid * 10, True, office_guard._ownership_generation,
        )


@pytest.fixture(autouse=True)
def _clean_state():
    office_guard.clear_owned_pids()
    for name in ("word", "excel", "ppt"):
        setattr(com_reader._tls, name, None)
    yield
    office_guard.clear_owned_pids()
    for name in ("word", "excel", "ppt"):
        setattr(com_reader._tls, name, None)


def test_normal_quit_uses_registry_and_removes_only_after_exit_confirmation(monkeypatch):
    _register()
    app = _FakeApp()
    monkeypatch.setattr(com_reader._tls, "excel", app, raising=False)
    calls = []

    def wait(snapshot, timeout):
        calls.append((dict(snapshot), timeout))
        return set(), 0.25

    result = com_reader.quit_com_apps(grace_sec=5, wait_fn=wait, quit_timeout_sec=5)

    assert app.quit_called
    assert calls[0][0][111].cleanup_pending
    assert office_guard._owned_pids == {}
    assert result.confirmed_exited == frozenset({111})


def test_registered_office_is_retried_even_when_tls_app_reference_is_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    _register()
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 111)])
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda *_args: "terminated")

    result = com_reader.quit_com_apps(grace_sec=0, quit_timeout_sec=1)

    assert result.forced_exited == frozenset({111})
    assert 111 not in office_guard._owned_pids


def test_partial_grace_exit_is_removed_while_remaining_process_is_retried(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    _register(111, "WINWORD.EXE")
    _register(222, "EXCEL.EXE")
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("WINWORD.EXE", 111), ("EXCEL.EXE", 222)])
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda pid, *_args: "terminated")

    result = com_reader.quit_com_apps(
        grace_sec=1, quit_timeout_sec=1,
        wait_fn=lambda _owned, _timeout: ({222}, 1.0),
    )

    assert result.confirmed_exited == frozenset({111, 222})
    assert 111 not in office_guard._owned_pids
    assert 222 not in office_guard._owned_pids


def test_quit_exception_still_releases_tls_and_preserves_owner_until_confirmed(monkeypatch):
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: None)
    _register()
    app = _FakeApp(raises=True)
    monkeypatch.setattr(com_reader._tls, "excel", app, raising=False)
    com_reader.quit_com_apps(grace_sec=0, quit_timeout_sec=5)
    assert getattr(com_reader._tls, "excel") is None
    assert 111 in office_guard._owned_pids
    assert office_guard._owned_pids[111].cleanup_pending


def test_blocked_quit_is_released_by_timeout_callback(monkeypatch):
    _register()
    released = threading.Event()
    quit_started = threading.Event()
    app = _FakeApp(on_quit=lambda: (quit_started.set(), released.wait(2)))

    def cleanup(snapshot, timeout_sec=2):
        released.set()
        return office_guard.OfficeCleanupResult(remaining=frozenset(snapshot))

    monkeypatch.setattr(office_guard, "cleanup_owned_processes", cleanup)
    def run_quit():
        com_reader._tls.excel = app
        com_reader.quit_com_apps(grace_sec=0, quit_timeout_sec=0.02)

    thread = threading.Thread(target=run_quit, daemon=True)
    thread.start()
    assert quit_started.wait(1)
    thread.join(1)
    assert not thread.is_alive()
    assert released.is_set()
    assert 111 in office_guard._owned_pids


def test_timeout_callback_exception_is_reported_and_owner_remains_pending(monkeypatch):
    _register()
    app = _FakeApp(on_quit=lambda: threading.Event().wait(0.06))
    monkeypatch.setattr(com_reader._tls, "excel", app, raising=False)
    monkeypatch.setattr(
        office_guard, "cleanup_owned_processes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fake kill failure")),
    )
    com_reader.quit_com_apps(grace_sec=0, quit_timeout_sec=0.01)
    assert 111 in office_guard._owned_pids
    assert office_guard._owned_pids[111].cleanup_pending
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        office_guard.ensure_office_available("EXCEL.EXE")


def test_late_cancelled_timer_generation_cannot_cleanup(monkeypatch):
    _register()
    snapshot = office_guard.begin_owned_cleanup("EXCEL.EXE")
    calls = []
    monkeypatch.setattr(office_guard, "cleanup_owned_processes", lambda *args: calls.append(args))
    watchdog = com_reader._OfficeQuitWatchdog(office_guard, "EXCEL.EXE", snapshot, 1)
    watchdog.arm()
    generation = watchdog._generation
    watchdog.disarm()
    watchdog._fire(generation)
    assert calls == []
    assert 111 in office_guard._owned_pids


@pytest.mark.parametrize(
    ("outcome", "kept", "forced"),
    [("remaining", True, False), ("identity_unknown", True, False), ("terminated", False, True),
     ("identity_changed", False, False)],
)
def test_cleanup_retains_unconfirmed_owner_and_removes_only_confirmed(monkeypatch, outcome, kept, forced):
    monkeypatch.setattr(sys, "platform", "win32")
    _register()
    snapshot = office_guard.begin_owned_cleanup("EXCEL.EXE")
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 111)])
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda *_args: outcome)
    result = office_guard.cleanup_owned_processes(snapshot)
    assert (111 in office_guard._owned_pids) is kept
    assert (111 in result.forced_exited) is forced
    if kept:
        assert result.remaining | result.identity_unknown
        retry_outcomes = iter(["terminated"])
        monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda *_args: next(retry_outcomes))
        retry = office_guard.cleanup_owned_processes(snapshot)
        assert retry.confirmed_exited == frozenset({111})
        assert 111 not in office_guard._owned_pids


def test_pid_reuse_with_same_exe_never_kills_new_process(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    _register()
    snapshot = office_guard.begin_owned_cleanup("EXCEL.EXE")
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 111)])
    outcomes = []
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda *_args: outcomes.append("called") or "identity_changed")
    result = office_guard.cleanup_owned_processes(snapshot)
    assert outcomes == ["called"]
    assert result.confirmed_exited == frozenset({111})
    assert result.forced_exited == frozenset()


def test_powerpoint_is_never_quit_or_killed(monkeypatch):
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: None)
    app = _FakeApp()
    monkeypatch.setattr(com_reader._tls, "ppt", app, raising=False)
    with office_guard._owned_lock:
        office_guard._owned_pids[333] = office_guard.OwnedOfficeProcess("POWERPNT.EXE", 3330, False)
    com_reader.quit_com_apps()
    assert not app.quit_called
    assert 333 in office_guard._owned_pids


class _ManualTimer:
    """Let each test decide exactly when the timeout callback runs."""

    callbacks = []

    def __init__(self, _interval, callback):
        self.callbacks.append(callback)

    def start(self):
        pass

    def cancel(self):
        pass


def test_completed_timeout_cleanup_is_not_retried_or_reported_as_failed(monkeypatch):
    """A successful timeout kill must stay successful even with zero grace."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(threading, "Timer", _ManualTimer)
    _register()
    kills = []
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 111)])
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda *args: kills.append(args) or "terminated")
    com_reader._tls.excel = _FakeApp(on_quit=lambda: _ManualTimer.callbacks[-1]())

    result = com_reader.quit_com_apps(grace_sec=0, quit_timeout_sec=1)

    assert len(kills) == 1
    assert result.successful
    assert result.confirmed_exited == result.forced_exited == frozenset({111})


def test_inflight_timeout_remains_gated_after_owner_removed_without_duplicate_cleanup(monkeypatch):
    """Disarming an active callback must neither join it nor launch a second kill."""
    _register()
    monkeypatch.setattr(threading, "Timer", _ManualTimer)
    started = threading.Event()
    release = threading.Event()
    calls = []
    callback_threads = []

    def cleanup(snapshot, timeout_sec=2):
        calls.append(snapshot)
        office_guard.finish_owned_cleanup(snapshot, set())
        started.set()
        assert release.wait(2)
        return office_guard.OfficeCleanupResult(confirmed_exited=frozenset(snapshot))

    def quit_while_cleanup_runs():
        thread = threading.Thread(target=_ManualTimer.callbacks[-1], daemon=True)
        callback_threads.append(thread)
        thread.start()
        assert started.wait(1)

    monkeypatch.setattr(office_guard, "cleanup_owned_processes", cleanup)
    com_reader._tls.excel = _FakeApp(on_quit=quit_while_cleanup_runs)
    try:
        result = com_reader.quit_com_apps(grace_sec=0, quit_timeout_sec=1)
        assert result.remaining == frozenset({111})
        assert not result.successful
        assert office_guard._owned_pids == {}
        with pytest.raises(office_guard.OfficeCleanupPendingError):
            office_guard.ensure_office_available("EXCEL.EXE")
        assert len(calls) == 1
    finally:
        release.set()
        for thread in callback_threads:
            thread.join(1)
            assert not thread.is_alive()
    office_guard.ensure_office_available("EXCEL.EXE")


def test_stale_cleanup_snapshot_cannot_kill_or_remove_new_registration(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    _register()
    snapshot = office_guard.begin_owned_cleanup("EXCEL.EXE")
    _register()  # Same PID/identity, but a new registry generation.
    calls = []
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 111)])
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda *args: calls.append(args) or "terminated")

    result = office_guard.cleanup_owned_processes(snapshot)

    assert calls == []
    assert not office_guard._owned_pids[111].cleanup_pending
    assert result.failed == frozenset({111})


def test_pending_retry_is_throttled_per_exe_and_does_not_gate_other_apps(monkeypatch):
    _register()
    snapshot = office_guard.begin_owned_cleanup("EXCEL.EXE")
    calls = []
    monkeypatch.setattr(office_guard, "cleanup_owned_processes", lambda *args, **kwargs: (
        calls.append(args) or office_guard.OfficeCleanupResult(remaining=frozenset(snapshot))
    ))
    for _ in range(20):
        with pytest.raises(office_guard.OfficeCleanupPendingError):
            office_guard.ensure_office_available("EXCEL.EXE")
    office_guard.ensure_office_available("WINWORD.EXE")
    assert len(calls) == 1


def test_pending_guard_precedes_user_busy_detection(monkeypatch):
    """Unqueryable ownership must still reach cleanup retry instead of user-busy skip."""
    from knowmate.secure import AutoReader
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: None)
    _register()
    office_guard.begin_owned_cleanup("EXCEL.EXE")
    calls = []
    monkeypatch.setattr(office_guard, "is_office_busy_for_ext", lambda ext: calls.append(ext) or True)
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        AutoReader._guard_office_busy(".xls", "sample.xls")
    assert calls == []


@pytest.mark.parametrize("identity", [3330, 4440, None])
def test_powerpoint_record_is_pruned_only_after_confirmed_exit_or_reuse(monkeypatch, identity):
    monkeypatch.setattr(sys, "platform", "win32")
    with office_guard._owned_lock:
        office_guard._owned_pids[333] = office_guard.OwnedOfficeProcess("POWERPNT.EXE", 3330, False)
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 333)])
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: identity)
    office_guard.prune_released_nonterminable_processes()
    assert (333 in office_guard._owned_pids) is (identity != 4440)
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [])
    office_guard.prune_released_nonterminable_processes()
    assert 333 not in office_guard._owned_pids
