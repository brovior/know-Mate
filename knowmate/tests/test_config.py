"""사용자 config.yaml 로드·호환 마이그레이션 테스트."""
from __future__ import annotations

import yaml


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
