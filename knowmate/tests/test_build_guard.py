"""배포 빌드 환경 검증 스크립트 테스트."""
from __future__ import annotations

import runpy
import subprocess

import pytest

from scripts import build_guard


def test_reads_exact_pyinstaller_pin(tmp_path):
    """한글 주석이 붙어도 고정 버전만 정확히 읽는다."""
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "pytest>=8\npyinstaller==6.21.0  # 빌드 버전\n",
        encoding="utf-8",
    )
    assert build_guard._pinned_pyinstaller_version(requirements) == "6.21.0"


def test_git_status_failure_is_not_treated_as_clean(monkeypatch):
    """Git 상태 조회 실패를 깨끗한 작업 트리로 오판하지 않는다."""
    failed = subprocess.CompletedProcess(
        args=["git", "status"], returncode=1, stdout="", stderr="access denied",
    )
    monkeypatch.setattr(build_guard.subprocess, "run", lambda *_args, **_kwargs: failed)

    with pytest.raises(RuntimeError, match="access denied"):
        build_guard._git("status", "--porcelain", allow_empty=True)


def test_prepare_rejects_version_mismatch(monkeypatch, tmp_path):
    """검증하지 않은 PyInstaller 버전이면 배포 빌드를 중단한다."""
    monkeypatch.setattr(build_guard, "_pinned_pyinstaller_version", lambda _path: "6.21.0")
    monkeypatch.setattr(build_guard.importlib.metadata, "version", lambda _name: "6.22.0")

    with pytest.raises(RuntimeError, match="버전 불일치"):
        build_guard.prepare(tmp_path)


def test_prepare_generates_importable_commit_info(monkeypatch, tmp_path):
    """검증된 환경에서는 PyInstaller가 포함할 출처 모듈을 만든다."""
    monkeypatch.setattr(build_guard, "_pinned_pyinstaller_version", lambda _path: "6.21.0")
    monkeypatch.setattr(build_guard.importlib.metadata, "version", lambda _name: "6.21.0")
    monkeypatch.setattr(build_guard, "_build_info", lambda: {
        "commit": "c" * 40,
        "short_commit": "c1d2e3f",
        "branch": "main",
        "dirty": False,
        "source_fingerprint": "",
        "origin_main": "c" * 40,
        "built_at_utc": "2026-09-14T00:00:00+00:00",
    })

    build_guard.prepare(tmp_path)

    generated = runpy.run_path(str(tmp_path / "aegisdesk_build_info.py"))
    assert generated["BUILD_INFO"]["short_commit"] == "c1d2e3f"


def test_dirty_source_fingerprint_changes_with_diff(monkeypatch):
    """같은 HEAD라도 변경 내용이 다르면 배포 식별자가 달라진다."""
    responses = iter((b"first diff", b"", b"second diff", b""))
    monkeypatch.setattr(build_guard, "_git_bytes", lambda *_args: next(responses))

    first = build_guard._source_fingerprint()
    second = build_guard._source_fingerprint()

    assert first != second


def test_dirty_source_fingerprint_includes_untracked_contents(monkeypatch, tmp_path):
    """아직 Git에 추가하지 않은 소스 내용도 시험 빌드 식별자에 반영한다."""
    extra = tmp_path / "extra.py"
    extra.write_text("first", encoding="utf-8")
    monkeypatch.setattr(build_guard, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        build_guard,
        "_git_bytes",
        lambda *args: b"extra.py\0" if args[0] == "ls-files" else b"",
    )

    first = build_guard._source_fingerprint()
    extra.write_text("second", encoding="utf-8")
    second = build_guard._source_fingerprint()

    assert first != second


def test_prepare_rejects_shadowing_root_module(monkeypatch, tmp_path):
    """저장소 루트의 오래된 출처 모듈이 새 생성 정보를 가리지 못하게 한다."""
    monkeypatch.setattr(build_guard, "REPO_ROOT", tmp_path)
    (tmp_path / "aegisdesk_build_info.py").write_text("BUILD_INFO = {}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="생성 정보를 가릴 수 있습니다"):
        build_guard.prepare(tmp_path / "generated")
