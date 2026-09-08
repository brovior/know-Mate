"""Knox .mysingle/.eml 메일 스캔 및 증분 인덱싱 모듈."""
from __future__ import annotations

import bisect
import logging
import os
import time
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

from knowmate.collector import failure_state
from knowmate.collector.mail_scan_state import (
    cache_matches,
    cache_success,
    load_mail_scan_state,
    normalize_path_key,
    prune_missing_files,
    save_mail_scan_state,
    set_cursor,
)

if TYPE_CHECKING:
    from knowmate.rag.email_indexer import EmailIndexer

logger = logging.getLogger(__name__)

_DEFAULT_MAIL_EXTS = [".mysingle", ".eml"]


def _iter_mail_files(folder: str, exts: tuple[str, ...]) -> Iterator[tuple[str, float, int]]:
    """os.scandir 스택 순회로 메일 파일을 (경로, mtime, size)로 yield 한다."""
    stack: list[str] = [folder]
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError as exc:
                        logger.warning("[mail_scanner] 항목 접근 실패: %s (%s)", entry.path, exc)
                        continue
                    if is_dir:
                        stack.append(entry.path)
                        continue
                    if entry.name.startswith("~$") or not entry.name.lower().endswith(exts):
                        continue
                    try:
                        st = entry.stat()  # Windows: scandir 캐시 재사용(무 syscall)
                        yield entry.path, st.st_mtime, st.st_size
                    except OSError as exc:
                        logger.warning("[mail_scanner] stat 실패: %s (%s)", entry.path, exc)
        except OSError as exc:
            logger.error("[mail_scanner] 폴더 스캔 실패: %s (%s)", current_dir, exc)


def scan_mail_folders(
    watch_folders: list[str], max_per_scan: int, extensions: list[str] | None = None
) -> list[dict]:
    """watch_folders의 모든 메일 후보를 최신순·경로순으로 결정적으로 반환한다.

    ``max_per_scan``은 호환을 위해 받지만 후보를 자르지 않는다. 실제 처리량 제한은
    ``run_mail_scan``이 파싱·DB 검증·인덱싱 시도에만 적용한다.
    """
    del max_per_scan
    exts = tuple(e.lower() for e in (extensions or _DEFAULT_MAIL_EXTS))
    found: list[dict] = []
    for folder_str in watch_folders:
        if not Path(folder_str).is_dir():
            logger.warning("[mail_scanner] 폴더 접근 불가, 건너뜀: %s", folder_str)
            continue
        for path, mtime, size in _iter_mail_files(folder_str, exts):
            found.append({"path": path, "mtime": mtime, "size": size})
    found.sort(key=lambda item: (-item["mtime"], normalize_path_key(item["path"])))
    return found


def run_mail_scan(
    watch_folders: list[str],
    email_indexer: "EmailIndexer",
    cfg: dict,
    on_progress=None,
    *,
    state_file: Path | None = None,
    failure_file: Path | None = None,
    get_now=None,
    retry_failures: bool = False,
) -> tuple[int, int]:
    """메일을 순환 처리하되 한 수집 사이클의 실제 시도 수를 제한한다.

    성공 캐시 적중과 백오프 대기는 예산을 쓰지 않는다. 상태가 없는 기존 설치는
    매 사이클 최대 N건만 ``is_indexed``로 확인해 캐시를 점진적으로 구축한다.
    """
    from knowmate.secure.mysingle_reader import parse_mail_file

    mail_cfg = cfg.get("mail", {})
    max_per_scan = max(int(mail_cfg.get("max_mails_per_scan", 500)), 0)
    progress_every = max(int(mail_cfg.get("batch_commit_every", 50)), 1)
    extensions = mail_cfg.get("extensions", _DEFAULT_MAIL_EXTS)
    now_fn = get_now or time.time

    if state_file is None or failure_file is None:
        from knowmate.config import get_data_dir
        data_dir = get_data_dir()
        state_file = state_file or data_dir / "mail_scan_state.json"
        failure_file = failure_file or data_dir / "mail_index_failure.json"
    table_was_recreated = bool(getattr(email_indexer, "table_was_recreated", False))
    invalidate_cache = table_was_recreated or bool(getattr(email_indexer, "table_is_empty", False))
    state = load_mail_scan_state(state_file, invalidate_cache=invalidate_cache)
    if invalidate_cache:
        # DB가 비어 있거나 재생성됐다는 사실을 메모리 플래그만으로 소비하면 안 된다.
        # 첫 DB 저장 뒤 상태 저장 전에 프로세스가 종료되면, 다음 시작에서 이전 성공
        # 캐시가 되살아 빈 DB를 정상으로 오인할 수 있다. 따라서 어떤 메일을 열거나
        # DB를 건드리기 전에 빈 상태를 먼저 디스크에 확정한다.
        if not save_mail_scan_state(state_file, state):
            logger.error("[mail_scanner] 캐시 무효화 상태를 저장하지 못해 이번 메일 스캔을 연기합니다")
            return 0, 0
        if table_was_recreated:
            # 같은 EmailIndexer 인스턴스의 다음 유휴 사이클은 새 캐시를 사용할 수 있다.
            email_indexer.table_was_recreated = False
    failures = failure_state.load_failures(failure_file)
    failures_dirty = False
    if retry_failures:
        failures_dirty = failure_state.request_retry_all(failures) > 0
    policy = failure_state.BackoffPolicy.from_config(cfg.get("collector", {}))
    candidates = scan_mail_folders(watch_folders, max_per_scan, extensions)

    if table_was_recreated:
        logger.info("[mail_scanner] 메일 테이블 재생성 감지 — 성공 캐시를 다시 구축합니다")
    roots_accessible = bool(watch_folders) and all(Path(folder).is_dir() for folder in watch_folders)
    seen_keys = {normalize_path_key(item["path"]) for item in candidates}
    pruned = prune_missing_files(state, seen_keys) if roots_accessible else 0
    failure_pruned = failure_state.prune(failures) if roots_accessible else 0
    state_dirty = pruned > 0
    failures_dirty = failures_dirty or failure_pruned > 0

    indexed_count = 0
    skipped_count = 0
    attempted_count = 0
    migrate_count = 0
    migrate_logged = False

    for item in _candidates_from_cursor(candidates, state.get("cursor")):
        path = item["path"]
        key = normalize_path_key(path)
        cached = state["files"].get(key)
        if cache_matches(cached, item):
            if path in failures:
                failure_state.note_success(failures, path)
                failures_dirty = True
            skipped_count += 1
            continue

        if failure_state.should_defer(
            failures.get(path), path, item["mtime"], item["size"], now_fn(), policy,
        ):
            skipped_count += 1
            continue

        if attempted_count >= max_per_scan:
            break
        attempted_count += 1
        is_migration = isinstance(cached, dict) and cached.get("index_version") != _email_index_version()
        if is_migration and not migrate_logged:
            logger.info("[mail_scanner] 인덱싱 포맷 변경 감지 — 메일을 재인덱싱합니다")
            migrate_logged = True

        try:
            parsed = parse_mail_file(path)
        except ValueError as exc:
            logger.warning("[mail_scanner] 파싱 실패, 다음 기회에 재시도: %s (%s)", path, exc)
            failure_state.note_failure(
                failures, path, failure_state.KIND_NEEDS_USER_ACTION, "parse", None,
                item["mtime"], item["size"], now_fn(),
            )
            failures_dirty = True
            skipped_count += 1
        except OSError as exc:
            logger.warning("[mail_scanner] 파일 접근 실패, 다음 기회에 재시도: %s (%s)", path, exc)
            failure_state.note_failure(
                failures, path, _mail_os_failure_kind(exc), "parse", None,
                item["mtime"], item["size"], now_fn(),
            )
            failures_dirty = True
            skipped_count += 1
        except Exception as exc:
            logger.warning("[mail_scanner] 파싱 실패, 다음 기회에 재시도: %s (%s)", path, exc)
            failure_state.note_failure(
                failures, path, failure_state.KIND_UNKNOWN_TRANSIENT, "parse", None,
                item["mtime"], item["size"], now_fn(),
            )
            failures_dirty = True
            skipped_count += 1
        else:
            try:
                if email_indexer.is_indexed(parsed["mail_uid"], item["mtime"]):
                    cache_success(state, item, parsed["mail_uid"])
                    state_dirty = True
                    if path in failures:
                        failure_state.note_success(failures, path)
                        failures_dirty = True
                    email_indexer.table_is_empty = False
                    skipped_count += 1
                else:
                    chunk_ids = email_indexer.index_mail(parsed, item["mtime"])
                    cache_success(state, item, parsed["mail_uid"])
                    state_dirty = True
                    if path in failures:
                        failure_state.note_success(failures, path)
                        failures_dirty = True
                    email_indexer.table_is_empty = False
                    indexed_count += 1
                    migrate_count += int(is_migration)
                    logger.info(
                        "[mail_scanner] [%s] %s -> %d청크",
                        "MIGRATE" if is_migration else "NEW", Path(path).name, len(chunk_ids),
                    )
            except Exception as exc:
                logger.warning("[mail_scanner] 인덱싱 실패, 다음 기회에 재시도: %s (%s)", path, exc)
                failure_state.note_failure(
                    failures, path, failure_state.KIND_UNKNOWN_TRANSIENT, "index", None,
                    item["mtime"], item["size"], now_fn(),
                )
                failures_dirty = True
                skipped_count += 1

        set_cursor(state, item)
        state_dirty = True
        if attempted_count % progress_every == 0:
            if on_progress:
                on_progress(attempted_count, len(candidates), Path(path).name)

    if state_dirty:
        save_mail_scan_state(state_file, state)
    if failures_dirty:
        failure_state.save_failures(failure_file, failures)
    if migrate_count:
        logger.info("[mail_scanner] 포맷 마이그레이션 완료: 재인덱싱=%d건", migrate_count)
    logger.info(
        "[mail_scanner] 스캔 완료: 전체=%d 시도=%d 인덱싱=%d (마이그레이션=%d) 스킵=%d%s",
        len(candidates), attempted_count, indexed_count, migrate_count, skipped_count,
        f" 캐시정리={pruned}" if pruned else "",
    )
    return indexed_count, skipped_count


def _candidates_from_cursor(candidates: list[dict], cursor: object) -> Iterator[dict]:
    """커서 다음부터 목록 끝·처음 순으로 후보를 정확히 한 바퀴 순회한다."""
    if not candidates:
        return
    keys = [(-item["mtime"], normalize_path_key(item["path"])) for item in candidates]
    start = 0
    if isinstance(cursor, dict) and isinstance(cursor.get("path"), str):
        try:
            start = bisect.bisect_right(keys, (-float(cursor["mtime"]), cursor["path"]))
        except (KeyError, TypeError, ValueError):
            start = 0
    for offset in range(len(candidates)):
        yield candidates[(start + offset) % len(candidates)]


def _email_index_version() -> str:
    """순환 의존 없이 현재 메일 인덱스 버전을 읽는다."""
    from knowmate.rag.email_indexer import EMAIL_INDEX_VERSION
    return EMAIL_INDEX_VERSION


def _mail_os_failure_kind(exc: OSError) -> str:
    """확실한 Windows 공유·잠금 오류만 짧은 재시도 대상으로 분류한다."""
    if getattr(exc, "winerror", None) in {32, 33}:
        return failure_state.KIND_TEMPORARY_BUSY
    return failure_state.KIND_UNKNOWN_TRANSIENT
