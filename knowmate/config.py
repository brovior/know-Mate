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
