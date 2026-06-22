"""config.yaml 싱글톤 로더."""
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_cache: dict[str, Any] | None = None


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

