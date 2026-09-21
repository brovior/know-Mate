"""config.yaml 싱글톤 로더 + 앱 데이터 폴더 관리."""
import logging
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 번들 기본 config (읽기 전용 템플릿). 소스 실행: 이 파일 자체.
# PyInstaller 번들(frozen): sys._MEIPASS 아래 동일 상대경로.
_BUNDLED_CONFIG_PATH = Path(__file__).parent / "config.yaml"

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


def _bundled_config_source() -> Path:
    """번들(frozen)이면 sys._MEIPASS 기준, 아니면 소스 트리의 config.yaml 경로를 반환한다."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "knowmate" / "config.yaml"
        if candidate.exists():
            return candidate
    return _BUNDLED_CONFIG_PATH


def _get_config_path() -> Path:
    """실제 읽고 쓰는 config.yaml 경로(%APPDATA%/AegisDesk/config.yaml)를 반환한다.

    없으면 번들 기본값(템플릿)을 최초 1회 시드로 복사한다. 단 watch_folders는
    배포자(마스터)의 개인 경로가 테스터에게 그대로 전달되지 않도록 빈 배열로 초기화한다.
    이후 이 파일은 사용자 소유이며 모든 항목을 자유롭게 수정할 수 있다(전체 설정 UI 지원).
    포터블(exe) 빌드에서는 번들 내부가 쓰기 불가/휘발성이므로 항상 APPDATA에 둔다.
    """
    target = get_data_dir() / "config.yaml"
    if not target.exists():
        source = _bundled_config_source()
        try:
            with source.open(encoding="utf-8") as f:
                seed_cfg = yaml.safe_load(f) or {}
            seed_cfg.setdefault("collector", {})["watch_folders"] = []
            with target.open("w", encoding="utf-8") as f:
                yaml.dump(seed_cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            logger.info("config.yaml 최초 시드 완료 (watch_folders 초기화): %s -> %s", source, target)
        except OSError as exc:
            logger.error("config.yaml 시드 실패 (%s -> %s): %s", source, target, exc)
            raise
    return target


def get_config() -> dict[str, Any]:
    """config.yaml을 읽어 dict로 반환한다. 최초 1회 로드 후 캐시."""
    global _cache
    if _cache is None:
        with _get_config_path().open(encoding="utf-8") as f:
            _cache = yaml.safe_load(f) or {}
        mail_cfg = _cache.get("mail")
        if isinstance(mail_cfg, dict) and "batch_commit_every" in mail_cfg:
            legacy_value = mail_cfg.pop("batch_commit_every")
            mail_cfg.setdefault("progress_report_every", legacy_value)
            try:
                _save_config(_cache)
            except OSError as exc:
                logger.warning("구형 메일 진행률 설정 이름 변경 저장 실패: %s", exc)
        maintenance = _cache.get("lancedb_maintenance")
        if isinstance(maintenance, dict):
            changed = False
            had_legacy_startup = "startup_optimize_when_small_fragments_reach" in maintenance
            legacy_startup = maintenance.pop("startup_optimize_when_small_fragments_reach", None)
            if "optimize_when_small_fragments_reach" not in maintenance and legacy_startup is not None:
                maintenance["optimize_when_small_fragments_reach"] = legacy_startup
                changed = True
                logger.info("lancedb_maintenance 구형 startup 임계값을 새 steady 임계값으로 이전했습니다")
            for key in ("optimize_every_mutations", "cycle_end_min_mutations"):
                if key in maintenance:
                    maintenance.pop(key)
                    changed = True
                    logger.warning("lancedb_maintenance 구형 %s 설정을 제거했습니다", key)
            if had_legacy_startup:
                changed = True
            # 기존 AppData config는 번들 기본 파일을 다시 시드하지 않는다. 새 정책의
            # 조절값도 실제 사용자 파일에 한 번 채워 넣어 이후 직접 확인·조정할 수 있게 한다.
            for key, default in (
                ("optimize_when_small_fragments_reach", 100),
                ("backlog_hard_limit_small_fragments", 1000),
                ("backlog_finalize_small_fragments_reach", 300),
                ("min_optimize_interval_sec", 86400),
                ("failure_cooldown_sec", 300),
            ):
                if key not in maintenance:
                    maintenance[key] = default
                    changed = True
            if changed:
                try:
                    _save_config(_cache)
                except OSError as exc:
                    logger.warning("lancedb_maintenance 설정 이전 저장 실패: %s", exc)
        collector = _cache.setdefault("collector", {})
        if isinstance(collector, dict):
            defaults = _bundled_document_state_defaults()
            changed = False
            for key, value in defaults.items():
                if key not in collector:
                    collector[key] = value
                    changed = True
            if changed:
                try:
                    _save_config(_cache)
                except OSError as exc:
                    logger.warning("document state flush 설정 이전 저장 실패: %s", exc)
    return _cache


def _bundled_document_state_defaults() -> dict[str, Any]:
    """Read document checkpoint defaults from the bundled YAML source."""
    with _bundled_config_source().open(encoding="utf-8") as handle:
        bundled = yaml.safe_load(handle) or {}
    collector = bundled.get("collector") if isinstance(bundled, dict) else None
    if not isinstance(collector, dict):
        raise RuntimeError("bundled collector configuration is missing")
    return {
        key: collector[key]
        for key in ("state_flush_docs", "state_flush_seconds")
    }


def document_state_flush_settings(collector: dict[str, Any]) -> tuple[int, float]:
    """Return flush settings, filling old in-memory mappings from bundled YAML."""
    defaults = _bundled_document_state_defaults()
    return (
        int(collector.get("state_flush_docs", defaults["state_flush_docs"])),
        float(collector.get("state_flush_seconds", defaults["state_flush_seconds"])),
    )


def update_watch_folders(folders: list[str]) -> None:
    """watch_folders를 갱신하고 config.yaml에 저장한다."""
    cfg = get_config()
    cfg.setdefault("collector", {})["watch_folders"] = folders
    _save_config(cfg)


def update_exclude_files(paths: list[str]) -> None:
    """exclude_files를 원자 저장한 뒤 공유 설정 객체에 반영한다."""
    cfg = get_config()
    updated = deepcopy(cfg)
    updated.setdefault("collector", {})["exclude_files"] = list(paths)
    _save_config(updated)
    # CollectorWorker 등은 get_config()가 반환한 객체를 계속 참조한다. 저장 성공
    # 뒤 기존 객체를 제자리 갱신해야 실행 중 구성도 디스크와 같은 값을 본다.
    cfg.clear()
    cfg.update(updated)


def update_settings(patch: dict[str, Any]) -> None:
    """설정 UI에서 받은 patch를 config에 병합하고 저장한다.

    patch 값이 dict면 해당 섹션 내부 키만 얕게 덮어쓴다(섹션 통째 교체 방지).
    patch 값이 스칼라면(log_level 등 최상위 키) 그대로 대입한다.
    patch 예: {"llm": {"base_url": "http://10.0.0.5"}, "log_level": "DEBUG"}
    """
    cfg = get_config()
    for key, value in patch.items():
        if isinstance(value, dict):
            cfg.setdefault(key, {}).update(value)
        else:
            cfg[key] = value
    _save_config(cfg)


def _save_config(cfg: dict[str, Any]) -> None:
    """현재 config dict를 임시 파일 작성 후 원자 교체한다."""
    target = _get_config_path()
    tmp = target.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    tmp.replace(target)
