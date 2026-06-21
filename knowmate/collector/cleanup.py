"""Orphan 정리 + 안전장치 모듈 (CLAUDE.md 8장 명세 전부 구현)."""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowmate.rag.indexer import Indexer

logger = logging.getLogger(__name__)


@dataclass
class CleanupReport:
    """한 사이클의 orphan 정리 결과."""

    scanned: int = 0
    newly_marked: int = 0          # soft delete 마킹된 청크 수
    physically_deleted: int = 0    # 물리 삭제된 청크 수
    skipped_folders: list[str] = field(default_factory=list)


class CleanupManager:
    """orphan 정리를 담당한다. 8장 안전장치를 전부 구현한다."""

    def __init__(
        self,
        indexer: "Indexer",
        max_delete_ratio: float = 0.30,
        dry_run: bool = True,
    ) -> None:
        """초기화. dry_run=True 면 실제 삭제 없이 로그만 출력한다."""
        self._indexer = indexer
        self._max_delete_ratio = max_delete_ratio
        self._dry_run = dry_run

    # ──────────────────────────────────────────────────────
    # public API
    # ──────────────────────────────────────────────────────

    def run(self, watch_folders: list[str], state: dict) -> CleanupReport:
        """전체 watch_folders 에 대해 orphan 정리를 수행하고 CleanupReport 를 반환한다."""
        report = CleanupReport()
        any_physical = False

        for folder_str in watch_folders:
            folder = Path(folder_str)

            # 안전장치 1: 폴더 루트 가드
            if not self._folder_accessible(folder):
                logger.warning(
                    "[cleanup] 폴더 접근 불가 — 건너뜀: %s", folder_str
                )
                report.skipped_folders.append(folder_str)
                continue

            # 해당 폴더 소속 orphan 탐색
            folder_orphans = self._find_orphans(folder_str, state)
            folder_indexed = self._count_indexed(folder_str, state)

            report.scanned += folder_indexed

            if not folder_orphans:
                continue

            # 안전장치 2: 대량 삭제 차단기
            if folder_indexed > 0:
                ratio = len(folder_orphans) / folder_indexed
            else:
                ratio = 1.0

            if ratio > self._max_delete_ratio:
                logger.error(
                    "[cleanup] 대량 삭제 차단 (%.0f%% > %.0f%%): %s",
                    ratio * 100,
                    self._max_delete_ratio * 100,
                    folder_str,
                )
                report.skipped_folders.append(folder_str)
                continue

            # orphan 처리
            marked, deleted = self._process_orphans(folder_orphans, state)
            report.newly_marked += marked
            report.physically_deleted += deleted
            if deleted > 0:
                any_physical = True

        # 안전장치 4: 물리 삭제가 있으면 optimize()
        if any_physical and not self._dry_run:
            self._indexer.optimize()

        # 안전장치 6: 사이클 리포트
        logger.info(
            "[cleanup] 완료 — 스캔 %d / 마킹 %d / 물리삭제 %d / 스킵폴더 %s",
            report.scanned,
            report.newly_marked,
            report.physically_deleted,
            report.skipped_folders or "없음",
        )
        return report

    # ──────────────────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────────────────

    def _folder_accessible(self, folder: Path) -> bool:
        """폴더가 존재하고 접근 가능하면 True."""
        try:
            return folder.exists() and folder.is_dir()
        except OSError:
            return False

    def _find_orphans(self, folder_str: str, state: dict) -> list[str]:
        """state 중 해당 폴더 소속이면서 디스크에 없는 경로 리스트를 반환한다."""
        orphans: list[str] = []
        for path_str in state:
            if not path_str.startswith(folder_str):
                continue
            if not Path(path_str).exists():
                orphans.append(path_str)
        return orphans

    def _count_indexed(self, folder_str: str, state: dict) -> int:
        """해당 폴더 소속 state 항목 수를 반환한다."""
        return sum(1 for p in state if p.startswith(folder_str))

    def _process_orphans(
        self, orphan_paths: list[str], state: dict
    ) -> tuple[int, int]:
        """orphan 경로를 처리하고 (새로 마킹된 수, 물리 삭제된 수) 를 반환한다."""
        # 안전장치 5: dry-run
        if self._dry_run:
            for p in orphan_paths:
                logger.info("[cleanup][dry-run] orphan 대상: %s", p)
            return 0, 0

        newly_marked = 0
        physically_deleted = 0

        for path_str in orphan_paths:
            entry = state.get(path_str, {})
            chunk_ids: list[str] = entry.get("chunk_ids", [])
            if not chunk_ids:
                continue

            # 안전장치 3: 2단계 soft delete (indexer 내부에서 miss_count 관리)
            self._indexer.delete_chunks(chunk_ids)

            # delete_chunks 이후 실제로 물리 삭제됐는지 확인
            deleted_now = self._check_physically_deleted(chunk_ids)
            if deleted_now:
                physically_deleted += deleted_now
                # state 에서도 제거
                state.pop(path_str, None)
            else:
                newly_marked += len(chunk_ids)

        return newly_marked, physically_deleted

    def _check_physically_deleted(self, chunk_ids: list[str]) -> int:
        """물리 삭제된 chunk_ids 수를 DB 조회로 확인한다."""
        if not chunk_ids:
            return 0
        try:
            id_list = ", ".join(f"'{cid}'" for cid in chunk_ids)
            df = (
                self._indexer.table.search()
                .where(f"chunk_id IN ({id_list})")
                .limit(len(chunk_ids) * 2)
                .to_arrow()
                .to_pandas()
            )
            # DB에 남아 있지 않은 것들이 물리 삭제된 것
            remaining = set(df["chunk_id"].tolist()) if not df.empty else set()
            return len(set(chunk_ids) - remaining)
        except Exception as exc:
            logger.warning("[cleanup] 삭제 확인 실패: %s", exc)
            return 0
