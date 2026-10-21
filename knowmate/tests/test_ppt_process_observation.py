"""Original-handle ownership, failure evidence and wait/close concurrency."""
import ctypes
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from knowmate.secure.ppt_process_observation import RetainedProcess


def make_observer(wait=None):
    kernel = SimpleNamespace(WaitForSingleObject=Mock(return_value=258),
                             CloseHandle=Mock(return_value=True))
    if wait is not None:
        kernel.WaitForSingleObject.side_effect = wait
    return RetainedProcess(10, 100, 500, kernel), kernel


def test_signaled_original_handle_closes_once_and_never_reopens_pid():
    observer, kernel = make_observer(lambda *_: 0)
    assert observer.poll().status == "exited"
    observer.close()
    assert observer.poll().status == "exited"
    kernel.WaitForSingleObject.assert_called_once_with(500, 0)
    kernel.CloseHandle.assert_called_once_with(500)


def test_wait_failed_captures_last_error_before_close(monkeypatch):
    order = []
    observer, kernel = make_observer(lambda *_: order.append("wait") or 0xFFFFFFFF)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: order.append("error") or 6, raising=False)
    kernel.CloseHandle.side_effect = lambda *_: order.append("close") or True
    result = observer.poll()
    assert result.status == "observation_failed" and result.error_code == 6
    assert order == ["wait", "error", "close"]
    assert observer.poll() == result
    kernel.CloseHandle.assert_called_once()


def test_native_exception_is_failure_without_opaque_message(caplog):
    error = OSError("private-document-marker")
    error.winerror = 5
    observer, kernel = make_observer(Mock(side_effect=error))
    result = observer.poll()
    assert result.status == "observation_failed" and result.error_code == 5
    assert "private-document-marker" not in caplog.text
    kernel.CloseHandle.assert_called_once()


def test_deadline_timeout_retains_handle_until_explicit_teardown():
    observer, kernel = make_observer()
    assert observer.wait_until(time.monotonic()).status == "timeout"
    kernel.CloseHandle.assert_not_called()
    observer.close()
    observer.close()
    assert observer.poll().status == "target_unconfirmed"
    kernel.CloseHandle.assert_called_once()


def test_close_cannot_overlap_inflight_native_wait():
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def wait(*_):
        entered.set()
        assert release.wait(5)
        return 258
    observer, kernel = make_observer(wait)
    polling = threading.Thread(target=observer.poll)
    closing = threading.Thread(target=lambda: (observer.close(), closed.set()))
    polling.start()
    assert entered.wait(5)
    closing.start()
    try:
        assert not closed.wait(.02)
        kernel.CloseHandle.assert_not_called()
    finally:
        release.set()
        polling.join(5)
        closing.join(5)
    assert not polling.is_alive() and not closing.is_alive()
    assert closed.is_set() and observer.poll().status == "target_unconfirmed"
    kernel.CloseHandle.assert_called_once()
