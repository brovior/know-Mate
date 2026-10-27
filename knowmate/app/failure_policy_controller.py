"""GUI ownership of one background exclusion command through native thread join."""
from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class FailurePolicyController:
    """Keep policy admission closed until its terminal and native exit agree."""

    def __init__(self) -> None:
        """Initialize an idle operation without filesystem or database access."""
        self._worker: Any = None
        self._closing = False
        self._unknown = False
        self._snapshot = self._idle()
        self._records: dict | None = None
        self._rollback_pending = False
        self.cache_valid = False

    @staticmethod
    def _idle() -> dict:
        """Return the bounded public status for an unused controller."""
        return dict(job_id="", revision=0, action="", stage="idle", done=0, total=0,
                    active=False, terminal=False, settings_saved=None, policy_unknown=False,
                    result={}, counts=None)

    @property
    def busy(self) -> bool:
        """Report a reserved operation even before or after native execution."""
        return self._worker is not None

    @property
    def blocked(self) -> bool:
        """Protect writes and new queries when policy state is unresolved."""
        return self.busy or self._unknown or self._closing

    @property
    def cache_only(self) -> bool:
        """Avoid GUI I/O during work and when restoring its latest result."""
        return self.blocked or self.cache_valid

    def status(self) -> dict:
        """Copy only public metadata without polling or performing I/O."""
        return deepcopy(self._snapshot)

    @staticmethod
    def _clear_unstarted(worker: Any, job_id: str) -> bool:
        """Release a failed reservation only after both native and command ownership clear."""
        try:
            if worker.isRunning() or not worker.wait(0):
                return False
            return bool(worker.clear_policy_command(job_id)
                        or getattr(worker, "policy_job_pending", True) is False)
        except Exception as exc:
            logger.warning("[policy] 미시작 예약 정리 확인 실패: error_type=%s", type(exc).__name__)
            return False

    def start(self, worker: Any, action: str, paths: tuple[str, ...]) -> dict:
        """Reserve on the GUI thread before attempting a native start."""
        if self.busy or self._closing or (self._unknown and action != "reconcile"):
            return dict(ok=False, accepted=False, error="policy_busy")
        job_id = uuid4().hex
        reserve = (getattr(worker, "request_policy_reconcile", None) if action == "reconcile"
                   else getattr(worker, "request_policy_job", None))
        if reserve is None:
            return dict(ok=False, accepted=False, error="not_ready")
        self._worker = worker
        try:
            result = reserve(job_id) if action == "reconcile" else reserve(job_id, action, paths)
        except Exception as exc:
            logger.warning("[policy] 작업 예약 실패: error_type=%s", type(exc).__name__)
            self._snapshot = dict(self._idle(), job_id=job_id, action=action, stage="error",
                                  total=len(paths), active=True, error_code="reservation_failed",
                                  policy_unknown=self._unknown)
            if self._clear_unstarted(worker, job_id):
                self._worker = None
                self._snapshot.update(active=False, terminal=True, settings_saved=False, status="error")
                return dict(ok=False, accepted=False, job_id=job_id, error="start_failed")
            self._rollback_pending = True
            return dict(ok=True, accepted=True, job_id=job_id)
        if result != "ok":
            self._worker = None
            return dict(ok=False, accepted=False, error=result)
        self.cache_valid = False
        self._snapshot = dict(self._idle(), job_id=job_id, action=action, stage="reserved",
                              total=len(paths), active=True, policy_unknown=self._unknown)
        try:
            worker.start()
        except Exception as exc:
            logger.warning("[policy] 작업 시작 확인 필요: error_type=%s", type(exc).__name__)
            # A start may throw after creating the native thread; keep that lease.
            try:
                stopped = not worker.isRunning() and worker.wait(0)
                cleared = stopped and worker.clear_policy_command(job_id)
            except Exception:
                cleared = False
            if cleared:
                self._snapshot.update(stage="error", active=False, terminal=True,
                                      error_code="start_failed", revision=1,
                                      settings_saved=False, status="error")
                self._worker = None
                return dict(ok=False, accepted=False, job_id=job_id, error="start_failed")
            self._snapshot["error_code"] = "start_failed"
            self._rollback_pending = True
        return dict(ok=True, accepted=True, job_id=job_id)

    def poll(self) -> tuple[bool, dict | None]:
        """Apply one latest snapshot and release only after nonblocking join."""
        worker = self._worker
        if worker is None:
            return False, None
        if self._rollback_pending and self._clear_unstarted(worker, self._snapshot["job_id"]):
            self._rollback_pending = False
            self._worker = None
            self._snapshot.update(active=False, terminal=True, stage="error",
                                  settings_saved=False, status="error")
            return True, self.status()
        try:
            snapshot = worker.policy_job_snapshot()
            take_records = getattr(worker, "take_policy_failure_records", None)
            if take_records is not None:
                records = take_records()
                if records is not None:
                    self._records = records
        except Exception as exc:
            return self._observation_failed(exc)
        if snapshot is None or snapshot.get("job_id") != self._snapshot["job_id"]:
            return False, None
        if snapshot.get("revision", -1) < self._snapshot["revision"]:
            return False, None
        candidate = deepcopy(snapshot)
        candidate.setdefault("result", {})
        candidate.setdefault("counts", None)
        self._unknown = bool(candidate.get("policy_unknown", False))
        terminal = bool(candidate.get("terminal", False))
        try:
            stopped = terminal and not worker.isRunning() and worker.wait(0)
            acknowledged = stopped and worker.acknowledge_policy_job(candidate["job_id"])
        except Exception as exc:
            return self._observation_failed(exc)
        candidate.update(active=True, terminal=False, policy_unknown=self._unknown)
        if acknowledged:
            candidate.update(active=False, terminal=True)
            self._worker = None
            self._rollback_pending = False
            self.cache_valid = True
            self._snapshot = candidate
            return True, self.status()
        changed = candidate != self._snapshot
        self._snapshot = candidate
        return changed, None

    def _observation_failed(self, error: Exception) -> tuple[bool, None]:
        """Keep ownership on transient native-observation errors without log flooding."""
        changed = self._snapshot.get("error_code") != "observation_failed"
        if changed:
            logger.warning("[policy] 진행 상태 확인 실패: error_type=%s", type(error).__name__)
        self._snapshot["error_code"] = "observation_failed"
        return changed, None

    def take_records(self) -> dict | None:
        """Transfer a detached worker-prepared failure baseline once."""
        records, self._records = self._records, None
        return records

    def begin_shutdown(self) -> None:
        """Seal admission without releasing a live policy writer."""
        self._closing = True

    def invalidate_cache(self) -> None:
        """Require ordinary status refresh after a different database mutation."""
        if not self.busy:
            self.cache_valid = False
