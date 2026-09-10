"""Knox .mysingle/.eml 메일 스캔 및 증분 인덱싱 모듈."""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

from knowmate.collector import failure_state
from knowmate.collector.mail_scan_state import (
    cache_matches,
    cache_success,
    clear_pending_delete,
    load_mail_scan_state,
    normalize_path_key,
    prune_missing_files,
    queue_pending_delete,
    save_mail_scan_state,
    set_cursor,
)

if TYPE_CHECKING:
    from knowmate.rag.email_indexer import EmailIndexer

logger = logging.getLogger(__name__)

_DEFAULT_MAIL_EXTS = [".mysingle", ".eml"]


@dataclass
class _PreparedMail:
    """메일별 파싱·상태 조회 뒤 교차메일 임베딩을 기다리는 작업이다."""

    item: dict
    parsed: dict
    check: object
    is_migration: bool
    job: object


@dataclass
class _UidWork:
    """한 스캔 안에서 같은 mail_uid의 primary와 복사본 결과를 직렬화한다."""

    mail_uid: str
    fingerprint: str
    mtime: float
    primary: _PreparedMail | None
    aliases: list[dict]
    outcome: str | None = None


def _content_fingerprint(parsed: dict) -> str:
    """인덱스 결과를 바꾸는 메일 내용을 메모리 안에서만 SHA-256으로 식별한다."""
    fields = (
        "message_id", "subject", "sender", "recipients", "mail_date", "thread_ref",
        "body_text", "source_type", "source_meta",
    )
    payload = {field: parsed.get(field, "") for field in fields}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


def _mail_extensions(extensions: list[str] | None) -> tuple[str, ...]:
    """비어 있는 메일 확장자 설정은 배포 기본값으로 보완한다."""
    return tuple(ext.lower() for ext in (extensions or _DEFAULT_MAIL_EXTS))


def _iter_scanned_mail_items(watch_folders: list[str], exts: tuple[str, ...]) -> Iterator[dict]:
    """파일마다 경로 키를 한 번 만들고, 중첩 root의 중복 파일은 한 번만 yield한다."""
    seen_paths: set[str] = set()
    for folder_str in watch_folders:
        if not Path(folder_str).is_dir():
            logger.warning("[mail_scanner] 폴더 접근 불가, 건너뜀: %s", folder_str)
            continue
        for path, mtime, size in _iter_mail_files(folder_str, exts):
            path_key = normalize_path_key(path)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            yield {
                "path": path,
                "path_key": path_key,
                "mtime": mtime,
                "size": size,
            }


def scan_mail_folders(
    watch_folders: list[str], max_per_scan: int, extensions: list[str] | None = None
) -> list[dict]:
    """watch_folders의 모든 메일 후보를 최신순·경로순으로 결정적으로 반환한다.

    ``max_per_scan``은 호환을 위해 받지만 후보를 자르지 않는다. 실제 처리량 제한은
    ``run_mail_scan``이 파싱·DB 검증·인덱싱 시도에만 적용한다.
    """
    del max_per_scan
    exts = _mail_extensions(extensions)
    found = list(_iter_scanned_mail_items(watch_folders, exts))
    found.sort(key=lambda item: (-item["mtime"], item["path_key"]))
    return found


def _collect_actionable_candidates(
    watch_folders: list[str],
    extensions: list[str] | None,
    state: dict,
    failures: dict,
    now: float,
    policy: failure_state.BackoffPolicy,
) -> tuple[list[dict], set[str], list[str], dict[str, float], int]:
    """한 번의 전체 순회에서 누락 정리용 경로와 실제 처리 후보만 분리한다."""
    exts = _mail_extensions(extensions)
    actionable: list[dict] = []
    seen_keys: set[str] = set()
    cached_failure_paths: list[str] = []
    cached_uid_mtimes: dict[str, float] = {}
    skipped_count = 0
    for item in _iter_scanned_mail_items(watch_folders, exts):
        seen_keys.add(item["path_key"])
        cached = state["files"].get(item["path_key"])
        if cache_matches(cached, item):
            cached_uid_mtimes[cached["mail_uid"]] = max(
                cached_uid_mtimes.get(cached["mail_uid"], float("-inf")), item["mtime"],
            )
            if item["path"] in failures:
                cached_failure_paths.append(item["path"])
            skipped_count += 1
            continue
        if failure_state.should_defer(
            failures.get(item["path"]), item["path"], item["mtime"], item["size"], now, policy,
        ):
            skipped_count += 1
            continue
        item["cache_entry"] = cached
        actionable.append(item)
    actionable.sort(key=lambda item: (-item["mtime"], item["path_key"]))
    return actionable, seen_keys, cached_failure_paths, cached_uid_mtimes, skipped_count


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
    extensions = mail_cfg.get("extensions") or _DEFAULT_MAIL_EXTS
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
    candidates, seen_keys, cached_failure_paths, cached_uid_mtimes, early_skipped_count = _collect_actionable_candidates(
        watch_folders, extensions, state, failures, now_fn(), policy,
    )

    if table_was_recreated:
        logger.info("[mail_scanner] 메일 테이블 재생성 감지 — 성공 캐시를 다시 구축합니다")
    roots_accessible = bool(watch_folders) and all(Path(folder).is_dir() for folder in watch_folders)
    pruned = prune_missing_files(state, seen_keys) if roots_accessible else 0
    failure_pruned = failure_state.prune(failures) if roots_accessible else 0
    state_dirty = pruned > 0
    state_dirty = _retry_pending_deletes(state, email_indexer) or state_dirty
    failures_dirty = failures_dirty or failure_pruned > 0
    for path in cached_failure_paths:
        failure_state.note_success(failures, path)
        failures_dirty = True

    indexed_count = 0
    skipped_count = early_skipped_count
    attempted_count = 0
    migrate_count = 0
    migrate_logged = False
    # 교차메일 배치는 API batch_size만큼만 보관한다. 한 메일 자체가 그보다 큰 경우만
    # 단일 메일 크기만큼 넘칠 수 있다. 이 제한은 본문/벡터가 500건 스캔 전체에 쌓이는
    # 것을 막는 동시에, 이미 완료한 앞 윈도우가 뒤의 전역 오류와 독립적으로 확정되게 한다.
    prepared_window: list[_PreparedMail] = []
    window_chunk_count = 0
    uid_work: dict[str, _UidWork] = {}
    resolved_count = 0
    reported_count = 0
    last_resolved_item: dict | None = None

    def report_resolved(item: dict) -> None:
        """실제 시도한 source의 최종 결과만 제한된 빈도로 진행률에 반영한다."""
        nonlocal resolved_count, reported_count, last_resolved_item
        resolved_count += 1
        last_resolved_item = item
        if on_progress and resolved_count - reported_count >= progress_every:
            on_progress(resolved_count, len(candidates), Path(item["path"]).name)
            reported_count = resolved_count

    def report_final_progress() -> None:
        """마지막 묶음의 실제 시도가 최종 확정된 뒤 한 번만 진행률을 보낸다."""
        if on_progress and last_resolved_item is not None and reported_count != resolved_count:
            on_progress(resolved_count, len(candidates), Path(last_resolved_item["path"]).name)

    def mark_failure(item: dict, stage: str = "index") -> None:
        """캐시를 만들지 않은 채 source 하나를 다음 주기로 미룬다."""
        nonlocal failures_dirty, skipped_count
        failure_state.note_failure(
            failures, item["path"], failure_state.KIND_UNKNOWN_TRANSIENT, stage, None,
            item["mtime"], item["size"], now_fn(),
        )
        failures_dirty = True
        skipped_count += 1
        report_resolved(item)

    def mark_success(item: dict, mail_uid: str) -> None:
        """primary 성공 뒤에만 alias까지 성공 캐시로 확정한다."""
        nonlocal state_dirty, failures_dirty
        path = item["path"]
        cache_success(state, item, mail_uid)
        state_dirty = True
        if path in failures:
            failure_state.note_success(failures, path)
            failures_dirty = True
        email_indexer.table_is_empty = False
        report_resolved(item)

    def finish_old_delete(pending: _PreparedMail) -> None:
        """새 세대 저장 후 캡처한 이전 ID만 durable queue를 거쳐 삭제한다."""
        check = pending.check
        if check.state.name != "STALE" or not check.old_chunk_ids or not pending.job.chunks:
            return
        queue_pending_delete(state, check.old_chunk_ids)
        if not save_mail_scan_state(state_file, state):
            logger.error(
                "[mail_scanner] 기존 메일 청크 삭제 대상을 저장하지 못해 삭제를 연기합니다: %s",
                pending.item["path"],
            )
            return
        try:
            deleted_ids = email_indexer.delete_chunk_ids(check.old_chunk_ids)
        except Exception as exc:
            logger.warning(
                "[mail_scanner] 기존 메일 청크 삭제 실패 — 다음 사이클 재시도: %s (%s)",
                pending.item["path"], exc,
            )
        else:
            clear_pending_delete(state, deleted_ids)
            save_mail_scan_state(state_file, state)

    def release_job(pending: _PreparedMail) -> None:
        """완료 윈도우가 본문과 벡터를 붙잡지 않도록 참조를 즉시 해제한다."""
        pending.job.chunks.clear()
        pending.job.vectors.clear()
        pending.job.parsed.clear()
        pending.parsed.clear()

    def finish_work(work: _UidWork, *, failed: bool) -> None:
        """UID primary 결과를 확정하고, alias는 같은 결과로 직렬화한다."""
        nonlocal indexed_count, migrate_count, skipped_count
        primary = work.primary
        if primary is None:
            return
        if failed:
            mark_failure(primary.item)
            aliases = work.aliases
            work.aliases = []
            for alias in aliases:
                mark_failure(alias)
            work.outcome = "failure"
            release_job(primary)
            work.primary = None
            return
        try:
            chunk_ids = email_indexer.commit_mail_job(primary.job)
        except Exception as exc:
            logger.warning(
                "[mail_scanner] 메일 저장 실패, 다음 기회에 재시도: %s (%s)",
                primary.item["path"], exc,
            )
            finish_work(work, failed=True)
            return
        mark_success(primary.item, work.mail_uid)
        indexed_count += 1
        migrate_count += int(primary.is_migration)
        finish_old_delete(primary)
        logger.info(
            "[mail_scanner] [%s] %s -> %d청크",
            "MIGRATE" if primary.is_migration else "NEW",
            Path(primary.item["path"]).name,
            len(chunk_ids),
        )
        aliases = work.aliases
        work.aliases = []
        for alias in aliases:
            mark_success(alias, work.mail_uid)
            skipped_count += 1
        work.outcome = "success"
        release_job(primary)
        work.primary = None

    def flush_window() -> bool:
        """현재 윈도우만 embed→메일별 commit한다; global 오류면 False를 반환한다."""
        nonlocal window_chunk_count
        if not prepared_window:
            return True
        batch_result = email_indexer.embed_mail_jobs([pending.job for pending in prepared_window])
        for pending in prepared_window:
            work = uid_work[pending.parsed["mail_uid"]]
            job = pending.job
            if getattr(job, "content_error", None) or any(vector is None for vector in job.vectors):
                reason = getattr(job, "content_error", None) or batch_result.blocking_error
                logger.warning(
                    "[mail_scanner] 메일 임베딩 실패, 다음 기회에 재시도: %s (%s)",
                    pending.item["path"], reason,
                )
                finish_work(work, failed=True)
            else:
                # 전역 오류가 뒤 batch에서 났어도 이미 완결된 앞 메일은 안전하게 저장한다.
                finish_work(work, failed=False)
        prepared_window.clear()
        window_chunk_count = 0
        return batch_result.blocking_error is None

    def begin_generation(
        item: dict, parsed: dict, fingerprint: str, is_migration: bool, work: _UidWork | None = None,
    ) -> bool:
        """UID의 선택된 세대를 상태 확인 뒤 즉시 확정하거나 현재 윈도우에 넣는다."""
        nonlocal indexed_count, migrate_count, skipped_count, window_chunk_count
        mail_uid = parsed["mail_uid"]
        if work is None:
            work = _UidWork(mail_uid, fingerprint, item["mtime"], None, [])
            uid_work[mail_uid] = work
        else:
            work.fingerprint = fingerprint
            work.mtime = item["mtime"]
            work.primary = None
            work.aliases = []
            work.outcome = None

        check = email_indexer.get_index_state(mail_uid, item["mtime"])
        if check.state.name == "ERROR":
            logger.warning("[mail_scanner] DB 상태 조회 실패, 변경하지 않고 연기: %s", item["path"])
            work.outcome = "failure"
            mark_failure(item)
            return True
        if check.state.name == "CURRENT":
            work.outcome = "success"
            mark_success(item, mail_uid)
            skipped_count += 1
            return True
        if hasattr(email_indexer, "prepare_mail"):
            try:
                job = email_indexer.prepare_mail(parsed, item["mtime"], check)
            except Exception:
                work.outcome = "failure"
                raise
            pending = _PreparedMail(item, parsed, check, is_migration, job)
            work.primary = pending
            prepared_window.append(pending)
            window_chunk_count += len(job.chunks)
            return window_chunk_count < email_indexer._batch_size or flush_window()

        try:
            chunk_ids = email_indexer.index_mail(
                parsed, item["mtime"], index_check=check, delete_old=False,
            )
        except Exception:
            work.outcome = "failure"
            raise
        mark_success(item, mail_uid)
        indexed_count += 1
        migrate_count += int(is_migration)
        work.outcome = "success"
        if check.state.name == "STALE" and check.old_chunk_ids:
            # 새 행과 기존 삭제 대상을 먼저 원자적으로 기록한다. 이후 삭제가 실패하거나
            # 앱이 종료돼도 다음 사이클에서 정확히 그 ID만 재시도한다.
            queue_pending_delete(state, check.old_chunk_ids)
            if not save_mail_scan_state(state_file, state):
                logger.error(
                    "[mail_scanner] 기존 메일 청크 삭제 대상을 저장하지 못해 삭제를 연기합니다: %s",
                    item["path"],
                )
            else:
                try:
                    deleted_ids = email_indexer.delete_chunk_ids(check.old_chunk_ids)
                except Exception as exc:
                    logger.warning(
                        "[mail_scanner] 기존 메일 청크 삭제 실패 — 다음 사이클 재시도: %s (%s)",
                        item["path"], exc,
                    )
                else:
                    clear_pending_delete(state, deleted_ids)
                    save_mail_scan_state(state_file, state)
        logger.info(
            "[mail_scanner] [%s] %s -> %d청크",
            "MIGRATE" if is_migration else "NEW", Path(item["path"]).name, len(chunk_ids),
        )
        return True

    stop_after_global_error = False
    for item in _candidates_from_cursor(candidates, state.get("cursor")):
        path = item["path"]
        if attempted_count >= max_per_scan:
            break
        attempted_count += 1
        cached = item.get("cache_entry")
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
            report_resolved(item)
        except OSError as exc:
            logger.warning("[mail_scanner] 파일 접근 실패, 다음 기회에 재시도: %s (%s)", path, exc)
            failure_state.note_failure(
                failures, path, _mail_os_failure_kind(exc), "parse", None,
                item["mtime"], item["size"], now_fn(),
            )
            failures_dirty = True
            skipped_count += 1
            report_resolved(item)
        except Exception as exc:
            logger.warning("[mail_scanner] 파싱 실패, 다음 기회에 재시도: %s (%s)", path, exc)
            failure_state.note_failure(
                failures, path, failure_state.KIND_UNKNOWN_TRANSIENT, "parse", None,
                item["mtime"], item["size"], now_fn(),
            )
            failures_dirty = True
            skipped_count += 1
            report_resolved(item)
        else:
            try:
                mail_uid = parsed["mail_uid"]
                fingerprint = _content_fingerprint(parsed)
                work = uid_work.get(mail_uid)
                if work is None:
                    cached_uid_mtime = cached_uid_mtimes.get(mail_uid)
                    if cached_uid_mtime is not None and item["mtime"] <= cached_uid_mtime:
                        # 더 최신(또는 동시각) 복사본은 이번 스캔의 성공 캐시라 파싱 대상에서
                        # 제외됐을 수 있다. 오래된 다른 본문이 그 세대를 되돌리지 않도록
                        # shadow로만 캐시한다. 캐시 본문은 저장하지 않는다.
                        mark_success(item, mail_uid)
                        skipped_count += 1
                    elif not begin_generation(item, parsed, fingerprint, is_migration):
                        stop_after_global_error = True
                elif fingerprint == work.fingerprint or item["mtime"] <= work.mtime:
                    # 같은 내용은 복사본이고, 다른 내용이라도 더 오래된 source는 현재
                    # 세대를 되돌릴 수 없다. primary가 끝난 뒤에만 함께 성공 캐시한다.
                    if work.outcome == "success":
                        mark_success(item, mail_uid)
                        skipped_count += 1
                    elif work.outcome == "failure":
                        mark_failure(item)
                    else:
                        work.aliases.append(item)
                else:
                    # 동일 UID의 더 새 내용은 앞 세대가 아직 배치 대기 중이어도
                    # 먼저 최종 결과를 낸 뒤 별도 STALE 교체로 처리한다. 이 강제 flush는
                    # 드문 충돌에서만 일어나며, 두 세대가 동시에 활성화되지 않게 한다.
                    if work.outcome is None and not flush_window():
                        stop_after_global_error = True
                        mark_failure(item)
                    if not stop_after_global_error and not begin_generation(
                        item, parsed, fingerprint, is_migration, work,
                    ):
                        stop_after_global_error = True
            except Exception as exc:
                logger.warning("[mail_scanner] 인덱싱 실패, 다음 기회에 재시도: %s (%s)", path, exc)
                mark_failure(item)

        set_cursor(state, item)
        state_dirty = True
        if stop_after_global_error:
            break

    if not stop_after_global_error:
        flush_window()

    report_final_progress()

    if state_dirty:
        save_mail_scan_state(state_file, state)
    if failures_dirty:
        failure_state.save_failures(failure_file, failures)
    if migrate_count:
        logger.info("[mail_scanner] 포맷 마이그레이션 완료: 재인덱싱=%d건", migrate_count)
    logger.info(
        "[mail_scanner] 스캔 완료: 전체=%d 처리후보=%d 시도=%d 인덱싱=%d (마이그레이션=%d) 스킵=%d%s",
        len(seen_keys), len(candidates), attempted_count, indexed_count, migrate_count, skipped_count,
        f" 캐시정리={pruned}" if pruned else "",
    )
    return indexed_count, skipped_count


def _retry_pending_deletes(state: dict, email_indexer: "EmailIndexer") -> bool:
    """캐시 적중 여부와 무관하게 이전 교체의 기존 청크 삭제를 다시 시도한다."""
    changed = False
    chunk_ids = state.get("pending_deletes", [])
    if not isinstance(chunk_ids, list) or not chunk_ids:
        return False
    try:
        deleted_ids = email_indexer.delete_chunk_ids(chunk_ids)
    except Exception as exc:
        logger.warning("[mail_scanner] 보류된 기존 메일 청크 삭제 실패 — 다음 사이클 재시도: %s", exc)
    else:
        clear_pending_delete(state, deleted_ids)
        changed = True
    return changed


def _candidates_from_cursor(candidates: list[dict], cursor: object) -> Iterator[dict]:
    """커서 다음부터 목록 끝·처음 순으로 후보를 정확히 한 바퀴 순회한다."""
    if not candidates:
        return
    keys = [(-item["mtime"], item["path_key"]) for item in candidates]
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
