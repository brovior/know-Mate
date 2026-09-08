"""메일 스캔 성공 캐시와 순환 커서를 관리한다."""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def normalize_path_key(path: str) -> str:
    """운영체제별 대소문자 규칙을 반영한 안정적인 경로 키를 반환한다."""
    return os.path.normcase(os.path.abspath(path))


def load_mail_scan_state(path: Path, *, invalidate_cache: bool = False) -> dict[str, Any]:
    """유효한 메일 스캔 상태를 읽고, 손상·버전 불일치는 빈 상태로 복구한다."""
    empty: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "cursor": None, "files": {}}
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[mail_scanner] 메일 상태 파일 읽기 실패, 초기화: %s (%s)", path, exc)
        return empty
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return empty

    cursor = _valid_cursor(raw.get("cursor"))
    files = {} if invalidate_cache else _valid_files(raw.get("files"))
    if files and any(entry["index_version"] != _email_index_version() for entry in files.values()):
        logger.info("[mail_scanner] 메일 인덱스 버전 변경 감지 — 성공 캐시를 초기화합니다")
        files = {}
    return {"schema_version": SCHEMA_VERSION, "cursor": cursor, "files": files}


def save_mail_scan_state(path: Path, state: dict[str, Any]) -> bool:
    """메일 상태와 커서를 하나의 JSON 파일로 원자적으로 저장한다."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.error("[mail_scanner] 메일 상태 저장 실패(다음 사이클 복구): %s (%s)", path, exc)
        return False


def cache_matches(entry: dict[str, Any] | None, item: dict[str, Any]) -> bool:
    """성공 캐시가 현재 파일의 mtime·size·메일 인덱스 버전과 일치하는지 확인한다."""
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("index_version") == _email_index_version()
        and entry.get("mtime") == item["mtime"]
        and entry.get("size") == item["size"]
        and isinstance(entry.get("mail_uid"), str)
    )


def cache_success(state: dict[str, Any], item: dict[str, Any], mail_uid: str) -> None:
    """성공하거나 DB 중복으로 확인된 메일의 캐시 항목을 기록한다."""
    key = normalize_path_key(item["path"])
    state["files"][key] = {
        "path": item["path"],
        "mtime": item["mtime"],
        "size": item["size"],
        "mail_uid": mail_uid,
        "index_version": _email_index_version(),
    }


def set_cursor(state: dict[str, Any], item: dict[str, Any]) -> None:
    """현재 항목까지 순회했음을 다음 사이클용 커서에 기록한다."""
    state["cursor"] = {"mtime": item["mtime"], "path": normalize_path_key(item["path"])}


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


def _valid_files(raw: object) -> dict[str, dict[str, Any]]:
    """손상된 성공 캐시 항목을 제외한 유효 항목만 반환한다."""
    if not isinstance(raw, dict):
        return {}
    valid: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        if (
            isinstance(entry.get("path"), str)
            and _is_finite_number(entry.get("mtime"))
            and isinstance(entry.get("size"), int)
            and not isinstance(entry.get("size"), bool)
            and isinstance(entry.get("mail_uid"), str)
            and isinstance(entry.get("index_version"), str)
        ):
            valid[key] = entry
    return valid


def _is_finite_number(value: object) -> bool:
    """유한한 숫자인지 확인한다."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _email_index_version() -> str:
    """현재 메일 인덱스 포맷 버전을 필요할 때만 가져온다."""
    from knowmate.rag.email_indexer import EMAIL_INDEX_VERSION
    return EMAIL_INDEX_VERSION
