"""Qt startup gate for PPT setup; Office and elevation stay in secure helpers."""
from __future__ import annotations

from collections.abc import Callable
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
import uuid
import weakref

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from knowmate.secure.ppt_installation import (
    DetachedSetupProcess, REQUIRED_VERSION, SetupResult, check_installation,
)

logger = logging.getLogger(__name__)
_SUPPRESS_REASONS = {"permission_declined", "mutation_failed", "launch_failed", "check_failed"}


def preparation_policy(mode: str, result: SetupResult) -> str:
    """Map the authoritative machine safety state before choosing a reader."""
    if not result.machine_safe:
        return "ppt_pending"
    if mode == "com":
        return "com_only"
    if result.status == "ready":
        return "ready"
    return "ppt_pending" if mode == "addin" else "com_only"


class PptPreparationCoordinator(QObject):
    """Defer worker construction and coalesce all starts until setup is observed."""

    _check_completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self, *, extractor: Any, extractor_mode: str, reader_mode: str,
        data_dir: Path, make_worker: Callable[[], Any], parent: QObject | None = None,
        checker: Callable[[], SetupResult] = check_installation,
        process_factory: Callable[[], Any] = DetachedSetupProcess,
        platform: str | None = None, observation_sec: float = 120,
    ) -> None:
        """Inject setup boundaries so state transitions can be tested without Office."""
        super().__init__(parent)
        self.state = "checking"
        self._extractor = extractor
        self._extractor_mode = extractor_mode
        self._reader_mode = reader_mode
        self._record = data_dir / "ppt_reader_preparation.json"
        self._make_worker = make_worker
        self._checker = checker
        self._process_factory = process_factory
        self._platform = sys.platform if platform is None else platform
        self._observation_sec = observation_sec
        self._worker: Any = None
        self._closed = False
        self._begun = False
        self._check_completed.connect(self._checked)
        self._pending_start = False
        self._retry_failures = False
        self._attempted = False
        self._after_install = False
        self._process: Any = None
        self._install_exit: int | None = None
        self._deadline = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._poll_setup)

    def begin(self) -> None:
        """Run initial checking asynchronously, skipping non-COM environments."""
        if self._closed or self._begun:
            return
        self._begun = True
        if self._extractor_mode != "auto" or self._platform != "win32":
            self._finish("com_only" if self._reader_mode == "com" else "ready")
        else:
            self._start_check()

    def request_start(self, *, retry_failures: bool = False) -> None:
        """Keep one queued cycle and preserve a broad failure retry request."""
        if self._closed:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._pending_start = True
        self._retry_failures = self._retry_failures or retry_failures
        self._drain_start()

    def cancel_start(self) -> None:
        """Cancel only a queued indexing request; an active setup keeps running."""
        self._pending_start = False
        self._retry_failures = False

    def shutdown(self) -> None:
        """Cancel callbacks and starts, releasing observation without killing setup."""
        self._closed = True
        self._pending_start = False
        self._timer.stop()
        self._close_observation()

    def _start_check(self) -> None:
        # This is a read-only setup check, never a collector. A daemon thread
        # avoids destroying a running QThread when the app exits. Completion is
        # delivered by Qt's queued signal; helper /check has its own timeout.
        reference, checker = weakref.ref(self), self._checker

        def run_check() -> None:
            try:
                value = checker()
            except Exception as exc:
                logger.warning("PPT 준비 점검 실패: %s", type(exc).__name__)
                value = SetupResult()
            owner = reference()
            if owner is not None and not owner._closed:
                try:
                    owner._check_completed.emit(value)
                except RuntimeError:
                    # The Qt parent may already have destroyed this QObject.
                    return

        threading.Thread(target=run_check, name="ppt-setup-check", daemon=True).start()

    def _checked(self, result: SetupResult) -> None:
        if self._closed:
            return
        if self._after_install:
            # An exited helper is not evidence of repaired registrations or locks.
            if self._install_exit in (11, 12, 13):
                reason = {11: "permission_declined", 12: "check_failed", 13: "mutation_failed"}[self._install_exit]
                self._save_suppression(reason)
            self._finish(preparation_policy(self._reader_mode, result))
            return
        installable = result.status in {"missing", "outdated", "repairable"}
        if (self._reader_mode != "com" and result.machine_safe and installable
                and result.auto_install_validated and not self._attempted
                and not self._suppressed()):
            self._attempted = True
            self.state = "installing"
            try:
                self._process = self._process_factory()
                self._process.start()
            except Exception as exc:
                logger.warning("PPT 준비 도구 실행 실패: %s", type(exc).__name__)
                self._save_suppression("launch_failed")
                self._close_observation()
                # Failure may have happened after creation; recheck safety first.
                self._after_install = True
                self.state = "rechecking"
                self._start_check()
                return
            self._deadline = time.monotonic() + self._observation_sec
            self._timer.start()
            return
        self._finish(preparation_policy(self._reader_mode, result))

    def _poll_setup(self) -> None:
        if self._closed or self._process is None:
            return
        try:
            exit_code = self._process.poll()
        except Exception as exc:
            logger.warning("PPT 준비 도구 종료 미확인: %s", type(exc).__name__)
            exit_code = None
            self._deadline = 0
        if exit_code is not None:
            self._timer.stop()
            self._install_exit = exit_code
            self._close_observation()
            self._after_install = True
            self.state = "rechecking"
            self._start_check()
        elif time.monotonic() >= self._deadline:
            # Stop observing only. The detached helper retains its reservation;
            # PPT must not use COM until a future run confirms machine safety.
            self._timer.stop()
            self._close_observation()
            self._finish("ppt_pending")

    def _close_observation(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                process.close()
            except Exception as exc:
                logger.warning("PPT 준비 도구 관찰 해제 실패: %s", type(exc).__name__)

    def _finish(self, policy: str) -> None:
        if self._closed or self._worker is not None:
            return
        self.state = policy
        try:
            setter = getattr(self._extractor, "set_ppt_preparation_policy", None)
            if setter is not None:
                setter(policy)
            self._worker = self._make_worker()
        except Exception as exc:
            # Worker construction now runs in a queued Qt callback, outside the
            # old synchronous main initialization try/except. Never let a Python
            # exception escape that callback and terminate the GUI event loop.
            logger.warning("수집기 초기화 실패: %s", type(exc).__name__)
            self._pending_start = False
            self.failed.emit("수집기가 초기화되지 않았습니다.")
            return
        logger.info("PPT 준비 상태: %s", policy)
        self._drain_start()

    def _drain_start(self) -> None:
        if self._closed or self._worker is None or not self._pending_start:
            return
        self._pending_start = False
        retry, self._retry_failures = self._retry_failures, False
        if not self._worker.isRunning():
            if retry and hasattr(self._worker, "request_failure_retry"):
                self._worker.request_failure_retry()
            self._worker.start()

    def _suppressed(self) -> bool:
        try:
            if self._record.stat().st_size > 4096:
                raise ValueError("record_size")
            record = json.loads(self._record.read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or set(record) != {"version", "reason"}
                    or not isinstance(record["version"], str)
                    or record["reason"] not in _SUPPRESS_REASONS):
                raise ValueError("record_contract")
            return record["version"] == REQUIRED_VERSION
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError):
            logger.warning("PPT 준비 기록 확인 실패 — 이번 실행 자동 요청 보류")
            return True

    def _save_suppression(self, reason: str) -> None:
        temporary = self._record.with_name(self._record.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            self._record.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as output:
                json.dump({"version": REQUIRED_VERSION, "reason": reason}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._record)
        except OSError as exc:
            logger.warning("PPT 준비 기록 저장 실패: %s", type(exc).__name__)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("PPT 준비 임시 기록 정리 실패")
