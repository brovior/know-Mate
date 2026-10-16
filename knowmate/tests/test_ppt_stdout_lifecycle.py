"""Portable regressions for test-only stdout stage, deadline and cleanup evidence."""

import ctypes
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowmate.tests import ppt_stdout_child as child
from knowmate.tests.ppt_stdout_lifecycle import (
    STAGES,
    WAIT_FAILED,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    StdoutFailure,
    StdoutLifecycle,
    WaitResult,
    read_progress,
)
from knowmate.tests.ppt_stdout_native import NativeStdoutChild


class ChildModel:
    """Model a real waitable process independently from progress and done files."""

    def __init__(self, tmp_path, scenario="normal", exit_delay=0):
        self.progress, self.done = tmp_path / "progress", tmp_path / "done"
        self.scenario, self.exit_delay = scenario, exit_delay
        self.now = 0.0
        self.released_at = None
        self.exited = False
        self.closed = False
        self.calls = []
        self.emit()
        self.lifecycle = StdoutLifecycle(self.progress, self.done,
            release=self.release, close_observation=self.close_observation,
            wait=self.wait, exit_code=lambda: 0, terminate=self.terminate, close_verifier=self.close,
            clock=lambda: self.now)
        self.lifecycle.has_control = True

    def emit(self):
        if self.scenario == "delay_ready" and self.now < 35:
            return
        count = 1
        if self.released_at is not None and self.scenario != "missing_release":
            count = 3 if self.scenario == "stop_flush" else 6
            if self.scenario != "stop_flush":
                self.done.touch()
            if self.scenario not in ("stop_flush", "stop_return") and self.now - self.released_at >= self.exit_delay:
                self.exited = True
        self.progress.write_text("".join(f"{stage} {index}\n" for index, stage in enumerate(STAGES[:count])),
                                 encoding="ascii")

    def release(self):
        self.calls.append(("release", self.now))
        if self.released_at is None:
            self.released_at = self.now
        self.emit()

    def close_observation(self):
        self.calls.append(("observation_close", self.now))

    def wait(self, milliseconds):
        assert not self.closed, "wait_after_close"
        self.calls.append(("wait", milliseconds, self.now))
        if not self.exited:
            self.now += milliseconds / 1000
            self.emit()
        return WaitResult(WAIT_OBJECT_0 if self.exited else WAIT_TIMEOUT)

    def terminate(self):
        assert not self.closed
        self.calls.append(("terminate", self.now))
        self.exited = True

    def close(self):
        assert not self.closed, "duplicate_close"
        self.calls.append(("verifier_close", self.now))
        self.closed = True


@pytest.mark.parametrize("raw", [b"", b"ready 0", b"ready 0\r", b"ready -1\n", b"ready 01\n",
    b"ready 1000000000000\n", b"ready 0\nready 1\n", b"release_seen 0\n", b"ready 0\nsecret 1\n",
    b"ready 2\nrelease_seen 1\n", b"ready 0\n\xff\n", b"x" * 4097])
def test_progress_rejects_incomplete_or_untrusted_record(tmp_path, raw):
    path = tmp_path / "progress"
    path.write_bytes(raw)
    assert read_progress(path) is None


def test_progress_requires_the_complete_ordered_prefix(tmp_path):
    path = tmp_path / "progress"
    assert read_progress(path) is None
    for count in range(1, 7):
        path.write_text("".join(f"{stage} {index * 123}\n" for index, stage in enumerate(STAGES[:count])))
        assert read_progress(path) == (STAGES[count - 1], (count - 1) * 123)


def test_done_and_observation_close_are_not_actual_exit(tmp_path, capsys):
    model = ChildModel(tmp_path, "stop_return")
    life = model.lifecycle
    life.require_alive()
    life.close_observation()
    life.require_alive()
    life.release_child()
    life.await_stage("release_seen", "release")
    assert life.done_path.is_file()
    with pytest.raises(StdoutFailure, match="actual_exit_timeout"):
        life.await_exit()
    life.primary_failed = True
    started_cleanup = model.now
    life.finish()
    output = capsys.readouterr().out
    assert "last_stage=before_return" in output and "done=true" in output
    assert "actual_exit_before_cleanup=false" in output
    assert "PPT_STDOUT_SUMMARY result=FAIL" in output
    assert model.exited and model.closed and model.now - started_cleanup <= 5
    assert any(call[0] == "terminate" for call in model.calls)


@pytest.mark.parametrize("scenario,phase,stage", [
    ("delay_ready", "ready", "unavailable"),
    ("missing_release", "release", "ready"),
    ("stop_flush", "exit", "stdout_before"),
])
def test_stage_timeouts_preserve_stage_and_clean_only_with_extra_budget(tmp_path, capsys, scenario, phase, stage):
    model = ChildModel(tmp_path, scenario)
    life = model.lifecycle
    if phase != "ready":
        life.await_stage("ready", "ready")
        life.require_alive()
        life.close_observation()
        life.require_alive()
        life.release_child()
    with pytest.raises(StdoutFailure, match="timeout"):
        if phase == "exit":
            life.await_stage("release_seen", "release")
            life.await_exit()
        else:
            life.await_stage("ready" if phase == "ready" else "release_seen", phase)
    assert 30 <= model.now < 30.11
    life.primary_failed = True
    before = model.now
    life.finish()
    output = capsys.readouterr().out
    assert f"phase={phase} last_stage={stage}" in output
    assert "actual_exit_before_cleanup=false" in output
    assert "cleanup=PASS" in output and "PPT_STDOUT_SUMMARY result=FAIL" in output
    assert model.exited and model.closed and model.now - before <= 5
    life.finish()  # No duplicate close, wait or termination.


def test_normal_exit_can_take_more_than_old_five_seconds(tmp_path, capsys):
    model = ChildModel(tmp_path, exit_delay=8)
    life = model.lifecycle
    life.await_stage("ready", "ready")
    life.require_alive()
    life.close_observation()
    life.require_alive()
    life.require_alive()
    life.release_child()
    life.await_stage("release_seen", "release")
    assert not life.actual_exit_before_cleanup
    life.await_exit()
    assert 8 <= model.now < 8.11
    life.filename = tmp_path / "deleted_stdout"
    life.body_complete = True
    life.phase = "complete"
    life.finish()
    output = capsys.readouterr().out
    assert "actual_exit_before_cleanup=true" in output and "PPT_STDOUT_SUMMARY result=PASS" in output
    assert not any(call[0] == "terminate" for call in model.calls)


def test_cleanup_uses_one_budget_including_release_and_close(tmp_path):
    model = ChildModel(tmp_path, "stop_flush")
    life = model.lifecycle
    def slow_release():
        model.now += 2
        model.release()
    def slow_close():
        model.now += 2
        model.close_observation()
    life.release, life.close_observation = slow_release, slow_close
    life.primary_failed = True
    life.finish()
    assert model.now <= 5
    waits = [call[1] for call in model.calls if call[0] == "wait"]
    assert sum(waits) <= 1000


def test_primary_error_survives_cleanup_errors_and_retained_handle_is_closed(tmp_path, capsys):
    model = ChildModel(tmp_path, "stop_flush")
    life = model.lifecycle
    def failed_release():
        error = OSError("secret-path-and-message")
        error.winerror = 5
        raise error
    life.release = failed_release
    life.close_observation = lambda: (_ for _ in ()).throw(RuntimeError("secret-SDDL"))
    life.primary_failed = True
    life.finish()
    output = capsys.readouterr().out
    assert "cleanup=FAIL cleanup_error=release cleanup_error_winerror=5" in output
    assert "secret" not in output and model.closed and model.exited


def test_cleanup_failure_after_success_is_a_test_failure(tmp_path, capsys):
    model = ChildModel(tmp_path)
    life = model.lifecycle
    life.release_child()
    life.await_exit()
    life.body_complete = True
    life.filename = tmp_path / "still_exists"
    life.filename.touch()
    with pytest.raises(StdoutFailure, match="finalization_failed"):
        life.finish()
    assert "cleanup_error=delete" in capsys.readouterr().out


def test_native_wait_preserves_failed_result_and_original_error(monkeypatch, tmp_path, capsys):
    native = NativeStdoutChild.__new__(NativeStdoutChild)
    native.handle = 123
    native.kernel = SimpleNamespace(WaitForSingleObject=lambda handle, ms: WAIT_FAILED)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 6, raising=False)
    assert native.wait(0) == WaitResult(WAIT_FAILED, 6)
    model = ChildModel(tmp_path)
    life = model.lifecycle
    life.wait = native.wait
    with pytest.raises(StdoutFailure, match="process_wait_failed"):
        life.require_alive()
    life.primary_failed = True
    life.finish()
    output = capsys.readouterr().out
    assert "wait_result=4294967295 winerror=6" in output
    assert "cleanup_wait_result=4294967295 cleanup_winerror=6" in output
    assert "cleanup=FAIL" in output and model.closed


def test_start_failure_with_no_child_has_no_wait_or_termination(tmp_path, capsys):
    model = ChildModel(tmp_path)
    life = model.lifecycle
    life.has_control = False
    life.primary_failed = True
    life.finish()
    assert [call[0] for call in model.calls] == ["release", "observation_close"]
    assert "PPT_STDOUT_SUMMARY result=FAIL" in capsys.readouterr().out


def test_real_test_child_emits_only_fixed_stages_and_stdout(tmp_path):
    progress, release, done = (tmp_path / name for name in ("progress", "release", "done"))
    release.touch()
    result = subprocess.run([sys.executable, str(Path(child.__file__)), str(progress), str(release), str(done), "normal"],
                            check=False, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout == "{}\n" and result.stderr == ""
    assert read_progress(progress)[0] == "before_return" and done.is_file()


@pytest.mark.parametrize("control,stage", [("delay_ready", None), ("delay_release", "ready"),
    ("stop_flush", "stdout_before"), ("stop_return", "before_return")])
def test_child_injection_stops_at_the_intended_stage(monkeypatch, tmp_path, control, stage, capsys):
    progress, release, done = (tmp_path / name for name in ("progress", "release", "done"))
    release.touch()
    class Pause(Exception):
        pass
    def pause(seconds):
        assert seconds == 35
        raise Pause()
    monkeypatch.setattr(child.time, "sleep", pause)
    with pytest.raises(Pause):
        child.run_child(progress, release, done, control)
    observed = read_progress(progress)
    assert (observed[0] if observed else None) == stage
    assert done.exists() == (control == "stop_return")
    assert capsys.readouterr().out == ("{}\n" if control in ("stop_flush", "stop_return") else "")


def test_child_missing_release_never_writes_stdout_or_done(monkeypatch, tmp_path, capsys):
    progress, release, done = (tmp_path / name for name in ("progress", "release", "done"))
    release.touch()  # Injection ignores even an accidental release.
    clock = [0.0]
    monkeypatch.setattr(child.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(child.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert child.run_child(progress, release, done, "missing_release") == 3
    assert read_progress(progress)[0] == "ready" and not done.exists()
    assert capsys.readouterr().out == ""


def test_release_stage_and_exit_share_one_thirty_second_deadline(tmp_path):
    model = ChildModel(tmp_path, "stop_return")
    life = model.lifecycle
    life.await_stage("ready", "ready")
    life.release_child()
    model.now = 29  # Late release observation cannot restart the normal deadline.
    life.await_stage("release_seen", "release")
    with pytest.raises(StdoutFailure, match="actual_exit_timeout"):
        life.await_exit()
    assert 30 <= model.now < 30.11
    life.primary_failed = True
    life.finish()
    assert model.now < 35.11 and model.closed


def test_abnormal_actual_exit_preserves_original_unsigned_code(tmp_path, capsys):
    model = ChildModel(tmp_path)
    life = model.lifecycle
    life.exit_code = lambda: 0xC000013A
    life.release_child()
    with pytest.raises(StdoutFailure, match="child_exit_nonzero"):
        life.await_exit()
    life.primary_failed = True
    life.finish()
    output = capsys.readouterr().out
    assert "actual_exit_before_cleanup=true actual_exit_code=3221225786" in output
    assert "PPT_STDOUT_SUMMARY result=FAIL" in output
    assert not any(call[0] == "terminate" for call in model.calls)


def test_exit_between_progress_read_and_wait_rechecks_progress(tmp_path):
    model = ChildModel(tmp_path, "missing_release")
    life = model.lifecycle
    life.release_child()
    def exit_after_flush(milliseconds):
        model.scenario = "normal"
        model.emit()
        return WaitResult(WAIT_OBJECT_0)
    life.wait = exit_after_flush
    life.await_stage("release_seen", "release")
    assert not life.actual_exit_before_cleanup
    life.await_exit()
    assert life.actual_exit_before_cleanup
