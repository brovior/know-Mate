"""Portable ownership regressions for the Windows-only stdout test adapter."""

import ctypes
from types import SimpleNamespace

import pytest

from knowmate.tests.ppt_stdout_lifecycle import StdoutLifecycle, WaitResult
from knowmate.tests.ppt_stdout_native import NativeStdoutChild


@pytest.mark.parametrize("duplicate", [True, False])
def test_early_failure_retains_only_the_launched_child_until_final_cleanup(monkeypatch, tmp_path, duplicate, capsys):
    calls = []
    closed = set()
    exited = [False]

    class Child:
        _handle = 111

        def close_observation(self):
            assert 111 not in closed
            calls.append(("child_close", 111))
            closed.add(111)

    child = Child()
    process = SimpleNamespace(_process=child)

    def close_observation():
        calls.append(("output_close",))
        if process._process is not None:
            process._process.close_observation()
            process._process = None

    process.close = close_observation

    def duplicate_handle(current, original, target, pointer, rights, inherit, options):
        assert original == 111 and inherit is False and options == 2
        pointer._obj.value = 333 if duplicate else None
        return duplicate

    def wait(handle, milliseconds):
        assert handle not in closed
        assert handle == (333 if duplicate else 111)
        calls.append(("wait", handle, milliseconds))
        return 0 if exited[0] else 258

    def terminate(handle, code):
        assert handle not in closed and handle == (333 if duplicate else 111)
        assert code == 125
        calls.append(("terminate", handle))
        exited[0] = True
        return True

    def close(handle):
        assert handle not in closed
        closed.add(handle)
        calls.append(("verifier_close", handle))
        return True

    native = NativeStdoutChild.__new__(NativeStdoutChild)
    native.process, native.child, native.handle = process, None, None
    native.duplicated, native.duplicate_error = False, None
    native.kernel = SimpleNamespace(GetCurrentProcess=lambda: 999, DuplicateHandle=duplicate_handle,
        WaitForSingleObject=wait, TerminateProcess=terminate, CloseHandle=close)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    native.attach(child)
    assert native.duplicate_error == (None if duplicate else 5)
    life = StdoutLifecycle(tmp_path / "progress", tmp_path / "done", release=lambda: calls.append(("release",)),
        close_observation=native.close_observation, wait=native.wait, terminate=native.terminate,
        exit_code=native.exit_code,
        close_verifier=native.close_verifier)
    life.has_control = True
    life.primary_failed = True
    life.native_error = native.duplicate_error
    life.finish()
    assert exited[0]
    assert closed == ({111, 333} if duplicate else {111})
    assert calls[0] == ("release",)
    assert [item[0] for item in calls].count("terminate") == 1
    assert calls[-1][0] == ("verifier_close" if duplicate else "child_close")
    assert "PPT_STDOUT_SUMMARY result=FAIL" in capsys.readouterr().out
    before = list(calls)
    life.finish()
    native.close_verifier()
    assert calls == before


def test_unconfirmed_forced_exit_remains_cleanup_failure(tmp_path, capsys):
    clock = [0.0]
    calls = []
    def wait(milliseconds):
        clock[0] += milliseconds / 1000
        calls.append(("wait", milliseconds))
        return WaitResult(258)
    life = StdoutLifecycle(tmp_path / "progress", tmp_path / "done", release=lambda: None,
        close_observation=lambda: None, wait=wait, terminate=lambda: calls.append(("terminate",)),
        exit_code=lambda: 0,
        close_verifier=lambda: calls.append(("close",)), clock=lambda: clock[0])
    life.has_control = True
    life.primary_failed = True
    life.finish()
    assert clock[0] <= 5 and calls[-1] == ("close",)
    output = capsys.readouterr().out
    assert "cleanup=FAIL cleanup_error=exit_unconfirmed" in output
    assert "actual_exit_before_cleanup=false" in output and "PPT_STDOUT_SUMMARY result=FAIL" in output


def test_exit_code_uses_the_same_retained_handle_and_preserves_full_unsigned_code():
    native = NativeStdoutChild.__new__(NativeStdoutChild)
    native.handle = 333
    def get_exit(handle, pointer):
        assert handle == 333
        pointer._obj.value = 0xC000013A
        return True
    native.kernel = SimpleNamespace(GetExitCodeProcess=get_exit)
    assert native.exit_code() == 0xC000013A
