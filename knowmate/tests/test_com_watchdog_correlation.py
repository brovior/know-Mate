"""Real daemon threads use captured file IDs without inheriting worker state."""
import threading

from knowmate.collector.com_watchdog import ComWatchdog
from knowmate.secure import com_stage


class ManualTimer:
    def __init__(self, interval, callback):
        self.callback = callback

    def start(self):
        pass

    def cancel(self):
        pass


def fire_on_thread(callback):
    results = []
    errors = []

    def run():
        try:
            results.append(callback())
            results.append(com_stage.get_attempt_context())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert not errors
    return results


def test_daemon_keeps_armed_ids_after_worker_moves_to_next_file(caplog):
    timers = []
    observed = []

    def factory(interval, callback):
        timer = ManualTimer(interval, callback)
        timers.append(timer)
        return timer

    def terminate(exe):
        observed.append((exe, com_stage.get_attempt_context()))
        return 1

    watchdog = ComWatchdog(terminate, factory, stage_fn=lambda: ("open", "open"))
    com_stage.set_attempt_context(11, 22, "EXCEL.EXE")
    try:
        armed_context = com_stage.get_attempt_context()
        watchdog.arm("EXCEL.EXE", 10)
        com_stage.set_attempt_context(33, 44, "WINWORD.EXE")
        worker_context = com_stage.get_attempt_context()
        with caplog.at_level("WARNING"):
            assert fire_on_thread(timers[0].callback) == [1, None]
        assert observed == [("EXCEL.EXE", armed_context)]
        assert com_stage.get_attempt_context() == worker_context
        assert "cycle_id=11 attempt_id=22" in caplog.text
        assert watchdog.disarm() == "open"
    finally:
        com_stage.clear_attempt_context()


def test_stale_daemon_generation_drops_context_without_terminating():
    timers = []
    terminated = []

    def factory(interval, callback):
        timer = ManualTimer(interval, callback)
        timers.append(timer)
        return timer

    watchdog = ComWatchdog(lambda exe: terminated.append(exe) or 1, factory)
    com_stage.set_attempt_context(1, 1, "EXCEL.EXE")
    try:
        watchdog.arm("EXCEL.EXE", 10)
        com_stage.set_attempt_context(1, 2, "WINWORD.EXE")
        watchdog.arm("WINWORD.EXE", 10)
        assert fire_on_thread(timers[0].callback) == [0, None]
        assert terminated == []
        assert com_stage.get_attempt_context().attempt_id == 2
    finally:
        watchdog.disarm()
        com_stage.clear_attempt_context()
