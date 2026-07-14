"""증분 스캔 모듈 — 폴더를 순회해 파일 변경을 감지한다."""
import logging
import os
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# 스캔 하트비트: 열거한 파일이 이 개수만큼 늘 때마다 on_progress 호출
_SCAN_HEARTBEAT_EVERY = 100

SUPPORTED_EXT = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf", ".txt"}

# 로컬로 취급하는 드라이브 문자
_LOCAL_DRIVES = {"c", "d", "e"}


def get_scope(path: str) -> str:
    """경로를 보고 'local' 또는 'shared'를 반환한다.

    - UNC 경로(\\server\share) → shared
    - C:, D:, E: 드라이브 → local
    - 그 외 매핑 드라이브(Z:, F: 등) → shared
    """
    p = path.replace("\\", "/")
    if p.startswith("//"):
        return "shared"
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        return "local" if drive in _LOCAL_DRIVES else "shared"
    return "local"


def scan_folder(
    folder: Path,
    max_file_size_mb: float = 30.0,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, dict]:
    """지원 확장자 파일의 mtime 과 size 를 수집해 반환한다.

    반환 dict 키는 str(절대경로), 값은 {"mtime": float, "size": int}.
    max_file_size_mb 초과 파일은 WARNING 로그 후 제외한다.
    on_progress: 열거 진행 중 주기적으로 (누적 발견 건수)로 호출된다(네트워크 드라이브 지연 대비 하트비트).
    """
    max_bytes = int(max_file_size_mb * 1024 * 1024)
    result: dict[str, dict] = {}
    walked = 0
    try:
        for dirpath, _dirs, files in os.walk(folder):
            for fname in files:
                walked += 1
                if on_progress and walked % _SCAN_HEARTBEAT_EVERY == 0:
                    on_progress(len(result))
                fpath = Path(dirpath) / fname
                if fpath.suffix.lower() not in SUPPORTED_EXT:
                    continue
                if fname.startswith("~$"):
                    continue
                try:
                    stat = fpath.stat()
                    if stat.st_size > max_bytes:
                        logger.warning(
                            "파일 크기 초과로 인덱싱 제외 (%.1fMB > %.1fMB): %s",
                            stat.st_size / 1024 / 1024, max_file_size_mb, fpath,
                        )
                        continue
                    result[str(fpath)] = {
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                    }
                except OSError as exc:
                    logger.warning("파일 stat 실패: %s (%s)", fpath, exc)
    except OSError as exc:
        logger.error("폴더 스캔 실패: %s (%s)", folder, exc)
    if on_progress:
        on_progress(len(result))   # 폴더 종료 시 최종 발견 건수 반영
    return result


def classify_changes(
    saved: dict, current: dict
) -> tuple[list[str], list[str], list[str]]:
    """저장된 상태와 현재 파일을 비교해 (신규, 변경, 삭제) 경로 리스트를 반환한다."""
    new: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    for path, meta in current.items():
        if path not in saved:
            new.append(path)
        else:
            prev = saved[path]
            if meta["mtime"] != prev.get("mtime") or meta["size"] != prev.get("size"):
                modified.append(path)

    for path in saved:
        if path not in current:
            deleted.append(path)

    return new, modified, deleted
