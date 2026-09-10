"""메일 스캔 성공 캐시와 순환 커서를 관리한다."""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_UID_RESOLUTION_CACHE_VERSION = 2


class _MailScanState(dict[str, Any]):
    """저장 필요 여부를 메모리에만 보관하는 메일 스캔 상태다."""

    def __init__(self, *args: Any, needs_save: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.needs_save = needs_save


def normalize_path_key(path: str) -> str:
    """운영체제별 대소문자 규칙을 반영한 안정적인 경로 키를 반환한다."""
    return os.path.normcase(os.path.abspath(path))


def load_mail_scan_state(path: Path, *, invalidate_cache: bool = False) -> dict[str, Any]:
    """유효한 상태를 읽고 v1은 성공 캐시를 보존한 v2로 올린다."""
    empty = _empty_state()
    if not path.exists():
        return empty
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[mail_scanner] 메일 상태 파일 읽기 실패, 초기화: %s (%s)", path, exc)
        return empty
    if not isinstance(raw, dict):
        return empty

    version = raw.get("schema_version")
    if version == 1:
        state = _MailScanState(
            {
                "schema_version": SCHEMA_VERSION,
                "cursor": _valid_cursor(raw.get("cursor")),
                "files": {} if invalidate_cache else _valid_v1_files(raw.get("files")),
                "pending_deletes": _valid_pending_deletes(raw.get("pending_deletes")),
            },
            needs_save=True,
        )
    elif version == SCHEMA_VERSION:
        files, files_changed = _valid_v2_files(raw.get("files"))
        state = _MailScanState(
            {
                "schema_version": SCHEMA_VERSION,
                "cursor": _valid_cursor(raw.get("cursor")),
                "files": {} if invalidate_cache else files,
                "pending_deletes": _valid_pending_deletes(raw.get("pending_deletes")),
            },
            needs_save=files_changed or invalidate_cache,
        )
        if raw != state:
            state.needs_save = True
    else:
        return empty

    files = state["files"]
    if files and any(entry["index_version"] != _email_index_version() for entry in files.values()):
        logger.info("[mail_scanner] 메일 인덱스 버전 변경 감지 — 성공 캐시를 초기화합니다")
        state["files"] = {}
        state.needs_save = True
    return state


def save_mail_scan_state(path: Path, state: dict[str, Any]) -> bool:
    """메일 상태와 커서를 하나의 JSON 파일로 원자적으로 저장한다."""
    serializable = _canonical_state(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(serializable, handle, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(path)
        if isinstance(state, _MailScanState):
            state.clear()
            state.update(serializable)
            state.needs_save = False
        return True
    except OSError as exc:
        logger.error("[mail_scanner] 메일 상태 저장 실패(다음 사이클 복구): %s (%s)", path, exc)
        return False


def state_needs_save(state: dict[str, Any]) -> bool:
    """로드 중 마이그레이션·정규화된 상태가 아직 저장되지 않았는지 확인한다."""
    return bool(getattr(state, "needs_save", False))


def cache_matches(entry: dict[str, Any] | None, item: dict[str, Any]) -> bool:
    """성공 캐시가 현재 파일의 mtime·size·메일 인덱스 버전과 일치하는지 확인한다."""
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("index_version") == _email_index_version()
        and entry.get("uid_resolution_version") == _UID_RESOLUTION_CACHE_VERSION
        and entry.get("mtime") == item["mtime"]
        and entry.get("size") == item["size"]
        and isinstance(entry.get("mail_uid"), str)
    )


def cache_success(state: dict[str, Any], item: dict[str, Any], mail_uid: str) -> None:
    """성공하거나 DB 중복으로 확인된 메일의 캐시 항목을 기록한다."""
    key = item.get("path_key")
    if not isinstance(key, str):
        key = normalize_path_key(item["path"])
    state["files"][key] = {
        "mtime": item["mtime"],
        "size": item["size"],
        "mail_uid": mail_uid,
        "index_version": _email_index_version(),
        # v2는 같은 UID의 서로 다른 본문을 한 세대로 잘못 캐시했던 이전 항목을
        # 한 번 재검증한다. 본문이나 본문 해시는 상태 파일에 저장하지 않는다.
        "uid_resolution_version": _UID_RESOLUTION_CACHE_VERSION,
    }


def queue_pending_delete(state: dict[str, Any], chunk_ids: tuple[str, ...] | list[str]) -> None:
    """파일 캐시와 분리된 durable 삭제 대기열에 기존 청크 ID를 넣는다."""
    existing = state.setdefault("pending_deletes", [])
    ids = [*existing, *chunk_ids] if isinstance(existing, list) else list(chunk_ids)
    state["pending_deletes"] = list(dict.fromkeys(
        chunk_id for chunk_id in ids if isinstance(chunk_id, str)
    ))


def clear_pending_delete(state: dict[str, Any], chunk_ids: tuple[str, ...] | list[str]) -> None:
    """확인된 삭제 성공 ID만 durable 대기열에서 제거한다."""
    deleted = set(chunk_id for chunk_id in chunk_ids if isinstance(chunk_id, str))
    pending = state.get("pending_deletes", [])
    if isinstance(pending, list):
        state["pending_deletes"] = [chunk_id for chunk_id in pending if chunk_id not in deleted]


def set_cursor(state: dict[str, Any], item: dict[str, Any]) -> None:
    """현재 항목까지 순회했음을 다음 사이클용 커서에 기록한다."""
    key = item.get("path_key")
    if not isinstance(key, str):
        key = normalize_path_key(item["path"])
    state["cursor"] = {"mtime": item["mtime"], "path": key}


def prune_missing_files(state: dict[str, Any], seen_keys: set[str]) -> int:
    """이번에 확인한 파일 목록에 없는 성공 캐시를 제거하고 제거 수를 반환한다."""
    files = state.get("files", {})
    if not isinstance(files, dict):
        state["files"] = {}
        return 0
    stale = [key for key in files if key not in seen_keys]
    for key in stale:
        files.pop(key, None)
    return len(stale)


def _valid_cursor(raw: object) -> dict[str, Any] | None:
    """저장된 커서의 최소 스키마를 검증한다."""
    if not isinstance(raw, dict):
        return None
    mtime, path = raw.get("mtime"), raw.get("path")
    if not _is_finite_number(mtime) or not isinstance(path, str):
        return None
    return {"mtime": float(mtime), "path": path}


def _empty_state() -> _MailScanState:
    """새 상태 파일의 v2 기본 구조를 만든다."""
    return _MailScanState({
        "schema_version": SCHEMA_VERSION, "cursor": None, "files": {}, "pending_deletes": [],
    })


def _valid_v1_files(raw: object) -> dict[str, dict[str, Any]]:
    """v1의 중복 path를 키로 승격해 v2 성공 캐시를 복구한다."""
    if not isinstance(raw, dict):
        return {}
    valid: dict[str, dict[str, Any]] = {}
    priorities: dict[str, tuple[float, int, str]] = {}
    for raw_key, entry in sorted(raw.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_key, str) or not isinstance(entry, dict):
            continue
        path = entry.get("path", raw_key)
        normalized = _valid_entry(entry)
        if not isinstance(path, str) or normalized is None:
            continue
        key = normalize_path_key(path)
        priority = (normalized["mtime"], normalized["size"], raw_key)
        if priority >= priorities.get(key, (float("-inf"), -1, "")):
            valid[key] = normalized
            priorities[key] = priority
    return valid


def _valid_v2_files(raw: object) -> tuple[dict[str, dict[str, Any]], bool]:
    """v2 파일 캐시를 검증하고 저장형과 다른 항목만 다시 쓰게 표시한다."""
    if not isinstance(raw, dict):
        return {}, raw is not None
    valid: dict[str, dict[str, Any]] = {}
    priorities: dict[str, tuple[float, int, str]] = {}
    changed = False
    for key, entry in raw.items():
        normalized = _valid_entry(entry) if isinstance(key, str) else None
        if normalized is None:
            changed = True
            continue
        canonical_key = normalize_path_key(key)
        priority = (normalized["mtime"], normalized["size"], key)
        if priority < priorities.get(canonical_key, (float("-inf"), -1, "")):
            changed = True
            continue
        valid[canonical_key] = normalized
        priorities[canonical_key] = priority
        if key != canonical_key or entry != normalized:
            changed = True
    return valid, changed


def _valid_entry(entry: object) -> dict[str, Any] | None:
    """본문 없이 v2 성공 캐시에 필요한 메타데이터만 보존한다."""
    if not isinstance(entry, dict):
        return None
    mtime, size = entry.get("mtime"), entry.get("size")
    uid_version = entry.get("uid_resolution_version", 0)
    if not (
        _is_finite_number(mtime)
        and isinstance(size, int) and not isinstance(size, bool)
        and isinstance(entry.get("mail_uid"), str)
        and isinstance(entry.get("index_version"), str)
        and isinstance(uid_version, int) and not isinstance(uid_version, bool)
    ):
        return None
    return {
        "mtime": float(mtime),
        "size": size,
        "mail_uid": entry["mail_uid"],
        "index_version": entry["index_version"],
        "uid_resolution_version": uid_version,
    }


def _canonical_state(state: dict[str, Any]) -> dict[str, Any]:
    """호출자가 준 구버전 상태도 안전한 v2 저장 형태로 축소한다."""
    legacy = state.get("schema_version") == 1
    files = _valid_v1_files(state.get("files")) if legacy else _valid_v2_files(state.get("files"))[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "cursor": _valid_cursor(state.get("cursor")),
        "files": files,
        "pending_deletes": _valid_pending_deletes(state.get("pending_deletes")),
    }


def _valid_pending_deletes(raw: object) -> list[str]:
    """삭제 대기열에서 유효한 chunk_id만 중복 없이 복구한다."""
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(chunk_id for chunk_id in raw if isinstance(chunk_id, str)))


def _is_finite_number(value: object) -> bool:
    """유한한 숫자인지 확인한다."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _email_index_version() -> str:
    """현재 메일 인덱스 포맷 버전을 필요할 때만 가져온다."""
    from knowmate.rag.email_indexer import EMAIL_INDEX_VERSION
    return EMAIL_INDEX_VERSION
