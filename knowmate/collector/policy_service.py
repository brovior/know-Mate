"""Run exclusion mutations on the collector's sole writer thread."""
from __future__ import annotations

import logging
import json
from typing import Any, Callable

from knowmate.collector.scanner import normalize_path_key
from knowmate.collector.state import save_state

logger = logging.getLogger(__name__)


def _new_result(requested: int, changed: int) -> dict[str, Any]:
    """Build fixed metadata for success, partial cleanup and early rejection."""
    return {
        "requested": requested, "changed": changed, "already_target": requested - changed,
        "attempted": 0, "succeeded": 0, "failed": 0, "not_attempted": changed,
        "delete_failed": 0, "state_cleanup_failed": False, "mail_cleanup_failed": False,
        "mail_pending_chunks": 0, "settings_saved": False, "policy_unknown": False,
        "cancelled": False, "status": "success", "error_code": "", "counts": None,
    }


def _counts(worker: Any) -> dict[str, int] | None:
    """Aggregate projected metadata without document text or vectors."""
    try:
        local: set[str] = set()
        shared: set[str] = set()
        indexer = getattr(worker, "_indexer", None)
        if indexer is not None:
            rows = indexer.table.search().select(["file_path", "scope", "is_deleted"]).to_arrow().to_pylist()
            for row in rows:
                if not row["is_deleted"]:
                    if row["scope"] == "local":
                        local.add(row["file_path"])
                    elif row["scope"] == "shared":
                        shared.add(row["file_path"])
        mail = getattr(worker, "_email_indexer", None)
        mail_count = 0
        if mail is not None:
            rows = mail.table.search().select(["mail_uid", "is_deleted"]).to_arrow().to_pylist()
            mail_count = len({row["mail_uid"] for row in rows if not row["is_deleted"]})
        return {"local_count": len(local), "shared_count": len(shared),
                "doc_count": len(local) + len(shared), "mail_count": mail_count}
    except Exception as exc:
        logger.warning("[policy] 건수 집계 실패: error_type=%s", type(exc).__name__)
        return None


def _load_document_state(path: Any) -> dict:
    """Keep unreadable state distinct from a genuinely missing checkpoint."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict):
        raise ValueError("invalid document state")
    return state


def reconcile_policy(worker: Any, action: str, paths: tuple[str, ...]) -> dict[str, Any]:
    """Read and publish durable settings after an uncertain save without writes."""
    from knowmate import config
    actual = config.read_exclude_files()
    config.publish_exclude_files(actual)
    actual_keys = {normalize_path_key(path) for path in actual}
    wanted = {normalize_path_key(path) for path in paths}
    applied = wanted.issubset(actual_keys) if action == "exclude" else not bool(wanted & actual_keys)
    from knowmate.collector.mail_scan_state import normalize_path_key as mail_key
    worker._mail_exclusions_reconciled.difference_update(
        mail_key(path) for path in paths if normalize_path_key(path) not in actual_keys)
    result = _new_result(len(paths), 0)
    result.update(settings_saved=applied, policy_unknown=False, status="reconciled",
                  counts=_counts(worker), not_attempted=len(paths))
    return result


def run_policy(
    worker: Any, action: str, paths: tuple[str, ...], publish: Callable[..., None],
) -> dict[str, Any]:
    """Apply durable policy before cancellable cleanup of searchable data."""
    from knowmate import config
    from knowmate.collector.mail_exclusion import mail_exclusion_paths, reconcile_mail_exclusions
    from knowmate.collector.mail_scan_state import normalize_path_key as mail_key

    if not paths:
        return _new_result(0, 0)
    cfg = config.get_config()
    excluded = list(cfg.get("collector", {}).get("exclude_files", []))
    keys = {normalize_path_key(path) for path in excluded if isinstance(path, str)}
    requested_keys = {normalize_path_key(path) for path in paths}
    changed = [path for path in paths if normalize_path_key(path) not in keys] if action == "exclude" else [
        path for path in paths if normalize_path_key(path) in keys]
    desired = excluded + changed if action == "exclude" else [
        path for path in excluded if not isinstance(path, str) or normalize_path_key(path) not in requested_keys]
    result = _new_result(len(paths), len(changed))
    if worker._cancelled:
        result.update(cancelled=True, status="cancelled")
        return result
    publish(stage="settings", total=len(changed), result=result)
    try:
        if changed:
            config.update_exclude_files(desired)
    except Exception as exc:
        logger.warning("[policy] 제외 설정 저장 실패: error_type=%s", type(exc).__name__)
        try:
            actual = config.read_exclude_files()
            config.publish_exclude_files(actual)
            result["settings_saved"] = {normalize_path_key(path) for path in actual} == {
                normalize_path_key(path) for path in desired}
        except Exception as read_exc:
            logger.warning("[policy] 제외 설정 확인 실패: error_type=%s", type(read_exc).__name__)
            result.update(settings_saved=None, policy_unknown=True)
        if result["settings_saved"] is not True:
            result.update(status="error", error_code="policy_unconfirmed" if result["policy_unknown"] else "config_save_failed")
            return result
    else:
        result["settings_saved"] = True
    publish(settings_saved=True, result=result)
    if action == "unexclude":
        worker._mail_exclusions_reconciled.difference_update(mail_key(path) for path in paths)
        result.update(attempted=len(changed), succeeded=len(changed), not_attempted=0)
    else:
        mails = mail_exclusion_paths([path for path in desired if isinstance(path, str)], cfg)
        mail_keys = {normalize_path_key(path) for path in mails}
        documents = [path for path in changed if normalize_path_key(path) not in mail_keys]
        successful: set[str] = set()
        publish(stage="documents", done=0, total=len(documents), result=result)
        for path in documents:
            if worker._cancelled:
                break
            result["attempted"] += 1
            try:
                worker._indexer.delete_file_chunks(path)
            except Exception as exc:
                result["failed"] += 1
                result["delete_failed"] += 1
                logger.warning("[policy] 문서 검색 데이터 정리 실패: error_type=%s", type(exc).__name__)
            else:
                successful.add(normalize_path_key(path))
                result["succeeded"] += 1
            result["not_attempted"] = len(changed) - result["attempted"]
            publish(done=result["attempted"], result=result)
        if successful:
            publish(stage="finishing", result=result)
            try:
                state = _load_document_state(worker._state_file)
                retained = {path: entry for path, entry in state.items() if normalize_path_key(path) not in successful}
                if len(retained) != len(state):
                    save_state(worker._state_file, retained)
            except Exception as exc:
                result["state_cleanup_failed"] = True
                logger.warning("[policy] 문서 상태 정리 실패: error_type=%s", type(exc).__name__)
        changed_mail = [path for path in changed if normalize_path_key(path) in mail_keys]
        if not worker._cancelled and getattr(worker, "_email_indexer", None) is not None:
            publish(stage="mail", total=None, result=result)
            result["attempted"] += len(changed_mail)
            try:
                report = reconcile_mail_exclusions(worker._email_indexer, worker._mail_state_file,
                                                   mails, worker._mail_exclusions_reconciled)
                result["mail_cleanup_failed"] = not report.ok
                result["mail_pending_chunks"] = report.pending_chunks
            except Exception as exc:
                result["mail_cleanup_failed"] = True
                logger.warning("[policy] 메일 검색 데이터 정리 실패: error_type=%s", type(exc).__name__)
            if result["mail_cleanup_failed"]:
                result["failed"] += len(changed_mail)
            else:
                result["succeeded"] += len(changed_mail)
            result["not_attempted"] = max(0, len(changed) - result["attempted"])
    result["cancelled"] = bool(worker._cancelled)
    result["status"] = "cancelled" if worker._cancelled else "partial" if (
        result["failed"] or result["state_cleanup_failed"] or result["mail_cleanup_failed"]
        or result["mail_pending_chunks"] or result["not_attempted"]
    ) else "success"
    if worker._cancelled:
        return result
    publish(stage="counts", total=None, result=result)
    result["counts"] = _counts(worker)
    return result
