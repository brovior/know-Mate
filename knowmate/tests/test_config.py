"""사용자 config.yaml 로드·호환 마이그레이션 테스트."""
from __future__ import annotations

import yaml
import pytest


def test_legacy_mail_progress_key_is_renamed_and_persisted(tmp_path, monkeypatch):
    """구형 진행률 키를 값 손실 없이 새 이름으로 저장한다."""
    import knowmate.config as config_module

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mail:\n  max_mails_per_scan: 500\n  batch_commit_every: 17\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_cache", None)
    monkeypatch.setattr(config_module, "_get_config_path", lambda: config_path)

    cfg = config_module.get_config()

    assert cfg["mail"]["progress_report_every"] == 17
    assert "batch_commit_every" not in cfg["mail"]
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["mail"]["progress_report_every"] == 17
    assert "batch_commit_every" not in persisted["mail"]


def test_new_mail_progress_key_wins_during_legacy_migration(tmp_path, monkeypatch):
    """두 키가 함께 있으면 새 이름의 명시값을 보존한다."""
    import knowmate.config as config_module

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mail:\n  progress_report_every: 25\n  batch_commit_every: 10\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_cache", None)
    monkeypatch.setattr(config_module, "_get_config_path", lambda: config_path)

    cfg = config_module.get_config()

    assert cfg["mail"]["progress_report_every"] == 25
    assert "batch_commit_every" not in cfg["mail"]
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["mail"]["progress_report_every"] == 25
    assert "batch_commit_every" not in persisted["mail"]


def test_legacy_lance_maintenance_config_is_migrated_and_completed(tmp_path, monkeypatch):
    """구형 mutation 정책을 제거하고 새 fragment 정책 기본값을 AppData config에 저장한다."""
    import knowmate.config as config_module

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "lancedb_maintenance:\n"
        "  enabled: true\n"
        "  startup_optimize_when_small_fragments_reach: 77\n"
        "  optimize_every_mutations: 100\n"
        "  cycle_end_min_mutations: 20\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_cache", None)
    monkeypatch.setattr(config_module, "_get_config_path", lambda: config_path)

    cfg = config_module.get_config()["lancedb_maintenance"]

    assert cfg == {
        "enabled": True,
        "optimize_when_small_fragments_reach": 77,
        "backlog_hard_limit_small_fragments": 1000,
        "backlog_finalize_small_fragments_reach": 300,
        "min_optimize_interval_sec": 86400,
        "failure_cooldown_sec": 300,
    }
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["lancedb_maintenance"] == cfg


def test_exclude_update_failure_keeps_shared_config_unchanged(monkeypatch):
    """제외 설정 저장 실패는 워커가 참조하는 공유 설정도 바꾸지 않는다."""
    import knowmate.config as config_module

    shared = {"collector": {"exclude_files": ["old.xlsx"]}}
    monkeypatch.setattr(config_module, "get_config", lambda: shared)
    monkeypatch.setattr(
        config_module, "_save_config",
        lambda _cfg: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError):
        config_module.update_exclude_files(["new.xlsx"])

    assert shared["collector"]["exclude_files"] == ["old.xlsx"]


def test_atomic_config_replace_failure_keeps_previous_file(tmp_path, monkeypatch):
    """설정 교체 실패는 기존 config.yaml 내용을 보존한다."""
    import knowmate.config as config_module

    config_path = tmp_path / "config.yaml"
    config_path.write_text("collector:\n  exclude_files: [old.xlsx]\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_get_config_path", lambda: config_path)

    original_replace = type(config_path).replace

    def fail_replace(self, target):
        if self.name.endswith(".tmp"):
            raise OSError("replace failed")
        return original_replace(self, target)

    monkeypatch.setattr(type(config_path), "replace", fail_replace)

    with pytest.raises(OSError):
        config_module._save_config({"collector": {"exclude_files": ["new.xlsx"]}})

    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["collector"]["exclude_files"] == ["old.xlsx"]
