"""Bounded policy command and progress ownership for the shared collector."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class PolicyJob:
    """Keep one command and one scalar snapshot until the GUI confirms join."""

    def __init__(self) -> None:
        """Create an idle mailbox without filesystem or database access."""
        self._lock = threading.Lock()
        self._command: tuple | None = None
        self._pending = False
        self._started = False
        self._unknown = False
        self._uncertain: tuple[str, tuple[str, ...]] | None = None
        self._snapshot: dict | None = None
        self._cache: dict | None = None

    @property
    def pending(self) -> bool:
        """Report ownership retained beyond terminal publication."""
        with self._lock:
            return self._pending

    @property
    def unknown(self) -> bool:
        """Report an unresolved durable setting independently of native join."""
        with self._lock:
            return self._unknown

    def reserve(self, job_id: str, action: str, paths: tuple[str, ...]) -> str:
        """Reserve one immutable command without beginning any I/O."""
        if not isinstance(job_id, str) or not job_id or action not in {"exclude", "unexclude", "reconcile"}:
            return "error"
        if not isinstance(paths, tuple) or any(not isinstance(path, str) or not path for path in paths):
            return "error"
        from knowmate.collector.scanner import normalize_path_key
        by_key: dict[str, str] = {}
        for path in paths:
            by_key.setdefault(normalize_path_key(path), path)
        paths = tuple(by_key.values())
        with self._lock:
            if self._pending:
                return "busy"
            if self._unknown and action != "reconcile":
                return "policy_unconfirmed"
            if action == "reconcile":
                if not self._unknown or self._uncertain is None:
                    return "error"
                original_action, original_paths = self._uncertain
            else:
                original_action, original_paths = action, paths
            self._command = (job_id, action, original_action, original_paths)
            self._pending, self._started = True, False
            self._cache = None
            self._snapshot = {
                "job_id": job_id, "revision": 1, "action": action, "stage": "reserved",
                "done": 0, "total": len(paths), "settings_saved": False,
                "terminal": False, "status": "running", "error_code": "",
                "policy_unknown": self._unknown, "result": {}, "counts": None,
            }
        return "ok"

    def publish(self, **values: Any) -> None:
        """Replace scalar metadata without queuing one Qt event per file."""
        if "result" in values:
            values["result"] = dict(values["result"])
            for name in ("attempted", "succeeded", "failed", "not_attempted"):
                values["result"]["cleanup_" + name] = values["result"].get(name, 0)
        with self._lock:
            if self._snapshot is None:
                return
            self._snapshot.update(deepcopy(values))
            self._snapshot["revision"] += 1

    def snapshot(self) -> dict | None:
        """Return detached progress that never exposes the live command."""
        with self._lock:
            return deepcopy(self._snapshot)

    def take_cache(self) -> dict | None:
        """Transfer one detached failure baseline prepared on the worker."""
        with self._lock:
            cache, self._cache = self._cache, None
            return cache

    def clear(self, job_id: str) -> bool:
        """Discard only a command confirmed not to have begun native work."""
        with self._lock:
            if self._started or self._snapshot is None or self._snapshot["job_id"] != job_id:
                return False
            self._command = None
            self._pending = False
            self._snapshot.update(terminal=True, stage="error", status="error", error_code="start_failed")
            self._snapshot["revision"] += 1
            return True

    def acknowledge(self, job_id: str) -> bool:
        """Release ownership after the caller verifies actual native exit."""
        with self._lock:
            if self._snapshot is None or self._snapshot["job_id"] != job_id or not self._snapshot["terminal"]:
                return False
            self._pending = False
            return True

    def run(self, worker: Any) -> bool:
        """Consume a command once and contain all policy failures on its owner."""
        with self._lock:
            command, self._command = self._command, None
            if command is None:
                return self._pending or self._unknown
            self._started = True
        _, action, original_action, paths = command
        result: dict[str, Any] = {}
        try:
            from knowmate.collector import failure_state
            records = failure_state.load_failures(worker._failure_file)
            with self._lock:
                self._cache = {"failure_records": {path: replace(record) for path, record in records.items()}}
            self.publish(stage="settings")
            from knowmate.collector.policy_service import reconcile_policy, run_policy
            result = reconcile_policy(worker, original_action, paths) if action == "reconcile" else run_policy(
                worker, action, paths, self.publish)
        except Exception as exc:
            logger.error("[policy] 제외 작업 오류: error_type=%s", type(exc).__name__)
            previous = self.snapshot() or {}
            result = dict(previous.get("result", {}))
            result.update(status="error", error_code="operation_failed",
                          settings_saved=previous.get("settings_saved", False))
        finally:
            with self._lock:
                unknown = bool(result.get("policy_unknown", self._unknown))
                self._unknown = unknown
                if unknown:
                    self._uncertain = (original_action, paths)
                elif action == "reconcile":
                    self._uncertain = None
            for name in ("attempted", "succeeded", "failed", "not_attempted"):
                result["cleanup_" + name] = result.get(name, 0)
            status = result.get("status", "error")
            with self._lock:
                self._snapshot.update(
                    stage="cancelled" if status == "cancelled" else "error" if status == "error" else "complete",
                    terminal=True, status=status, result=deepcopy(result),
                    done=result.get("attempted", 0), total=result.get("changed", 0),
                    settings_saved=result.get("settings_saved"), policy_unknown=unknown,
                    counts=result.get("counts"), error_code=result.get("error_code", ""))
                self._snapshot["revision"] += 1
        return True
