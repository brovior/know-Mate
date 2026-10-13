"""File-scoped PPT safety checks with a frozen COM outcome before embedding."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import time
import uuid
from typing import Callable


class PptPreparationPending(RuntimeError):
    """PPT preparation is unresolved; defer without a damaged-file failure."""


class PptOperationError(RuntimeError):
    """A fixed-code safety failure that must never trigger add-in fallback."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PptComOutcome:
    """Immutable COM result; its deadline does not extend into embedding."""
    generation: str
    started: float
    ended: float
    deadline: float | None
    expired: bool
    close_ok: bool
    successful: bool


class PptOperation:
    """Check cancellation/source changes and freeze one extraction's COM result."""

    def __init__(
        self, path: str, timeout: float | None,
        cancelled: Callable[[], bool] = lambda: False,
        externally_fired: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        stat = Path(path).stat()
        self._source = (stat.st_size, stat.st_mtime_ns)
        self._cancelled = cancelled
        self._fired = externally_fired
        self._clock = clock
        self.generation = uuid.uuid4().hex
        self.started = clock()
        self.deadline = self.started + timeout if timeout is not None and timeout > 0 else None
        self.outcome: PptComOutcome | None = None

    def _check_current(self) -> None:
        if self._cancelled():
            raise PptOperationError("cancelled")
        try:
            stat = Path(self.path).stat()
        except OSError as exc:
            raise PptOperationError("source_changed") from exc
        if (stat.st_size, stat.st_mtime_ns) != self._source:
            raise PptOperationError("source_changed")

    def check(self) -> None:
        """Check the original deadline and source at a COM boundary."""
        self._check_current()
        if self._fired() or (self.deadline is not None and self._clock() >= self.deadline):
            raise PptOperationError("deadline_expired")

    def finish(self, close_ok: bool, successful: bool) -> None:
        """Freeze the actual close/extraction end; never reset the original deadline."""
        if self.outcome is not None:
            raise PptOperationError("duplicate_outcome")
        ended = self._clock()
        expired = self._fired() or (self.deadline is not None and ended >= self.deadline)
        self.outcome = PptComOutcome(self.generation, self.started, ended, self.deadline,
                                     expired, close_ok, successful)

    def validate_commit(self) -> None:
        """Validate the frozen COM result and current source, independent of embedding time."""
        result = self.outcome
        if result is None or result.generation != self.generation:
            raise PptOperationError("missing_outcome")
        if result.expired:
            raise PptOperationError("deadline_expired")
        if not result.close_ok:
            raise PptOperationError("close_unconfirmed")
        if not result.successful:
            raise PptOperationError("extract_failed")
        self._check_current()
