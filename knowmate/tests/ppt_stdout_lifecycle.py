"""Pure deadlines and single-owner cleanup for the native stdout lifetime test."""

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from knowmate.tests.ppt_stdout_child import STAGES

WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED = 0, 258, 0xFFFFFFFF
PHASE_SECONDS, CLEANUP_SECONDS = 30, 5


@dataclass(frozen=True)
class WaitResult:
    """Preserve the raw OS result and immediately captured Win32 failure code."""

    result: int
    winerror: int | None = None


def read_progress(path: Path) -> tuple[str, int] | None:
    """Treat partial, unordered, oversized or non-ASCII progress as unavailable."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if not raw or len(raw) > 4096 or not raw.endswith(b"\n"):
            return None
        lines = raw.decode("ascii").splitlines()
        if not 1 <= len(lines) <= len(STAGES):
            return None
        previous = -1
        for index, line in enumerate(lines):
            match = re.fullmatch(r"([a-z_]+) (0|[1-9][0-9]{0,11})", line)
            if not match or match[1] != STAGES[index] or int(match[2]) < previous:
                return None
            previous = int(match[2])
        return STAGES[len(lines) - 1], previous
    except (OSError, UnicodeError):
        return None


class StdoutFailure(AssertionError):
    """A fixed diagnostic reason without exception messages or fixture paths."""


class StdoutLifecycle:
    """Own one test child through bounded observation and one failure finalizer."""

    def __init__(self, progress: Path, done: Path, *, release: Callable[[], None],
                 close_observation: Callable[[], None], wait: Callable[[int], WaitResult],
                 exit_code: Callable[[], int],
                 terminate: Callable[[], None], close_verifier: Callable[[], None],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.progress, self.done_path = progress, done
        self.release = release
        self.close_observation, self.wait = close_observation, wait
        self.exit_code = exit_code
        self.terminate, self.close_verifier, self.clock = terminate, close_verifier, clock
        self.has_control = False
        self.phase = "start"
        self.last_wait: WaitResult | None = None
        self.cleanup_wait: WaitResult | None = None
        self.actual_exit_before_cleanup = False
        self.actual_exit_code: int | None = None
        self._exit_deadline: float | None = None
        self.primary_failed = False
        self.body_complete = False
        self.filename: Path | None = None
        self.cleanup_error = "none"
        self.native_error: int | None = None
        self.cleanup_native_error: int | None = None
        self._finished = False

    def _wait(self, milliseconds: int) -> WaitResult:
        result = self.wait(milliseconds)
        self.last_wait = result
        if result.result not in (WAIT_OBJECT_0, WAIT_TIMEOUT):
            raise StdoutFailure("process_wait_failed")
        return result

    def require_alive(self) -> None:
        """Use the retained real process handle to prove pre-release liveness."""
        if self._wait(0).result != WAIT_TIMEOUT:
            raise StdoutFailure("child_exited_before_release")

    def await_stage(self, stage: str, phase: str) -> None:
        """Observe a child stage within its own finite thirty-second deadline."""
        self.phase = phase
        deadline = self._exit_deadline if phase == "release" else self.clock() + PHASE_SECONDS
        if deadline is None:
            raise StdoutFailure("release_not_started")
        while True:
            observed = read_progress(self.progress)
            if observed and STAGES.index(observed[0]) >= STAGES.index(stage):
                return
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise StdoutFailure("child_stage_timeout")
            if self._wait(min(50, max(1, math.ceil(remaining * 1000)))).result == WAIT_OBJECT_0:
                # The child can flush its stage between our read and the OS
                # wait returning signaled. Re-read before classifying absence.
                observed = read_progress(self.progress)
                if observed and STAGES.index(observed[0]) >= STAGES.index(stage):
                    return
                raise StdoutFailure("child_exited_before_stage")

    def release_child(self) -> None:
        """Start the single normal-exit deadline before sending release."""
        self.phase = "release"
        self._exit_deadline = self.clock() + PHASE_SECONDS
        self.release()

    def await_exit(self) -> None:
        """Recognize exit only when the same retained process handle signals."""
        self.phase = "exit"
        deadline = self._exit_deadline
        if deadline is None:
            raise StdoutFailure("release_not_started")
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise StdoutFailure("child_actual_exit_timeout")
            if self._wait(min(100, max(1, math.ceil(remaining * 1000)))).result == WAIT_OBJECT_0:
                self.actual_exit_before_cleanup = True
                try:
                    self.actual_exit_code = self.exit_code()
                except OSError as error:
                    self.native_error = getattr(error, "winerror", None)
                    raise StdoutFailure("child_exit_code_unavailable") from None
                if self.actual_exit_code != 0:
                    raise StdoutFailure("child_exit_nonzero")
                return

    def _cleanup_step(self, name: str, operation: Callable[[], object]) -> object | None:
        try:
            return operation()
        except BaseException as error:  # noqa: BLE001 - finish every cleanup step after an interrupt too.
            # Preserve the first cleanup failure, independently of the body error.
            if self.cleanup_error == "none":
                self.cleanup_error = name
                native = getattr(error, "winerror", None)
                if name == "wait" and self.cleanup_wait is not None:
                    native = self.cleanup_wait.winerror
                if type(native) is int and 0 <= native <= 0xFFFFFFFF:
                    self.cleanup_native_error = native
            return None

    def finish(self) -> None:
        """Release, bound cleanup to five seconds, then report without hiding body errors."""
        if self._finished:
            return
        self._finished = True
        deadline = self.clock() + CLEANUP_SECONDS
        # Snapshot before cleanup release/termination can advance the child.
        observed = read_progress(self.progress)
        try:
            done = self.done_path.is_file()
        except OSError:
            done = False
        self._cleanup_step("release", self.release)
        self._cleanup_step("observation_close", self.close_observation)
        if self.has_control:
            def wait_cleanup(milliseconds: int) -> WaitResult:
                value = self.wait(milliseconds)
                self.cleanup_wait = value
                if value.result not in (WAIT_OBJECT_0, WAIT_TIMEOUT):
                    raise StdoutFailure("cleanup_wait_failed")
                return value

            remaining = max(0, deadline - self.clock())
            result = self._cleanup_step("wait", lambda: wait_cleanup(min(250, int(remaining * 1000))))
            if not isinstance(result, WaitResult) or result.result != WAIT_OBJECT_0:
                self._cleanup_step("terminate", self.terminate)
                remaining = max(0, deadline - self.clock())
                result = self._cleanup_step("wait", lambda: wait_cleanup(int(remaining * 1000)))
                if ((not isinstance(result, WaitResult) or result.result != WAIT_OBJECT_0)
                        and self.cleanup_error == "none"):
                    self.cleanup_error = "exit_unconfirmed"
            self._cleanup_step("verifier_close", self.close_verifier)
        if self.body_complete and self.cleanup_error == "none" and self.filename is not None:
            def deleted() -> None:
                try:
                    self.filename.stat()
                except FileNotFoundError:
                    return
                raise StdoutFailure("stdout_last_close_not_deleted")
            self._cleanup_step("delete", deleted)
        success = (self.body_complete and not self.primary_failed
                   and self.actual_exit_before_cleanup and self.actual_exit_code == 0
                   and done and self.cleanup_error == "none")
        cleanup = "PASS" if self.cleanup_error == "none" else "FAIL"
        def number(value: int | None) -> str:
            return "unavailable" if value is None else str(value)
        last = self.last_wait
        cleaned = self.cleanup_wait
        print("PPT_STDOUT_DIAGNOSTIC"
              f" phase={self.phase} last_stage={observed[0] if observed else 'unavailable'}"
              f" stage_started_ms={observed[1] if observed else 'unavailable'}"
              f" actual_exit_before_cleanup={str(self.actual_exit_before_cleanup).lower()}"
              f" actual_exit_code={number(self.actual_exit_code)}"
              f" done={str(done).lower()} wait_result={number(last.result if last else None)}"
              f" winerror={number(last.winerror if last and last.winerror is not None else self.native_error)}"
              f" cleanup={cleanup}"
              f" cleanup_error={self.cleanup_error}"
              f" cleanup_error_winerror={number(self.cleanup_native_error)}"
              f" cleanup_wait_result={number(cleaned.result if cleaned else None)}"
              f" cleanup_winerror={number(cleaned.winerror if cleaned else None)}",
              flush=True)
        print(f"PPT_STDOUT_SUMMARY result={'PASS' if success else 'FAIL'}", flush=True)
        if not success and not self.primary_failed:
            raise StdoutFailure("stdout_finalization_failed")
