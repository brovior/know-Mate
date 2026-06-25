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

        # QThread에서 COM 사용 시 초기화 필수.
        # MTA로 초기화해야 메시지 펌프 없이 Office STA 서버를 호출할 수 있다.
        # (STA로 초기화하면 펌프 부재로 Documents.Open 등이 무한 대기)
        _com_initialized = False
        try:
            import pythoncom  # type: ignore
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            _com_initialized = True
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("COM 초기화 경고: %s", exc)

        try:
            self._run_cycle()
        except Exception as exc:
            logger.exception("수집기 예외 발생: %s", exc)
            self.error.emit(str(exc))
        finally:
            elapsed = time.time() - start
            logger.info("수집기 사이클 완료: %.1f초", elapsed)
            if _com_initialized:
                # COM 앱 Quit은 반드시 생성 스레드(여기)에서 수행해야 한다(STA)
                try:
                    from knowmate.secure.com_reader import quit_com_apps
                    quit_com_apps()
                except Exception:
                    pass
                import pythoncom  # type: ignore
                pythoncom.CoUninitialize()

    def cancel(self):
        """취소 플래그를 설정한다. 현재 처리 중인 파일 완료 후 중단된다."""
        self._cancelled = True
        logger.info("수집기 취소 요청됨")

    def _purge_removed_folders(self, watch_folders: list[str], state: dict) -> None:
        """watch_folders에 속하지 않는 청크를 LanceDB에서 직접 삭제한다.

        state.json 대신 LanceDB의 file_path 컬럼을 기준으로 삭제해
        state와 DB 불일치 상황도 처리한다.
        사용자가 명시적으로 폴더를 제거한 경우이므로 dry_run과 무관하게 즉시 삭제한다.
        """
        normalized = [f.replace("\\", "/").rstrip("/") for f in watch_folders]

        def belongs_to_any(path_str: str) -> bool:
            p = path_str.replace("\\", "/")
            return any(p.startswith(w + "/") or p == w for w in normalized)

        # state에서 제거된 폴더 항목 정리
        stale_state_paths = [p for p in list(state.keys()) if not belongs_to_any(p)]
        for p in stale_state_paths:
            state.pop(p, None)

        # LanceDB에서 현재 file_path 목록 조회 후 직접 삭제
        try:
            df = self._indexer.table.to_arrow().to_pandas()
        except Exception as exc:
            logger.warning("[purge] DB 조회 실패: %s", exc)
            return

        if df.empty:
            return

        # watch_folders에 속하지 않는 file_path 추출
        stale_mask = ~df["file_path"].apply(belongs_to_any)
        stale_paths_db = df.loc[stale_mask, "file_path"].unique().tolist()

        if not stale_paths_db:
            return

        logger.info("[purge] 제거된 폴더 DB 청크 정리: %d개 경로", len(stale_paths_db))

        # 경로별로 삭제 (SQL 길이 제한 방지)
        any_deleted = False
        for path_str in stale_paths_db:
            try:
                safe = path_str.replace("'", "''")
                self._indexer.table.delete(f"file_path = '{safe}'")
                any_deleted = True
                logger.info("[purge] 삭제 완료: %s", path_str)
            except Exception as exc:
                logger.error("[purge] 삭제 실패: %s - %s", path_str, exc)

        if any_deleted:
            try:
                self._indexer.optimize()
            except Exception as exc:
                logger.warning("[purge] optimize 실패: %s", exc)

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
        max_file_size_mb = float(chunk_cfg.get("max_file_size_mb", 30.0))
        max_chunks_per_file = int(chunk_cfg.get("max_chunks_per_file", 500))
        xlsx_max_rows_per_sheet = int(chunk_cfg.get("xlsx_max_rows_per_sheet", 2000))

        self._indexer._chunk_size = chunk_size
        self._indexer._overlap = overlap
        self._indexer._max_chunks_per_file = max_chunks_per_file
        self._indexer._xlsx_max_rows_per_sheet = xlsx_max_rows_per_sheet

        state = load_state(self._state_file)
        heap = []

        for folder_str in watch_folders:
            folder = Path(folder_str)
            if not folder.exists():
                logger.warning("watch_folder 없음: %s", folder_str)
                continue
            current = scan_folder(folder, max_file_size_mb=max_file_size_mb)
            new_paths, mod_paths, _ = classify_changes(state, current)
            for p in new_paths:
                heapq.heappush(heap, IndexTask(PRIORITY_NEW, p, "new"))
            for p in mod_paths:
                heapq.heappush(heap, IndexTask(PRIORITY_MODIFIED, p, "modified"))

        total = len(heap)
        done = 0
        failed = []

        # 시작 시 한 번 알림 (total=0 이면 즉시 완료 흐름으로 넘어감)
        self.progress.emit(0, total, "스캔 완료" if total == 0 else "")

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
                logger.debug("[단계1] 텍스트 추출 시작: %s", task.path)
                text = self._extractor.extract(task.path)
                logger.debug("[단계2] 텍스트 추출 완료: %s (%d자)", task.path, len(text))
                stat = Path(task.path).stat()
                scope = get_scope(task.path)

                if task.action == "modified":
                    old_ids = state.get(task.path, {}).get("chunk_ids", [])
                    if old_ids:
                        logger.debug("[단계3] 기존 청크 삭제: %d개", len(old_ids))
                        self._indexer.delete_chunks(old_ids)

                logger.debug("[단계4] 임베딩·저장 시작: %s", task.path)
                chunk_ids = self._indexer.index_file(
                    path=task.path,
                    text=text,
                    mtime=stat.st_mtime,
                    scope=scope,
                )
                logger.debug("[단계5] 임베딩·저장 완료: %s -> %d청크", task.path, len(chunk_ids))
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

        # watch_folders에서 제거된 폴더의 청크를 즉시 정리한다
        self._purge_removed_folders(watch_folders, state)

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
    """유휴 시간 경과 시 인덱싱을 트리거한다.

    단일 워커를 공유하기 위해 워커를 직접 생성하지 않고,
    trigger/is_busy 콜백으로 외부 워커를 제어한다.
    이미 인덱싱(수동/유휴 무관)이 진행 중이면 건너뛴다.
    """

    def __init__(self, trigger, is_busy, idle_seconds=60, parent=None):
        """스케줄러를 초기화한다.

        trigger: () -> None, 인덱싱을 시작하는 콜백
        is_busy: () -> bool, 인덱싱이 진행 중이면 True를 반환하는 콜백
        """
        super().__init__(parent)
        self._trigger = trigger
        self._is_busy = is_busy
        self._idle_seconds = idle_seconds
        self._timer = QTimer(self)
        self._timer.setInterval(idle_seconds * 1000)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_idle)

    def start(self):
        """스케줄러를 시작한다."""
        self._timer.start()
        logger.info("IdleScheduler 시작 (idle=%ds)", self._idle_seconds)

    def stop(self):
        """스케줄러를 중지한다."""
        self._timer.stop()
        logger.info("IdleScheduler 중지")

    def reset_idle(self):
        """사용자 입력 이벤트 시 유휴 타이머를 리셋한다."""
        if self._timer.isActive():
            self._timer.start()

    def _on_idle(self):
        """유휴 시간 경과 시 인덱싱을 트리거한다. 진행 중이면 건너뛰고 재예약한다."""
        if self._is_busy():
            logger.debug("IdleScheduler: 인덱싱 진행 중, 건너뜀")
            self._timer.start()  # 다음 사이클에 재시도
            return
        logger.info("IdleScheduler: 유휴 감지 -> 수집기 실행")
        try:
            self._trigger()
        finally:
            self._timer.start()  # 다음 유휴 사이클 예약
