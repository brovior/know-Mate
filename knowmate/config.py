"""config.yaml 싱글톤 로더 + 앱 데이터 폴더 관리."""
import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_cache: dict[str, Any] | None = None

# 앱 데이터 루트 (%APPDATA%/AegisDesk). 구버전 KnowMate 폴더는 1회 자동 이전.
_APP_DIR_NAME = "AegisDesk"
_LEGACY_DIR_NAME = "KnowMate"
_data_dir_migrated = False


def get_data_dir() -> Path:
    """앱 데이터 루트(%APPDATA%/AegisDesk)를 반환한다.

    구버전 KnowMate 폴더가 있으면 통째로 AegisDesk로 1회 이전한다
    (km.key·index·threads.json 보존). 폴더가 없으면 생성한다.
    """
    global _data_dir_migrated
    base = Path(os.environ.get("APPDATA", "."))
    data_dir = base / _APP_DIR_NAME
    if not _data_dir_migrated:
        legacy = base / _LEGACY_DIR_NAME
        if legacy.exists() and not data_dir.exists():
            try:
                legacy.rename(data_dir)
            except OSError:
                pass  # 이전 실패 시 신규 폴더로 진행 (기존 인덱스는 재인덱싱 필요)
        _data_dir_migrated = True
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_config() -> dict[str, Any]:
    """config.yaml을 읽어 dict로 반환한다. 최초 1회 로드 후 캐시."""
    global _cache
    if _cache is None:
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            _cache = yaml.safe_load(f) or {}
    return _cache


def update_watch_folders(folders: list[str]) -> None:
    """watch_folders를 갱신하고 config.yaml에 저장한다."""
    cfg = get_config()
    cfg.setdefault("collector", {})["watch_folders"] = folders
    with _CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

