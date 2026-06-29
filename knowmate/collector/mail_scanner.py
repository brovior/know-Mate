"""Knox .mysingle 메일 스캔 및 증분 인덱싱 모듈."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowmate.rag.email_indexer import EmailIndexer

logger = logging.getLogger(__name__)


def scan_mail_folders(watch_folders: list[str], max_per_scan: int) -> list[dict]:
    """
    watch_folders 에서 .mysingle 파일을 수집해 최신 mtime 순으로 반환한다.

    반환: [{"path": str, "mtime": float}, ...]
    """
    found: list[dict] = []
    for folder_str in watch_folders:
        folder = Path(folder_str)
        if not folder.is_dir():
            logger.warning("[mail_scanner] 폴더 접근 불가, 건너뜀: %s", folder_str)
            continue
        for p in folder.rglob("*.mysingle"):
            try:
                mtime = p.stat().st_mtime
                found.append({"path": str(p), "mtime": mtime})
            except OSError as exc:
                logger.warning("[mail_scanner] stat 실패: %s (%s)", p, exc)

    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found[:max_per_scan]


def run_mail_scan(
    watch_folders: list[str],
    email_indexer: "EmailIndexer",
    cfg: dict,
    on_progress=None,
) -> tuple[int, int]:
    """
    watch_folders 내 .mysingle 파일을 증분 인덱싱한다.

    - is_indexed() True면 건너뜀 (dedup)
    - 파싱 실패 시 WARNING 후 continue (사이클 중단 없음)
    - orphan 정리 없음 (메일함 = 백업저장소)

    반환: (인덱싱 건수, 건너뜀 건수)
    """
    from knowmate.secure.mysingle_reader import parse_mysingle

    mail_cfg = cfg.get("mail", {})
    max_per_scan = mail_cfg.get("max_mails_per_scan", 500)
    batch_every = mail_cfg.get("batch_commit_every", 50)

    candidates = scan_mail_folders(watch_folders, max_per_scan)
    indexed_count = 0
    skipped_count = 0

    for i, item in enumerate(candidates):
        path = item["path"]
        mtime = item["mtime"]

        # source_file 기준 mtime 사전 체크 (파싱 비용 절약)
        if _is_source_indexed(email_indexer, path, mtime):
            skipped_count += 1
            continue

        try:
            parsed = parse_mysingle(path)
        except Exception as exc:
            logger.warning("[mail_scanner] 파싱 실패, 건너뜀: %s (%s)", path, exc)
            skipped_count += 1
            continue

        if email_indexer.is_indexed(parsed["mail_uid"], mtime):
            skipped_count += 1
            continue

        try:
            chunk_ids = email_indexer.index_mail(parsed, mtime)
            indexed_count += 1
            logger.info(
                "[mail_scanner] [NEW] %s -> %d청크 (subject=%s)",
                Path(path).name, len(chunk_ids), parsed.get("subject", "")[:40],
            )
        except Exception as exc:
            logger.warning("[mail_scanner] 인덱싱 실패: %s (%s)", path, exc)
            skipped_count += 1

        if on_progress and (indexed_count % batch_every == 0):
            on_progress(i + 1, len(candidates), Path(path).name)

    logger.info(
        "[mail_scanner] 스캔 완료: 전체=%d 인덱싱=%d 스킵=%d",
        len(candidates), indexed_count, skipped_count,
    )
    return indexed_count, skipped_count


def _is_source_indexed(email_indexer: "EmailIndexer", source_file: str, mtime: float) -> bool:
    """source_file + mtime + 인덱스 버전 기준 사전 중복 검사."""
    import json
    from knowmate.rag.email_indexer import EMAIL_INDEX_VERSION
    try:
        escaped = source_file.replace("'", "''")
        df = (
            email_indexer.table.search()
            .where(f"source_file = '{escaped}' AND is_deleted = false")
            .limit(1)
            .to_arrow()
            .to_pandas()
        )
        if df.empty:
            return False
        row = df.iloc[0]
        if abs(float(row["mtime"]) - mtime) >= 1.0:
            return False
        try:
            meta = json.loads(row.get("source_meta", "{}") or "{}")
            if meta.get("_index_version") != EMAIL_INDEX_VERSION:
                return False
        except Exception:
            return False
        return True
    except Exception:
        return False
