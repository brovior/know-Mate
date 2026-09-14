"""배포 exe에 내장된 Git 빌드 출처 정보를 읽는다."""
from __future__ import annotations

from typing import Any


def get_build_info() -> dict[str, Any]:
    """빌드 출처 정보를 반환하며 소스 실행에서는 빈 dict로 폴백한다."""
    try:
        from aegisdesk_build_info import BUILD_INFO
    except (ImportError, AttributeError):
        return {}
    return dict(BUILD_INFO) if isinstance(BUILD_INFO, dict) else {}


def version_label(base_version: str) -> str:
    """사용자가 빌드를 구분할 수 있도록 버전에 짧은 커밋을 붙인다."""
    info = get_build_info()
    commit = str(info.get("short_commit", "")).strip()
    if not commit:
        return base_version
    dirty = ""
    if info.get("dirty") is True:
        fingerprint = str(info.get("source_fingerprint", "")).strip() or "unknown"
        dirty = f"-dirty.{fingerprint}"
    return f"{base_version} ({commit}{dirty})"
