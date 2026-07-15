"""Knox .mysingle 메일 스캔 및 증분 인덱싱 모듈."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowmate.rag.email_indexer import EmailIndexer

logger = logging.getLogger(__name__)


_DEFAULT_MAIL_EXTS = [".mysingle", ".eml"]


def scan_mail_folders(
    watch_folders: list[str], max_per_scan: int, extensions: list[str] | None = None
) -> list[dict]:
    """
    watch_folders 에서 메일 파일(.mysingle/.eml)을 수집해 최신 mtime 순으로 반환한다.

    반환: [{"path": str, "mtime": float}, ...]
    """
    exts = extensions or _DEFAULT_MAIL_EXTS
    found: list[dict] = []
    for folder_str in watch_folders:
        folder = Path(folder_str)
        if not folder.is_dir():
            logger.warning("[mail_scanner] 폴더 접근 불가, 건너뜀: %s", folder_str)
            continue
        for ext in exts:
            for p in folder.rglob(f"*{ext}"):
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
    from knowmate.secure.mysingle_reader import parse_mail_file

    mail_cfg = cfg.get("mail", {})
    max_per_scan = mail_cfg.get("max_mails_per_scan", 500)
    batch_every = mail_cfg.get("batch_commit_every", 50)
    extensions = mail_cfg.get("extensions", _DEFAULT_MAIL_EXTS)

    candidates = scan_mail_folders(watch_folders, max_per_scan, extensions)
    indexed_count = 0
    skipped_count = 0
    migrate_count = 0       # 버전 불일치로 재인덱싱된 건수
    migrate_logged = False  # 마이그레이션 시작 안내 1회만 출력

    for i, item in enumerate(candidates):
        path = item["path"]
        mtime = item["mtime"]

        # source_file 기준 사전 상태 분류 (파싱 비용 절약)
        state = _source_index_state(email_indexer, path, mtime)
        if state == "indexed":
            skipped_count += 1
            continue

        is_migration = state == "stale_version"
        if is_migration and not migrate_logged:
            logger.info(
                "[mail_scanner] 인덱싱 포맷 변경 감지 — 기존 메일 재인덱싱을 시작합니다 (전체=%d건)",
                len(candidates),
            )
            migrate_logged = True

        try:
            parsed = parse_mail_file(path)
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
            if is_migration:
                migrate_count += 1
            tag = "MIGRATE" if is_migration else "NEW"
            logger.info(
                "[mail_scanner] [%s] %s -> %d청크 (subject=%s)",
                tag, Path(path).name, len(chunk_ids), parsed.get("subject", "")[:40],
            )
        except Exception as exc:
            logger.warning("[mail_scanner] 인덱싱 실패: %s (%s)", path, exc)
            skipped_count += 1

        if on_progress and (indexed_count % batch_every == 0):
            on_progress(i + 1, len(candidates), Path(path).name)

    if migrate_count:
        logger.info("[mail_scanner] 포맷 마이그레이션 완료: 재인덱싱=%d건", migrate_count)
    logger.info(
        "[mail_scanner] 스캔 완료: 전체=%d 인덱싱=%d (마이그레이션=%d) 스킵=%d",
        len(candidates), indexed_count, migrate_count, skipped_count,
    )
    return indexed_count, skipped_count


def _source_index_state(email_indexer: "EmailIndexer", source_file: str, mtime: float) -> str:
    """
    source_file 의 인덱싱 상태를 분류한다.

    반환:
      "indexed"       — mtime·버전 모두 일치, 재인덱싱 불필요 (스킵)
      "stale_version" — mtime 일치하나 인덱스 버전 불일치 (포맷 마이그레이션)
      "new_or_changed"— 미인덱싱 또는 파일 변경 (mtime 불일치)
    """
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
            return "new_or_changed"
        row = df.iloc[0]
        if abs(float(row["mtime"]) - mtime) >= 1.0:
            return "new_or_changed"
        try:
            meta = json.loads(row.get("source_meta", "{}") or "{}")
            if meta.get("_index_version") != EMAIL_INDEX_VERSION:
                return "stale_version"
        except Exception:
            return "stale_version"
        return "indexed"
    except Exception:
        return "new_or_changed"
