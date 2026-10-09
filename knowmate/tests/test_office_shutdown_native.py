"""Independent review of Windows process termination ordering and error handling."""
from types import SimpleNamespace

import pytest

from knowmate.secure import office_guard as guard


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def install_kernel(monkeypatch, *, identity=777, terminate=True, wait=0, open_handle=101, identity_ok=True):
    calls = []

    def created(handle, created_at, *_other):
        calls.append(("identity", handle))
        created_at._obj.dwLowDateTime = identity
        created_at._obj.dwHighDateTime = 0
        return identity_ok

    kernel = SimpleNamespace(
        OpenProcess=Function(lambda flags, inherit, pid: calls.append(("open", flags, pid)) or open_handle),
        GetProcessTimes=Function(created),
        TerminateProcess=Function(lambda handle, code: calls.append(("terminate", handle)) or terminate),
        WaitForSingleObject=Function(lambda handle, milliseconds: calls.append(("wait", handle, milliseconds)) or wait),
        CloseHandle=Function(lambda handle: calls.append(("close", handle)) or True),
    )
    import ctypes
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    return calls


def test_pid_reuse_is_checked_on_termination_handle(monkeypatch):
    calls = install_kernel(monkeypatch, identity=778)
    outcome = guard._terminate_and_confirm(12345, guard.OwnedOfficeProcess("WINWORD.EXE", 777), 0.1)
    assert outcome == "identity_changed"
    assert not any(call[0] == "terminate" for call in calls)
    assert calls[-1] == ("close", 101)


@pytest.mark.parametrize(("wait", "expected"), [(0, "terminated"), (0x102, "remaining"), (0xFFFFFFFF, "failed")])
def test_exit_request_waits_on_the_same_verified_handle(monkeypatch, wait, expected):
    calls = install_kernel(monkeypatch, wait=wait)
    outcome = guard._terminate_and_confirm(12345, guard.OwnedOfficeProcess("WINWORD.EXE", 777), 0.1)
    assert outcome == expected
    assert calls[0][1] & 0x00100000  # SYNCHRONIZE is required for the wait.
    assert ("identity", 101) in calls
    assert ("terminate", 101) in calls
    assert any(call[0] == "wait" and call[1] == 101 and 0 <= call[2] <= 100 for call in calls)
    assert calls[-1] == ("close", 101)


def test_failed_request_can_still_mean_original_process_already_exited(monkeypatch):
    calls = install_kernel(monkeypatch, terminate=False, wait=0)
    outcome = guard._terminate_and_confirm(12345, guard.OwnedOfficeProcess("WINWORD.EXE", 777), 0.1)
    assert outcome == "exited"
    assert calls[-1] == ("close", 101)


def test_open_failure_is_visible_and_keeps_identity_unresolved(monkeypatch, caplog):
    calls = install_kernel(monkeypatch, open_handle=0)
    outcome = guard._terminate_and_confirm(12345, guard.OwnedOfficeProcess("WINWORD.EXE", 777), 0.1)
    assert outcome == "identity_unknown"
    assert "error=5" in caplog.text
    assert not any(call[0] in {"close", "terminate"} for call in calls)


def test_creation_identity_failure_never_terminates(monkeypatch, caplog):
    calls = install_kernel(monkeypatch, identity_ok=False)
    outcome = guard._terminate_and_confirm(12345, guard.OwnedOfficeProcess("WINWORD.EXE", 777), 0.1)
    assert outcome == "identity_unknown"
    assert "error=5" in caplog.text
    assert not any(call[0] == "terminate" for call in calls)
    assert calls[-1] == ("close", 101)


def test_unknown_original_identity_cannot_be_treated_as_pid_reuse(monkeypatch):
    calls = install_kernel(monkeypatch)
    assert guard._terminate_and_confirm(12345, guard.OwnedOfficeProcess("WINWORD.EXE", None), 0.1) == "identity_unknown"
    assert calls == []


def test_failed_termination_request_does_not_discard_live_process(monkeypatch):
    calls = install_kernel(monkeypatch, terminate=False, wait=0x102)
    assert guard._terminate_and_confirm(12345, guard.OwnedOfficeProcess("WINWORD.EXE", 777), 0.1) == "failed"
    assert ("wait", 101, 0) in calls
    assert calls[-1] == ("close", 101)


@pytest.mark.parametrize(("identity", "identity_ok", "wait", "remaining"), [
    (777, True, 0, set()), (777, True, 0x102, {12345}),
    (778, True, 0, set()), (777, False, 0, {12345}),
])
def test_grace_wait_uses_identity_from_its_own_handle(monkeypatch, identity, identity_ok, wait, remaining):
    import sys
    monkeypatch.setattr(sys, "platform", "win32")
    calls = install_kernel(monkeypatch, identity=identity, identity_ok=identity_ok, wait=wait)
    result, _elapsed = guard.wait_for_owned_exit({12345: guard.OwnedOfficeProcess("WINWORD.EXE", 777)}, 0.1)
    assert result == remaining
    assert ("identity", 101) in calls
    assert calls[-1] == ("close", 101)
    assert not any(call[0] == "terminate" for call in calls)
    if identity != 777 or not identity_ok:
        assert not any(call[0] == "wait" for call in calls)


@pytest.mark.parametrize(("processes", "remaining"), [
    (None, {12345}), ([("WINWORD.EXE", 12345)], {12345}), ([], set()),
])
def test_grace_open_failure_only_clears_a_confirmed_absent_process(monkeypatch, processes, remaining):
    import sys
    monkeypatch.setattr(sys, "platform", "win32")
    install_kernel(monkeypatch, open_handle=0)
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: processes)
    result, _elapsed = guard.wait_for_owned_exit({12345: guard.OwnedOfficeProcess("WINWORD.EXE", 777)}, 0.1)
    assert result == remaining
