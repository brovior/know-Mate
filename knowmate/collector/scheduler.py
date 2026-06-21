"""QThread 기반 수집기 워커 + 유휴시간 스케줄러 (CLAUDE.md 5장 원칙8)."""
from __future__ import annotations

import heapq
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from knowmate.collector.cleanup import CleanupManager
from knowmate.collector.scanner import classify_changes, get_scope, scan_folder
from knowmate.collector.state import load_state, save_state

if TYPE_CHECKING:
    from knowmate.rag.indexer import Indexer
    from knowmate.secure.base import TextExtractor

logger = logging.getLogger(__name__)

PRIORITY_NEW = 1
PRIORITY_MODIFIED = 2
PRIORITY_ORPHAN = 3


@dataclass(order=True)
class IndexTask:
    """우선순위 큐용 태스크."""

    priority: int
    path: str = field(compare=False)
    action: str = field(compare=False)


class CollectorWorker(QThread):
    """증분 인덱싱 사이클을 QThread 워커에서 실행한다."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    indexing_needed = pyqtSignal(str)

    def __init__(self, config, indexer, extractor, state_file=None, parent=None):
        """수집기 워커를 초기화한다."""
        super().__init__(parent)
        self._config = config
        self._indexer = indexer
        self._extractor = extractor
        self._cancelled = False
        appdata = os.environ.get("APPDATA", str(Path.home()))
        default_state_file = Path(appdata) / "KnowMate" / "index_state.json"
        self._state_file = state_file or default_state_file

    def run(self):
        """증분 스캔 사이클 1회를 실행한다."""
        self._cancelled = False
        start = time.time()
        try:
            self._run_cycle()
        except Exception as exc:
            logger.exception("수집기 예외 발생: %s", exc)
            self.error.emit(str(exc))
        finally:
            elapsed = time.time() - start
            logger.info("수집기 사이클 완료: %.1f초", elapsed)

    def cancel(self):
        """취소 플래그를 설정한다. 현재 처리 중인 파일 완료 후 중단된다."""
        self._cancelled = True
        logger.info("수집기 취소 요청됨")

    def _run_cycle(self):
        """스캔 -> 분류 -> 인덱싱 -> orphan 정리 -> 저장 순으로 사이클을 실행한다."""
        from datetime import datetime, timezone
        collector_cfg = self._config.get("collector", {})
        cleanup_cfg = self._config.get("cleanup", {})
        chunk_cfg = self._config.get("chunking", {})

        watch_folders = collector_cfg.get("watch_folders", [])
        dry_run = cleanup_cfg.get("dry_run", True)
        max_delete_ratio = float(cleanup_cfg.get("max_delete_ratio", 0.30))
        chunk_size = int(chunk_cfg.get("chunk_size", 400))
        overlap = int(chunk_cfg.get("overlap", 80))

        self._indexer._chunk_size = chunk_size
        self._indexer._overlap = overlap

        state = load_state(self._state_file)
        heap = []

        for folder_str in watch_folders:
            folder = Path(folder_str)
            if not folder.exists():
                logger.warning("watch_folder 없음: %s", folder_str)
                continue
            current = scan_folder(folder)
            new_paths, mod_paths, _ = classify_changes(state, current)
            for p in new_paths:
                heapq.heappush(heap, IndexTask(PRIORITY_NEW, p, "new"))
            for p in mod_paths:
                heapq.heappush(heap, IndexTask(PRIORITY_MODIFIED, p, "modified"))

        total = len(heap)
        done = 0
        failed = []

        while heap:
            if self._cancelled:
                logger.info("수집기 취소됨")
                self.finished.emit(f"인덱싱 취소됨 ({done}/{total}건 처리 완료)")
                save_state(self._state_file, state)
                return

            task = heapq.heappop(heap)
            filename = Path(task.path).name
            done += 1
            self.progress.emit(done, total, filename)

            try:
                text = self._extractor.extract(task.path)
                stat = Path(task.path).stat()
                scope = get_scope(task.path)

                if task.action == "modified":
                    old_ids = state.get(task.path, {}).get("chunk_ids", [])
                    if old_ids:
                        self._indexer.delete_chunks(old_ids)

                chunk_ids = self._indexer.index_file(
                    path=task.path,
                    text=text,
                    mtime=stat.st_mtime,
                    scope=scope,
                )
                state[task.path] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                    "chunk_ids": chunk_ids,
                }
                logger.info("[%s] %s -> %d청크", task.action, task.path, len(chunk_ids))
            except Exception as exc:
                logger.error("파일 처리 실패 (건너뜀): %s - %s", task.path, exc)
                failed.append(task.path)

        cleanup = CleanupManager(
            indexer=self._indexer,
            max_delete_ratio=max_delete_ratio,
            dry_run=dry_run,
        )
        report = cleanup.run(watch_folders, state)

        if report.skipped_folders:
            self.indexing_needed.emit(f"일부 폴더 정리 건너뜀: {report.skipped_folders}")

        save_state(self._state_file, state)

        summary = (
            f"인덱싱 완료 - 처리 {done}건 / 실패 {len(failed)}건 / "
            f"orphan 마킹 {report.newly_marked}건 / "
            f"물리삭제 {report.physically_deleted}건"
        )
        if failed:
            logger.warning("실패 파일 목록: %s", failed)
        self.finished.emit(summary)


class IdleScheduler(QObject):
    """마지막 입력 이벤트로부터 idle_seconds 경과 시 워커를 실행한다."""

    def __init__(self, worker_factory, idle_seconds=60, parent=None):
        """스케줄러를 초기화한다."""
        super().__init__(parent)
        self._worker_factory = worker_factory
        self._idle_seconds = idle_seconds
        self._timer = QTimer(self)
        self._timer.setInterval(idle_seconds * 1000)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_idle)
        self._worker = None

    def start(self):
        """스케줄러를 시작한다."""
        self._timer.start()
        logger.info("IdleScheduler 시작 (idle=%ds)", self._idle_seconds)

    def stop(self):
        """스케줄러를 중지한다."""
        self._timer.stop()
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        logger.info("IdleScheduler 중지")

    def reset_idle(self):
        """사용자 입력 이벤트 시 유휴 타이머를 리셋한다."""
        if self._timer.isActive():
            self._timer.start()

    def _on_idle(self):
        """유휴 시간 경과 시 워커를 실행한다."""
        if self._worker and self._worker.isRunning():
            logger.debug("IdleScheduler: 이전 워커 실행 중, 건너뜀")
            return
        logger.info("IdleScheduler: 유휴 감지 -> 수집기 실행")
        self._worker = self._worker_factory()
        self._worker.finished.connect(lambda _: self._timer.start())
        self._worker.start()
