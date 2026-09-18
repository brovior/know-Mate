"""메일 경로 제외의 검색 캐시 정리와 내구성 있는 청크 삭제."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from knowmate.collector.mail_scan_state import (
    clear_pending_delete,
    load_mail_scan_state,
    normalize_path_key,
    queue_pending_delete,
    save_mail_scan_state,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAIL_EXTS = {".mysingle", ".eml"}


@dataclass(frozen=True)
class MailExclusionReport:
    """메일 제외 정리 결과 집계다."""

    ok: bool
    paths: int = 0
    captured_chunks: int = 0
    deleted_chunks: int = 0
    pending_chunks: int = 0
    cache_entries_removed: int = 0
    error: str = ""


def mail_exclusion_paths(paths: list[str], cfg: dict) -> list[str]:
    """현재·기본 메일 확장자에 해당하는 제외 경로만 반환한다."""
    configured = cfg.get("mail", {}).get("extensions") or []
    extensions = _DEFAULT_MAIL_EXTS | {
        str(ext).lower() for ext in configured if isinstance(ext, str)
    }
    return [path for path in paths if Path(path).suffix.lower() in extensions]


def reconcile_mail_exclusions(
    email_indexer,
    state_file: Path,
    excluded_paths: list[str],
    reconciled_keys: set[str],
    *,
    preloaded_state: dict | None = None,
) -> MailExclusionReport:
    """메일 제외 경로를 캐시·삭제 대기열·DB에 순서대로 반영한다."""
    current_by_key = {
        normalize_path_key(path): path
        for path in excluded_paths if isinstance(path, str) and path
    }
    reconciled_keys.intersection_update(current_by_key)
    targets = [
        path for key, path in current_by_key.items()
        if key not in reconciled_keys
    ]

    if preloaded_state is None:
        try:
            state = load_mail_scan_state(state_file, strict=True)
        except Exception as exc:
            logger.warning("[mail_exclude] 상태 읽기 실패 — 제외 정리 연기: %s", exc)
            return MailExclusionReport(False, paths=len(targets), error="state load failed")
    else:
        state = preloaded_state

    # 이전 사이클에서 내구성 있게 예약된 삭제를 후보 판정보다 먼저 끝낸다.
    pending_before = tuple(
        chunk_id for chunk_id in state.get("pending_deletes", [])
        if isinstance(chunk_id, str)
    )
    deleted_pending: tuple[str, ...] = ()
    if pending_before:
        try:
            deleted_pending = tuple(email_indexer.delete_chunk_ids(pending_before))
        except Exception as exc:
            logger.warning("[mail_exclude] 보류 청크 삭제 재시도 실패: %s", exc)
        else:
            clear_pending_delete(state, deleted_pending)
            if not save_mail_scan_state(state_file, state):
                # 디스크에는 과거 ID가 남아 다음 시작에서 멱등 재시도된다.
                logger.warning("[mail_exclude] 보류 청크 삭제 결과 저장 실패")

    if not targets:
        remaining = state.get("pending_deletes", [])
        return MailExclusionReport(
            True,
            deleted_chunks=len(deleted_pending),
            pending_chunks=len(remaining) if isinstance(remaining, list) else 0,
        )

    try:
        refs = email_indexer.get_source_chunk_refs(targets)
    except Exception as exc:
        logger.warning("[mail_exclude] 제외 대상 청크 조회 실패: %s", exc)
        return MailExclusionReport(False, paths=len(targets), error="chunk lookup failed")

    chunk_ids = tuple(dict.fromkeys(
        row["chunk_id"] for row in refs if isinstance(row.get("chunk_id"), str)
    ))
    affected_uids = {
        row["mail_uid"] for row in refs if isinstance(row.get("mail_uid"), str)
    }
    target_keys = {normalize_path_key(path) for path in targets}
    files = state.get("files", {})
    removed = 0
    if isinstance(files, dict):
        for key, entry in list(files.items()):
            if key in target_keys or (
                isinstance(entry, dict) and entry.get("mail_uid") in affected_uids
            ):
                files.pop(key, None)
                removed += 1

    queue_pending_delete(state, chunk_ids)
    if (removed or chunk_ids) and not save_mail_scan_state(state_file, state):
        logger.warning("[mail_exclude] 캐시·삭제 예약 저장 실패 — DB 삭제 안 함")
        return MailExclusionReport(
            False, paths=len(targets), captured_chunks=len(chunk_ids),
            cache_entries_removed=removed, error="state save failed",
        )

    # 여기까지 오면 캐시 무효화와 정확한 ID가 내구성 있게 저장됐다.
    reconciled_keys.update(target_keys)
    deleted_now: tuple[str, ...] = ()
    if chunk_ids:
        try:
            deleted_now = tuple(email_indexer.delete_chunk_ids(chunk_ids))
        except Exception as exc:
            logger.warning("[mail_exclude] 청크 삭제 실패 — 다음 사이클 재시도: %s", exc)
        else:
            clear_pending_delete(state, deleted_now)
            if not save_mail_scan_state(state_file, state):
                logger.warning("[mail_exclude] 삭제 완료 상태 저장 실패 — 다음 시작에서 멱등 재시도")

    remaining = state.get("pending_deletes", [])
    return MailExclusionReport(
        True,
        paths=len(targets),
        captured_chunks=len(chunk_ids),
        deleted_chunks=len(deleted_pending) + len(deleted_now),
        pending_chunks=len(remaining) if isinstance(remaining, list) else 0,
        cache_entries_removed=removed,
    )
