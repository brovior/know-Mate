"""QThread 기반 수집기 워커 + 유휴시간 스케줄러 (CLAUDE.md 5장 원칙8)."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from knowmate.collector.cleanup import CleanupManager
from knowmate.collector.scanner import get_scope, iter_scan_folder
from knowmate.collector.state import load_state, save_state
from knowmate.secure.office_guard import OfficeBusyError
from knowmate.secure.signature import UnreadableFormatError, is_ole2, is_zip

if TYPE_CHECKING:
    from knowmate.rag.indexer import Indexer
    from knowmate.rag.email_indexer import EmailIndexer
    from knowmate.secure.base import TextExtractor

logger = logging.getLogger(__name__)

PRIORITY_NEW = 1
PRIORITY_MODIFIED = 2
PRIORITY_ORPHAN = 3

# state.json에 캐시된 추출 방식(method)에 따른 큐 내 우선순위 보정.
# COM 경유(구형 바이너리·DRM 래핑) 파일은 사용자 세션(DRM/SSO)이 유효한
# 유휴 초입에 먼저 처리되도록 같은 action 우선순위 내에서 앞당긴다.
_COM_RANK = 0
_PLAIN_RANK = 1

# 큐 종료 신호의 정렬 키 — 모든 실제 항목((PRIORITY_ORPHAN, _PLAIN_RANK)까지)보다
# 항상 낮은 우선순위라 스캔이 끝난 뒤 넣어도 실제 항목보다 먼저 소비되지 않는다.
_SENTINEL_KEY = (PRIORITY_ORPHAN + 1, _PLAIN_RANK + 1)

# state 캐싱용 방식 분류 — AutoReader의 실제 라우팅 판단(secure/__init__.py)과
# 동일한 기준(확장자 + zip 서명)을 재사용한다. extractor 모드가 auto가 아니어도
# (fake/plain) 무해 — 다음 사이클 큐 우선순위 힌트로만 쓰이고 실제 추출
# 경로에는 전혀 영향을 주지 않는다.
_COM_EXTS = {".doc", ".xls", ".ppt"}
_OOXML_EXTS = {".docx", ".xlsx", ".pptx"}


def _classify_extract_method(path: str) -> str:
    """다음 사이클 우선순위 힌트로 쓸 추출 방식을 분류한다 ('com' | 'plain')."""
    ext = Path(path).suffix.lower()
    if ext in _COM_EXTS:
        return "com"
    if ext in _OOXML_EXTS and not is_zip(path):
        return "com"
    return "plain"


def _is_drm_suspected(path: str) -> bool:
    """DRM 세션 만료 시 COM Open이 무의미(또는 로그인 대기로 위험)할 수 있는
    파일인지 판별한다.

    정상 레거시 바이너리(OLE2 매직)나 정상 OOXML(zip 매직)은 COM을 타더라도
    (전자는 항상, 후자는 오라벨·DRM일 때만) 실제 파일 자체가 온전하므로
    DRM 세션 여부와 무관하게 열린다 — 스킵 대상이 아니다. 두 시그니처 중
    어느 것도 아닌 파일만 DRM 래핑으로 의심해 스킵 후보로 삼는다.
    """
    ext = Path(path).suffix.lower()
    if ext in _COM_EXTS:
        return not is_ole2(path)
    if ext in _OOXML_EXTS:
        return not is_zip(path)
    return False


@dataclass(order=True)
class IndexTask:
    """우선순위 큐용 태스크."""

    priority: int
    path: str = field(compare=False)
    action: str = field(compare=False)
    com_rank: int = field(default=_PLAIN_RANK, compare=False)
    size: int = field(default=0, compare=False)  # COM 워치독 타임아웃(크기 비례) 산정용


def _com_timeout_for_size(size_bytes: int, base: float, per_mb: float, cap: float) -> float:
    """파일 크기에 비례한 COM 추출 타임아웃(초)을 반환한다.

    크기가 파싱 시간과 완벽 비례하진 않지만(시트 수·수식 복잡도 변수) 충분한
    근사다. 작은 파일의 행오버는 빨리(base) 잡고, 셀 단위 COM 왕복이 느린 대형
    파일은 넉넉히(base + per_mb×MB) 보호하되 cap으로 상한을 둔다.
    """
    mb = max(size_bytes, 0) / (1024 * 1024)
    return min(base + per_mb * mb, cap)


class CollectorWorker(QThread):
    """증분 인덱싱 사이클을 QThread 워커에서 실행한다."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    indexing_needed = pyqtSignal(str)

    def __init__(self, config, indexer, extractor, state_file=None, email_indexer=None,
                 parent=None, get_idle_seconds=None):
        """수집기 워커를 초기화한다.

        get_idle_seconds: () -> float, 현재 OS 유휴 경과초 조회(테스트 주입용,
            기본은 collector.idle_util.get_idle_seconds). DRM 세션 만료 추정에
            쓴다 — 사이클 시작 시 1회가 아니라 파일 처리 중 실시간으로 조회해,
            긴 사이클 도중 세션이 만료되면 그 시점부터 DRM 문서를 건너뛴다.
        """
        super().__init__(parent)
        self._config = config
        self._indexer = indexer
        self._extractor = extractor
        self._email_indexer = email_indexer
        self._cancelled = False
        if get_idle_seconds is None:
            from knowmate.collector.idle_util import get_idle_seconds as _default
            get_idle_seconds = _default
        self._get_idle_seconds = get_idle_seconds
        from knowmate.config import get_data_dir
        default_state_file = get_data_dir() / "index_state.json"
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

    def _purge_removed_folders(
        self, watch_folders: list[str], state: dict, dry_run: bool = True,
        max_delete_ratio: float = 0.30,
    ) -> None:
        """watch_folders에 속하지 않는 청크를 LanceDB에서 직접 삭제한다.

        state.json 대신 LanceDB의 file_path 컬럼을 기준으로 삭제해
        state와 DB 불일치 상황도 처리한다.

        안전장치:
        - watch_folders가 비어 있으면(온보딩 전·config 초기화 직후 등) 아무것도
          "제거된 폴더"로 간주하지 않고 즉시 건너뛴다. 빈 목록을 "전부 삭제"로
          해석하지 않는다.
        - dry_run=True이면 state·DB 어느 쪽도 변경하지 않는다(완전한 예행연습).
          기존에는 dry_run이어도 state 항목이 먼저 지워지는 버그가 있었다.
        - 삭제 대상이 전체 인덱스의 max_delete_ratio를 초과하면 CleanupManager와
          동일한 대량 삭제 차단을 적용한다(이 함수엔 원래 이 안전장치가 없었다).
        """
        if not watch_folders:
            logger.info("[purge] watch_folders 비어 있음 — 정리 건너뜀 (전체 삭제 오판 방지)")
            return

        normalized = [f.replace("\\", "/").rstrip("/") for f in watch_folders]

        def belongs_to_any(path_str: str) -> bool:
            p = path_str.replace("\\", "/")
            return any(p.startswith(w + "/") or p == w for w in normalized)

        # LanceDB에서 현재 file_path 목록 조회
        try:
            df = self._indexer.table.to_arrow().to_pandas()
        except Exception as exc:
            logger.warning("[purge] DB 조회 실패: %s", exc)
            return

        if df.empty:
            return

        total_indexed = df["file_path"].nunique()
        stale_mask = ~df["file_path"].apply(belongs_to_any)
        stale_paths_db = df.loc[stale_mask, "file_path"].unique().tolist()

        if not stale_paths_db:
            return

        # 대량 삭제 차단 (CleanupManager.run()의 안전장치와 대칭)
        ratio = len(stale_paths_db) / total_indexed if total_indexed else 1.0
        if ratio > max_delete_ratio:
            logger.error(
                "[purge] 대량 삭제 차단 (%.0f%% > %.0f%%): %d/%d개 경로. "
                "watch_folders 설정을 확인하세요: %s",
                ratio * 100, max_delete_ratio * 100,
                len(stale_paths_db), total_indexed, watch_folders,
            )
            self.indexing_needed.emit(
                f"대량 삭제가 감지되어 정리를 건너뛰었습니다 "
                f"({len(stale_paths_db)}/{total_indexed}개 경로). watch_folders 설정을 확인하세요."
            )
            return

        if dry_run:
            logger.info(
                "[purge][dry-run] 제거된 폴더 DB 청크 정리 대상 %d개 경로 (실제 삭제 생략, state도 미변경): %s",
                len(stale_paths_db),
                stale_paths_db,
            )
            return

        # 실제 삭제 시에만 state에서도 제거 (dry_run 시엔 state를 건드리지 않는다)
        stale_state_paths = [p for p in list(state.keys()) if not belongs_to_any(p)]
        for p in stale_state_paths:
            state.pop(p, None)

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

        # 유휴가 이 임계를 넘으면 DRM/SSO 세션이 만료됐을 가능성이 크다고 보고,
        # DRM 래핑으로 의심되는 파일(COM Open이 실패하거나 로그인 대기로 멈출 수
        # 있음)만 건너뛴다. 정상 레거시(OLE2)·정상 OOXML(zip) 문서는 COM을 타더라도
        # 세션과 무관하게 동작하므로 대상이 아니다.
        #
        # 판정은 사이클 시작 1회가 아니라 **파일 처리 중 실시간**(_get_idle_seconds)
        # 으로 한다 — 수동 재인덱싱(유휴 0으로 시작)도, 밤새 도는 긴 사이클도, 심지어
        # 사용자가 도중에 복귀(유휴 리셋)한 경우도 그 시점의 실제 유휴로 올바르게
        # 판정된다(0이면 판정 비활성).
        drm_idle_threshold_sec = float(collector_cfg.get("drm_idle_threshold_sec", 480))

        # COM 추출 행오버 방지 워치독 — Office Open/셀 순회가 멈추면 그 파일 크기에
        # 비례한 시간(base + per_mb×MB, cap 상한) 뒤 해당 Office 프로세스를 강제
        # 종료해 블로킹을 풀고 그 파일만 실패 처리한다(사이클은 계속). base<=0이면 비활성.
        com_timeout_base = float(collector_cfg.get("com_timeout_base_sec", 60))
        com_timeout_per_mb = float(collector_cfg.get("com_timeout_per_mb_sec", 20))
        com_timeout_cap = float(collector_cfg.get("com_timeout_max_sec", 600))

        self._indexer._chunk_size = chunk_size
        self._indexer._overlap = overlap
        self._indexer._max_chunks_per_file = max_chunks_per_file
        self._indexer._xlsx_max_rows_per_sheet = xlsx_max_rows_per_sheet

        state = load_state(self._state_file)

        # ── 스캔·인덱싱 파이프라인 (하이브리드) ──────────────────────────
        # 생산자 스레드: 폴더를 walk 하며 신규/변경 파일을 큐에 넣는다(os.walk+stat만).
        # 소비자(현재 스레드): 큐에서 꺼내 즉시 인덱싱(추출·임베딩·LanceDB 쓰기는 여기서만).
        # 열거 완료 전엔 total 미정(-2)으로, 완료 후엔 확정 total 로 진행률을 emit 한다.
        #
        # 우선순위 큐: (action우선순위, com_rank) 오름차순 — NEW가 MODIFIED보다,
        # COM 경유(전 사이클에 method='com'으로 기록된) 파일이 같은 action 내에서
        # plain보다 먼저 처리된다. 정렬 키에 단조증가 seq를 끼워 넣어 IndexTask나
        # 종료 신호(_SENTINEL)끼리 직접 비교되는 일이 없도록 한다(타입 비교 오류 방지).
        # 종료 신호는 스캔이 끝난 뒤에만 최저 우선순위로 넣으므로, 그 시점까지 큐에
        # 들어온 모든 실제 항목보다 항상 나중에 소비된다.
        import itertools
        import queue as _queue
        import threading as _threading

        task_queue: "_queue.PriorityQueue" = _queue.PriorityQueue()
        _SENTINEL = None
        _seq = itertools.count()
        producer_state = {"total": None, "seen": set(), "drm_deferred": 0, "drm_skip_logged": False}

        # COM 추출 행오버 워치독 (base<=0이면 비활성)
        from knowmate.collector.com_watchdog import ComWatchdog
        from knowmate.secure import office_guard as _og
        watchdog = ComWatchdog(terminate_fn=_og.terminate_stuck_office) if com_timeout_base > 0 else None

        logger.info("[collector] 작업 시작 — 폴더 스캔·인덱싱 파이프라인")
        self.progress.emit(0, -2, "인덱싱 시작...")

        def _producer() -> None:
            from knowmate.rag.indexer import DOC_INDEX_VERSION
            found = 0
            migrate_logged = False
            try:
                for folder_str in watch_folders:
                    folder = Path(folder_str)
                    if not folder.exists():
                        logger.warning("watch_folder 없음: %s", folder_str)
                        continue
                    for path, meta in iter_scan_folder(
                        folder,
                        max_file_size_mb=max_file_size_mb,
                        cancel_check=lambda: self._cancelled,
                    ):
                        if self._cancelled:
                            break
                        producer_state["seen"].add(path)
                        prev = state.get(path)
                        if prev is None:
                            action = "new"
                        elif meta["mtime"] != prev.get("mtime") or meta["size"] != prev.get("size"):
                            action = "modified"
                        elif prev.get("index_version") != DOC_INDEX_VERSION:
                            # 인덱싱 포맷 변경 → 1회 자동 재인덱싱
                            if not migrate_logged:
                                logger.info("[collector] 문서 인덱싱 포맷 변경 감지 — 기존 문서 재인덱싱 시작")
                                migrate_logged = True
                            action = "modified"
                        else:
                            continue  # 변경 없음 → 인덱싱 대상 아님
                        # 실시간 유휴 판정: 값싼 유휴 조회(_get_idle_seconds)를 먼저 하고,
                        # 임계를 넘었을 때만 파일 시그니처(_is_drm_suspected, 파일 읽기)를
                        # 확인한다. 사이클 시작 1회가 아니라 파일마다 현재 유휴를 보므로,
                        # 긴 사이클 도중 세션이 만료되면 그 시점부터 자동으로 스킵된다.
                        if (
                            drm_idle_threshold_sec > 0
                            and self._get_idle_seconds() >= drm_idle_threshold_sec
                            and _is_drm_suspected(path)
                        ):
                            # 유휴가 길어 DRM 세션 만료가 의심되는 상황 — COM Open이
                            # 실패하거나 로그인 대기로 멈출 수 있어 큐에 넣지 않는다.
                            # state 불변 → 다음 유효 사이클(세션 살아있을 때)에 재시도.
                            if not producer_state["drm_skip_logged"]:
                                logger.info(
                                    "[collector] 유휴 %.0fs >= DRM 임계 %.0fs — 이 시점부터 "
                                    "DRM 의심 문서 스킵(세션 만료 추정)",
                                    self._get_idle_seconds(), drm_idle_threshold_sec,
                                )
                                producer_state["drm_skip_logged"] = True
                            producer_state["drm_deferred"] += 1
                            continue
                        priority = PRIORITY_NEW if action == "new" else PRIORITY_MODIFIED
                        # 이전 사이클에 COM 경유로 기록된 파일이면 우선순위를 앞당긴다
                        # (DRM/구형 바이너리 — 세션 유효한 유휴 초입에 먼저 처리).
                        # 처음 보는 파일은 힌트가 없어 plain과 동일하게 취급된다.
                        com_rank = _COM_RANK if (prev or {}).get("method") == "com" else _PLAIN_RANK
                        sort_key = (priority, com_rank)
                        task_queue.put((
                            sort_key, next(_seq),
                            IndexTask(priority, path, action, com_rank, size=meta.get("size", 0)),
                        ))
                        found += 1
                    if self._cancelled:
                        break
            except Exception as exc:
                logger.exception("[collector] 스캔 생산자 예외: %s", exc)
            finally:
                producer_state["total"] = found  # 확정 총계
                task_queue.put((_SENTINEL_KEY, next(_seq), _SENTINEL))  # 소비자 종료 신호(항상 마지막)

        producer = _threading.Thread(target=_producer, name="scan-producer", daemon=True)
        producer.start()

        done = 0
        failed = []
        deferred = []
        unreadable = []

        while True:
            _sort_key, _seq_no, task = task_queue.get()
            if task is _SENTINEL:
                break
            if self._cancelled:
                logger.info("수집기 취소됨")
                self.finished.emit(f"인덱싱 취소됨 ({done}건 처리 완료)")
                producer.join(timeout=5)
                save_state(self._state_file, state)
                return

            filename = Path(task.path).name
            done += 1
            # 생산자 완료 시 확정 total(>0), 진행 중이면 -2(총계 미정)
            total_known = producer_state["total"]
            self.progress.emit(done, total_known if total_known is not None else -2, filename)

            try:
                logger.debug("[단계1] 텍스트 추출 시작: %s", task.path)
                _extract_t0 = time.perf_counter()
                # COM 경유 파일만 워치독 무장(plain은 COM을 안 타 무의미하고, 느린
                # openpyxl 파싱 중 애먼 Office를 죽이는 오발을 피함).
                _wd_exe = None
                if watchdog is not None and _classify_extract_method(task.path) == "com":
                    _wd_exe = _og.process_for_ext(Path(task.path).suffix.lower())
                try:
                    if _wd_exe:
                        _timeout = _com_timeout_for_size(
                            task.size, com_timeout_base, com_timeout_per_mb, com_timeout_cap
                        )
                        _og.begin_com_op(_wd_exe)
                        watchdog.arm(_wd_exe, _timeout)
                    text = self._extractor.extract(task.path)
                finally:
                    if _wd_exe:
                        watchdog.disarm()
                        _og.end_com_op()
                extract_sec = time.perf_counter() - _extract_t0
                logger.debug("[단계2] 텍스트 추출 완료: %s (%d자, %.2fs)", task.path, len(text), extract_sec)
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
                from knowmate.rag.indexer import DOC_INDEX_VERSION
                # 다음 사이클 큐 우선순위 힌트용 — COM 경유 파일이었는지 기록
                # (실제 추출 경로와 무관하게 확장자·zip 서명으로 분류하는 저비용 재확인).
                method = _classify_extract_method(task.path)
                state[task.path] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                    "chunk_ids": chunk_ids,
                    "index_version": DOC_INDEX_VERSION,
                    "method": method,
                }
                logger.info(
                    "[%s] %s -> %d청크 (extract=%.2fs)",
                    task.action, task.path, len(chunk_ids), extract_sec,
                )
            except OfficeBusyError as exc:
                # 사용자가 Office를 열어둔 상태 → 이번 사이클만 연기(실패 아님).
                # state를 갱신하지 않으므로 다음 유휴 사이클에서 자동 재시도된다.
                logger.warning("[collector] Office 점유로 연기(다음 사이클 재시도): %s", exc)
                deferred.append(task.path)
            except UnreadableFormatError as exc:
                # OOXML 확장자이나 zip 아님(DRM 래핑·손상 등)에 COM도 불가한 경우.
                # 일반 실패와 구분해 로그·요약에 표시 — "버그"가 아니라 DRM/손상임을 알림.
                logger.warning("[collector] 판독불가(DRM/암호화·손상 추정): %s", exc)
                unreadable.append(task.path)
            except Exception as exc:
                logger.error("파일 처리 실패 (건너뜀): %s - %s", task.path, exc)
                failed.append(task.path)

        # 생산자 스레드 정리 (정상 종료 시 이미 끝나 있음)
        producer.join(timeout=5)

        # watch_folders에서 제거된 폴더의 청크를 정리한다 (dry_run·대량삭제차단 준수)
        self._purge_removed_folders(
            watch_folders, state, dry_run=dry_run, max_delete_ratio=max_delete_ratio,
        )

        cleanup = CleanupManager(
            indexer=self._indexer,
            max_delete_ratio=max_delete_ratio,
            dry_run=dry_run,
        )
        report = cleanup.run(watch_folders, state)

        if report.skipped_folders:
            self.indexing_needed.emit(f"일부 폴더 정리 건너뜀: {report.skipped_folders}")

        save_state(self._state_file, state)

        # 메일 스캔 (.mysingle) — mail.enabled: true 일 때만
        mail_indexed = 0
        if self._email_indexer and self._config.get("mail", {}).get("enabled", False):
            from knowmate.collector.mail_scanner import run_mail_scan
            try:
                mail_indexed, _ = run_mail_scan(
                    watch_folders, self._email_indexer, self._config,
                    on_progress=lambda cur, tot, fn: self.progress.emit(cur, tot, fn),
                )
            except Exception as exc:
                logger.error("[mail_scanner] 메일 스캔 실패: %s", exc)

        summary = (
            f"인덱싱 완료 - 처리 {done}건 / 실패 {len(failed)}건 / "
            f"orphan 마킹 {report.newly_marked}건 / "
            f"물리삭제 {report.physically_deleted}건"
        )
        if mail_indexed:
            summary += f" / 메일 {mail_indexed}건"
        if deferred:
            summary += f" / Office 점유로 연기 {len(deferred)}건"
        if unreadable:
            summary += f" / 판독불가 {len(unreadable)}건"
        drm_deferred_count = producer_state.get("drm_deferred", 0)
        if drm_deferred_count:
            summary += f" / 유휴로 DRM 문서 스킵 {drm_deferred_count}건"
        com_timeout_count = watchdog.timeout_count if watchdog is not None else 0
        if com_timeout_count:
            summary += f" / COM 시간초과 강제해제 {com_timeout_count}건"
        if failed:
            logger.warning("실패 파일 목록: %s", failed)
        if deferred:
            logger.info("Office 점유로 연기된 파일 %d건(다음 사이클 재시도)", len(deferred))
        if unreadable:
            logger.info("판독불가(DRM/암호화·손상 추정) 파일 %d건: %s", len(unreadable), unreadable)
        if drm_deferred_count:
            logger.info(
                "DRM 세션 만료 추정으로 DRM 의심 문서 %d건 스킵(활동 재개·세션 유효 시 재시도)",
                drm_deferred_count,
            )
        if com_timeout_count:
            logger.warning(
                "COM 추출 행오버 %d건을 타임아웃으로 강제 해제(해당 파일은 실패·다음 사이클 재시도)",
                com_timeout_count,
            )
        self.finished.emit(summary)


# 실제 유휴에 못 미칠 때 다음 재확인까지 최소 대기(너무 촘촘한 재확인 방지)
_MIN_RECHECK_SECONDS = 5.0

# 복귀 감지 워처(Phase D)의 폴링 간격과 "방금 활동함" 판정 임계.
# _RECOVERY_ACTIVE_SECONDS는 이 워처의 폴링 간격보다 작아야 "막 돌아왔다"는
# 신호로 유효하다(그렇지 않으면 유휴 지속 중에도 우연히 낮게 잡힐 수 있음).
_RECOVERY_POLL_SECONDS = 15.0
_RECOVERY_ACTIVE_SECONDS = 10.0


class IdleScheduler(QObject):
    """실제 OS 유휴 시간이 임계를 넘었을 때만 인덱싱을 트리거한다.

    이전 구현은 idle_seconds마다 무조건 도는 주기 타이머였다(사용자가
    작업 중이어도 트리거됨 — 실제 유휴 감지가 없었다). 이제 타이머가
    만료돼도 GetLastInputInfo(읽기전용)로 실제 유휴 시간을 확인해, 임계에
    못 미치면 트리거하지 않고 남은 시간만큼만 재예약한다.

    단일 워커를 공유하기 위해 워커를 직접 생성하지 않고,
    trigger/is_busy 콜백으로 외부 워커를 제어한다.
    이미 인덱싱(수동/유휴 무관)이 진행 중이면 건너뛴다.

    복귀 감지(Phase D): 유휴가 `drm_idle_threshold_sec`를 넘으면(그 사이클들에서
    DRM 의심 문서가 스킵됐을 가능성) 별도의 경량 폴링 워처가 그 사실을 기억해
    두었다가, 사용자 활동이 재개된 직후(유휴가 다시 짧아진 시점) idle_elapsed=0으로
    즉시 한 번 트리거한다 — DRM 스킵이 걸리지 않는 정상 사이클이라 밀린 DRM
    문서가 자연히 캐치업된다. 다음 정기 유휴 사이클(최대 idle_seconds 뒤)까지
    기다리지 않기 위함.
    """

    def __init__(
        self, trigger, is_busy, idle_seconds=60, parent=None, get_idle_seconds=None,
        drm_idle_threshold_sec=480.0,
    ):
        """스케줄러를 초기화한다.

        trigger: () -> None, 인덱싱을 시작하는 콜백. DRM 스킵 판단(Phase C)은
            워커가 사이클 도중 실시간 유휴를 직접 조회하므로 여기서 유휴 값을
            넘기지 않는다.
        is_busy: () -> bool, 인덱싱이 진행 중이면 True를 반환하는 콜백
        get_idle_seconds: () -> float, 실제 OS 유휴 경과초 조회(테스트 주입용,
            기본은 collector.idle_util.get_idle_seconds)
        drm_idle_threshold_sec: 이 이상 유휴가 지속됐다가 활동이 재개되면
            복귀 캐치업 트리거를 1회 발동한다(collector.drm_idle_threshold_sec와
            동일 값을 넘겨 일관성을 유지해야 함).
        """
        super().__init__(parent)
        self._trigger = trigger
        self._is_busy = is_busy
        self._idle_seconds = idle_seconds
        self._drm_idle_threshold_sec = drm_idle_threshold_sec
        if get_idle_seconds is None:
            from knowmate.collector.idle_util import get_idle_seconds as _default
            get_idle_seconds = _default
        self._get_idle_seconds = get_idle_seconds
        self._timer = QTimer(self)
        self._timer.setInterval(idle_seconds * 1000)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_idle)

        # 복귀 감지 워처 — idle_seconds 디바운스와 독립적으로 계속 도는 경량 폴링
        self._was_long_idle = False
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setInterval(int(_RECOVERY_POLL_SECONDS * 1000))
        self._recovery_timer.timeout.connect(self._on_recovery_check)

    def start(self):
        """스케줄러를 시작한다."""
        self._timer.setInterval(self._idle_seconds * 1000)
        self._timer.start()
        self._was_long_idle = False
        self._recovery_timer.start()
        logger.info("IdleScheduler 시작 (idle=%ds)", self._idle_seconds)

    def stop(self):
        """스케줄러를 중지한다."""
        self._timer.stop()
        self._recovery_timer.stop()
        logger.info("IdleScheduler 중지")

    def reset_idle(self):
        """호환용: 유휴 타이머를 즉시 재시작한다.

        실제 유휴 판정은 이제 OS 조회(_get_idle_seconds)로 하므로 필수는
        아니지만, 사용자 활동을 안 시점에 다음 재확인을 즉시 원위치로
        되돌리고 싶을 때 호출할 수 있다.
        """
        if self._timer.isActive():
            self._timer.setInterval(self._idle_seconds * 1000)
            self._timer.start()

    def _on_idle(self):
        """타이머 만료 시 실제 유휴 시간을 확인해 임계 이상이면 트리거한다.

        진행 중이면 건너뛰고 재예약. 임계에 못 미치면(사용자가 작업 중)
        남은 시간만큼만 재확인을 예약해 계속 폴링하지 않는다.
        """
        if self._is_busy():
            logger.debug("IdleScheduler: 인덱싱 진행 중, 건너뜀")
            self._timer.setInterval(self._idle_seconds * 1000)
            self._timer.start()
            return

        actual_idle = self._get_idle_seconds()
        if actual_idle < self._idle_seconds:
            remaining = self._idle_seconds - actual_idle
            wait = max(remaining, _MIN_RECHECK_SECONDS)
            logger.debug(
                "IdleScheduler: 실제 유휴 %.0fs < 임계 %ds(사용자 활동 중) — %.0fs 후 재확인",
                actual_idle, self._idle_seconds, wait,
            )
            self._timer.setInterval(int(wait * 1000))
            self._timer.start()
            return

        logger.info("IdleScheduler: 유휴 감지(%.0fs) -> 수집기 실행", actual_idle)
        self._timer.setInterval(self._idle_seconds * 1000)
        try:
            self._trigger()
        finally:
            self._timer.start()  # 다음 유휴 사이클 예약

    def _on_recovery_check(self):
        """장시간 유휴 뒤 활동 재개 순간을 포착해 DRM 캐치업 사이클을 즉시 트리거한다.

        idle_seconds 디바운스와 무관하게 독립적으로 폴링한다 — 그래야 다음
        정기 유휴 사이클까지 기다리지 않고 복귀 직후 즉시 캐치업할 수 있다.
        """
        if self._is_busy():
            return  # 진행 중인 사이클이 끝난 뒤 다음 폴링에서 재판단

        current = self._get_idle_seconds()
        if current >= self._drm_idle_threshold_sec:
            self._was_long_idle = True
            return

        if self._was_long_idle and current < _RECOVERY_ACTIVE_SECONDS:
            self._was_long_idle = False
            logger.info(
                "IdleScheduler: 장시간 유휴(DRM 스킵 추정) 후 활동 재개 감지 "
                "-> DRM 캐치업 사이클 즉시 실행"
            )
            # 활동이 막 재개돼 유휴가 낮으므로, 워커가 실시간 유휴를 조회하면
            # DRM 스킵 없이 밀린 DRM 문서를 정상 처리한다.
            self._trigger()
